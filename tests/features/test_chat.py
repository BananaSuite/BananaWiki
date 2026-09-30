"""Direct messages: access rules, IDOR, attachments, incremental fetch, exports, admin pages."""

from __future__ import annotations

import pytest

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope

from .chat_support import JSON, chat_folder, send, set_settings, start_dm, switch, upload


@pytest.fixture
def people(make_user):
    return make_user("alice"), make_user("bob"), make_user("carol")


@pytest.fixture
def dm(client, login, people):
    alice, _bob, _carol = people
    login(client, alice)
    return start_dm(client, "bob")


def test_start_and_send(client, dm, db):
    response = send(client, f"/chats/{dm}/send", "hi <script>")
    assert response.json["ok"]
    page = client.get(f"/chats/{dm}")
    assert page.status_code == 200
    assert b"hi &lt;script&gt;" in page.data
    assert db.scalar("SELECT unread_count_user1 + unread_count_user2 FROM chats WHERE id = ?", (dm,)) == 1


def test_start_validation(client, login, people, make_user):
    login(client, people[0])
    assert client.post("/chats/new", data={"username": "alice"}, headers=JSON).status_code == 400
    assert client.post("/chats/new", data={"username": "nobody"}, headers=JSON).status_code == 400
    make_user("muted", chat_disabled=1)
    assert client.post("/chats/new", data={"username": "muted"}, headers=JSON).status_code == 400
    # Starting twice returns the same conversation.
    assert start_dm(client, "bob") == start_dm(client, "BOB")


def test_message_validation(client, dm, db):
    set_settings(db, chat_max_message_length=10)
    assert send(client, f"/chats/{dm}/send", "x" * 11).status_code == 400
    assert send(client, f"/chats/{dm}/send", "   ").status_code == 400
    assert send(client, f"/chats/{dm}/send", "x" * 10).status_code == 200


def test_non_participant_gets_404_everywhere(client, login, dm, people, db):
    send(client, f"/chats/{dm}/send", "secret", attachment=upload())
    message_id = db.scalar("SELECT id FROM chat_messages WHERE chat_id = ?", (dm,))
    attachment_id = db.scalar("SELECT id FROM chat_attachments")
    switch(client, login, people[2])
    assert client.get(f"/chats/{dm}").status_code == 404
    assert client.get(f"/chats/{dm}/messages?after=0", headers=JSON).status_code == 404
    assert send(client, f"/chats/{dm}/send", "intrude").status_code == 404
    assert client.post(f"/chats/{dm}/delete_message", data={"message_id": message_id}).status_code == 404
    assert client.get(f"/chats/attachments/{attachment_id}/download").status_code == 404
    assert client.get(f"/chats/{dm}/export").status_code == 404
    assert client.post(f"/chats/{dm}/clear").status_code == 404
    assert db.scalar("SELECT COUNT(*) FROM chat_messages") == 1


def test_delete_message_idor_and_ownership(client, login, dm, people, db):
    send(client, f"/chats/{dm}/send", "from alice")
    alice_msg = db.scalar("SELECT id FROM chat_messages WHERE chat_id = ?", (dm,))
    switch(client, login, people[2])
    own_chat = start_dm(client, "bob")
    # A message of another conversation cannot be deleted through one's own conversation.
    response = client.post(f"/chats/{own_chat}/delete_message", data={"message_id": alice_msg}, headers=JSON)
    assert response.status_code == 404
    switch(client, login, people[1])
    response = client.post(f"/chats/{dm}/delete_message", data={"message_id": alice_msg}, headers=JSON)
    assert response.status_code == 403
    assert db.scalar("SELECT is_deleted FROM chat_messages WHERE id = ?", (alice_msg,)) == 0


def test_delete_erases_text_attachment_and_blob(app, client, dm, db):
    send(client, f"/chats/{dm}/send", "oops", attachment=upload())
    row = db.one("SELECT a.id, a.filename, m.id AS message_id FROM chat_attachments a JOIN chat_messages m "
                 "ON m.id = a.message_id")
    blob = db.insert("file_blobs", {"filename": row["filename"], "content": b"x"})
    db.execute("UPDATE chat_attachments SET blob_id = ? WHERE id = ?", (blob, row["id"]))
    assert (chat_folder(app) / row["filename"]).exists()
    response = client.post(f"/chats/{dm}/delete_message", data={"message_id": row["message_id"]}, headers=JSON)
    assert response.json["ok"]
    message = db.one("SELECT content, is_deleted FROM chat_messages WHERE id = ?", (row["message_id"],))
    assert message == {"content": "", "is_deleted": 1}
    assert db.scalar("SELECT COUNT(*) FROM chat_attachments") == 0
    assert db.scalar("SELECT COUNT(*) FROM file_blobs") == 0
    assert not (chat_folder(app) / row["filename"]).exists()
    assert client.get(f"/chats/attachments/{row['id']}/download").status_code == 404


