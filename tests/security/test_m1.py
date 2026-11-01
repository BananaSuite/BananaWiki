"""Regression tests for the m1 findings of the full re-audit pass 7 (core logging, impersonation auditing)."""

from __future__ import annotations

import io
import json
import logging

from bananawiki.core import logformat

# MW1-2: a logged value cannot forge further log lines, and JSON logs escape every control character

FORGED = "x\n2026-10-28 01:00:00,000 WARNING [bananawiki] Owner password reset by root\x9b31m\u202e"


def _unhandled(path: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("bananawiki", logging.ERROR, __file__, 1, "Unhandled error on %s %s (request %s)",
                             ("GET", path, "abc"), exc_info)


def test_mw1_2_a_line_feed_in_a_logged_value_does_not_start_a_new_text_line():
    line = logformat.formatter("text").format(_unhandled(FORGED))
    assert "\n" not in line
    assert line.endswith("ERROR [bananawiki] Unhandled error on GET x\\n2026-10-28 01:00:00,000 WARNING [bananawiki] "
                         "Owner password reset by root\\x9b31m\\u202e (request abc)")


def test_mw1_2_a_traceback_keeps_its_lines_after_the_one_line_message():
    try:
        raise ValueError("bad")
    except ValueError as error:
        text = logformat.formatter("text").format(_unhandled(FORGED, (type(error), error, error.__traceback__)))
    first, *rest = text.splitlines()
    assert "Owner password reset by root" in first and rest[0] == "Traceback (most recent call last):"


def test_mw1_2_json_lines_escape_c1_and_bidi_controls_and_stay_valid():
    line = logformat.formatter("json").format(_unhandled(FORGED))
    assert all(not (0x7F <= ord(c) < 0xA0 or c in "\u2028\u2029\u202e") for c in line), line
    assert "\\u009b31m\\u202e" in line
    entry = json.loads(line)
    assert entry["message"] == f"Unhandled error on GET {FORGED} (request abc)"
    assert "Ünïcödé 🍌" in logformat.formatter("json").format(_unhandled("Ünïcödé 🍌")), "other text stays as it is"


def test_mw1_2_the_wiki_log_holds_one_line_for_a_500_on_a_forged_path(app, client, make_user, login, monkeypatch):
    login(client, make_user("reader"))

    def broken(**_values):
        raise RuntimeError("boom")

    monkeypatch.setitem(app.view_functions, "pages.view", broken)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logformat.formatter("text"))
    logger = logging.getLogger("bananawiki")
    logger.addHandler(handler)
    try:
        response = client.get("/page/x%0A2026-10-28%2001:00:00,000%20WARNING%20[bananawiki]%20Forged%C2%9B31m")
    finally:
        logger.removeHandler(handler)
    assert response.status_code == 500
    lines = [line for line in stream.getvalue().splitlines() if "Unhandled error" in line or "Forged" in line]
    assert len(lines) == 1 and "Unhandled error on GET /page/x\\n2026-10-28" in lines[0], lines
    assert not any(line.startswith("2026-10-28 01:00:00,000") for line in stream.getvalue().splitlines())


# MW3-1: an administrator impersonating a member is audited reading, exporting and writing in the member's name

JSON = {"Accept": "application/json"}


def _audit(db, since: int = 0) -> list[dict]:
    return db.all("SELECT * FROM audit_log WHERE id > ? ORDER BY id", (since,))


def _last_id(db) -> int:
    return db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")


def _impersonating(app, make_user, login, member):
    admin = make_user("plainadmin", role="admin")
    client = app.test_client()
    login(client, admin)
    assert client.post(f"/admin/users/{member['id']}/impersonate").status_code == 302
    return admin, client


def _dm(app, login, sender, recipient, text: str) -> int:
    client = app.test_client()
    login(client, sender)
    chat_id = client.post("/chats/new", data={"username": recipient["username"]}, headers=JSON).json["chat_id"]
    assert client.post(f"/chats/{chat_id}/send", data={"content": text}, headers=JSON).status_code == 200
    return chat_id


