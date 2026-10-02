"""Membership changes must respect ownership acquired by an overlapping transfer."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from flask import g, has_request_context, request

from bananawiki.core.sqlite import Session

from .chat_support import JSON, chat_folder, create_group, send, upload


def pause_before_membership_write(monkeypatch, path):
    """Let a transfer commit after the other request's preliminary reads."""
    ready, resume = Event(), Event()
    transaction = Session.transaction

    @contextmanager
    def delayed_transaction(session):
        if (has_request_context() and request.path == path
                and not g.get("membership_write_paused")):
            g.membership_write_paused = True
            ready.set()
            assert resume.wait(timeout=10), "ownership transfer did not complete"
        with transaction(session) as opened:
            yield opened

    monkeypatch.setattr(Session, "transaction", delayed_transaction)
    return ready, resume


def setup_group(app, make_user, login):
    owner, target = make_user("original_owner"), make_user("next_owner")
    owner_client, target_client = app.test_client(), app.test_client()
    login(owner_client, owner)
    login(target_client, target)
    group = create_group(owner_client)
    assert owner_client.post(f"/groups/{group}/members/add", data={"username": target["username"]},
                             headers=JSON).status_code == 200
    return owner, target, owner_client, target_client, group


def assert_target_is_sole_owner(db, group, target):
    assert db.column("SELECT user_id FROM group_members WHERE group_id = ? AND role = 'owner' AND banned = 0",
                     (group,)) == [target["id"]]
    assert db.scalar("SELECT creator_id FROM group_chats WHERE id = ?", (group,)) == target["id"]


def test_member_cannot_leave_after_becoming_owner(app, make_user, login, db, monkeypatch):
    _owner, target, owner_client, target_client, group = setup_group(app, make_user, login)
    path = f"/groups/{group}/leave"
    ready, resume = pause_before_membership_write(monkeypatch, path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        leave = pool.submit(target_client.post, path, headers=JSON)
        try:
            assert ready.wait(timeout=5)
            assert owner_client.post(f"/groups/{group}/transfer", data={"user_id": target["id"]},
                                     headers=JSON).status_code == 200
        finally:
            resume.set()
        assert leave.result(timeout=5).status_code == 409
    assert_target_is_sole_owner(db, group, target)


@pytest.mark.parametrize("permanent", ["0", "1"])
def test_former_owner_cannot_remove_new_owner_after_transfer(app, make_user, login, db, monkeypatch, permanent):
    _owner, target, owner_client, _target_client, group = setup_group(app, make_user, login)
    # Separate browsers keep the concurrent requests' cookies independent.
    transfer_client = app.test_client()
    login(transfer_client, _owner)
    path = f"/groups/{group}/kick"
    ready, resume = pause_before_membership_write(monkeypatch, path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        kick = pool.submit(owner_client.post, path, data={"user_id": target["id"], "permanent": permanent},
                           headers=JSON)
        try:
            assert ready.wait(timeout=5)
            assert transfer_client.post(f"/groups/{group}/transfer", data={"user_id": target["id"]},
                                        headers=JSON).status_code == 200
        finally:
            resume.set()
        assert kick.result(timeout=5).status_code == 403
    assert_target_is_sole_owner(db, group, target)


def test_unban_cannot_remove_an_admin_who_took_ownership(app, make_user, login, db, monkeypatch):
    _owner, target, owner_client, target_client, group = setup_group(app, make_user, login)
    db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (target["id"],))
    assert owner_client.post(f"/groups/{group}/kick", data={"user_id": target["id"], "permanent": "1"},
                             headers=JSON).status_code == 200
    path = f"/groups/{group}/unban"
    ready, resume = pause_before_membership_write(monkeypatch, path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        unban = pool.submit(owner_client.post, path, data={"user_id": target["id"]}, headers=JSON)
        try:
            assert ready.wait(timeout=5)
            assert target_client.post(f"/groups/{group}/admin_takeover", headers=JSON).status_code == 200
        finally:
            resume.set()
        assert unban.result(timeout=5).status_code == 404
    assert_target_is_sole_owner(db, group, target)


@pytest.mark.parametrize("action", ["delete", "regenerate_code", "clear", "members/add"])
def test_group_mutation_rechecks_revoked_authority(app, make_user, login, db, monkeypatch, action):
    owner, target, owner_client, target_client, group = setup_group(app, make_user, login)
    extra_member = make_user("extra_member")
    assert send(owner_client, f"/groups/{group}/send", "Keep this conversation").status_code == 200
    initial_code = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    initial_messages = db.scalar("SELECT COUNT(*) FROM group_messages WHERE group_id = ?", (group,))
    path = f"/groups/{group}/{action}"
    ready, resume = pause_before_membership_write(monkeypatch, path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        mutation = pool.submit(owner_client.post, path,
                               data={"username": extra_member["username"]}, headers=JSON)
        try:
            assert ready.wait(timeout=5)
            assert owner_client.post(f"/groups/{group}/transfer", data={"user_id": target["id"]},
                                     headers=JSON).status_code == 200
            assert target_client.post(f"/groups/{group}/demote", data={"user_id": owner["id"]},
                                      headers=JSON).status_code == 200
        finally:
            resume.set()
        assert mutation.result(timeout=5).status_code == 403
    assert_target_is_sole_owner(db, group, target)
    assert db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,)) == initial_code
    assert db.scalar("SELECT COUNT(*) FROM group_messages WHERE group_id = ?", (group,)) >= initial_messages
    assert not db.scalar("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
                         (group, extra_member["id"]))


def test_message_deletion_rechecks_revoked_moderation_rights(app, make_user, login, db, monkeypatch):
    owner, target, owner_client, target_client, group = setup_group(app, make_user, login)
    response = send(target_client, f"/groups/{group}/send", "Keep my message and attachment", attachment=upload())
    assert response.status_code == 200
    message_id = response.json["message_id"]
    filename = db.scalar("SELECT filename FROM group_attachments WHERE message_id = ?", (message_id,))
    transfer_client = app.test_client()
    login(transfer_client, owner)
    path = f"/groups/{group}/delete_message"
    ready, resume = pause_before_membership_write(monkeypatch, path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        deletion = pool.submit(owner_client.post, path, data={"message_id": message_id}, headers=JSON)
        try:
            assert ready.wait(timeout=5)
            assert transfer_client.post(f"/groups/{group}/transfer", data={"user_id": target["id"]},
                                        headers=JSON).status_code == 200
            assert target_client.post(f"/groups/{group}/demote", data={"user_id": owner["id"]},
                                      headers=JSON).status_code == 200
        finally:
            resume.set()
        assert deletion.result(timeout=5).status_code == 403
    assert_target_is_sole_owner(db, group, target)
    message = db.one("SELECT content, is_deleted FROM group_messages WHERE id = ?", (message_id,))
    assert message == {"content": "Keep my message and attachment", "is_deleted": 0}
    assert db.scalar("SELECT COUNT(*) FROM group_attachments WHERE message_id = ?", (message_id,)) == 1
    assert (chat_folder(app) / filename).read_bytes() == b"hello file"
