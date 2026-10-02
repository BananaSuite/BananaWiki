"""Group chats: membership, moderation permissions, bans, timeouts, invite codes, global room."""

from __future__ import annotations

import pytest

from .chat_support import JSON, chat_folder, create_group, send, set_settings, switch, upload


@pytest.fixture
def people(make_user):
    return {name: make_user(name) for name in ("owner", "mod", "member", "outsider")}


@pytest.fixture
def group(client, login, people, db):
    """A group owned by ``owner`` with ``mod`` (moderator) and ``member``; the client is the owner."""
    login(client, people["owner"])
    group_id = create_group(client)
    for name in ("mod", "member"):
        client.post(f"/groups/{group_id}/members/add", data={"username": name}, headers=JSON)
    client.post(f"/groups/{group_id}/promote", data={"user_id": people["mod"]["id"]}, headers=JSON)
    return group_id


def role(db, group_id, user):
    return db.scalar("SELECT role FROM group_members WHERE group_id = ? AND user_id = ? AND banned = 0",
                     (group_id, user["id"]))


def post(client, group_id, action, **data):
    return client.post(f"/groups/{group_id}/{action}", data=data, headers=JSON)


def test_create_and_system_messages_are_translated(client, group, db):
    assert db.scalar("SELECT role FROM group_members WHERE group_id = ? AND role = 'owner'", (group,)) == "owner"
    payload = client.get(f"/groups/{group}/messages?after=0", headers=JSON).json
    texts = [m["text"] for m in payload["messages"] if m["system"]]
    assert texts[0] == "owner created the group"
    assert "mod was added by owner" in texts
    stored = db.scalar("SELECT content FROM group_messages ORDER BY id LIMIT 1")
    assert stored.startswith('{"t": "chat.system.created"')


def test_create_validation(client, login, people, db):
    login(client, people["outsider"])
    assert client.post("/groups/new", data={"name": ""}).status_code == 400
    assert client.post("/groups/new", data={"name": "x" * 101}).status_code == 400
    set_settings(db, chat_allow_group_creation=0)
    assert client.post("/groups/new", data={"name": "ok"}, headers=JSON).status_code == 403


@pytest.mark.parametrize("target", ["/\\evil.example/path", "//evil.example/path", "https://evil.example/path"])
def test_group_badge_redirect_refuses_external_targets(client, group, db, target):
    set_settings(db, profile_group_badges_enabled=1)
    response = client.post("/settings/group-badges", data={"group_id": group, "visible": "1", "next": target})
    assert response.status_code == 302
    assert response.headers["Location"] == "/groups"
    assert db.scalar("SELECT visible FROM profile_group_badges WHERE group_id = ?", (group,)) == 1


def test_group_badge_redirect_keeps_local_target(client, group, db):
    set_settings(db, profile_group_badges_enabled=1)
    response = client.post("/settings/group-badges", data={"group_id": group, "visible": "1",
                                                          "next": "/settings#groups"})
    assert response.headers["Location"] == "/settings#groups"


def test_join_by_code_and_rate_limit(client, login, group, people, db):
    code = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    assert len(code) >= 10
    switch(client, login, people["outsider"])
    assert client.post("/groups/join", data={"invite_code": "nope"}, headers=JSON).status_code == 404
    response = client.post("/groups/join", data={"invite_code": code}, headers=JSON)
    assert response.json["group_id"] == group
    assert role(db, group, people["outsider"]) == "member"
    for _ in range(10):
        client.post("/groups/join", data={"invite_code": "guess"}, headers=JSON)
    assert client.post("/groups/join", data={"invite_code": code}, headers=JSON).status_code == 429


def test_non_member_cannot_read(client, login, group, people, db):
    send(client, f"/groups/{group}/send", "inside", attachment=upload())
    attachment_id = db.scalar("SELECT id FROM group_attachments")
    switch(client, login, people["outsider"])
    assert client.get(f"/groups/{group}").status_code == 302
    assert client.get(f"/groups/{group}/messages?after=0", headers=JSON).status_code == 403
    assert client.get(f"/groups/attachments/{attachment_id}/download").status_code == 404
    assert send(client, f"/groups/{group}/send", "hi").status_code == 403
    assert client.get(f"/groups/{group}/export", headers=JSON).status_code == 403
    assert client.get("/groups/999").status_code == 404


def test_member_cannot_moderate(client, login, group, people, db):
    switch(client, login, people["member"])
    target = people["outsider"]["id"]
    assert post(client, group, "members/add", username="outsider").status_code == 403
    assert post(client, group, "kick", user_id=people["mod"]["id"]).status_code == 403
    assert post(client, group, "promote", user_id=people["mod"]["id"]).status_code == 403
    assert post(client, group, "timeout", user_id=people["mod"]["id"], duration="5").status_code == 403
    assert post(client, group, "regenerate_code").status_code == 403
    assert post(client, group, "clear").status_code == 403
    assert post(client, group, "delete").status_code == 403
    assert post(client, group, "toggle_active").status_code == 403
    assert post(client, group, "admin_takeover").status_code == 403
    assert target and role(db, group, people["mod"]) == "moderator"