def test_incremental_fetch(client, login, dm, people, db):
    sender = db.scalar("SELECT user1_id FROM chats WHERE id = ?", (dm,))
    for number in range(60):
        db.insert("chat_messages", {"chat_id": dm, "sender_id": sender, "content": f"message {number}"})
    ids = db.column("SELECT id FROM chat_messages ORDER BY id")
    page = client.get(f"/chats/{dm}")
    assert b"message 59" in page.data and b"message 9<" not in page.data
    older = client.get(f"/chats/{dm}/messages?before={ids[10]}", headers=JSON).json
    assert [m["id"] for m in older["messages"]] == ids[:10]
    assert older["has_more"] is False
    first = client.get(f"/chats/{dm}/messages?after={ids[-3]}", headers=JSON).json
    assert [m["text"] for m in first["messages"]] == ["message 58", "message 59"]
    assert first["first_id"] == ids[0] and first["deleted"] == []
    cursor = first["cursor"]
    db.execute("UPDATE chat_messages SET is_deleted = 1, deleted_at = ? WHERE id = ?", (cursor, ids[5]))
    later = client.get(f"/chats/{dm}/messages?after={ids[-1]}&since={cursor}", headers=JSON).json
    assert later["messages"] == [] and later["deleted"] == [ids[5]]
    client.post(f"/chats/{dm}/clear")
    cleared = client.get(f"/chats/{dm}/messages?after={ids[-1]}", headers=JSON).json
    assert cleared["first_id"] is None


def test_polling_does_not_write_and_read_resets(client, login, dm, people, db):
    send(client, f"/chats/{dm}/send", "ping")
    switch(client, login, people[1])
    assert b'class="count-pill"' in client.get("/chats").data
    client.get(f"/chats/{dm}/messages?after=0", headers=JSON)
    assert db.scalar("SELECT unread_count_user1 + unread_count_user2 FROM chats") == 1
    assert client.post(f"/chats/{dm}/read", headers=JSON).json == {"ok": True}
    assert db.scalar("SELECT unread_count_user1 + unread_count_user2 FROM chats") == 0


def test_attachment_rules(client, login, dm, people, db, admin):
    set_settings(db, chat_attachments_per_day_limit=2)
    assert send(client, f"/chats/{dm}/send", "", attachment=upload("run.exe")).status_code == 400
    assert send(client, f"/chats/{dm}/send", "", attachment=upload("page.html")).status_code == 400
    assert send(client, f"/chats/{dm}/send", "", attachment=upload("a.txt")).status_code == 200
    assert send(client, f"/chats/{dm}/send", "", attachment=upload("b.txt")).status_code == 200
    assert send(client, f"/chats/{dm}/send", "", attachment=upload("c.txt")).status_code == 429
    set_settings(db, chat_attachments_per_day_limit=10, chat_max_attachment_size_mb=1)
    big = upload("big.txt", b"x" * (1024 * 1024 + 1))
    assert send(client, f"/chats/{dm}/send", "", attachment=big).status_code == 400
    set_settings(db, chat_attachments_enabled=0)
    assert send(client, f"/chats/{dm}/send", "", attachment=upload("d.txt")).status_code == 403
    attachment_id = db.scalar("SELECT MIN(id) FROM chat_attachments")
    response = client.get(f"/chats/attachments/{attachment_id}/download")
    assert response.status_code == 200 and response.data == b"hello file"
    assert response.headers["Content-Type"].startswith("application/octet-stream") or "text/plain" in \
        response.headers["Content-Type"]
    switch(client, login, people[1])
    assert client.get(f"/chats/attachments/{attachment_id}/download").status_code == 200
    switch(client, login, admin)
    assert client.get(f"/chats/attachments/{attachment_id}/download").status_code == 200


def test_legacy_blob_only_attachment_is_restored(app, client, dm, db):
    message_id = db.insert("chat_messages", {"chat_id": dm, "sender_id": db.scalar(
        "SELECT user1_id FROM chats WHERE id = ?", (dm,)), "content": "old"})
    blob = db.insert("file_blobs", {"filename": "legacy.txt", "content": b"from blob"})
    attachment_id = db.insert("chat_attachments", {"message_id": message_id, "filename": "legacy.txt",
                                                   "original_name": "legacy.txt", "file_size": 9, "blob_id": blob})
    response = client.get(f"/chats/attachments/{attachment_id}/download")
    assert response.status_code == 200 and response.data == b"from blob"