def test_mw3_1_an_impersonated_data_export_is_audited(app, make_user, login, db):
    bob = make_user("bob")
    admin, client = _impersonating(app, make_user, login, bob)
    since = _last_id(db)
    response = client.get("/settings/export")
    assert response.status_code == 200
    response.get_data()
    entries = [(row["action"], row["actor_id"], row["target_id"]) for row in _audit(db, since)]
    assert entries == [("user.data_exported", admin["id"], bob["id"])]


def test_mw3_1_a_member_exporting_their_own_data_is_not_audited(app, client, make_user, login, db):
    bob = make_user("bob")
    login(client, bob)
    since = _last_id(db)
    assert client.get("/settings/export").status_code == 200
    assert _audit(db, since) == []


def test_mw3_1_reading_a_dm_while_impersonating_is_audited(app, make_user, login, db):
    alice, bob = make_user("alice"), make_user("bob")
    chat_id = _dm(app, login, alice, bob, "secret evidence")
    admin, client = _impersonating(app, make_user, login, bob)
    since = _last_id(db)
    assert b"secret evidence" in client.get(f"/chats/{chat_id}").data
    for _ in range(2):  # an open page polling: one entry per window, which opening the page started
        assert b"secret evidence" in client.get(f"/chats/{chat_id}/messages?after=0", headers=JSON).data
    assert b"secret evidence" in client.get(f"/chats/{chat_id}/export").data
    entries = _audit(db, since)
    assert [(row["action"], row["actor_id"], row["target_id"]) for row in entries] == [
        ("chat.dm_read", admin["id"], str(chat_id)), ("chat.dm_exported", admin["id"], str(chat_id))]
    assert all(json.loads(row["details"])["as_user"] == "bob" for row in entries)


def test_mw3_1_a_dm_sent_in_the_members_name_is_audited_with_the_message(app, make_user, login, db):
    bob = make_user("bob")
    make_user("carol")
    admin, client = _impersonating(app, make_user, login, bob)
    since = _last_id(db)
    chat_id = client.post("/chats/new", data={"username": "carol"}, headers=JSON).json["chat_id"]
    message_id = client.post(f"/chats/{chat_id}/send", data={"content": "I resign. -- bob"},
                             headers=JSON).json["message_id"]
    assert client.post(f"/chats/{chat_id}/read", headers=JSON).status_code == 200
    entries = _audit(db, since)
    assert [(row["action"], row["actor_id"]) for row in entries] == [("chat.impersonated_action", admin["id"])] * 2
    sent = json.loads(entries[1]["details"])
    assert (entries[1]["target_type"], entries[1]["target_id"]) == ("chat", str(chat_id))
    assert sent == {"as_user": "bob", "endpoint": "chat.dm_send", "status": 200, "message_id": message_id}
    assert json.loads(entries[0]["details"])["endpoint"] == "chat.dm_new"
    assert db.scalar("SELECT sender_id FROM chat_messages WHERE id = ?", (message_id,)) == bob["id"]


def test_mw3_1_members_reading_and_sending_themselves_are_not_audited(app, make_user, login, db):
    alice, bob = make_user("alice"), make_user("bob")
    since = _last_id(db)
    chat_id = _dm(app, login, alice, bob, "hello")
    client = app.test_client()
    login(client, bob)
    after_logins = _last_id(db)
    assert client.get(f"/chats/{chat_id}").status_code == 200
    assert client.post(f"/chats/{chat_id}/send", data={"content": "hi"}, headers=JSON).status_code == 200
    assert {row["action"] for row in _audit(db, since)} <= {"user.login"}
    assert _audit(db, after_logins) == []


