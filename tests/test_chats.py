"""
Tests for the chat / direct messaging feature.

Covers:
  - Chat creation and listing
  - Sending and viewing messages
  - Attachment upload and download (with size and count limits)
  - Admin chat monitoring (global list, per-user filter, reading chats)
  - Access control (participants only, admin override)
  - Message cleanup (nightly purge)
  - Cannot message yourself
"""

import io
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Fresh temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr(config, "UPLOAD_FOLDER", upload_dir)
    chat_att_dir = str(tmp_path / "chat_attachments")
    os.makedirs(chat_att_dir, exist_ok=True)
    monkeypatch.setattr(config, "CHAT_ATTACHMENT_FOLDER", chat_att_dir)
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_uid():
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def alice_uid(admin_uid):
    from werkzeug.security import generate_password_hash
    import db
    return db.create_user("alice", generate_password_hash("alice123"), role="user")


@pytest.fixture
def bob_uid(admin_uid):
    from werkzeug.security import generate_password_hash
    import db
    return db.create_user("bob", generate_password_hash("bob123"), role="user")


@pytest.fixture
def admin_client(client, admin_uid):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def alice_client(client, alice_uid):
    client.post("/login", data={"username": "alice", "password": "alice123"})
    return client


@pytest.fixture
def bob_client(client, bob_uid):
    client.post("/login", data={"username": "bob", "password": "bob123"})
    return client


# ---------------------------------------------------------------------------
# Chat list
# ---------------------------------------------------------------------------

def test_chat_list_requires_login(client, admin_uid):
    resp = client.get("/chats")
    assert resp.status_code == 302


def test_chat_list_empty(alice_client):
    resp = alice_client.get("/chats")
    assert resp.status_code == 200
    assert b"No chats yet" in resp.data


# ---------------------------------------------------------------------------
# Start new chat
# ---------------------------------------------------------------------------

def test_new_chat_page(alice_client, bob_uid):
    resp = alice_client.get("/chats/new")
    assert resp.status_code == 200
    assert b"Start a New Chat" in resp.data


def test_new_chat_autocomplete_shows_only_published_profiles(alice_client, bob_uid):
    """Datalist must only contain users with a published, non-disabled profile."""
    import db
    from werkzeug.security import generate_password_hash

    # ghost_uid: registered but no profile at all → must NOT appear
    db.create_user("ghost", generate_password_hash("pw"))

    # unpublished_uid: has a profile but page_published=False → must NOT appear
    unpublished_uid = db.create_user("unpublished_user", generate_password_hash("pw"))
    db.upsert_user_profile(unpublished_uid, real_name="Unpublished", page_published=False)

    # bob has no profile either → must NOT appear in datalist
    # published_uid: has a published profile → MUST appear
    published_uid = db.create_user("published_user", generate_password_hash("pw"))
    db.upsert_user_profile(published_uid, real_name="Published", page_published=True)

    resp = alice_client.get("/chats/new")
    assert resp.status_code == 200
    assert b"published_user" in resp.data
    assert b"ghost" not in resp.data
    assert b"unpublished_user" not in resp.data
    assert b"bob" not in resp.data


def test_new_chat_autocomplete_excludes_suspended_users(alice_client):
    """Suspended users must not appear in the datalist even if their profile is published."""
    import db
    from werkzeug.security import generate_password_hash

    suspended_uid = db.create_user("suspended_user", generate_password_hash("pw"))
    db.upsert_user_profile(suspended_uid, real_name="Suspended", page_published=True)
    db.update_user(suspended_uid, suspended=1)

    resp = alice_client.get("/chats/new")
    assert resp.status_code == 200
    assert b'data-username="suspended_user"' not in resp.data


