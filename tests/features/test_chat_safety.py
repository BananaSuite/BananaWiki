"""Direct messages stay accountable.

* Clearing a conversation hides it for the participant who cleared it; the
  other keeps their copy. Messages both have cleared are erased, and an
  administrator can clear a conversation for both when moderating.
* Messages can be reported to the administrators, with a copy of the text.
* Administrators reading private conversations leaves an audit log entry.
"""

from __future__ import annotations

import pytest

from .chat_support import JSON, chat_folder, create_group, send, switch, upload


@pytest.fixture
def people(make_user):
    return make_user("alice"), make_user("bob"), make_user("carol")


@pytest.fixture
def dm(client, login, people):
    alice, bob, _carol = people
    login(client, alice)
    from .chat_support import start_dm

    chat = start_dm(client, "bob")
    send(client, f"/chats/{chat}/send", "hello bob", attachment=upload("notes.txt", b"alice notes"))
    switch(client, login, bob)
    send(client, f"/chats/{chat}/send", "something abusive")
    return chat


def _texts(client, chat):
    return [m["text"] for m in client.get(f"/chats/{chat}/messages?after=0", headers=JSON).json["messages"]]


def _audit(db, action):
    return db.all("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))


# Clearing


def test_clearing_hides_the_conversation_for_the_participant_who_cleared_it(client, login, dm, people, db):
    alice, bob, _carol = people
    attachment = db.scalar("SELECT id FROM chat_attachments")
    switch(client, login, bob)
    response = client.post(f"/chats/{dm}/clear", headers=JSON)
    assert response.status_code == 200 and response.json["ok"]
    assert _texts(client, dm) == []
    assert client.get(f"/chats/{dm}/messages?after=0", headers=JSON).json["first_id"] is None
    page = client.get(f"/chats/{dm}").get_data(as_text=True)
    assert "something abusive" not in page and "hello bob" not in page
    assert "something abusive" not in client.get("/chats").get_data(as_text=True)
    assert "hello bob" not in client.get(f"/chats/{dm}/export").get_data(as_text=True)
    assert client.get(f"/chats/attachments/{attachment}/download").status_code == 404
    # The other participant keeps the whole conversation, attachment included.
    switch(client, login, alice)
    assert _texts(client, dm) == ["hello bob", "something abusive"]
    assert client.get(f"/chats/attachments/{attachment}/download").status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?", (dm,)) == 2
    # Later messages reach both.
    send(client, f"/chats/{dm}/send", "still there?")
    switch(client, login, bob)
    assert _texts(client, dm) == ["still there?"]


def test_hidden_messages_cannot_be_deleted_by_the_one_who_hid_them(client, login, dm, people, db):
    _alice, bob, _carol = people
    abusive = db.scalar("SELECT id FROM chat_messages WHERE content = 'something abusive'")
    switch(client, login, bob)
    client.post(f"/chats/{dm}/clear", headers=JSON)
    response = client.post(f"/chats/{dm}/delete_message", data={"message_id": abusive}, headers=JSON)
    assert response.status_code == 404


def test_messages_both_participants_cleared_are_erased(app, client, login, dm, people, db):
    alice, bob, _carol = people
    name = db.scalar("SELECT filename FROM chat_attachments")
    switch(client, login, bob)
    client.post(f"/chats/{dm}/clear", headers=JSON)
    send(client, f"/chats/{dm}/send", "after bob cleared")
    switch(client, login, alice)
    client.post(f"/chats/{dm}/clear", headers=JSON)
    assert db.column("SELECT content FROM chat_messages WHERE chat_id = ?", (dm,)) == ["after bob cleared"]
    assert db.scalar("SELECT COUNT(*) FROM chat_attachments") == 0
    assert not (chat_folder(app) / name).exists()
    switch(client, login, bob)
    assert _texts(client, dm) == ["after bob cleared"]


def test_an_administrator_can_clear_a_conversation_for_both(client, login, dm, people, admin, db):
    switch(client, login, admin)
    response = client.post(f"/admin/chats/{dm}/clear")
    assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?", (dm,)) == 0
    entries = _audit(db, "chat.dm_cleared")
    assert len(entries) == 1 and entries[0]["actor_id"] == admin["id"] and entries[0]["target_id"] == str(dm)


def test_clearing_needs_post_and_participation(client, login, dm, people, db):
    switch(client, login, people[2])
    assert client.post(f"/chats/{dm}/clear", headers=JSON).status_code == 404
    assert client.post(f"/admin/chats/{dm}/clear").status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM chat_messages") == 2


# Reports


def test_a_participant_reports_a_message_and_the_report_outlives_it(client, login, dm, people, admin, db):
    alice, bob, carol = people
    abusive = db.scalar("SELECT id FROM chat_messages WHERE content = 'something abusive'")
    own = db.scalar("SELECT id FROM chat_messages WHERE content = 'hello bob'")
    switch(client, login, alice)
    page = client.get(f"/chats/{dm}").get_data(as_text=True)
    assert f'action="/chats/{dm}/report"' in page
    assert client.post(f"/chats/{dm}/report", data={"message_id": own}, headers=JSON).status_code == 400
    response = client.post(f"/chats/{dm}/report", data={"message_id": abusive}, headers=JSON)
    assert response.status_code == 200 and response.json["ok"]
    client.post(f"/chats/{dm}/report", data={"message_id": abusive}, headers=JSON)  # once per reporter
    report = db.one("SELECT * FROM chat__reports")
    assert db.scalar("SELECT COUNT(*) FROM chat__reports") == 1
    assert (report["kind"], report["parent_id"], report["message_id"]) == ("dm", dm, abusive)
    assert (report["reporter_id"], report["reported_user_id"]) == (alice["id"], bob["id"])
    assert report["content"] == "something abusive" and report["status"] == "open"
    # The sender deletes the message; the administrators still see what was reported.
    switch(client, login, bob)
    client.post(f"/chats/{dm}/delete_message", data={"message_id": abusive}, headers=JSON)
    switch(client, login, carol)
    assert client.post(f"/chats/{dm}/report", data={"message_id": abusive}, headers=JSON).status_code == 404
    assert client.get("/admin/chats/reports").status_code == 403
    switch(client, login, admin)
    listing = client.get("/admin/chats/reports").get_data(as_text=True)
    assert "something abusive" in listing and "bob" in listing and "alice" in listing
    client.post(f"/admin/chats/reports/{report['id']}/resolve")
    assert db.scalar("SELECT status FROM chat__reports") == "resolved"
    assert db.scalar("SELECT resolved_by FROM chat__reports") == admin["id"]
    assert len(_audit(db, "chat.report_resolved")) == 1


def test_a_group_member_reports_a_message(client, login, people, db, admin):
    alice, bob, _carol = people
    login(client, alice)
    group = create_group(client, "Team")
    code = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    switch(client, login, bob)
    assert client.post("/groups/join", data={"invite_code": code}, headers=JSON).status_code == 200
    send(client, f"/groups/{group}/send", "rude words")
    message = db.scalar("SELECT id FROM group_messages WHERE content = 'rude words'")
    switch(client, login, alice)
    assert client.post(f"/groups/{group}/report", data={"message_id": message}, headers=JSON).status_code == 200
    assert db.one("SELECT kind, reporter_id, content FROM chat__reports") == {
        "kind": "group", "reporter_id": alice["id"], "content": "rude words"}


# Administrators reading


def test_administrators_reading_private_conversations_is_audited(client, login, dm, people, admin, db):
    alice, _bob, _carol = people
    switch(client, login, alice)
    group = create_group(client, "Private team")
    switch(client, login, admin)
    assert client.get(f"/admin/chats/{dm}").status_code == 200
    entries = _audit(db, "chat.dm_read")
    assert len(entries) == 1 and entries[0]["target_id"] == str(dm) and entries[0]["actor_id"] == admin["id"]
    assert client.get(f"/admin/groups/{group}").status_code == 200
    assert client.get(f"/groups/{group}").status_code == 200  # not a member: reading as an administrator
    assert [row["target_id"] for row in _audit(db, "chat.group_read")] == [str(group), str(group)]
    attachment = db.scalar("SELECT id FROM chat_attachments")
    assert client.get(f"/chats/attachments/{attachment}/download").status_code == 200
    assert len(_audit(db, "chat.dm_attachment_read")) == 1


def test_administrators_reading_a_private_group_another_way_is_audited(client, login, people, admin, db):
    alice, _bob, _carol = people
    login(client, alice)
    group = create_group(client, "Private team")
    send(client, f"/groups/{group}/send", "plans", attachment=upload("plans.txt", b"secret plans"))
    attachment = db.scalar("SELECT id FROM group_attachments")
    # A member's own reads are not recorded.
    assert client.get(f"/groups/attachments/{attachment}/download").status_code == 200
    assert _audit(db, "chat.group_attachment_read") == []
    switch(client, login, admin)
    assert client.get(f"/groups/attachments/{attachment}/download").status_code == 200
    entries = _audit(db, "chat.group_attachment_read")
    assert len(entries) == 1 and entries[0]["actor_id"] == admin["id"] and entries[0]["target_id"] == str(group)
    assert client.get(f"/groups/{group}/export").status_code == 200
    assert [row["target_id"] for row in _audit(db, "chat.group_exported")] == [str(group)]
    assert client.post(f"/groups/{group}/admin_takeover", headers=JSON).status_code == 200
    assert [row["target_id"] for row in _audit(db, "chat.group_taken_over")] == [str(group)]
