"""Retention cleanup and housekeeping jobs: files and their legacy blobs go together."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import pytest

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.chat import retention

from .chat_support import chat_folder, set_settings


@pytest.fixture
def users(make_user):
    return make_user("alice"), make_user("bob")


def add_message(app, db, table, parent_column, parent_id, sender, *, age_days, filename=None, deleted=False):
    created = sql_in(days=-age_days)
    message_id = db.insert(table, {parent_column: parent_id, "sender_id": sender["id"],
                                   "content": "old text", "created_at": created, "is_deleted": int(deleted)})
    if filename:
        (chat_folder(app)).mkdir(parents=True, exist_ok=True)
        (chat_folder(app) / filename).write_bytes(b"data")
        blob = db.insert("file_blobs", {"filename": filename, "content": b"data"})
        attachments = "chat_attachments" if table == "chat_messages" else "group_attachments"
        db.insert(attachments, {"message_id": message_id, "filename": filename, "original_name": filename,
                                "file_size": 4, "created_at": created, "blob_id": blob})
    return message_id


@pytest.fixture
def data(app, db, users):
    alice, bob = users
    chat_id = db.insert("chats", {"user1_id": min(alice["id"], bob["id"]), "user2_id": max(alice["id"], bob["id"]),
                                  "created_at": sql_in(days=-100)})
    group_id = db.insert("group_chats", {"name": "G", "invite_code": "abcdefghij", "created_at": sql_in(days=-100)})
    return {
        "chat": chat_id, "group": group_id,
        "old_dm": add_message(app, db, "chat_messages", "chat_id", chat_id, alice, age_days=40, filename="old.txt"),
        "new_dm": add_message(app, db, "chat_messages", "chat_id", chat_id, bob, age_days=1, filename="new.txt"),
        "old_group": add_message(app, db, "group_messages", "group_id", group_id, alice, age_days=40,
                                 filename="gold.txt"),
    }


def run(app, **kwargs):
    with app.app_context(), connection_scope():
        return retention.run_retention(**kwargs)


def test_retention_deletes_messages_files_and_blobs(app, db, data):
    set_settings(db, chat_dm_message_retention_days=30, chat_dm_attachment_retention_days=0,
                 chat_group_message_retention_days=0, chat_group_attachment_retention_days=7)
    summary = run(app, force=True)
    assert summary == {"messages": 1, "attachments": 2, "chats": 0}
    assert db.column("SELECT id FROM chat_messages") == [data["new_dm"]]
    assert db.column("SELECT id FROM group_messages") == [data["old_group"]]  # only its attachment went
    assert db.column("SELECT filename FROM file_blobs") == ["new.txt"]
    folder = chat_folder(app)
    assert not (folder / "old.txt").exists() and not (folder / "gold.txt").exists()
    assert (folder / "new.txt").exists()
    assert db.scalar("SELECT last_chat_cleanup_at FROM site_settings") is not None


def test_retention_off_or_not_due(app, db, data):
    set_settings(db, chat_dm_attachment_retention_days=0, chat_group_attachment_retention_days=0)
    assert run(app, force=True) is None  # every number 0: everything is kept
    set_settings(db, chat_dm_message_retention_days=30, last_chat_cleanup_at=sql_in(hours=-1))
    assert run(app) is None  # it ran an hour ago: next time tomorrow
    assert db.scalar("SELECT COUNT(*) FROM chat_messages") == 2


def test_empty_old_conversations_are_dropped(app, db, users, data):
    set_settings(db, chat_dm_message_retention_days=1)
    db.execute("UPDATE chat_messages SET created_at = ?", (sql_in(days=-5),))
    assert run(app, force=True)["chats"] == 1
    assert db.scalar("SELECT COUNT(*) FROM chats") == 0


def test_next_run_schedule(app, db):
    set_settings(db, chat_dm_message_retention_days=30, last_chat_cleanup_at="2026-01-01 03:10:00")
    with app.app_context(), connection_scope():
        due = retention.next_run(datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert due == datetime(2026, 1, 2, 3, tzinfo=UTC)  # daily, at 03:00 site time (UTC here)


def test_housekeeping_purges_legacy_deleted_content_and_orphans(app, db, users, data):
    alice = users[0]
    kept = add_message(app, db, "chat_messages", "chat_id", data["chat"], alice, age_days=2, filename="gone.txt",
                       deleted=True)
    folder = chat_folder(app)
    (folder / "orphan.bin").write_bytes(b"x")
    old = time.time() - 3 * 86400
    os.utime(folder / "orphan.bin", (old, old))
    db.insert("file_blobs", {"filename": "orphan.bin", "content": b"x"})
    (folder / "fresh.bin").write_bytes(b"x")
    with app.app_context(), connection_scope():
        summary = retention.housekeeping()
    assert summary == {"purged_attachments": 1, "orphan_files": 1, "orphan_blobs": 1}
    assert db.scalar("SELECT content FROM chat_messages WHERE id = ?", (kept,)) == ""
    assert not (folder / "gone.txt").exists() and not (folder / "orphan.bin").exists()
    assert (folder / "fresh.bin").exists() and (folder / "new.txt").exists()
    assert "gone.txt" not in db.column("SELECT filename FROM file_blobs")


def test_jobs_are_registered(app):
    names = [job.name for _feature, job in app.extensions["bananawiki.scheduler"].jobs()]
    assert "chat.retention" in names and "chat.housekeeping" in names
    assert "chat.retention" in app.extensions["bananawiki.scheduler"].run_due(force=True, only="chat.retention")
