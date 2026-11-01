"""Regression tests for the r3 findings of the full re-audit pass 12 (account deletion and direct conversation files)."""

from __future__ import annotations

import io

from bananawiki.wiki import storage

PASSWORD = "correct horse battery"

# RW3-3: deleting an account removes the files of its direct conversations at once


def _exchange(app, make_user, login, db, first: str, second: str) -> tuple[dict, dict, dict, list[str]]:
    """*first* and *second* each send an attachment in their conversation; one file also has a legacy blob."""
    one, two = make_user(first), make_user(second)
    clients = {first: app.test_client(), second: app.test_client()}
    login(clients[first], one)
    login(clients[second], two)
    assert clients[first].post("/chats/new", data={"username": second}).status_code == 302
    chat_id = db.scalar("SELECT id FROM chats WHERE user1_id IN (?, ?) AND user2_id IN (?, ?)",
                        (one["id"], two["id"], one["id"], two["id"]))
    for name, client in clients.items():
        response = client.post(f"/chats/{chat_id}/send", content_type="multipart/form-data",
                               data={"content": f"from {name}",
                                     "attachment": (io.BytesIO(b"private " + name.encode()), f"{name}-scan.pdf")})
        assert response.status_code == 302, response.data[:200]
    files = db.column("SELECT filename FROM chat_attachments WHERE message_id IN "
                      "(SELECT id FROM chat_messages WHERE chat_id = ?)", (chat_id,))
    assert len(files) == 2
    blob = db.insert("file_blobs", {"filename": files[0], "content": b"legacy copy"})
    db.execute("UPDATE chat_attachments SET blob_id = ? WHERE filename = ?", (blob, files[0]))
    return one, two, clients, files


def _on_disk(app, files: list[str]) -> list[str]:
    with app.app_context():
        folder = storage.folder_path("chat_attachments")
    return [name for name in files if (folder / name).exists()]


def test_rw3_3_own_deletion_removes_both_participants_files(app, make_user, login, db):
    alice, _bob, clients, files = _exchange(app, make_user, login, db, "alice", "bob")
    # An unrelated conversation keeps its files.
    _carol, _dave, _, kept = _exchange(app, make_user, login, db, "carol", "dave")
    assert clients["alice"].post("/settings/delete", data={"password": PASSWORD}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (alice["id"],)) == 0
    assert _on_disk(app, files) == []
    assert db.scalar("SELECT COUNT(*) FROM file_blobs WHERE filename = ?", (files[0],)) == 0
    assert sorted(_on_disk(app, kept)) == sorted(kept)
    assert db.scalar("SELECT COUNT(*) FROM file_blobs WHERE filename = ?", (kept[0],)) == 1


def test_rw3_3_shared_deletion_path_removes_the_files(app, make_user, login, db):
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope

    _erin, frank, _, files = _exchange(app, make_user, login, db, "erin", "frank")
    with app.test_request_context(), connection_scope():
        accounts.delete(frank, deleted_by=None)
    assert db.scalar("SELECT COUNT(*) FROM chats") == 0
    assert _on_disk(app, files) == []


def test_rw3_3_merge_removes_the_pairs_conversation_files(app, make_user, login, db):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.users import merge

    for delete_source in (True, False):
        old, new, _, files = _exchange(app, make_user, login, db, f"old{int(delete_source)}",
                                       f"new{int(delete_source)}")
        admin = make_user(f"boss{int(delete_source)}", role="owner")
        with app.test_request_context(), connection_scope():
            merge.execute(old, new, admin, delete_source=delete_source)
        assert _on_disk(app, files) == []
        assert db.scalar("SELECT COUNT(*) FROM file_blobs WHERE filename = ?", (files[0],)) == 0


def test_rw3_3_files_go_while_chat_is_switched_off_or_hidden_by_bm_mode(app, make_user, login, db):
    """Switching chat off keeps its data, so ``user.delete`` still reaches it: the files must go too."""
    from bananawiki.wiki import accounts, bm, registry
    from bananawiki.wiki.db import connection_scope

    for how in ("switched off", "bm mode"):
        with app.test_request_context(), connection_scope():
            registry.set_enabled("chat", True)
        one, _two, _, files = _exchange(app, make_user, login, db, f"gina{how[0]}", f"hank{how[0]}")
        with app.test_request_context(), connection_scope():
            if how == "switched off":
                registry.set_enabled("chat", False)
            else:
                bm.save(mode=True, features={}, simplifications_on={})
        with app.test_request_context(), connection_scope():
            assert not registry.is_enabled("chat"), how
            accounts.delete(one, deleted_by=None)
        assert db.scalar("SELECT COUNT(*) FROM chat_attachments WHERE filename IN (?, ?)", tuple(files)) == 0
        assert _on_disk(app, files) == [], how


def test_rw3_3_a_rolled_back_deletion_keeps_the_files(app, make_user, login, db):
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.db import db as wiki_db

    one, _two, _, files = _exchange(app, make_user, login, db, "ivan", "judy")
    for nested in (False, True):
        with app.test_request_context(), connection_scope():
            try:
                with wiki_db.transaction():
                    if nested:
                        with wiki_db.transaction():
                            accounts.delete(one, deleted_by=None, emit_event=False)
                            raise RuntimeError("savepoint rolled back")
                    accounts.delete(one, deleted_by=None, emit_event=False)
                    raise RuntimeError("rolled back")
            except RuntimeError:
                pass
        assert sorted(_on_disk(app, files)) == sorted(files)
        assert db.scalar("SELECT COUNT(*) FROM chat_attachments WHERE filename IN (?, ?)", tuple(files)) == 2


def test_rw3_3_a_rolled_back_savepoint_keeps_its_files_when_the_outer_transaction_commits(app, make_user, login, db):
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.db import db as wiki_db

    one, _two, _, files = _exchange(app, make_user, login, db, "kate", "leo")
    with app.test_request_context(), connection_scope(), wiki_db.transaction():
        try:
            with wiki_db.transaction():
                accounts.delete(one, deleted_by=None, emit_event=False)
                raise RuntimeError("savepoint rolled back")
        except RuntimeError:
            pass
        wiki_db.execute("UPDATE site_settings SET chat_max_message_length = 4000 WHERE id = 1")
    assert db.scalar("SELECT chat_max_message_length FROM site_settings WHERE id = 1") == 4000
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (one["id"],)) == 1
    assert sorted(_on_disk(app, files)) == sorted(files)


def test_rw3_3_removing_a_wiki_user_from_the_hosting_portal_removes_the_files(app, make_user, login, db):
    from bananawiki.ops import tenant_task

    one, _two, _, files = _exchange(app, make_user, login, db, "mona", "ned")
    _other, _, _, kept = _exchange(app, make_user, login, db, "olga", "pete")
    assert tenant_task.remove_user(app.config["BW"], {"username": "mona"}) == {}
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (one["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM chat_attachments WHERE filename IN (?, ?)", tuple(files)) == 0
    assert db.scalar("SELECT COUNT(*) FROM file_blobs WHERE filename = ?", (files[0],)) == 0
    assert _on_disk(app, files) == []
    assert sorted(_on_disk(app, kept)) == sorted(kept)
    assert db.scalar("SELECT COUNT(*) FROM file_blobs WHERE filename = ?", (kept[0],)) == 1