def test_start_chat_with_user(alice_client, bob_uid):
    resp = alice_client.post("/chats/new", data={"username": "bob"},
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"bob" in resp.data


def test_start_chat_with_self(alice_client, alice_uid):
    resp = alice_client.post("/chats/new", data={"username": "alice"},
                             follow_redirects=True)
    assert b"cannot start a chat with yourself" in resp.data


def test_start_chat_with_nonexistent_user(alice_client, bob_uid):
    resp = alice_client.post("/chats/new", data={"username": "nobody"},
                             follow_redirects=True)
    assert b"User not found" in resp.data


def test_start_chat_empty_username(alice_client, bob_uid):
    resp = alice_client.post("/chats/new", data={"username": ""},
                             follow_redirects=True)
    assert b"Please enter a username" in resp.data


# ---------------------------------------------------------------------------
# Send messages
# ---------------------------------------------------------------------------

def test_send_message(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data={"content": "Hello Bob!"},
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"Hello Bob!" in resp.data


def test_send_empty_message(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data={"content": ""},
                             follow_redirects=True)
    assert b"Message cannot be empty" in resp.data


def test_send_too_long_message(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data={"content": "x" * 5001},
                             follow_redirects=True)
    assert b"cannot exceed" in resp.data


def test_non_participant_cannot_send(alice_client, bob_uid, admin_uid):
    import db
    # Create a chat between bob and admin
    chat = db.get_or_create_chat(bob_uid, admin_uid)
    # Alice tries to send to it
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data={"content": "sneaky"},
                             follow_redirects=True)
    assert b"Access denied" in resp.data


# ---------------------------------------------------------------------------
# View chat
# ---------------------------------------------------------------------------

def test_view_chat(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    resp = alice_client.get(f"/chats/{chat['id']}")
    assert resp.status_code == 200
    assert b"bob" in resp.data


def test_chat_messages_partial_for_participant(alice_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "Live hello")
    resp = alice_client.get(f"/chats/{chat['id']}/messages")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["message_count"] == 1
    assert data["latest_message_id"] > 0
    assert data["state_token"]
    assert "Live hello" in data["html"]


def test_chat_messages_partial_state_token_changes_when_message_deleted(alice_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    message_id = db.send_chat_message(chat["id"], alice_uid, "Delete me")
    before = alice_client.get(f"/chats/{chat['id']}/messages").get_json()
    db.delete_chat_message(message_id)
    after = alice_client.get(f"/chats/{chat['id']}/messages").get_json()
    assert before["message_count"] == after["message_count"] == 1
    assert before["latest_message_id"] == after["latest_message_id"] == message_id
    assert before["state_token"] != after["state_token"]
    assert "Delete me" in before["html"]
    assert "This message was deleted." in after["html"]


def test_non_participant_cannot_view(alice_client, bob_uid, admin_uid):
    import db
    chat = db.get_or_create_chat(bob_uid, admin_uid)
    resp = alice_client.get(f"/chats/{chat['id']}", follow_redirects=True)
    assert b"Access denied" in resp.data


# ---------------------------------------------------------------------------
# Chat shows in list
# ---------------------------------------------------------------------------

def test_chat_shows_in_list(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    db.send_chat_message(chat["id"], alice["id"], "Hey")
    resp = alice_client.get("/chats")
    assert resp.status_code == 200
    assert b"bob" in resp.data


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def test_send_message_with_attachment(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    data = {
        "content": "Check this out",
        "attachment": (io.BytesIO(b"file content"), "test.txt"),
    }
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data=data,
                             content_type="multipart/form-data",
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"test.txt" in resp.data


def test_attachment_download(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    data = {
        "content": "Has attachment",
        "attachment": (io.BytesIO(b"hello"), "readme.txt"),
    }
    alice_client.post(f"/chats/{chat['id']}/send",
                      data=data, content_type="multipart/form-data")
    messages = db.get_chat_messages(chat["id"])
    att = messages[0]["attachments"][0]
    resp = alice_client.get(f"/chats/attachments/{att['id']}/download")
    assert resp.status_code == 200


def test_attachment_bad_extension(alice_client, bob_uid):
    import db
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    data = {
        "content": "Bad file",
        "attachment": (io.BytesIO(b"#!/bin/bash"), "evil.exe"),
    }
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data=data,
                             content_type="multipart/form-data",
                             follow_redirects=True)
    assert b"File type not allowed" in resp.data


def test_attachment_daily_limit(alice_client, bob_uid):
    import db
    # Set the DB setting to enforce a limit of 1 attachment per day
    db.update_site_settings(chat_attachments_per_day_limit=1)
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    # First attachment should work
    data = {
        "content": "First",
        "attachment": (io.BytesIO(b"a"), "a.txt"),
    }
    alice_client.post(f"/chats/{chat['id']}/send",
                      data=data, content_type="multipart/form-data")
    # Second should hit the limit
    data2 = {
        "content": "Second",
        "attachment": (io.BytesIO(b"b"), "b.txt"),
    }
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data=data2,
                             content_type="multipart/form-data",
                             follow_redirects=True)
    assert b"Daily attachment limit" in resp.data


def test_attachment_daily_limit_ignores_old_messages(alice_uid, bob_uid):
    """Yesterday's attachments must not count toward today's daily quota."""
    import db
    from db._connection import get_db

    chat = db.get_or_create_chat(alice_uid, bob_uid)
    # Create a message dated yesterday and attach a file
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO chat_messages (chat_id, sender_id, content, created_at) "
            "VALUES (?, ?, 'old', datetime('now', '-1 day'))",
            (chat["id"], alice_uid),
        )
        old_msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chat_attachments (message_id, filename, original_name, file_size, created_at) "
            "VALUES (?, 'f.txt', 'f.txt', 100, datetime('now', '-1 day'))",
            (old_msg_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Yesterday's attachment should NOT count
    assert db.get_user_chat_attachment_count_today(alice_uid) == 0

    # A new message today should count
    msg_id = db.send_chat_message(chat["id"], alice_uid, "today")
    db.add_chat_attachment(msg_id, "t.txt", "t.txt", 50)
    assert db.get_user_chat_attachment_count_today(alice_uid) == 1


def test_chat_cleanup_banner_honors_legacy_attachment_disable(alice_uid, bob_uid):
    """Legacy attachment cleanup settings should keep the DM cleanup banner hidden when disabled."""
    import db
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(
        chat_cleanup_enabled=1,
        chat_auto_clear_messages=0,
        chat_auto_clear_attachments=0,
        chat_attachment_retention_days=14,
        chat_dm_auto_clear_messages=0,
        chat_dm_auto_clear_attachments=1,
        chat_dm_attachment_retention_days=7,
    )
    chat = db.get_or_create_chat(alice_uid, bob_uid)

    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}")

    assert b"dm-cleanup-" not in resp.data


# ---------------------------------------------------------------------------
# Admin chat monitoring
# ---------------------------------------------------------------------------

def test_admin_chats_list(admin_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "Hi Bob")
    resp = admin_client.get("/admin/chats")
    assert resp.status_code == 200
    assert b"alice" in resp.data
    assert b"bob" in resp.data


def test_admin_chats_filter_by_user(admin_client, alice_uid, bob_uid):
    import db
    db.get_or_create_chat(alice_uid, bob_uid)
    resp = admin_client.get(f"/admin/chats?user_id={alice_uid}")
    assert resp.status_code == 200
    assert b"alice" in resp.data


def test_admin_read_chat(admin_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "Secret message")
    resp = admin_client.get(f"/admin/chats/{chat['id']}")
    assert resp.status_code == 200
    assert b"Secret message" in resp.data


def test_non_admin_cannot_access_admin_chats(alice_client, bob_uid):
    resp = alice_client.get("/admin/chats", follow_redirects=True)
    assert b"Admin access required" in resp.data


def test_admin_can_download_chat_attachment(admin_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    msg_id = db.send_chat_message(chat["id"], alice_uid, "With file")
    # Write a physical file
    os.makedirs(config.CHAT_ATTACHMENT_FOLDER, exist_ok=True)
    fpath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, "testfile.txt")
    with open(fpath, "w") as f:
        f.write("content")
    db.add_chat_attachment(msg_id, "testfile.txt", "report.txt", 7)
    att = db.get_chat_messages(chat["id"])[0]["attachments"][0]
    resp = admin_client.get(f"/chats/attachments/{att['id']}/download")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def test_db_get_or_create_chat_idempotent(alice_uid, bob_uid):
    import db
    chat1 = db.get_or_create_chat(alice_uid, bob_uid)
    chat2 = db.get_or_create_chat(bob_uid, alice_uid)
    assert chat1["id"] == chat2["id"]


def test_db_is_chat_participant(alice_uid, bob_uid, admin_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    assert db.is_chat_participant(chat["id"], alice_uid)
    assert db.is_chat_participant(chat["id"], bob_uid)
    assert not db.is_chat_participant(chat["id"], admin_uid)


def test_db_chat_messages_order(alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "first")
    db.send_chat_message(chat["id"], bob_uid, "second")
    msgs = db.get_chat_messages(chat["id"])
    assert len(msgs) == 2
    assert msgs[0]["content"] == "first"
    assert msgs[1]["content"] == "second"


def test_sender_can_delete_own_chat_message(alice_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    msg_id = db.send_chat_message(chat["id"], alice_uid, "delete me")
    resp = alice_client.post(f"/chats/{chat['id']}/delete_message",
                             data={"message_id": msg_id},
                             follow_redirects=True)
    assert b"successfully deleted" in resp.data
    messages = db.get_chat_messages(chat["id"])
    assert len(messages) == 1
    assert messages[0]["is_deleted"] == 1
    assert messages[0]["content"] == "delete me"


def test_participant_cannot_delete_other_users_chat_message(bob_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    msg_id = db.send_chat_message(chat["id"], alice_uid, "keep me")
    resp = bob_client.post(f"/chats/{chat['id']}/delete_message",
                           data={"message_id": msg_id},
                           follow_redirects=True)
    assert b"required permissions" in resp.data
    assert len(db.get_chat_messages(chat["id"])) == 1


def test_deleted_chat_message_shows_placeholder_to_participants(alice_uid, bob_uid):
    import db
    from app import app
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    msg_id = db.send_chat_message(chat["id"], alice_uid, "hidden after delete")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    alice = app.test_client()
    bob = app.test_client()
    alice.post("/login", data={"username": "alice", "password": "alice123"})
    bob.post("/login", data={"username": "bob", "password": "bob123"})
    alice.post(f"/chats/{chat['id']}/delete_message", data={"message_id": msg_id}, follow_redirects=True)
    resp = bob.get(f"/chats/{chat['id']}")
    assert resp.status_code == 200
    assert b"This message was deleted." in resp.data
    assert b"hidden after delete" not in resp.data


def test_deleted_chat_attachment_hidden_from_participants_but_visible_to_admin(alice_uid, bob_uid, admin_uid):
    import db
    from app import app
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    msg_id = db.send_chat_message(chat["id"], alice_uid, "Attachment message")
    os.makedirs(config.CHAT_ATTACHMENT_FOLDER, exist_ok=True)
    fpath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, "deleted-chat-file.txt")
    with open(fpath, "w", encoding="utf-8") as handle:
        handle.write("content")
    att_id = db.add_chat_attachment(msg_id, "deleted-chat-file.txt", "report.txt", 7)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    alice = app.test_client()
    bob = app.test_client()
    admin = app.test_client()
    alice.post("/login", data={"username": "alice", "password": "alice123"})
    bob.post("/login", data={"username": "bob", "password": "bob123"})
    admin.post("/login", data={"username": "admin", "password": "admin123"})
    alice.post(f"/chats/{chat['id']}/delete_message", data={"message_id": msg_id}, follow_redirects=True)

    participant_view = bob.get(f"/chats/{chat['id']}")
    assert b"This message was deleted." in participant_view.data
    assert b"report.txt" not in participant_view.data
    assert bob.get(f"/chats/attachments/{att_id}/download").status_code == 403

    admin_view = admin.get(f"/admin/chats/{chat['id']}")
    assert admin_view.status_code == 200
    assert b"Attachment message" in admin_view.data
    assert b"report.txt" in admin_view.data
    assert admin.get(f"/chats/attachments/{att_id}/download").status_code == 200


def test_db_cleanup_old_chat_messages(alice_uid, bob_uid):
    import db
    import sqlite3
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    msg_id = db.send_chat_message(chat["id"], alice_uid, "to be deleted")
    db.add_chat_attachment(msg_id, "stored.txt", "original.txt", 100)
    # Backdate the message so it falls outside the retention window
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        "UPDATE chat_messages SET created_at = datetime('now', '-31 days') WHERE id = ?",
        (msg_id,)
    )
    conn.commit()
    conn.close()
    files = db.cleanup_old_chat_messages()
    assert "stored.txt" in files
    assert len(db.get_chat_messages(chat["id"])) == 0


def test_chat_clear_removes_attachment_file(alice_client, alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    msg_id = db.send_chat_message(chat["id"], alice_uid, "with file")
    filepath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, "clear-me.txt")
    with open(filepath, "w", encoding="utf-8") as handle:
        handle.write("content")
    db.add_chat_attachment(msg_id, "clear-me.txt", "clear-me.txt", 7)
    resp = alice_client.post(f"/chats/{chat['id']}/clear", follow_redirects=True)
    assert resp.status_code == 200
    assert b"successfully cleared" in resp.data
    assert not os.path.exists(filepath)
    assert db.get_chat_messages(chat["id"]) == []


def test_db_get_all_messages_for_backup(alice_uid, bob_uid):
    import db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "backup me")
    msgs = db.get_all_messages_for_backup()
    assert len(msgs) == 1
    assert msgs[0]["sender_name"] == "alice"
    assert msgs[0]["receiver_name"] == "bob"


def test_chat_cleanup_logs_on_backup_failure(monkeypatch):
    """Verify that the cleanup module can import get_logger and log errors."""
    from wiki_logger import get_logger

    logger = get_logger()
    logged = []

    def capture_error(msg, *args, **kwargs):
        logged.append(msg)

    monkeypatch.setattr(logger, "error", capture_error)

    # Simulate the exact error-logging pattern used in _run_chat_cleanup
    try:
        raise RuntimeError("telegram down")
    except Exception:
        try:
            get_logger().error("Chat backup failed before cleanup", exc_info=True)
        except Exception:
            pass

    assert any("Chat backup failed" in m for m in logged)


def test_attachment_oversized_file(alice_client, bob_uid):
    import db
    # Set a 1 MB limit via site settings, then send a 2 MB file
    db.update_site_settings(chat_max_attachment_size_mb=1)
    alice = db.get_user_by_username("alice")
    chat = db.get_or_create_chat(alice["id"], bob_uid)
    data = {
        "content": "Big file",
        "attachment": (io.BytesIO(b"x" * (2 * 1024 * 1024)), "big.txt"),
    }
    resp = alice_client.post(f"/chats/{chat['id']}/send",
                             data=data,
                             content_type="multipart/form-data",
                             follow_redirects=True)
    assert b"File exceeds the" in resp.data
    assert b"MB limit" in resp.data
    # The text message must NOT be committed when the attachment is rejected
    msgs = db.get_chat_messages(chat["id"])
    assert len(msgs) == 0


def test_admin_chat_monitor_link_in_account(admin_client):
    resp = admin_client.get("/settings")
    assert resp.status_code == 200
    assert b"Chat Monitor" in resp.data


# ---------------------------------------------------------------------------
# Chat disabled for DMs
# ---------------------------------------------------------------------------

def test_chat_disabled_user_cannot_start_dm(alice_uid, bob_uid):
    """A user with chat disabled should not be able to start a DM."""
    import db
    from app import app
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/chats/new", follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


def test_chat_disabled_user_cannot_access_chat_list(alice_uid):
    """A user with chat disabled should not be able to access the DM list."""
    import db
    from app import app
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/chats", follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


def test_chat_disabled_user_cannot_send_dm(alice_uid, bob_uid):
    """A user with chat disabled should not be able to send DMs."""
    import db
    from app import app
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.post(f"/chats/{chat['id']}/send",
                      data={"content": "test"},
                      follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


def test_chat_disabled_user_cannot_view_existing_dm(alice_uid, bob_uid):
    """A user with chat disabled should not be able to view an existing DM."""
    import db
    from app import app
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}", follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


@pytest.mark.parametrize('unavailable', [False, True])
def test_cleanup_timer_reschedules_when_disabled(admin_uid, monkeypatch, unavailable):
    """Keep scheduling without deleting chats when disabled or storage fails."""
    import threading
    import db

    db.update_site_settings(chat_cleanup_enabled=0)

    # Track all Timer creations to observe scheduling behaviour
    timer_targets = []
    _OrigTimer = threading.Timer

    class _TrackingTimer(_OrigTimer):
        """Timer subclass that records creation without actually starting."""

        def __init__(self, interval, function, args=None, kwargs=None):
            """Record the target function for later inspection."""
            timer_targets.append(function)
            super().__init__(interval, function, args, kwargs)

        def start(self):
            """Suppress real start so no background thread fires."""
            pass

    from app import app
    app.config["TESTING"] = True

    with app.test_request_context():
        # Use the tracking timer, then register chat routes on a scratch app
        # to get a fresh closure pair (_schedule_chat_cleanup / _run_chat_cleanup)
        import flask
        scratch = flask.Flask(__name__)
        scratch.config["TESTING"] = False
        threading.Timer = _TrackingTimer
        try:
            from routes.chat import register_chat_routes
            timer_targets.clear()
            register_chat_routes(scratch)
            # register_chat_routes calls _schedule_chat_cleanup() at the end,
            # which creates a Timer targeting _run_chat_cleanup.
            assert timer_targets, "Initial schedule did not create a timer"
            run_cleanup = timer_targets[-1]

            timer_targets.clear()
            if unavailable:
                import sqlite3
                def failed_settings():
                    raise sqlite3.OperationalError('database is unavailable')
                monkeypatch.setattr(db, 'get_site_settings', failed_settings)
            # Call _run_chat_cleanup with cleanup disabled
            run_cleanup()
            # It MUST reschedule even though cleanup is disabled
            assert timer_targets, (
                "Timer chain broke: _run_chat_cleanup did not reschedule "
                "when cleanup was disabled"
            )
        finally:
            threading.Timer = _OrigTimer


# ---------------------------------------------------------------------------
# Deleted user participant guards (Issue-13)
# ---------------------------------------------------------------------------

def _make_chat_with_deleted_user(alice_uid, bob_uid):
    """Create a chat, send a message, then remove one user row without CASCADE.

    Returns the chat id.
    """
    import db
    from db._connection import get_db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "Hello before deletion")
    # Remove bob directly with FK enforcement off so the chat survives
    conn = get_db()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM users WHERE id=?", (bob_uid,))
        conn.commit()
    finally:
        conn.close()
    return chat["id"]


def test_admin_chat_view_with_deleted_participant(admin_client, admin_uid, alice_uid, bob_uid):
    """Admin chat view should not crash when a participant's account was deleted."""
    chat_id = _make_chat_with_deleted_user(alice_uid, bob_uid)
    resp = admin_client.get(f"/admin/chats/{chat_id}")
    assert resp.status_code == 200
    assert b"[deleted user]" in resp.data
    assert b"alice" in resp.data
    assert b"Hello before deletion" in resp.data


def test_admin_chat_view_both_deleted(admin_client, admin_uid, alice_uid, bob_uid):
    """Admin chat view should handle both participants deleted."""
    import db
    from db._connection import get_db
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "Orphaned message")
    conn = get_db()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM users WHERE id=?", (alice_uid,))
        conn.execute("DELETE FROM users WHERE id=?", (bob_uid,))
        conn.commit()
    finally:
        conn.close()
    resp = admin_client.get(f"/admin/chats/{chat['id']}")
    assert resp.status_code == 200
    assert resp.data.count(b"[deleted user]") >= 2


def test_chat_export_with_deleted_participant(alice_uid, bob_uid, admin_uid):
    """Chat export should not crash when the other participant was deleted."""
    import db
    from app import app
    from db._connection import get_db
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "Export me")

    # Remove bob directly with FK enforcement off so the chat survives
    conn = get_db()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM users WHERE id=?", (bob_uid,))
        conn.commit()
    finally:
        conn.close()

    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}/export")
        assert resp.status_code == 200
        assert b"[deleted user]" in resp.data
