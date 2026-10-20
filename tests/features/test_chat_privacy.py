"""Privacy and accountability of chats.

* A member someone blocked is not told: what they send in a conversation they
  already have is withheld from the one who blocked them, never refused, and
  the one who blocked them is not suggested to them.
* A sender takes back a direct message only shortly after sending it; later it
  belongs to the recipient's copy too, unless the sender deletes their account
  (documented exception: the conversation goes with it, reported copies stay).
* Moderation views and exports hide the addresses of accounts the
  administrator may not manage, as the sessions pages do.
* Administrators reading a private group as JSON are audited, once per window.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from conftest import PASSWORD

from .chat_support import JSON, create_group, send, start_dm, switch, upload


@pytest.fixture
def people(make_user):
    return make_user("alice"), make_user("bob"), make_user("carol")


def _texts(client, chat):
    return [m["text"] for m in client.get(f"/chats/{chat}/messages?after=0", headers=JSON).json["messages"]]


def _export(client, chat):
    """The transcript of a conversation export (zipped with its attachments when there are any)."""
    response = client.get(f"/chats/{chat}/export")
    assert response.status_code == 200
    if response.mimetype == "application/zip":
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as bundle:
            return next(bundle.read(name) for name in bundle.namelist() if name.endswith(".txt")).decode()
    return response.get_data(as_text=True)


def _block(client, user):
    assert client.post("/chats/blocks", data={"user_id": user["id"]}, headers=JSON).status_code == 200


def _send_from(client, url, content, address):
    response = client.post(url, data={"content": content}, headers=JSON, content_type="multipart/form-data",
                           environ_base={"REMOTE_ADDR": address})
    assert response.status_code == 200, response.data[:300]


def _audit(db, action):
    return db.all("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))


# Blocks stay private


def test_a_blocked_member_is_not_told_in_a_conversation_they_already_have(client, login, people, db, admin):
    alice, bob, _carol = people
    login(client, bob)
    chat = start_dm(client, "alice")
    send(client, f"/chats/{chat}/send", "hi alice")
    switch(client, login, alice)
    _block(client, bob)

    switch(client, login, bob)
    response = send(client, f"/chats/{chat}/send", "are you there?", attachment=upload("x.txt", b"x"))
    assert response.status_code == 200 and response.json["ok"]
    assert _texts(client, chat) == ["hi alice", "are you there?"]
    page = client.get(f"/chats/{chat}").get_data(as_text=True)
    assert 'id="chat-composer"' in page and "are you there?" in page
    assert "cannot receive" not in page and "Not delivered" not in page
    assert "are you there?" in client.get("/chats").get_data(as_text=True)
    assert "are you there?" in _export(client, chat)
    attachment = db.scalar("SELECT id FROM chat_attachments")
    assert client.get(f"/chats/attachments/{attachment}/download").status_code == 200
    withheld = db.one("SELECT id, withheld FROM chat_messages WHERE content = 'are you there?'")
    assert withheld["withheld"] == 1

    switch(client, login, alice)
    # Blocking marked "hi alice" read; what is withheld never counts as unread.
    assert db.scalar("SELECT unread_count_user1 + unread_count_user2 FROM chats") == 0
    assert _texts(client, chat) == ["hi alice"]
    assert "are you there?" not in client.get(f"/chats/{chat}").get_data(as_text=True)
    assert "are you there?" not in client.get("/chats").get_data(as_text=True)
    assert "are you there?" not in _export(client, chat)
    assert client.get(f"/chats/attachments/{attachment}/download").status_code == 404
    assert client.post(f"/chats/{chat}/report", data={"message_id": withheld["id"]},
                       headers=JSON).status_code == 404
    # Unblocking delivers what comes next, never what was withheld.
    assert client.post("/chats/blocks/remove", data={"user_id": bob["id"]}, headers=JSON).status_code == 200
    switch(client, login, bob)
    send(client, f"/chats/{chat}/send", "sorry")
    switch(client, login, alice)
    assert _texts(client, chat) == ["hi alice", "sorry"]

    # The administrators see everything, and what was not delivered.
    switch(client, login, admin)
    page = client.get(f"/admin/chats/{chat}").get_data(as_text=True)
    assert "are you there?" in page and "Not delivered: the recipient had blocked the sender" in page


def test_people_who_blocked_you_are_not_suggested_and_answer_like_inactive_accounts(client, login, people, db,
                                                                                    make_user):
    alice, bob, _carol = people
    make_user("alix", suspended=1)
    login(client, alice)
    _block(client, bob)
    switch(client, login, bob)
    assert client.get("/chats/users?q=ali").json == {"users": []}
    blocked = client.post("/chats/new", data={"username": "alice"}, headers=JSON)
    suspended = client.post("/chats/new", data={"username": "alix"}, headers=JSON)
    assert blocked.status_code == suspended.status_code == 400 and blocked.json == suspended.json
    group = create_group(client, "Bob's group")
    blocked = client.post(f"/groups/{group}/members/add", data={"username": "alice"}, headers=JSON)
    suspended = client.post(f"/groups/{group}/members/add", data={"username": "alix"}, headers=JSON)
    assert blocked.status_code == suspended.status_code == 404 and blocked.json == suspended.json
    switch(client, login, _carol)
    assert client.get("/chats/users?q=ali").json == {"users": ["alice"]}


# Taking back a direct message


def test_a_sender_takes_back_a_direct_message_only_shortly_after_sending_it(client, login, people, db):
    alice, _bob, _carol = people
    login(client, _bob)
    chat = start_dm(client, "alice")
    send(client, f"/chats/{chat}/send", "you will regret this")
    send(client, f"/chats/{chat}/send", "typo")
    old = db.scalar("SELECT id FROM chat_messages WHERE content = 'you will regret this'")
    recent = db.scalar("SELECT id FROM chat_messages WHERE content = 'typo'")
    db.execute("UPDATE chat_messages SET created_at = datetime('now', '-16 minutes') WHERE id = ?", (old,))
    shown = client.get(f"/chats/{chat}/messages?after=0", headers=JSON).json["messages"]
    assert {m["text"]: m["can_delete"] for m in shown} == {"you will regret this": False, "typo": True}
    refused = client.post(f"/chats/{chat}/delete_message", data={"message_id": old}, headers=JSON)
    assert refused.status_code == 403 and "15 minutes" in refused.json["error"]
    assert client.post(f"/chats/{chat}/delete_message", data={"message_id": recent}, headers=JSON).status_code == 200
    switch(client, login, alice)
    assert _texts(client, chat) == ["you will regret this", ""]
    assert db.scalar("SELECT content FROM chat_messages WHERE id = ?", (old,)) == "you will regret this"
    # Group messages are moderated by the group: their senders still delete them at any time.
    group = create_group(client, "Team")
    send(client, f"/groups/{group}/send", "old news")
    message = db.scalar("SELECT id FROM group_messages WHERE content = 'old news'")
    db.execute("UPDATE group_messages SET created_at = datetime('now', '-2 days') WHERE id = ?", (message,))
    assert client.post(f"/groups/{group}/delete_message", data={"message_id": message},
                       headers=JSON).status_code == 200


def test_deleting_an_account_removes_its_conversations_but_not_reported_copies(client, login, people, db):
    # The documented exception to the delete window.
    # chats.user1_id/user2_id cascade, so the recipient loses the conversation; what she
    # reported stays with the administrators (chat__reports.reported_user_id is SET NULL).
    alice, bob, _carol = people
    login(client, bob)
    chat = start_dm(client, "alice")
    send(client, f"/chats/{chat}/send", "you will regret this")
    message = db.scalar("SELECT id FROM chat_messages WHERE content = 'you will regret this'")
    db.execute("UPDATE chat_messages SET created_at = datetime('now', '-16 minutes') WHERE id = ?", (message,))
    switch(client, login, alice)
    assert client.post(f"/chats/{chat}/report", data={"message_id": message}, headers=JSON).status_code == 200
    switch(client, login, bob)
    client.post("/settings/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (bob["id"],)) is None
    assert db.scalar("SELECT COUNT(*) FROM chats") == 0
    assert db.all("SELECT content, reported_user_id FROM chat__reports") == [
        {"content": "you will regret this", "reported_user_id": None}]


# Addresses in moderation views


def test_moderation_views_hide_addresses_of_accounts_the_administrator_may_not_manage(client, login, make_user,
                                                                                     admin, db):
    owner = make_user("olivia", role="owner")
    other_admin = make_user("adam", role="admin")
    bob = make_user("bob")
    login(client, owner)
    chat = start_dm(client, "bob")
    _send_from(client, f"/chats/{chat}/send", "from home", "203.0.113.77")
    group = create_group(client, "Owners' room")
    _send_from(client, f"/groups/{group}/send", "group hello", "203.0.113.77")
    switch(client, login, bob)
    _send_from(client, f"/chats/{chat}/send", "hello owner", "198.51.100.5")
    switch(client, login, other_admin)
    peer = start_dm(client, admin["username"])
    _send_from(client, f"/chats/{peer}/send", "admin to admin", "192.0.2.9")

    switch(client, login, admin)  # a plain administrator
    _send_from(client, f"/chats/{peer}/send", "my own", "192.0.2.10")
    page = client.get(f"/admin/chats/{chat}").get_data(as_text=True)
    assert "203.0.113.77" not in page and "IP hidden" in page and "198.51.100.5" in page
    page = client.get(f"/admin/chats/{peer}").get_data(as_text=True)
    assert "192.0.2.9" not in page and "192.0.2.10" in page
    page = client.get(f"/admin/groups/{group}").get_data(as_text=True)
    assert "group hello" in page and "203.0.113.77" not in page and "IP hidden" in page
    exported = client.get(f"/groups/{group}/export").get_data(as_text=True)
    assert "group hello" in exported and "203.0.113.77" not in exported and "olivia (IP hidden)" in exported
    exported = client.get(f"/chats/{peer}/export").get_data(as_text=True)
    assert "adam (IP hidden)" in exported and "192.0.2.10" in exported and "192.0.2.9" not in exported

    switch(client, login, owner)  # owners see administrators and their own address
    page = client.get(f"/admin/chats/{chat}").get_data(as_text=True)
    assert "203.0.113.77" in page and "198.51.100.5" in page
    assert "192.0.2.9" in client.get(f"/admin/chats/{peer}").get_data(as_text=True)


# Administrators reading groups


def test_reading_a_private_group_as_json_is_audited_once_per_window(app, client, login, people, admin, db):
    alice, _bob, _carol = people
    login(client, alice)
    group = create_group(client, "Private team")
    send(client, f"/groups/{group}/send", "secret plans for tomorrow")
    client.get(f"/groups/{group}/messages?after=0", headers=JSON)
    switch(client, login, admin)
    for _ in range(3):
        response = client.get(f"/groups/{group}/messages?before=999999999&limit=100", headers=JSON)
        assert "secret plans for tomorrow" in [m["text"] for m in response.json["messages"]]
    entries = _audit(db, "chat.group_read")
    assert len(entries) == 1 and entries[0]["actor_id"] == admin["id"] and entries[0]["target_id"] == str(group)
    assert client.get(f"/groups/{group}").status_code == 200  # opening the page is always recorded
    client.get(f"/groups/{group}/messages?after=0", headers=JSON)
    assert len(_audit(db, "chat.group_read")) == 2
    app.extensions["bananawiki.limiter"].reset()  # the window has passed
    client.get(f"/groups/{group}/messages?after=0", headers=JSON)
    assert len(_audit(db, "chat.group_read")) == 3
    # The global room is open to everyone: reading it is not recorded.
    global_room = client.post("/groups/global", headers=JSON).json["group_id"]
    client.post(f"/groups/{global_room}/leave", headers=JSON)
    client.get(f"/groups/{global_room}/messages?after=0", headers=JSON)
    assert len(_audit(db, "chat.group_read")) == 3