def test_mw3_1_a_private_group_read_and_written_while_impersonating_is_audited(app, make_user, login, db):
    bob = make_user("bob")
    owner = app.test_client()
    login(owner, bob)
    group_id = owner.post("/groups/new", data={"name": "Union"}, headers=JSON).json["group_id"]
    assert owner.post(f"/groups/{group_id}/send", data={"content": "secret plan"}, headers=JSON).status_code == 200
    admin, client = _impersonating(app, make_user, login, bob)
    since = _last_id(db)
    assert b"secret plan" in client.get(f"/groups/{group_id}").data
    assert b"secret plan" in client.get(f"/groups/{group_id}/messages", headers=JSON).data
    assert b"secret plan" in client.get(f"/groups/{group_id}/export").data
    message_id = client.post(f"/groups/{group_id}/send", data={"content": "Strike is off."}, headers=JSON).json["message_id"]
    entries = _audit(db, since)
    assert [(row["action"], row["actor_id"], row["target_id"]) for row in entries] == [
        ("chat.group_read", admin["id"], str(group_id)), ("chat.group_exported", admin["id"], str(group_id)),
        ("chat.impersonated_action", admin["id"], str(group_id))]
    assert json.loads(entries[-1]["details"])["message_id"] == message_id


def test_mw3_1_the_conversation_lists_show_no_previews_while_impersonating(app, make_user, login, db):
    alice, bob = make_user("alice"), make_user("bob")
    _dm(app, login, alice, bob, "secret evidence")
    owner = app.test_client()
    login(owner, bob)
    group_id = owner.post("/groups/new", data={"name": "Union"}, headers=JSON).json["group_id"]
    assert owner.post(f"/groups/{group_id}/send", data={"content": "secret plan"}, headers=JSON).status_code == 200
    assert owner.post("/groups/global").status_code == 302
    global_id = db.scalar("SELECT id FROM group_chats WHERE is_global = 1")
    assert owner.post(f"/groups/{global_id}/send", data={"content": "hello all"}, headers=JSON).status_code == 200
    assert b"secret evidence" in owner.get("/chats").data and b"secret plan" in owner.get("/groups").data
    admin, client = _impersonating(app, make_user, login, bob)
    since = _last_id(db)
    chats, group_list = client.get("/chats"), client.get("/groups")
    assert chats.status_code == group_list.status_code == 200
    assert b"secret evidence" not in chats.data and b"alice" in chats.data
    assert b"secret plan" not in group_list.data and b"Union" in group_list.data
    assert b"hello all" in group_list.data, "the global room is open to all"
    assert chats.data.count(b"Open the conversation to read it") == group_list.data.count(b"Open the conversation") == 1
    assert _audit(db, since) == [], "nothing was read, so nothing is recorded"


def test_mw3_1_attachments_downloaded_while_impersonating_are_audited(app, make_user, login, db):
    alice, bob = make_user("alice"), make_user("bob")
    sender = app.test_client()
    login(sender, alice)
    chat_id = sender.post("/chats/new", data={"username": "bob"}, headers=JSON).json["chat_id"]
    assert sender.post(f"/chats/{chat_id}/send", data={"content": "see file", "attachment": (io.BytesIO(b"dm"), "a.txt")},
                       headers=JSON, content_type="multipart/form-data").status_code == 200
    login(sender, bob)
    group_id = sender.post("/groups/new", data={"name": "Union"}, headers=JSON).json["group_id"]
    assert sender.post(f"/groups/{group_id}/send", data={"content": "file", "attachment": (io.BytesIO(b"gr"), "g.txt")},
                       headers=JSON, content_type="multipart/form-data").status_code == 200
    dm_file = db.scalar("SELECT id FROM chat_attachments")
    group_file = db.scalar("SELECT id FROM group_attachments")
    admin, client = _impersonating(app, make_user, login, bob)
    since = _last_id(db)
    assert client.get(f"/chats/attachments/{dm_file}/download").data == b"dm"
    assert client.get(f"/groups/attachments/{group_file}/download").data == b"gr"
    entries = _audit(db, since)
    assert [(row["action"], row["actor_id"], row["target_id"]) for row in entries] == [
        ("chat.dm_attachment_read", admin["id"], str(chat_id)),
        ("chat.group_attachment_read", admin["id"], str(group_id))]
    assert [json.loads(row["details"]) for row in entries] == [
        {"as_user": "bob", "attachment_id": dm_file}, {"as_user": "bob", "attachment_id": group_file}]
