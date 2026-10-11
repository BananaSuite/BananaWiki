"""Members block other members in chats (chat.blocks).

* What a blocked member sends the member who blocked them in a conversation
  they already have is withheld from her; they cannot start a conversation
  with her or add her to a group (the refusals read like those for an
  inactive account, and an existing conversation or membership is answered
  as for anyone). The member who blocked cannot message them either.
* In groups they share, the blocked member's messages are hidden from the
  member who blocked them (system messages stay) and are not unread for them.
* Blocking is private and is undone in the account settings.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest

from .chat_support import JSON, chat_folder, create_group, send, start_dm, switch, upload


@pytest.fixture
def people(make_user):
    return make_user("alice"), make_user("bob"), make_user("carol")


def _block(client, user, **extra):
    return client.post("/chats/blocks", data={"user_id": user["id"], **extra}, headers=JSON)


def _unblock(client, user):
    return client.post("/chats/blocks/remove", data={"user_id": user["id"]}, headers=JSON)


def _texts(client, url):
    return [m["text"] for m in client.get(url, headers=JSON).json["messages"]]


def _join(client, db, group):
    code = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    assert client.post("/groups/join", data={"invite_code": code}, headers=JSON).status_code == 200


# ── Direct messages ──────────────────────────────────────────────────────────


def test_what_a_blocked_member_sends_is_withheld_from_the_one_who_blocked_them(app, client, login, people, db):
    alice, bob, _carol = people
    login(client, bob)
    chat = start_dm(client, "alice")
    send(client, f"/chats/{chat}/send", "hi alice")
    switch(client, login, alice)
    assert _block(client, bob).status_code == 200
    assert db.all("SELECT blocker_id, blocked_id FROM chat__blocks") == [
        {"blocker_id": alice["id"], "blocked_id": bob["id"]}]

    switch(client, login, bob)
    response = send(client, f"/chats/{chat}/send", "are you there?", attachment=upload("x.txt", b"x"))
    assert response.status_code == 200
    assert db.all("SELECT content, withheld FROM chat_messages ORDER BY id") == [
        {"content": "hi alice", "withheld": 0}, {"content": "are you there?", "withheld": 1}]
    assert len(list(chat_folder(app).glob("*"))) == 1
    page = client.get(f"/chats/{chat}").get_data(as_text=True)
    assert 'id="chat-composer"' in page and "are you there?" in page
    # "New conversation" opens the one they have, as it does for anyone else.
    reopened = client.post("/chats/new", data={"username": "alice"}, headers=JSON)
    assert reopened.status_code == 200 and reopened.json["chat_id"] == chat
    # Blocking marked "hi alice" read for alice; what is withheld never counts as unread.
    assert db.scalar("SELECT unread_count_user1 + unread_count_user2 FROM chats") == 0


def test_the_member_who_blocked_cannot_message_until_they_unblock(client, login, people, db):
    alice, bob, _carol = people
    login(client, alice)
    chat = start_dm(client, "bob")
    _block(client, bob)
    page = client.get(f"/chats/{chat}").get_data(as_text=True)
    assert "You blocked bob." in page and 'id="chat-composer"' not in page
    assert 'action="/chats/blocks/remove"' in page
    response = send(client, f"/chats/{chat}/send", "one more thing")
    assert response.status_code == 403 and "Unblock" in response.json["error"]
    assert client.post("/chats/new", data={"username": "bob"}, headers=JSON).status_code == 403

    assert _unblock(client, bob).status_code == 200
    assert send(client, f"/chats/{chat}/send", "sorry").status_code == 200
    switch(client, login, bob)
    assert send(client, f"/chats/{chat}/send", "no problem").status_code == 200
    assert _texts(client, f"/chats/{chat}/messages?after=0") == ["sorry", "no problem"]


def test_starting_a_conversation_with_someone_who_blocked_you_reads_like_an_inactive_name(client, login, people):
    alice, bob, _carol = people
    login(client, alice)
    _block(client, bob)
    switch(client, login, bob)
    refused = client.post("/chats/new", data={"username": "alice"}, headers=JSON)
    unknown = client.post("/chats/new", data={"username": "nobody_here"}, headers=JSON)
    assert refused.status_code == unknown.status_code == 400
    assert refused.json["error"] == unknown.json["error"]


# ── Groups ───────────────────────────────────────────────────────────────────


def test_group_messages_of_a_blocked_member_are_hidden_from_the_one_who_blocked(client, login, people, db):
    alice, bob, carol = people
    login(client, carol)
    group = create_group(client, "Team")
    send(client, f"/groups/{group}/send", "first")
    for person in (alice, bob):
        switch(client, login, person)
        _join(client, db, group)
    switch(client, login, alice)
    _block(client, bob)
    client.get(f"/groups/{group}")  # read so far
    switch(client, login, bob)
    for n in range(3):
        send(client, f"/groups/{group}/send", f"bob says {n}")
    switch(client, login, carol)
    send(client, f"/groups/{group}/send", "middle")
    send(client, f"/groups/{group}/send", "last")

    switch(client, login, alice)
    # Bob's messages are not unread for alice and never her group's preview.
    unread = {row["user_id"]: row["unread_count"] for row in db.all(
        "SELECT user_id, unread_count FROM group_members WHERE group_id = ?", (group,))}
    assert unread[alice["id"]] == 2 and unread[carol["id"]] == 3
    listing = client.get("/groups").get_data(as_text=True)
    assert "last" in listing and "bob says" not in listing
    texts = _texts(client, f"/groups/{group}/messages?after=0")
    assert "bob says 0" not in " ".join(texts) and texts[-1] == "last" and "first" in texts
    assert any("bob joined" in text for text in texts), "system messages stay"
    page = client.get(f"/groups/{group}").get_data(as_text=True)
    assert "bob says" not in page and "Messages from members you blocked are hidden here." in page
    # Paging backwards skips them too, and knows when only hidden messages are older.
    last = db.scalar("SELECT id FROM group_messages WHERE content = 'last'")
    older = client.get(f"/groups/{group}/messages?before={last}&limit=1", headers=JSON).json
    assert [m["text"] for m in older["messages"]] == ["middle"] and older["has_more"] is True
    db.execute("DELETE FROM group_messages WHERE group_id = ? AND id < (SELECT MIN(id) FROM group_messages "
               "WHERE sender_id = ?)", (group, bob["id"]))
    older = client.get(f"/groups/{group}/messages?before={last}&limit=1", headers=JSON).json
    assert [m["text"] for m in older["messages"]] == ["middle"] and older["has_more"] is False

    switch(client, login, carol)
    assert [t for t in _texts(client, f"/groups/{group}/messages?after=0") if t.startswith("bob says")] == [
        "bob says 0", "bob says 1", "bob says 2"]

    switch(client, login, alice)
    _unblock(client, bob)
    assert "bob says 2" in _texts(client, f"/groups/{group}/messages?after=0")


def test_an_administrator_reading_a_group_they_are_not_in_sees_everything(client, login, people, db, admin):
    _alice, bob, _carol = people
    login(client, bob)
    group = create_group(client, "Team")
    send(client, f"/groups/{group}/send", "bob's plans")
    switch(client, login, admin)
    _block(client, bob)
    assert "bob's plans" in _texts(client, f"/groups/{group}/messages?after=0")
    page = client.get(f"/groups/{group}").get_data(as_text=True)
    assert "Messages from members you blocked are hidden here." not in page


def test_a_blocked_member_cannot_add_the_one_who_blocked_them_to_a_group(client, login, people, db):
    alice, bob, carol = people
    login(client, alice)
    _block(client, bob)
    switch(client, login, bob)
    group = create_group(client, "Bob's group")
    response = client.post(f"/groups/{group}/members/add", data={"username": "alice"}, headers=JSON)
    unknown = client.post(f"/groups/{group}/members/add", data={"username": "nobody_here"}, headers=JSON)
    assert response.status_code == unknown.status_code == 404 and response.json == unknown.json
    assert client.post(f"/groups/{group}/members/add", data={"username": "carol"}, headers=JSON).status_code == 200
    assert db.scalar("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?", (group, alice["id"])) is None
    # The one who blocked can still add the blocked member to her own group.
    switch(client, login, alice)
    own = create_group(client, "Alice's group")
    assert client.post(f"/groups/{own}/members/add", data={"username": "bob"}, headers=JSON).status_code == 200


def test_adding_a_member_who_blocked_you_answers_as_for_any_member(client, login, people, db):
    # Second 1.7 audit (FNU-2 follow-up): the block check ran before the membership check,
    # so "add alice" answered "no active user" although alice visibly posts in the group.
    alice, bob, carol = people
    login(client, bob)
    group = create_group(client, "Bob's group")
    for person in (alice, carol):
        switch(client, login, person)
        _join(client, db, group)
    switch(client, login, alice)
    _block(client, bob)
    send(client, f"/groups/{group}/send", "still here")
    switch(client, login, bob)
    blocker = client.post(f"/groups/{group}/members/add", data={"username": "alice"}, headers=JSON)
    member = client.post(f"/groups/{group}/members/add", data={"username": "carol"}, headers=JSON)
    assert blocker.status_code == member.status_code == 409
    assert blocker.json["error"] == member.json["error"].replace("carol", "alice")


def test_block_and_unblock_buttons_in_the_member_list(client, login, people, db):
    alice, bob, _carol = people
    login(client, bob)
    group = create_group(client, "Team")
    switch(client, login, alice)
    _join(client, db, group)
    page = client.get(f"/groups/{group}").get_data(as_text=True)
    assert 'aria-label="Block bob"' in page and 'aria-label="Block alice"' not in page
    response = client.post("/chats/blocks", data={"user_id": bob["id"], "next": f"/groups/{group}"})
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/groups/{group}")
    page = client.get(f"/groups/{group}").get_data(as_text=True)
    assert 'aria-label="Unblock bob"' in page


# ── Settings, privacy and refusals ───────────────────────────────────────────


def test_blocking_by_name_and_unblocking_from_the_settings(client, login, people, db):
    alice, bob, _carol = people
    login(client, alice)
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="blocked-people"' in page and "You have not blocked anyone." in page
    response = client.post("/chats/blocks", data={"username": "BOB", "next": "/settings#blocked-people"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/settings#blocked-people")
    page = client.get("/settings").get_data(as_text=True)
    assert 'aria-label="Unblock bob"' in page
    client.post("/chats/blocks/remove", data={"user_id": bob["id"]})
    assert db.scalar("SELECT COUNT(*) FROM chat__blocks") == 0


def test_blocking_is_private(admin_client, client, login, people, db, admin):
    alice, bob, _carol = people
    login(client, alice)
    _block(client, bob)
    switch(client, login, bob)
    assert "alice" not in client.get("/settings").get_data(as_text=True).split('id="blocked-people"')[1]
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action LIKE '%block%'") == 0
    switch(client, login, admin)
    exported = zipfile.ZipFile(io.BytesIO(client.get(f"/admin/users/{bob['id']}/export").get_data()))
    assert json.loads(exported.read("data/chat__blocks.json")) == []
    exported = zipfile.ZipFile(io.BytesIO(client.get(f"/admin/users/{alice['id']}/export").get_data()))
    assert [row["blocked_id"] for row in json.loads(exported.read("data/chat__blocks.json"))] == [bob["id"]]


def test_block_refusals(client, login, people, db, monkeypatch, make_user):
    alice, bob, carol = people
    login(client, alice)
    assert _block(client, alice).status_code == 400
    assert client.post("/chats/blocks", data={"username": "nobody_here"}, headers=JSON).status_code == 404
    assert client.post("/chats/blocks", data={}, headers=JSON).status_code == 400
    assert _unblock(client, bob).status_code == 404
    assert _block(client, bob).status_code == 200
    assert _block(client, bob).status_code == 200, "blocking twice is harmless"
    from bananawiki.wiki.features.chat import blocks

    monkeypatch.setattr(blocks, "MAX_BLOCKS", 1)
    response = _block(client, carol)
    assert response.status_code == 400 and "at most 1" in response.json["error"]
    assert db.scalar("SELECT COUNT(*) FROM chat__blocks") == 1
    # Without chat there is nothing to block.
    mute = make_user("mute")
    db.execute("UPDATE users SET chat_disabled = 1 WHERE id = ?", (mute["id"],))
    switch(client, login, mute)
    assert _block(client, bob).status_code == 403
    assert client.get("/chats/blocks").status_code == 405


def test_blocks_go_with_the_accounts(app, db, people, admin_client):
    alice, bob, _carol = people
    db.execute("INSERT INTO chat__blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, '2026-01-01 00:00:00')",
               (alice["id"], bob["id"]))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO chat__blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, "
                   "'2026-01-01 00:00:00')", (alice["id"], alice["id"]))
    # Merging bob into alice must not leave alice blocking herself.
    admin_client.post("/admin/users/merge", data={"source_username": "bob", "target_username": "alice",
                                                  "action": "execute", "delete_source": "1"})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (bob["id"],)) is None
    assert db.scalar("SELECT COUNT(*) FROM chat__blocks") == 0


def test_chat_translations_have_the_same_keys_and_placeholders():
    folder = Path(__file__).resolve().parents[2] / "bananawiki/wiki/features/chat/translations"
    english = json.loads((folder / "en.json").read_text(encoding="utf-8"))
    italian = json.loads((folder / "it.json").read_text(encoding="utf-8"))
    assert set(english) == set(italian)
    for key, text in english.items():
        assert set(re.findall(r"{(\w+)}", text)) == set(re.findall(r"{(\w+)}", italian[key])), key