def test_moderator_limits(client, login, group, people, db):
    switch(client, login, people["mod"])
    assert post(client, group, "kick", user_id=people["owner"]["id"]).status_code == 403
    assert post(client, group, "promote", user_id=people["member"]["id"]).status_code == 403
    assert post(client, group, "timeout", user_id=people["member"]["id"], duration="30").status_code == 200
    switch(client, login, people["member"])
    assert send(client, f"/groups/{group}/send", "muted?").status_code == 403
    switch(client, login, people["mod"])
    assert post(client, group, "untimeout", user_id=people["member"]["id"]).status_code == 200
    switch(client, login, people["member"])
    assert send(client, f"/groups/{group}/send", "free").status_code == 200


def test_audit_10_revoked_chat_access_cannot_moderate(client, login, group, people, db):
    db.execute("UPDATE users SET chat_disabled = 1 WHERE id = ?", (people["mod"]["id"],))
    switch(client, login, people["mod"])
    assert post(client, group, "kick", user_id=people["member"]["id"]).status_code == 403
    assert post(client, group, "members/add", username="outsider").status_code == 403
    db.execute("UPDATE users SET chat_disabled = 0 WHERE id = ?", (people["mod"]["id"],))
    set_settings(db, chat_group_enabled=0)
    assert post(client, group, "kick", user_id=people["member"]["id"]).status_code == 403
    assert role(db, group, people["member"]) == "member"


def test_ban_and_unban(client, login, group, people, db):
    code = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    assert post(client, group, "kick", user_id=people["member"]["id"], permanent="1").json["ok"]
    switch(client, login, people["member"])
    assert client.get(f"/groups/{group}").status_code == 302
    assert client.post("/groups/join", data={"invite_code": code}, headers=JSON).status_code == 403
    assert post(client, group, "unban", user_id=people["member"]["id"]).status_code == 403
    switch(client, login, people["owner"])
    assert post(client, group, "members/add", username="member").status_code == 409
    assert post(client, group, "unban", user_id=people["member"]["id"]).json["ok"]
    switch(client, login, people["member"])
    assert client.post("/groups/join", data={"invite_code": code}, headers=JSON).status_code == 200


def test_kick_allows_rejoin_and_owner_rules(client, login, group, people, db):
    assert post(client, group, "kick", user_id=people["owner"]["id"]).status_code == 409
    assert post(client, group, "kick", user_id=people["member"]["id"]).json["ok"]
    assert role(db, group, people["member"]) is None
    assert post(client, group, "leave").status_code == 409


def test_roles_transfer_and_step_down(client, login, group, people, db):
    assert post(client, group, "demote", user_id=people["mod"]["id"]).json["ok"]
    assert role(db, group, people["mod"]) == "member"
    assert post(client, group, "transfer", user_id=people["member"]["id"]).json["ok"]
    assert role(db, group, people["member"]) == "owner"
    assert role(db, group, people["owner"]) == "moderator"
    assert post(client, group, "self-downgrade").json["ok"]
    assert role(db, group, people["owner"]) == "member"
    assert post(client, group, "leave").json["ok"]


def test_regenerate_code(client, login, group, people, db):
    old = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    assert post(client, group, "regenerate_code", custom_code="abc").status_code == 400
    assert post(client, group, "regenerate_code", custom_code="abc!defg").status_code == 400
    assert post(client, group, "regenerate_code", custom_code="Secret42").json["ok"]
    other = create_group(client, "Other")
    assert post(client, other, "regenerate_code", custom_code="Secret42").status_code == 409
    assert post(client, group, "regenerate_code").json["ok"]
    new = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    assert new not in (old, "Secret42")
    switch(client, login, people["outsider"])
    assert client.post("/groups/join", data={"invite_code": old}, headers=JSON).status_code == 404


def test_message_deletion_rules(client, login, group, people, db):
    switch(client, login, people["member"])
    send(client, f"/groups/{group}/send", "member text")
    member_msg = db.scalar("SELECT MAX(id) FROM group_messages")
    system_msg = db.scalar("SELECT MIN(id) FROM group_messages")
    switch(client, login, people["outsider"])
    assert post(client, group, "delete_message", message_id=member_msg).status_code == 403
    switch(client, login, people["mod"])
    assert post(client, group, "delete_message", message_id=system_msg).status_code == 403
    assert post(client, group, "delete_message", message_id=member_msg).json["ok"]
    assert db.scalar("SELECT content FROM group_messages WHERE id = ?", (member_msg,)) == ""
    texts = [m["text"] for m in client.get(f"/groups/{group}/messages?after={member_msg}", headers=JSON).json["messages"]]
    assert texts == ["mod deleted a message"]