def test_chat_disabled_and_site_switch(client, login, make_user, db, admin):
    muted = make_user("muted", chat_disabled=1)
    login(client, muted)
    assert client.get("/chats").status_code == 403
    assert client.get("/groups").status_code == 403
    switch(client, login, make_user("dave"))
    set_settings(db, chat_dm_enabled=0)
    assert client.get("/chats").status_code == 403
    set_settings(db, chat_dm_enabled=1, chat_allow_dm_creation=0)
    assert client.post("/chats/new", data={"username": "admin_user"}, headers=JSON).status_code == 403
    switch(client, login, admin)
    set_settings(db, chat_dm_enabled=0)
    assert client.get("/chats").status_code == 200


def test_feature_switch(app, client, login, people):
    login(client, people[0])
    with app.app_context(), connection_scope():
        registry.set_enabled("chat", False)
    assert client.get("/chats").status_code == 404
    assert b"/chats" not in client.get("/_probe/private").data


def test_export_hides_ip_for_participants(client, dm):
    send(client, f"/chats/{dm}/send", "exported text")
    response = client.get(f"/chats/{dm}/export")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment")
    assert b"exported text" in response.data
    assert b"127.0.0.1" not in response.data


def test_export_with_attachments_is_a_zip(client, dm):
    import io
    import zipfile

    send(client, f"/chats/{dm}/send", "with file", attachment=upload("doc.txt", b"file body"))
    response = client.get(f"/chats/{dm}/export")
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    names = archive.namelist()
    assert any(name.startswith("attachments/") for name in names)
    assert b"file body" in archive.read([n for n in names if n.startswith("attachments/")][0])


def test_clear_removes_files(app, client, dm, db):
    send(client, f"/chats/{dm}/send", "x", attachment=upload())
    name = db.scalar("SELECT filename FROM chat_attachments")
    client.post(f"/chats/{dm}/clear")
    assert db.scalar("SELECT COUNT(*) FROM chat_messages") == 0
    assert not (chat_folder(app) / name).exists()


def test_admin_pages(client, login, dm, people, admin, db):
    send(client, f"/chats/{dm}/send", "watched")
    message_id = db.scalar("SELECT id FROM chat_messages")
    client.post(f"/chats/{dm}/delete_message", data={"message_id": message_id})
    assert client.get("/admin/chats").status_code == 403
    switch(client, login, admin)
    listing = client.get("/admin/chats?user=alice")
    assert listing.status_code == 200 and b"alice" in listing.data
    view = client.get(f"/admin/chats/{dm}")
    assert view.status_code == 200
    assert b"watched" not in view.data  # deleted content is gone for administrators too
    assert client.get("/admin/chats/999").status_code == 404


def test_admin_settings(client, admin_client, db):
    form = {
        "chat_dm_enabled": "1", "chat_group_enabled": "1", "chat_max_message_length": "200",
        "chat_max_attachment_size_mb": "3", "chat_attachments_per_day_limit": "4", "chat_cleanup_enabled": "1",
        "chat_cleanup_frequency_days": "2", "chat_cleanup_hour": "4", "chat_dm_auto_clear_messages": "1",
        "chat_dm_message_retention_days": "30", "chat_dm_attachment_retention_days": "7",
        "chat_group_message_retention_days": "0", "chat_group_attachment_retention_days": "7",
    }
    assert admin_client.post("/admin/chats/settings", data=form).status_code == 302
    row = db.one("SELECT chat_max_message_length, chat_allow_dm_creation, chat_dm_message_retention_days, "
                 "chat_cleanup_split_configured FROM site_settings")
    assert row == {"chat_max_message_length": 200, "chat_allow_dm_creation": 0,
                   "chat_dm_message_retention_days": 30, "chat_cleanup_split_configured": 1}
    bad = dict(form, chat_dm_message_retention_days="0")
    assert admin_client.post("/admin/chats/settings", data=bad).status_code == 400
    assert admin_client.get("/admin/chats/settings").status_code == 200


def test_send_is_rate_limited(client, dm):
    statuses = [send(client, f"/chats/{dm}/send", "flood").status_code for _ in range(31)]
    assert statuses[-1] == 429 and statuses.count(200) == 30


def test_user_suggestions(client, dm, make_user):
    make_user("bobby", suspended=1)
    users = client.get("/chats/users?q=bo", headers=JSON).json["users"]
    assert users == ["bob"]