def test_delete_group_removes_files_and_blobs(app, client, group, db):
    send(client, f"/groups/{group}/send", "file", attachment=upload())
    row = db.one("SELECT id, filename FROM group_attachments")
    blob = db.insert("file_blobs", {"filename": row["filename"], "content": b"x"})
    db.execute("UPDATE group_attachments SET blob_id = ? WHERE id = ?", (blob, row["id"]))
    db.execute("INSERT INTO profile_group_badges (user_id, group_id, visible) SELECT user_id, group_id, 1 "
               "FROM group_members WHERE group_id = ?", (group,))
    assert post(client, group, "delete").json["ok"]
    assert db.scalar("SELECT COUNT(*) FROM group_chats WHERE id = ?", (group,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM file_blobs") == 0
    assert db.scalar("SELECT COUNT(*) FROM profile_group_badges") == 0
    assert not (chat_folder(app) / row["filename"]).exists()


def test_opening_the_global_room_does_not_change_anything(client, login, people, db):
    """A GET (a link, an <img> on another site) used to create the room and join the visitor."""
    db.execute("DELETE FROM group_chats WHERE is_global = 1")
    login(client, people["outsider"])
    page = client.get("/groups/global")
    assert page.status_code == 200 and b'method="post"' in page.data
    assert db.scalar("SELECT COUNT(*) FROM group_chats WHERE is_global = 1") == 0
    assert db.scalar("SELECT COUNT(*) FROM group_members WHERE user_id = ?", (people["outsider"]["id"],)) == 0
    assert client.post("/groups/global").status_code == 302
    global_id = db.scalar("SELECT id FROM group_chats WHERE is_global = 1")
    assert db.scalar("SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
                     (global_id, people["outsider"]["id"])) == 1


def test_global_room(client, login, people, admin, db):
    login(client, people["member"])
    global_id = db.scalar("SELECT id FROM group_chats WHERE is_global = 1")
    assert global_id, "created by the migration, not by the first visit"
    first = client.post("/groups/global")
    assert first.status_code == 302
    assert client.get("/groups/global").headers["Location"].endswith(f"/groups/{global_id}")
    switch(client, login, people["outsider"])
    client.post("/groups/global")
    # Only site administrators moderate the global room.
    assert post(client, global_id, "timeout", user_id=people["member"]["id"], duration="5").status_code == 403
    switch(client, login, admin)
    assert client.get(f"/groups/{global_id}").status_code == 200  # without joining
    assert post(client, global_id, "timeout", user_id=people["member"]["id"], duration="indefinite").json["ok"]
    assert post(client, global_id, "delete").status_code == 409
    assert post(client, global_id, "toggle_active").json["ok"]
    switch(client, login, people["outsider"])
    assert send(client, f"/groups/{global_id}/send", "paused?").status_code == 403


def test_global_room_owner_role_does_not_grant_site_admin_powers(client, login, people, admin, db):
    global_id = db.scalar("SELECT id FROM group_chats WHERE is_global = 1")
    login(client, people["member"])
    client.post("/groups/global")
    message = send(client, f"/groups/{global_id}/send", "global message").json["message_id"]
    switch(client, login, admin)
    assert post(client, global_id, "admin_takeover").status_code == 200
    # A former site administrator keeps their membership, but the global
    # room still reserves moderation and management for current admins.
    db.execute("UPDATE users SET role = 'user' WHERE id = ?", (admin["id"],))
    assert post(client, global_id, "members/add", username="outsider").status_code == 403
    assert post(client, global_id, "clear").status_code == 403
    assert post(client, global_id, "regenerate_code").status_code == 403
    assert post(client, global_id, "delete_message", message_id=message).status_code == 403
    assert db.scalar("SELECT is_deleted FROM group_messages WHERE id = ?", (message,)) == 0


def test_admin_takeover_and_admin_pages(client, login, group, people, admin, db):
    switch(client, login, admin)
    assert client.get("/admin/groups").status_code == 200
    assert client.get(f"/admin/groups/{group}").status_code == 200
    assert post(client, group, "admin_takeover").json["ok"]
    assert role(db, group, admin) == "owner"
    assert role(db, group, people["owner"]) == "moderator"
    response = client.post(f"/admin/groups/{group}/delete")
    assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM group_chats WHERE id = ?", (group,)) == 0


def test_group_page_renders_for_roles(client, login, group, people):
    assert b"regenerate_code" in client.get(f"/groups/{group}").data
    switch(client, login, people["member"])
    page = client.get(f"/groups/{group}").data
    assert b"regenerate_code" not in page and b"/kick" not in page


def test_profile_badges(app, client, login, group, people, db):
    set_settings(db, profile_group_badges_enabled=1)
    other = create_group(client, "Hidden")
    assert client.post("/settings/group-badges", data={"group_id": group, "visible": "1"}).status_code == 302
    from bananawiki.wiki.features.chat import badges

    with app.test_request_context():
        html = str(badges.render_profile_section(profile_user=people["owner"]))
    assert "Team" in html and "Hidden" not in html and other
    switch(client, login, people["outsider"])
    assert client.post("/settings/group-badges", data={"group_id": group, "visible": "1"},
                       headers=JSON).status_code == 403
    switch(client, login, people["owner"])
    client.post(f"/groups/{group}/transfer", data={"user_id": people["mod"]["id"]})
    client.post(f"/groups/{group}/leave")
    assert db.scalar("SELECT COUNT(*) FROM profile_group_badges WHERE user_id = ?", (people["owner"]["id"],)) == 0
