"""
Tests for the group chat feature.

Covers:
  - Group creation and listing
  - Joining groups (invite code and direct add)
  - Global chat (auto-created, auto-join)
  - Sending and viewing messages
  - System messages for events
  - Moderation: timeout, untimeout, delete messages
  - Ownership: promote, demote, transfer
  - Access control (members only, role restrictions)
  - Attachment upload and download
  - Admin group monitoring
  - Leaving groups
  - Database helpers (cleanup, backup)
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
# Group list
# ---------------------------------------------------------------------------

def test_group_list_requires_login(client, admin_uid):
    resp = client.get("/groups")
    assert resp.status_code == 302


def test_group_list_empty(alice_client):
    resp = alice_client.get("/groups")
    assert resp.status_code == 200
    assert b"No groups yet" in resp.data


# ---------------------------------------------------------------------------
# Create group
# ---------------------------------------------------------------------------

def test_create_group_page(alice_client):
    resp = alice_client.get("/groups/new")
    assert resp.status_code == 200
    assert b"Create a New Group" in resp.data


def test_create_group(alice_client):
    resp = alice_client.post("/groups/new", data={"name": "Test Group"},
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"Test Group" in resp.data


def test_create_group_empty_name(alice_client):
    resp = alice_client.post("/groups/new", data={"name": ""},
                             follow_redirects=True)
    assert b"Group name is required" in resp.data


def test_create_group_too_long_name(alice_client):
    resp = alice_client.post("/groups/new", data={"name": "x" * 101},
                             follow_redirects=True)
    assert b"Group name too long" in resp.data


def test_create_group_system_message(alice_client, alice_uid):
    import db
    alice_client.post("/groups/new", data={"name": "My Group"})
    groups = db.get_user_groups(alice_uid)
    assert len(groups) == 1
    messages = db.get_group_messages(groups[0]["id"])
    assert any(m["is_system"] and "created the group" in m["content"] for m in messages)


def test_creator_is_owner(alice_client, alice_uid):
    import db
    alice_client.post("/groups/new", data={"name": "Owned Group"})
    groups = db.get_user_groups(alice_uid)
    member = db.get_group_member(groups[0]["id"], alice_uid)
    assert member["role"] == "owner"


# ---------------------------------------------------------------------------
# Join group
# ---------------------------------------------------------------------------

def test_join_group_page(alice_client):
    resp = alice_client.get("/groups/join")
    assert resp.status_code == 200
    assert b"Join a Group" in resp.data


def test_join_group_with_code(alice_client, bob_uid):
    import db
    group = db.create_group_chat("Bob's Group", bob_uid)
    resp = alice_client.post("/groups/join",
                             data={"invite_code": group["invite_code"]},
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"Bob&#39;s Group" in resp.data or b"Bob's Group" in resp.data


def test_join_group_invalid_code(alice_client):
    resp = alice_client.post("/groups/join",
                             data={"invite_code": "BADCODE1"},
                             follow_redirects=True)
    assert b"Invalid invite code" in resp.data


def test_join_group_empty_code(alice_client):
    resp = alice_client.post("/groups/join",
                             data={"invite_code": ""},
                             follow_redirects=True)
    assert b"Please enter an invite code" in resp.data


def test_join_group_already_member(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Alice's Group", alice_uid)
    resp = alice_client.post("/groups/join",
                             data={"invite_code": group["invite_code"]},
                             follow_redirects=True)
    assert b"already a member" in resp.data


def test_join_group_system_message(alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.send_group_system_message(group["id"], "bob joined the group")
    messages = db.get_group_messages(group["id"])
    system_msgs = [m for m in messages if m["is_system"]]
    assert any("bob joined" in m["content"] for m in system_msgs)


# ---------------------------------------------------------------------------
# Global chat
# ---------------------------------------------------------------------------

def test_global_chat_auto_join(alice_client, alice_uid):
    resp = alice_client.get("/groups/global", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Global Chat" in resp.data


def test_global_chat_idempotent(alice_uid, bob_uid):
    import db
    g1 = db.get_or_create_global_chat()
    g2 = db.get_or_create_global_chat()
    assert g1["id"] == g2["id"]


def test_global_chat_shown_in_groups(alice_client, alice_uid):
    alice_client.get("/groups/global", follow_redirects=True)
    resp = alice_client.get("/groups")
    assert b"Global Chat" in resp.data


# ---------------------------------------------------------------------------
# Send messages
# ---------------------------------------------------------------------------

def test_send_group_message(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = alice_client.post(f"/groups/{group['id']}/send",
                             data={"content": "Hello Group!"},
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"Hello Group!" in resp.data


def test_send_empty_group_message(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = alice_client.post(f"/groups/{group['id']}/send",
                             data={"content": ""},
                             follow_redirects=True)
    assert b"Message cannot be empty" in resp.data


def test_send_too_long_group_message(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = alice_client.post(f"/groups/{group['id']}/send",
                             data={"content": "x" * 5001},
                             follow_redirects=True)
    assert b"cannot exceed" in resp.data


def test_non_member_cannot_send(alice_client, bob_uid):
    import db
    group = db.create_group_chat("Bob Only", bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/send",
                             data={"content": "sneaky"},
                             follow_redirects=True)
    assert b"Access denied" in resp.data


def test_timed_out_cannot_send(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", bob_uid)
    db.add_group_member(group["id"], alice_uid)
    db.set_group_member_timeout(group["id"], alice_uid, "9999-12-31T23:59:59+00:00")
    resp = alice_client.post(f"/groups/{group['id']}/send",
                             data={"content": "trying"},
                             follow_redirects=True)
    assert b"timed out" in resp.data


# ---------------------------------------------------------------------------
# View group
# ---------------------------------------------------------------------------

def test_view_group(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = alice_client.get(f"/groups/{group['id']}")
    assert resp.status_code == 200
    assert b"Test" in resp.data


def test_non_member_cannot_view(alice_client, bob_uid):
    import db
    group = db.create_group_chat("Private", bob_uid)
    resp = alice_client.get(f"/groups/{group['id']}", follow_redirects=True)
    assert b"not a member" in resp.data


# ---------------------------------------------------------------------------
# Group shows in list
# ---------------------------------------------------------------------------

def test_group_shows_in_list(alice_client, alice_uid):
    import db
    group = db.create_group_chat("My Group", alice_uid)
    db.send_group_message(group["id"], alice_uid, "Hey team")
    resp = alice_client.get("/groups")
    assert b"My Group" in resp.data


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def test_send_group_message_with_attachment(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    data = {
        "content": "Check this out",
        "attachment": (io.BytesIO(b"file content"), "test.txt"),
    }
    resp = alice_client.post(f"/groups/{group['id']}/send",
                             data=data,
                             content_type="multipart/form-data",
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"test.txt" in resp.data


def test_group_attachment_download(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    data = {
        "content": "Has attachment",
        "attachment": (io.BytesIO(b"hello"), "readme.txt"),
    }
    alice_client.post(f"/groups/{group['id']}/send",
                      data=data, content_type="multipart/form-data")
    messages = db.get_group_messages(group["id"])
    # Find the user message (not system)
    user_msgs = [m for m in messages if not m["is_system"]]
    att = user_msgs[0]["attachments"][0]
    resp = alice_client.get(f"/groups/attachments/{att['id']}/download")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Moderation: Add member
# ---------------------------------------------------------------------------

def test_add_member_to_group(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = alice_client.post(f"/groups/{group['id']}/members/add",
                             data={"username": "bob"},
                             follow_redirects=True)
    assert b"bob" in resp.data
    assert b"has been added" in resp.data


def test_non_mod_cannot_add_member(alice_uid, bob_uid):
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/members/add",
                      data={"username": "admin"},
                      follow_redirects=True)
        assert b"do not have permission" in resp.data


# ---------------------------------------------------------------------------
# Moderation: Delete message
# ---------------------------------------------------------------------------

def test_owner_can_delete_message(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    msg_id = db.send_group_message(group["id"], bob_uid, "delete me")
    resp = alice_client.post(f"/groups/{group['id']}/delete_message",
                             data={"message_id": msg_id},
                             follow_redirects=True)
    assert b"Message deleted" in resp.data
    messages = db.get_group_messages(group["id"])
    user_msgs = [m for m in messages if not m["is_system"]]
    assert len(user_msgs) == 1
    assert user_msgs[0]["is_deleted"] == 1
    assert user_msgs[0]["content"] == "delete me"


def test_member_can_delete_own_group_message(alice_uid, bob_uid):
    import db
    from app import app
    group = db.create_group_chat("SelfDelete", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    msg_id = db.send_group_message(group["id"], bob_uid, "my own message")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/delete_message",
                      data={"message_id": msg_id},
                      follow_redirects=True)
        assert b"Message deleted" in resp.data
    user_msgs = [m for m in db.get_group_messages(group["id"]) if not m["is_system"]]
    assert len(user_msgs) == 1
    assert user_msgs[0]["is_deleted"] == 1
    assert user_msgs[0]["content"] == "my own message"


def test_member_cannot_delete_message(alice_uid, bob_uid):
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    msg_id = db.send_group_message(group["id"], alice_uid, "keep me")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/delete_message",
                      data={"message_id": msg_id},
                      follow_redirects=True)
        assert b"required permissions" in resp.data


def test_deleted_group_message_shows_placeholder_to_members(alice_uid, bob_uid):
    import db
    from app import app
    group = db.create_group_chat("DeletedPlaceholder", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    msg_id = db.send_group_message(group["id"], bob_uid, "remove me from member view")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    alice = app.test_client()
    bob = app.test_client()
    alice.post("/login", data={"username": "alice", "password": "alice123"})
    bob.post("/login", data={"username": "bob", "password": "bob123"})
    bob.post(f"/groups/{group['id']}/delete_message", data={"message_id": msg_id}, follow_redirects=True)
    resp = alice.get(f"/groups/{group['id']}")
    assert resp.status_code == 200
    assert b"This message was deleted." in resp.data
    assert b"remove me from member view" not in resp.data


def test_deleted_group_attachment_hidden_from_members_but_visible_to_admin(alice_uid, bob_uid, admin_uid):
    import db
    from app import app
    group = db.create_group_chat("DeletedAttachment", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    msg_id = db.send_group_message(group["id"], bob_uid, "Attachment message")
    os.makedirs(config.CHAT_ATTACHMENT_FOLDER, exist_ok=True)
    fpath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, "deleted-group-file.txt")
    with open(fpath, "w", encoding="utf-8") as handle:
        handle.write("content")
    att_id = db.add_group_attachment(msg_id, "deleted-group-file.txt", "group-report.txt", 7)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    alice = app.test_client()
    bob = app.test_client()
    admin = app.test_client()
    alice.post("/login", data={"username": "alice", "password": "alice123"})
    bob.post("/login", data={"username": "bob", "password": "bob123"})
    admin.post("/login", data={"username": "admin", "password": "admin123"})
    bob.post(f"/groups/{group['id']}/delete_message", data={"message_id": msg_id}, follow_redirects=True)

    member_view = alice.get(f"/groups/{group['id']}")
    assert b"This message was deleted." in member_view.data
    assert b"group-report.txt" not in member_view.data
    assert alice.get(f"/groups/attachments/{att_id}/download").status_code == 403

    admin_view = admin.get(f"/admin/groups/{group['id']}")
    assert admin_view.status_code == 200
    assert b"Attachment message" in admin_view.data
    assert b"group-report.txt" in admin_view.data
    assert admin.get(f"/groups/attachments/{att_id}/download").status_code == 200


def test_group_messages_partial_for_member(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Live Group", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.send_group_message(group["id"], bob_uid, "Group live hello")
    resp = alice_client.get(f"/groups/{group['id']}/messages")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["message_count"] >= 1
    assert data["latest_message_id"] > 0
    assert data["state_token"]
    assert "Group live hello" in data["html"]


def test_group_messages_partial_state_token_changes_when_attachment_added(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Live Group", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    message_id = db.send_group_message(group["id"], bob_uid, "Attachment incoming")
    before = alice_client.get(f"/groups/{group['id']}/messages").get_json()
    db.add_group_attachment(message_id, "live-group-file.txt", "live-group-file.txt", 4)
    after = alice_client.get(f"/groups/{group['id']}/messages").get_json()
    assert before["message_count"] == after["message_count"]
    assert before["latest_message_id"] == after["latest_message_id"] == message_id
    assert before["state_token"] != after["state_token"]
    assert "live-group-file.txt" not in before["html"]
    assert "live-group-file.txt" in after["html"]


# ---------------------------------------------------------------------------
# Moderation: Timeout
# ---------------------------------------------------------------------------

def test_timeout_member(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/timeout",
                             data={"user_id": bob_uid, "duration": "30"},
                             follow_redirects=True)
    assert b"timed out" in resp.data
    assert db.is_group_member_timed_out(group["id"], bob_uid)


def test_untimeout_member(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.set_group_member_timeout(group["id"], bob_uid, "9999-12-31T23:59:59+00:00")
    resp = alice_client.post(f"/groups/{group['id']}/untimeout",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"timeout has been removed" in resp.data
    assert not db.is_group_member_timed_out(group["id"], bob_uid)


def test_indefinite_timeout(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/timeout",
                             data={"user_id": bob_uid, "duration": "indefinite"},
                             follow_redirects=True)
    assert b"timed out" in resp.data
    assert db.is_group_member_timed_out(group["id"], bob_uid)


def test_cannot_timeout_self(admin_client, admin_uid):
    import db
    group = db.get_or_create_global_chat()
    resp = admin_client.post(f"/groups/{group['id']}/timeout",
                             data={"user_id": admin_uid, "duration": "30"},
                             follow_redirects=True)
    assert b"cannot time yourself out" in resp.data
    assert not db.is_group_member_timed_out(group["id"], admin_uid)


def test_mod_cannot_timeout_owner(alice_uid, bob_uid):
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/timeout",
                      data={"user_id": alice_uid, "duration": "30"},
                      follow_redirects=True)
        assert b"cannot timeout" in resp.data


# ---------------------------------------------------------------------------
# Ownership: Promote / Demote
# ---------------------------------------------------------------------------

def test_promote_member(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/promote",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"now a moderator" in resp.data
    assert db.get_group_member_role(group["id"], bob_uid) == "moderator"


def test_demote_moderator(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    resp = alice_client.post(f"/groups/{group['id']}/demote",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"no longer a moderator" in resp.data
    assert db.get_group_member_role(group["id"], bob_uid) == "member"


def test_non_owner_cannot_promote(alice_uid, bob_uid):
    import db
    from werkzeug.security import generate_password_hash
    from app import app
    charlie_uid = db.create_user("charlie", generate_password_hash("charlie123"), role="user")
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    db.add_group_member(group["id"], charlie_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/promote",
                      data={"user_id": charlie_uid},
                      follow_redirects=True)
        assert b"Only the group owner" in resp.data


# ---------------------------------------------------------------------------
# Self-Downgrade
# ---------------------------------------------------------------------------

def test_moderator_can_self_downgrade(alice_uid, bob_uid):
    """Test that a moderator can voluntarily downgrade themselves to member."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/self-downgrade",
                      follow_redirects=True)
        assert b"You are now a regular member" in resp.data
    assert db.get_group_member_role(group["id"], bob_uid) == "member"


def test_owner_cannot_self_downgrade(alice_client, alice_uid):
    """Test that owners cannot use self-downgrade (must transfer ownership first)."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = alice_client.post(f"/groups/{group['id']}/self-downgrade",
                             follow_redirects=True)
    assert b"Owners cannot self-downgrade" in resp.data
    # Owner role should be unchanged
    assert db.get_group_member_role(group["id"], alice_uid) == "owner"


def test_member_cannot_self_downgrade(alice_uid, bob_uid):
    """Test that regular members cannot self-downgrade (already member)."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)  # Bob joins as member
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/self-downgrade",
                      follow_redirects=True)
        assert b"already a member" in resp.data
    # Member role should be unchanged
    assert db.get_group_member_role(group["id"], bob_uid) == "member"


def test_non_member_cannot_self_downgrade(alice_uid, bob_uid):
    """Test that non-members cannot access self-downgrade endpoint."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    # Bob is not added to the group
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/self-downgrade",
                      follow_redirects=True)
        assert b"not a member" in resp.data
    # Verify Bob is still not a member
    assert db.get_group_member_role(group["id"], bob_uid) is None


# ---------------------------------------------------------------------------
# Ownership: Transfer
# ---------------------------------------------------------------------------

def test_transfer_ownership(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/transfer",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"Ownership transferred" in resp.data
    assert db.get_group_member_role(group["id"], bob_uid) == "owner"
    assert db.get_group_member_role(group["id"], alice_uid) == "moderator"


def test_cannot_transfer_global_chat(admin_client, admin_uid, alice_uid):
    import db
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], admin_uid)
    db.add_group_member(group["id"], alice_uid)
    db.set_group_member_role(group["id"], admin_uid, "owner")
    resp = admin_client.post(f"/groups/{group['id']}/transfer",
                             data={"user_id": alice_uid},
                             follow_redirects=True)
    assert b"Cannot transfer" in resp.data


# ---------------------------------------------------------------------------
# Leave group
# ---------------------------------------------------------------------------

def test_member_can_leave(alice_uid, bob_uid):
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/leave", follow_redirects=True)
        assert b"You left the group" in resp.data
    assert not db.is_group_member(group["id"], bob_uid)


def test_owner_cannot_leave_without_transfer(alice_client, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = alice_client.post(f"/groups/{group['id']}/leave",
                             follow_redirects=True)
    assert b"must transfer ownership" in resp.data


# ---------------------------------------------------------------------------
# Kick member
# ---------------------------------------------------------------------------

def test_owner_can_kick(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/kick",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"has been removed" in resp.data
    assert not db.is_group_member(group["id"], bob_uid)


def test_mod_cannot_kick_owner(alice_uid, bob_uid):
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/kick",
                      data={"user_id": alice_uid},
                      follow_redirects=True)
        assert b"cannot remove" in resp.data


def test_owner_cannot_self_kick(alice_client, alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/kick",
                             data={"user_id": alice_uid},
                             follow_redirects=True)
    assert b"owner cannot be removed" in resp.data
    assert db.is_group_member(group["id"], alice_uid)


def test_admin_cannot_kick_group_owner(admin_client, admin_uid, alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], admin_uid)
    resp = admin_client.post(f"/groups/{group['id']}/kick",
                             data={"user_id": alice_uid},
                             follow_redirects=True)
    assert b"owner cannot be removed" in resp.data
    assert db.is_group_member(group["id"], alice_uid)
    assert db.get_group_member_role(group["id"], alice_uid) == "owner"


# ---------------------------------------------------------------------------
# Admin monitoring
# ---------------------------------------------------------------------------

def test_admin_groups_list(admin_client, alice_uid):
    import db
    db.create_group_chat("AdminTest", alice_uid)
    resp = admin_client.get("/admin/groups")
    assert resp.status_code == 200
    assert b"AdminTest" in resp.data


def test_admin_group_view(admin_client, alice_uid):
    import db
    group = db.create_group_chat("AdminView", alice_uid)
    db.send_group_message(group["id"], alice_uid, "Secret group msg")
    resp = admin_client.get(f"/admin/groups/{group['id']}")
    assert resp.status_code == 200
    assert b"Secret group msg" in resp.data


def test_non_admin_cannot_access_admin_groups(alice_client):
    resp = alice_client.get("/admin/groups", follow_redirects=True)
    assert b"Admin access required" in resp.data


def test_admin_group_monitor_link_in_account(admin_client):
    resp = admin_client.get("/settings")
    assert resp.status_code == 200
    assert b"Group Monitor" in resp.data


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def test_db_create_group_has_invite_code(alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    assert group["invite_code"]
    assert len(group["invite_code"]) == 8


def test_db_get_group_by_invite(alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    found = db.get_group_chat_by_invite(group["invite_code"])
    assert found["id"] == group["id"]


def test_db_group_membership(alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    assert db.is_group_member(group["id"], alice_uid)
    assert not db.is_group_member(group["id"], bob_uid)
    db.add_group_member(group["id"], bob_uid)
    assert db.is_group_member(group["id"], bob_uid)


def test_db_group_message_order(alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.send_group_message(group["id"], alice_uid, "first")
    db.send_group_message(group["id"], bob_uid, "second")
    msgs = db.get_group_messages(group["id"])
    # Filter non-system messages
    user_msgs = [m for m in msgs if not m["is_system"]]
    assert user_msgs[0]["content"] == "first"
    assert user_msgs[1]["content"] == "second"


def test_db_cleanup_group_messages(alice_uid):
    import db
    import sqlite3
    group = db.create_group_chat("Test", alice_uid)
    msg_id = db.send_group_message(group["id"], alice_uid, "to be deleted")
    db.add_group_attachment(msg_id, "stored.txt", "original.txt", 100)
    # Backdate the message so it falls outside the retention window
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        "UPDATE group_messages SET created_at = datetime('now', '-31 days') WHERE id = ?",
        (msg_id,)
    )
    conn.commit()
    conn.close()
    files = db.cleanup_old_group_messages()
    assert "stored.txt" in files
    msgs = db.get_group_messages(group["id"])
    assert len(msgs) == 0


def test_db_group_backup(alice_uid):
    import db
    group = db.create_group_chat("Backup Test", alice_uid)
    db.send_group_message(group["id"], alice_uid, "backup me")
    msgs = db.get_all_group_messages_for_backup()
    assert len(msgs) >= 1
    user_msgs = [m for m in msgs if m["content"] == "backup me"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["group_name"] == "Backup Test"


def test_db_timeout_expired(alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    # Set timeout in the past
    db.set_group_member_timeout(group["id"], bob_uid, "2020-01-01T00:00:00+00:00")
    assert not db.is_group_member_timed_out(group["id"], bob_uid)


def test_db_timeout_no_timeout(alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    assert not db.is_group_member_timed_out(group["id"], alice_uid)


def test_db_delete_group_message(alice_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    msg_id = db.send_group_message(group["id"], alice_uid, "to delete")
    db.add_group_attachment(msg_id, "del.txt", "del.txt", 50)
    db.delete_group_message(msg_id)
    msg = db.get_group_message_by_id(msg_id)
    assert msg is not None
    assert msg["is_deleted"] == 1


def test_db_transfer_ownership(alice_uid, bob_uid):
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.transfer_group_ownership(group["id"], alice_uid, bob_uid)
    assert db.get_group_member_role(group["id"], bob_uid) == "owner"
    assert db.get_group_member_role(group["id"], alice_uid) == "moderator"
    updated = db.get_group_chat(group["id"])
    assert updated["creator_id"] == bob_uid


# ---------------------------------------------------------------------------
# Global chat moderation by site admin
# ---------------------------------------------------------------------------

def test_site_admin_can_delete_global_msg(admin_client, admin_uid, alice_uid):
    import db
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], admin_uid)
    db.add_group_member(group["id"], alice_uid)
    msg_id = db.send_group_message(group["id"], alice_uid, "bad msg")
    resp = admin_client.post(f"/groups/{group['id']}/delete_message",
                             data={"message_id": msg_id},
                             follow_redirects=True)
    assert b"Message deleted" in resp.data


def test_site_admin_can_timeout_in_global(admin_client, admin_uid, alice_uid):
    import db
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], admin_uid)
    db.add_group_member(group["id"], alice_uid)
    resp = admin_client.post(f"/groups/{group['id']}/timeout",
                             data={"user_id": alice_uid, "duration": "5"},
                             follow_redirects=True)
    assert b"timed out" in resp.data


def test_non_admin_cannot_moderate_global(alice_uid, bob_uid):
    import db
    from app import app
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], alice_uid)
    db.add_group_member(group["id"], bob_uid)
    msg_id = db.send_group_message(group["id"], bob_uid, "innocent msg")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.post(f"/groups/{group['id']}/delete_message",
                      data={"message_id": msg_id},
                      follow_redirects=True)
        assert b"Only site admins" in resp.data


# ---------------------------------------------------------------------------
# Edge cases (verification fixes)
# ---------------------------------------------------------------------------

def test_untimeout_non_member_rejected(alice_client, alice_uid, bob_uid):
    """Untimeout should fail if target is not a member of the group."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    # bob is NOT a member
    resp = alice_client.post(f"/groups/{group['id']}/untimeout",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"User is not a member" in resp.data


def test_expired_timeout_icon_not_shown(alice_client, alice_uid, bob_uid):
    """Expired timeout should not show the ⏳ icon in the member list."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    # Set timeout in the past
    db.set_group_member_timeout(group["id"], bob_uid, "2020-01-01T00:00:00+00:00")
    resp = alice_client.get(f"/groups/{group['id']}")
    assert resp.status_code == 200
    # The ⏳ icon should NOT appear for expired timeouts
    assert b"xs-2dfae06c" not in resp.data


def test_active_timeout_icon_shown(alice_client, alice_uid, bob_uid):
    """Active timeout should show the ⏳ icon in the member list."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    # Set timeout far in the future
    db.set_group_member_timeout(group["id"], bob_uid, "9999-12-31T23:59:59+00:00")
    resp = alice_client.get(f"/groups/{group['id']}")
    assert resp.status_code == 200
    assert "⏳".encode() in resp.data


def test_untimeout_system_message_grammar(alice_client, alice_uid, bob_uid):
    """System message for untimeout should use proper grammar."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.set_group_member_timeout(group["id"], bob_uid, "9999-12-31T23:59:59+00:00")
    alice_client.post(f"/groups/{group['id']}/untimeout",
                      data={"user_id": bob_uid},
                      follow_redirects=True)
    messages = db.get_group_messages(group["id"])
    sys_msgs = [m for m in messages if m["is_system"]]
    assert any("timeout was removed" in m["content"] for m in sys_msgs)


# ---------------------------------------------------------------------------
# Ban / Unban
# ---------------------------------------------------------------------------

def test_owner_can_ban_member(alice_client, alice_uid, bob_uid):
    """Owner can permanently ban a member from the group."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/kick",
                             data={"user_id": bob_uid, "permanent": "1"},
                             follow_redirects=True)
    assert b"has been banned" in resp.data
    assert db.is_group_member_banned(group["id"], bob_uid)
    # Banned user should not appear in the regular members list
    members = db.get_group_members(group["id"])
    member_ids = [m["user_id"] for m in members]
    assert bob_uid not in member_ids


def test_banned_user_cannot_rejoin_via_code(alice_uid, bob_uid):
    """A banned user should not be able to rejoin via invite code."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.ban_group_member(group["id"], bob_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post("/groups/join",
                      data={"invite_code": group["invite_code"]},
                      follow_redirects=True)
        assert b"banned" in resp.data


def test_unban_member(alice_client, alice_uid, bob_uid):
    """Owner/mod can revoke a ban, allowing the user to rejoin."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.ban_group_member(group["id"], bob_uid)
    assert db.is_group_member_banned(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/unban",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"ban has been revoked" in resp.data
    assert not db.is_group_member_banned(group["id"], bob_uid)


def test_unban_member_calls_notify_change(alice_client, alice_uid, bob_uid, monkeypatch):
    """Unbanning a member should call notify_change for Telegram sync."""
    import db
    from routes import groups as groups_mod
    calls = []
    monkeypatch.setattr(groups_mod, "notify_change", lambda *a, **kw: calls.append(a))
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.ban_group_member(group["id"], bob_uid)
    alice_client.post(f"/groups/{group['id']}/unban",
                      data={"user_id": bob_uid},
                      follow_redirects=True)
    assert any(c[0] == "group_unban" for c in calls)


def test_unban_non_banned_member(alice_client, alice_uid, bob_uid):
    """Unbanning a non-banned user should show an error."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/unban",
                             data={"user_id": bob_uid},
                             follow_redirects=True)
    assert b"not banned" in resp.data


def test_banned_user_not_in_members_list(alice_client, alice_uid, bob_uid):
    """Banned users should not appear in the regular members list."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.ban_group_member(group["id"], bob_uid)
    members = db.get_group_members(group["id"])
    member_ids = [m["user_id"] for m in members]
    assert bob_uid not in member_ids


def test_banned_members_in_banned_list(alice_client, alice_uid, bob_uid):
    """Banned users should appear in the banned members list."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.ban_group_member(group["id"], bob_uid)
    banned = db.get_group_banned_members(group["id"])
    banned_ids = [m["user_id"] for m in banned]
    assert bob_uid in banned_ids


def test_cannot_add_banned_user(alice_client, alice_uid, bob_uid):
    """Adding a banned user should fail with an appropriate message."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.ban_group_member(group["id"], bob_uid)
    resp = alice_client.post(f"/groups/{group['id']}/members/add",
                             data={"username": "bob"},
                             follow_redirects=True)
    assert b"banned" in resp.data


def test_kick_without_ban_allows_rejoin(alice_uid, bob_uid):
    """Kicked (non-banned) user can rejoin the group via invite code."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.remove_group_member(group["id"], bob_uid)
    assert not db.is_group_member(group["id"], bob_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post("/groups/join",
                      data={"invite_code": group["invite_code"]},
                      follow_redirects=True)
        assert resp.status_code == 200
        assert db.is_group_member(group["id"], bob_uid)


# ---------------------------------------------------------------------------
# Regenerate invite code
# ---------------------------------------------------------------------------

def test_owner_can_regenerate_invite_code(alice_client, alice_uid):
    """Owner should be able to regenerate the invite code."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    old_code = group["invite_code"]
    resp = alice_client.post(f"/groups/{group['id']}/regenerate_code",
                             follow_redirects=True)
    assert b"Invite code regenerated" in resp.data
    updated = db.get_group_chat(group["id"])
    assert updated["invite_code"] != old_code


def test_non_owner_cannot_regenerate_code(alice_uid, bob_uid):
    """Non-owners should not be able to regenerate the invite code."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/regenerate_code",
                      follow_redirects=True)
        assert b"Only the group owner" in resp.data


def test_old_code_invalidated_after_regenerate(alice_client, alice_uid, bob_uid):
    """After regeneration, the old invite code should no longer work."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    old_code = group["invite_code"]
    alice_client.post(f"/groups/{group['id']}/regenerate_code")
    found = db.get_group_chat_by_invite(old_code)
    assert found is None


def test_db_regenerate_invite_code(alice_uid):
    """DB helper should generate a new code and invalidate the old one."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    old_code = group["invite_code"]
    new_code = db.regenerate_group_invite_code(group["id"])
    assert new_code != old_code
    assert len(new_code) == 8
    assert db.get_group_chat_by_invite(old_code) is None
    assert db.get_group_chat_by_invite(new_code) is not None


# ---------------------------------------------------------------------------
# Admin take ownership
# ---------------------------------------------------------------------------

def test_admin_can_take_ownership(admin_client, admin_uid, alice_uid):
    """Admin should be able to take ownership of any group."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = admin_client.post(f"/groups/{group['id']}/admin_takeover",
                             follow_redirects=True)
    assert b"now the owner" in resp.data
    assert db.get_group_member_role(group["id"], admin_uid) == "owner"
    assert db.get_group_member_role(group["id"], alice_uid) == "moderator"


def test_admin_can_view_group_without_joining(admin_client, alice_uid, admin_uid):
    """Admins can open the moderation panel without joining or taking ownership."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    resp = admin_client.get(f"/groups/{group['id']}")
    assert resp.status_code == 200
    assert b"Site admin override active" in resp.data
    assert db.get_group_member_role(group["id"], admin_uid) is None
    assert db.get_group_member_role(group["id"], alice_uid) == "owner"


def test_admin_can_kick_without_takeover(admin_client, admin_uid, alice_uid, bob_uid):
    """Admins can remove members from regular groups without changing ownership."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = admin_client.post(
        f"/groups/{group['id']}/kick",
        data={"user_id": bob_uid},
        follow_redirects=True,
    )
    assert b"can be added back later if needed" in resp.data
    assert not db.is_group_member(group["id"], bob_uid)
    assert db.get_group_member_role(group["id"], admin_uid) is None
    assert db.get_group_member_role(group["id"], alice_uid) == "owner"


def test_admin_can_ban_without_takeover(admin_client, admin_uid, alice_uid, bob_uid):
    """Admins can ban members from regular groups without changing ownership."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = admin_client.post(
        f"/groups/{group['id']}/kick",
        data={"user_id": bob_uid, "permanent": "1"},
        follow_redirects=True,
    )
    assert b"cannot rejoin until the ban is revoked" in resp.data
    assert db.is_group_member_banned(group["id"], bob_uid)
    assert db.get_group_member_role(group["id"], admin_uid) is None
    assert db.get_group_member_role(group["id"], alice_uid) == "owner"


def test_admin_can_promote_without_takeover(admin_client, admin_uid, alice_uid, bob_uid):
    """Site admins can promote group members without being the group owner."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    resp = admin_client.post(
        f"/groups/{group['id']}/promote",
        data={"user_id": bob_uid},
        follow_redirects=True,
    )
    assert b"now a moderator" in resp.data
    assert db.get_group_member_role(group["id"], bob_uid) == "moderator"
    # Admin did not need to become owner
    assert db.get_group_member_role(group["id"], admin_uid) is None
    assert db.get_group_member_role(group["id"], alice_uid) == "owner"


def test_admin_can_demote_without_takeover(admin_client, admin_uid, alice_uid, bob_uid):
    """Site admins can demote group moderators without being the group owner."""
    import db
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    resp = admin_client.post(
        f"/groups/{group['id']}/demote",
        data={"user_id": bob_uid},
        follow_redirects=True,
    )
    assert b"no longer a moderator" in resp.data
    assert db.get_group_member_role(group["id"], bob_uid) == "member"
    # Admin did not need to become owner
    assert db.get_group_member_role(group["id"], admin_uid) is None
    assert db.get_group_member_role(group["id"], alice_uid) == "owner"


def test_admin_can_take_ownership_of_global(admin_client, admin_uid):
    """Admin should be able to take ownership of the global chat."""
    import db
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], admin_uid)
    resp = admin_client.post(f"/groups/{group['id']}/admin_takeover",
                             follow_redirects=True)
    assert b"now the owner" in resp.data
    assert db.get_group_member_role(group["id"], admin_uid) == "owner"


def test_non_admin_cannot_take_ownership(alice_uid, bob_uid):
    """Non-admin users should not be able to take ownership."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/admin_takeover",
                      follow_redirects=True)
        assert b"Admin access required" in resp.data


# ---------------------------------------------------------------------------
# Chat disabled feature
# ---------------------------------------------------------------------------

def test_chat_disabled_user_cannot_create_group(alice_uid):
    """A user with chat disabled should not be able to create a group."""
    import db
    from app import app
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/groups/new", follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


def test_chat_disabled_user_cannot_access_group_list(alice_uid):
    """A user with chat disabled should not be able to access the group list."""
    import db
    from app import app
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/groups", follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


def test_chat_disabled_user_cannot_send_group_message(alice_uid, bob_uid):
    """A user with chat disabled should not be able to send group messages."""
    import db
    from app import app
    group = db.create_group_chat("Test", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    db.set_user_chat_disabled(bob_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/send",
                      data={"content": "test message"},
                      follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


def test_chat_disabled_user_cannot_view_group(alice_uid, bob_uid):
    """A user with chat disabled should not be able to open an existing group."""
    import db
    from app import app
    group = db.create_group_chat("Test", bob_uid)
    db.add_group_member(group["id"], alice_uid)
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/groups/{group['id']}", follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data


def test_chat_disabled_user_cannot_join_global_chat(alice_uid):
    """A user with chat disabled should not be able to auto-join the global chat."""
    import db
    from app import app
    group = db.get_or_create_global_chat()
    db.set_user_chat_disabled(alice_uid, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/groups/global", follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data
    assert not db.is_group_member(group["id"], alice_uid)


def test_banned_user_cannot_rejoin_global_chat(alice_uid):
    """A banned user should not be able to rejoin the global chat through auto-join."""
    import db
    from app import app
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], alice_uid)
    db.ban_group_member(group["id"], alice_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/groups/global", follow_redirects=True)
        assert b"currently banned" in resp.data
    assert db.is_group_member_banned(group["id"], alice_uid)


def test_group_global_route_respects_group_disable_setting(alice_uid):
    """Non-admin users should be blocked from the global chat when groups are disabled."""
    import db
    from app import app
    db.update_site_settings(chat_group_enabled=0)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/groups/global", follow_redirects=True)
        assert b"Group chats are currently disabled" in resp.data


def test_admin_can_toggle_chat(admin_client, admin_uid, alice_uid):
    """Admin should be able to disable and re-enable chat for a user."""
    import db
    assert not db.is_user_chat_disabled(alice_uid)
    # Disable
    admin_client.post(f"/admin/users/{alice_uid}/toggle_chat",
                             follow_redirects=True)
    assert db.is_user_chat_disabled(alice_uid)
    # Re-enable
    admin_client.post(f"/admin/users/{alice_uid}/toggle_chat",
                             follow_redirects=True)
    assert not db.is_user_chat_disabled(alice_uid)


def test_db_chat_disabled_helpers(alice_uid):
    """DB helpers for chat disable should work correctly."""
    import db
    assert not db.is_user_chat_disabled(alice_uid)
    db.set_user_chat_disabled(alice_uid, True)
    assert db.is_user_chat_disabled(alice_uid)
    db.set_user_chat_disabled(alice_uid, False)
    assert not db.is_user_chat_disabled(alice_uid)


# ---------------------------------------------------------------------------
# Global chat: site admin kick in global
# ---------------------------------------------------------------------------

def test_site_admin_can_kick_in_global(admin_client, admin_uid, alice_uid):
    """Site admin should be able to kick users from the global chat."""
    import db
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], admin_uid)
    db.add_group_member(group["id"], alice_uid)
    resp = admin_client.post(f"/groups/{group['id']}/kick",
                             data={"user_id": alice_uid},
                             follow_redirects=True)
    assert b"has been removed" in resp.data


def test_site_admin_can_ban_in_global(admin_client, admin_uid, alice_uid):
    """Site admin should be able to ban users from the global chat."""
    import db
    group = db.get_or_create_global_chat()
    db.add_group_member(group["id"], admin_uid)
    db.add_group_member(group["id"], alice_uid)
    resp = admin_client.post(f"/groups/{group['id']}/kick",
                             data={"user_id": alice_uid, "permanent": "1"},
                             follow_redirects=True)
    assert b"has been banned" in resp.data
    assert db.is_group_member_banned(group["id"], alice_uid)


# ---------------------------------------------------------------------------
# Group deletion (new feature)
# ---------------------------------------------------------------------------

def test_owner_can_delete_group(alice_client, alice_uid):
    """Group owner should be able to permanently delete a non-global group."""
    import db
    group = db.create_group_chat("ToDelete", alice_uid)
    group_id = group["id"]
    resp = alice_client.post(f"/groups/{group_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"has been deleted" in resp.data
    assert db.get_group_chat(group_id) is None


def test_non_owner_cannot_delete_group(alice_uid, bob_uid):
    """Non-owners (regular members) must not be able to delete a group."""
    import db
    from app import app
    group = db.create_group_chat("Protected", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/delete", follow_redirects=True)
        assert b"Only the group owner" in resp.data
    # Group still exists
    assert db.get_group_chat(group["id"]) is not None


def test_cannot_delete_global_chat(alice_client, alice_uid):
    """The global chat must never be deletable via the regular delete route."""
    import db
    global_chat = db.get_or_create_global_chat()
    db.add_group_member(global_chat["id"], alice_uid)
    resp = alice_client.post(f"/groups/{global_chat['id']}/delete",
                             follow_redirects=True)
    assert b"cannot be deleted" in resp.data
    assert db.get_group_chat(global_chat["id"]) is not None


def test_admin_can_delete_group_via_owner_route(admin_client, admin_uid, alice_uid):
    """Site admin should be able to delete any non-global group via the owner route."""
    import db
    group = db.create_group_chat("AdminDelete", alice_uid)
    group_id = group["id"]
    resp = admin_client.post(f"/groups/{group_id}/delete", follow_redirects=True)
    assert b"has been deleted" in resp.data
    assert db.get_group_chat(group_id) is None


def test_admin_can_delete_group_via_admin_panel(admin_client, admin_uid, alice_uid):
    """Admin panel delete route should permanently delete any non-global group."""
    import db
    group = db.create_group_chat("AdminPanelDelete", alice_uid)
    group_id = group["id"]
    resp = admin_client.post(f"/admin/groups/{group_id}/delete", follow_redirects=True)
    assert b"has been deleted" in resp.data
    assert db.get_group_chat(group_id) is None


def test_admin_cannot_delete_global_via_admin_panel(admin_client, admin_uid):
    """Admin panel delete route must refuse to delete the global chat."""
    import db
    global_chat = db.get_or_create_global_chat()
    resp = admin_client.post(f"/admin/groups/{global_chat['id']}/delete",
                             follow_redirects=True)
    assert b"cannot be deleted" in resp.data
    assert db.get_group_chat(global_chat["id"]) is not None


def test_delete_group_removes_messages(alice_client, alice_uid):
    """Deleting a group should cascade-delete all its messages."""
    import db
    group = db.create_group_chat("MsgClean", alice_uid)
    db.send_group_message(group["id"], alice_uid, "hello")
    db.send_group_message(group["id"], alice_uid, "world")
    alice_client.post(f"/groups/{group['id']}/delete", follow_redirects=True)
    assert db.get_group_chat(group["id"]) is None


def test_db_delete_group_chat(alice_uid):
    """DB helper delete_group_chat should remove the group and return attachment filenames."""
    import db
    group = db.create_group_chat("DBDelete", alice_uid)
    group_id = group["id"]
    db.send_group_message(group_id, alice_uid, "msg")
    files = db.delete_group_chat(group_id)
    assert isinstance(files, list)
    assert db.get_group_chat(group_id) is None


# ---------------------------------------------------------------------------
# Global chat deactivation / reactivation (new feature)
# ---------------------------------------------------------------------------

def test_admin_can_deactivate_global_chat(admin_client, admin_uid):
    """Site admin should be able to deactivate the global chat."""
    import db
    global_chat = db.get_or_create_global_chat()
    db.add_group_member(global_chat["id"], admin_uid)
    assert global_chat["is_active"] == 1
    resp = admin_client.post(f"/groups/{global_chat['id']}/toggle_active",
                             follow_redirects=True)
    assert b"deactivated" in resp.data
    updated = db.get_group_chat(global_chat["id"])
    assert updated["is_active"] == 0


def test_admin_can_reactivate_global_chat(admin_client, admin_uid):
    """Site admin should be able to reactivate a deactivated global chat."""
    import db
    global_chat = db.get_or_create_global_chat()
    db.set_group_chat_active(global_chat["id"], False)
    db.add_group_member(global_chat["id"], admin_uid)
    resp = admin_client.post(f"/groups/{global_chat['id']}/toggle_active",
                             follow_redirects=True)
    assert b"reactivated" in resp.data
    updated = db.get_group_chat(global_chat["id"])
    assert updated["is_active"] == 1


def test_non_admin_cannot_toggle_global_active(alice_uid):
    """Non-admins must not be able to deactivate/reactivate the global chat."""
    import db
    from app import app
    global_chat = db.get_or_create_global_chat()
    db.add_group_member(global_chat["id"], alice_uid)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.post(f"/groups/{global_chat['id']}/toggle_active",
                      follow_redirects=True)
        # admin_required decorator should block this
        assert resp.status_code in (200, 302, 403)
    # State unchanged
    assert db.get_group_chat(global_chat["id"])["is_active"] == 1


def test_cannot_send_to_deactivated_global_chat(alice_uid):
    """Members must not be able to send messages to a deactivated global chat."""
    import db
    from app import app
    global_chat = db.get_or_create_global_chat()
    db.add_group_member(global_chat["id"], alice_uid)
    db.set_group_chat_active(global_chat["id"], False)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.post(f"/groups/{global_chat['id']}/send",
                      data={"content": "hello"},
                      follow_redirects=True)
        assert b"deactivated" in resp.data


def test_cannot_toggle_active_on_non_global_group(admin_client, admin_uid, alice_uid):
    """toggle_active route must reject non-global groups."""
    import db
    group = db.create_group_chat("Regular", alice_uid)
    db.add_group_member(group["id"], admin_uid)
    resp = admin_client.post(f"/groups/{group['id']}/toggle_active",
                             follow_redirects=True)
    assert b"Only the global chat" in resp.data


def test_db_set_group_chat_active(alice_uid):
    """DB helper set_group_chat_active should flip is_active correctly."""
    import db
    group = db.create_group_chat("ActiveTest", alice_uid)
    assert db.get_group_chat(group["id"])["is_active"] == 1
    db.set_group_chat_active(group["id"], False)
    assert db.get_group_chat(group["id"])["is_active"] == 0
    db.set_group_chat_active(group["id"], True)
    assert db.get_group_chat(group["id"])["is_active"] == 1


# ---------------------------------------------------------------------------
# Custom join codes (new feature)
# ---------------------------------------------------------------------------

def test_owner_can_set_custom_invite_code(alice_client, alice_uid):
    """Owner should be able to set a custom alphanumeric invite code."""
    import db
    group = db.create_group_chat("CustomCode", alice_uid)
    resp = alice_client.post(f"/groups/{group['id']}/regenerate_code",
                             data={"custom_code": "myteam42"},
                             follow_redirects=True)
    assert b"myteam42" in resp.data
    updated = db.get_group_chat(group["id"])
    assert updated["invite_code"] == "myteam42"


def test_custom_code_invalid_characters_rejected(alice_client, alice_uid):
    """Custom code with non-alphanumeric characters must be rejected."""
    import db
    group = db.create_group_chat("BadCode", alice_uid)
    old_code = group["invite_code"]
    resp = alice_client.post(f"/groups/{group['id']}/regenerate_code",
                             data={"custom_code": "bad-code!"},
                             follow_redirects=True)
    assert b"alphanumeric" in resp.data
    # Code unchanged
    assert db.get_group_chat(group["id"])["invite_code"] == old_code


def test_custom_code_too_long_rejected(alice_client, alice_uid):
    """Custom code longer than 32 characters must be rejected."""
    import db
    group = db.create_group_chat("LongCode", alice_uid)
    old_code = group["invite_code"]
    resp = alice_client.post(f"/groups/{group['id']}/regenerate_code",
                             data={"custom_code": "A" * 33},
                             follow_redirects=True)
    assert b"alphanumeric" in resp.data
    assert db.get_group_chat(group["id"])["invite_code"] == old_code


def test_custom_code_duplicate_rejected(alice_client, alice_uid, bob_uid):
    """Custom code already used by another group must be rejected."""
    import db
    group1 = db.create_group_chat("Group1", alice_uid)
    group2 = db.create_group_chat("Group2", bob_uid)
    # Set group2's code to something known
    db.regenerate_group_invite_code(group2["id"], "takencode")
    old_code = group1["invite_code"]
    resp = alice_client.post(f"/groups/{group1['id']}/regenerate_code",
                             data={"custom_code": "takencode"},
                             follow_redirects=True)
    assert b"already in use" in resp.data
    assert db.get_group_chat(group1["id"])["invite_code"] == old_code


def test_custom_code_integrity_error_handled(alice_client, alice_uid, bob_uid):
    """IntegrityError on concurrent custom code collision returns a flash error, not 500."""
    import db
    from unittest.mock import patch

    group1 = db.create_group_chat("Group1", alice_uid)
    group2 = db.create_group_chat("Group2", bob_uid)
    db.regenerate_group_invite_code(group2["id"], "racecode")
    old_code = group1["invite_code"]

    # Simulate TOCTOU: the uniqueness check passes (mock returns None)
    # but the DB UPDATE hits a UNIQUE constraint (raises IntegrityError).
    with patch("db.get_group_chat_by_invite", return_value=None):
        with patch("db.regenerate_group_invite_code", side_effect=db.IntegrityError):
            resp = alice_client.post(
                f"/groups/{group1['id']}/regenerate_code",
                data={"custom_code": "racecode"},
                follow_redirects=True,
            )
    assert b"already in use" in resp.data
    assert db.get_group_chat(group1["id"])["invite_code"] == old_code


def test_custom_code_same_group_allowed(alice_client, alice_uid):
    """Setting the same custom code on the same group again should be allowed."""
    import db
    group = db.create_group_chat("SameCode", alice_uid)
    db.regenerate_group_invite_code(group["id"], "mycode")
    resp = alice_client.post(f"/groups/{group['id']}/regenerate_code",
                             data={"custom_code": "mycode"},
                             follow_redirects=True)
    assert b"mycode" in resp.data
    assert db.get_group_chat(group["id"])["invite_code"] == "mycode"


def test_blank_custom_code_generates_random(alice_client, alice_uid):
    """Submitting a blank custom_code field should fall back to random generation."""
    import db
    group = db.create_group_chat("RandomFallback", alice_uid)
    old_code = group["invite_code"]
    resp = alice_client.post(f"/groups/{group['id']}/regenerate_code",
                             data={"custom_code": ""},
                             follow_redirects=True)
    assert b"Invite code regenerated" in resp.data
    new_code = db.get_group_chat(group["id"])["invite_code"]
    assert new_code != old_code


def test_custom_code_join_works(alice_uid, bob_uid):
    """A group set with a custom code should be joinable using that code."""
    import db
    from app import app
    group = db.create_group_chat("JoinCustom", alice_uid)
    db.regenerate_group_invite_code(group["id"], "joinme99")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post("/groups/join", data={"invite_code": "joinme99"},
                      follow_redirects=True)
        assert b"joined" in resp.data.lower()
    assert db.is_group_member(group["id"], bob_uid)


def test_db_custom_invite_code(alice_uid):
    """DB helper regenerate_group_invite_code should accept and store a custom code."""
    import db
    group = db.create_group_chat("DBCustom", alice_uid)
    result = db.regenerate_group_invite_code(group["id"], "customXYZ")
    assert result == "customXYZ"
    assert db.get_group_chat(group["id"])["invite_code"] == "customXYZ"
    assert db.get_group_chat_by_invite("customXYZ") is not None


# ---------------------------------------------------------------------------
# Sidebar search API (new feature)
# ---------------------------------------------------------------------------

def test_sidebar_search_requires_login(client, admin_uid):
    """Sidebar search API must require authentication."""
    resp = client.get("/api/sidebar/search?q=test")
    assert resp.status_code == 302  # redirect to login


def test_sidebar_search_empty_query(admin_client, admin_uid):
    """Empty query should return empty results without error."""
    resp = admin_client.get("/api/sidebar/search?q=")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"categories": [], "pages": []}


def test_sidebar_search_finds_pages_by_title(admin_client, admin_uid):
    """Sidebar search should return matching pages by title."""
    import db
    db.create_page("Python Guide", "python-guide", content="intro")
    db.create_page("JavaScript Basics", "js-basics", content="intro")
    resp = admin_client.get("/api/sidebar/search?q=Python")
    assert resp.status_code == 200
    data = resp.get_json()
    titles = [p["title"] for p in data["pages"]]
    assert "Python Guide" in titles
    assert "JavaScript Basics" not in titles


def test_sidebar_search_finds_categories(admin_client, admin_uid):
    """Sidebar search should return matching categories."""
    import db
    db.create_category("Engineering")
    db.create_category("Marketing")
    resp = admin_client.get("/api/sidebar/search?q=Engin")
    assert resp.status_code == 200
    data = resp.get_json()
    cat_names = [c["name"] for c in data["categories"]]
    assert "Engineering" in cat_names
    assert "Marketing" not in cat_names


def test_sidebar_search_content_scope(admin_client, admin_uid):
    """scope=content should also match pages whose body contains the query."""
    import db
    db.create_page("Unrelated Title", "unrelated", content="python scripting")
    db.create_page("Python Title", "python-title", content="something else")
    resp = admin_client.get("/api/sidebar/search?q=python&scope=content")
    assert resp.status_code == 200
    data = resp.get_json()
    slugs = [p["slug"] for p in data["pages"]]
    # Both pages should appear: one by content, one by title
    assert "unrelated" in slugs
    assert "python-title" in slugs


def test_sidebar_search_title_scope_excludes_content_only(admin_client, admin_uid):
    """scope=title (default) must NOT match pages that only have the keyword in content."""
    import db
    db.create_page("Random Title", "random-title", content="secret keyword")
    resp = admin_client.get("/api/sidebar/search?q=secret&scope=title")
    assert resp.status_code == 200
    data = resp.get_json()
    slugs = [p["slug"] for p in data["pages"]]
    assert "random-title" not in slugs


def test_sidebar_search_hides_deindexed_from_regular_user(alice_uid):
    """Regular users should not see deindexed pages in sidebar search results."""
    import db
    from app import app
    page_id = db.create_page("Secret Page", "secret-page", content="hidden")
    db.set_page_deindexed(page_id, True)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get("/api/sidebar/search?q=Secret")
        data = resp.get_json()
        slugs = [p["slug"] for p in data["pages"]]
        assert "secret-page" not in slugs


def test_sidebar_search_shows_deindexed_to_editor_with_permission(admin_uid):
    """Editors with the current deindex permission should see hidden pages in sidebar search."""
    import db
    from app import app
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions
    editor_uid = db.create_user("editor1", generate_password_hash("editor123"),
                                role="editor")
    page_id = db.create_page("Hidden Page", "hidden-page", content="deindexed")
    db.set_page_deindexed(page_id, True)
    db.set_user_permissions(editor_uid, get_default_permissions("editor"), read_restricted=False)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "editor1", "password": "editor123"})
        resp = c.get("/api/sidebar/search?q=Hidden")
        data = resp.get_json()
        slugs = [p["slug"] for p in data["pages"]]
        assert "hidden-page" in slugs


def test_sidebar_search_no_results(admin_client, admin_uid):
    """Searching for a non-existent term should return empty lists."""
    resp = admin_client.get("/api/sidebar/search?q=zzznoresults999")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["pages"] == []
    assert data["categories"] == []


def test_sidebar_search_returns_correct_structure(admin_client, admin_uid):
    """Each page result must have id, title, slug, category_id fields."""
    import db
    db.create_page("Structured Result", "structured-result")
    resp = admin_client.get("/api/sidebar/search?q=Structured")
    data = resp.get_json()
    assert len(data["pages"]) > 0
    page = data["pages"][0]
    assert "id" in page
    assert "title" in page
    assert "slug" in page
    assert "category_id" in page


def test_sidebar_search_category_returns_correct_structure(admin_client, admin_uid):
    """Each category result must have id, name, parent_id fields."""
    import db
    db.create_category("StructuredCat")
    resp = admin_client.get("/api/sidebar/search?q=StructuredCat")
    data = resp.get_json()
    assert len(data["categories"]) > 0
    cat = data["categories"][0]
    assert "id" in cat
    assert "name" in cat
    assert "parent_id" in cat


# ---------------------------------------------------------------------------
# GAP fixes: group clear by moderators, export restrictions, chat_disabled
# on join, unread badges in sidebar, template variable ordering
# ---------------------------------------------------------------------------


def test_moderator_can_clear_group_chat(alice_uid, bob_uid):
    """Moderators should be able to clear group chat messages."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("ModClearTest", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    db.send_group_message(group["id"], alice_uid, "test message")
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/clear", follow_redirects=True)
        assert resp.status_code == 200
        assert b"cleared successfully" in resp.data


def test_regular_member_cannot_clear_group_chat(alice_uid, bob_uid):
    """Regular members should not be able to clear group chat messages."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("MemberClearTest", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post(f"/groups/{group['id']}/clear", follow_redirects=True)
        assert resp.status_code == 200
        assert b"required permissions" in resp.data


def test_site_admin_can_clear_group_chat(admin_client, admin_uid, alice_uid):
    """Site admins should be able to clear any group chat."""
    import db
    group = db.create_group_chat("AdminClearTest", alice_uid)
    db.send_group_message(group["id"], alice_uid, "test message")
    resp = admin_client.post(f"/groups/{group['id']}/clear", follow_redirects=True)
    assert resp.status_code == 200
    assert b"cleared successfully" in resp.data


def test_group_export_blocked_for_moderator(alice_uid, bob_uid):
    """Moderators should not be able to export group chat data."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("ExportTest", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    db.send_group_message(group["id"], alice_uid, "export test msg")
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.get(f"/groups/{group['id']}/export", follow_redirects=True)
        assert b"Only group owners and site admins can export" in resp.data


def test_group_export_allowed_for_owner(alice_uid):
    """Group owners should be able to export group chat data."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("OwnerExportTest", alice_uid)
    db.send_group_message(group["id"], alice_uid, "export msg")
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/groups/{group['id']}/export")
        assert resp.status_code == 200


def test_group_export_allowed_for_site_admin(admin_client, admin_uid, alice_uid):
    """Site admins should be able to export any group chat data."""
    import db
    group = db.create_group_chat("AdminExportTest", alice_uid)
    db.send_group_message(group["id"], alice_uid, "export msg")
    resp = admin_client.get(f"/groups/{group['id']}/export")
    assert resp.status_code == 200


def test_chat_disabled_user_cannot_join_group(alice_uid, bob_uid):
    """Users with chat disabled should not be able to join groups."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("JoinTest", alice_uid)
    invite_code = group["invite_code"]
    db.set_user_chat_disabled(bob_uid, True)
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.post("/groups/join", data={"invite_code": invite_code},
                       follow_redirects=True)
        assert b"chat privileges have been disabled" in resp.data
        assert not db.is_group_member(group["id"], bob_uid)


def test_sidebar_shows_unread_dm_badge(admin_client, admin_uid, alice_uid):
    """Sidebar should display unread DM badge when there are unread messages."""
    import db
    chat = db.get_or_create_chat(admin_uid, alice_uid)
    db.send_chat_message(chat["id"], alice_uid, "hello")
    db.increment_unread_count(chat["id"], admin_uid)
    resp = admin_client.get("/")
    assert b"unread-badge" in resp.data


def test_sidebar_shows_unread_group_badge(admin_client, admin_uid, alice_uid):
    """Sidebar should display unread group badge when there are unread messages."""
    import db
    group = db.create_group_chat("BadgeTest", alice_uid)
    db.add_group_member(group["id"], admin_uid)
    db.send_group_message(group["id"], alice_uid, "hello group")
    db.increment_group_unread_count(group["id"], admin_uid)
    resp = admin_client.get("/")
    assert b"unread-badge" in resp.data


def test_group_chat_buttons_visible_to_owner(alice_uid):
    """Export and Clear Chat buttons should be visible to group owner."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("OwnerButtonsTest", alice_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/groups/{group['id']}")
        assert b"Export" in resp.data
        assert b"Clear Chat" in resp.data


def test_group_chat_clear_button_visible_to_moderator(alice_uid, bob_uid):
    """Clear Chat button should be visible to group moderators."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("ModButtonsTest", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.get(f"/groups/{group['id']}")
        assert b"Clear Chat" in resp.data


def test_group_chat_export_button_hidden_from_moderator(alice_uid, bob_uid):
    """Export button should not be visible to group moderators."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("ModExportHiddenTest", alice_uid)
    db.add_group_member(group["id"], bob_uid, role="moderator")
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.get(f"/groups/{group['id']}")
        assert b"Export</a>" not in resp.data


def test_group_chat_export_button_hidden_from_regular_member(alice_uid, bob_uid):
    """Export button should not be visible to regular group members."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    group = db.create_group_chat("MemberExportHiddenTest", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "bob", "password": "bob123"})
        resp = c.get(f"/groups/{group['id']}")
        assert b"Export</a>" not in resp.data


def test_chat_attachment_limits_from_settings(alice_uid, bob_uid):
    """Chat templates should display attachment limits from site settings."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(
        chat_max_attachment_size_mb=10,
        chat_attachments_per_day_limit=20,
    )
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}")
        assert b"Max 10 MB" in resp.data
        assert b"20 attachments per day" in resp.data


def test_group_chat_attachment_limits_from_settings(alice_uid, bob_uid):
    """Group chat templates should display attachment limits from site settings."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(
        chat_max_attachment_size_mb=8,
        chat_attachments_per_day_limit=15,
    )
    group = db.create_group_chat("AttachLimitsTest", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/groups/{group['id']}")
        assert b"Max 8 MB" in resp.data
        assert b"15 attachments per day" in resp.data


def test_group_attachment_daily_limit_ignores_old_messages(alice_uid, bob_uid):
    """Yesterday's group attachments must not count toward today's daily quota."""
    import db
    from db._connection import get_db

    group = db.create_group_chat("QuotaTest", alice_uid)
    # Create a message dated yesterday and attach a file
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO group_messages (group_id, sender_id, content, created_at) "
            "VALUES (?, ?, 'old', datetime('now', '-1 day'))",
            (group["id"], alice_uid),
        )
        old_msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO group_attachments (message_id, filename, original_name, file_size, created_at) "
            "VALUES (?, 'f.txt', 'f.txt', 100, datetime('now', '-1 day'))",
            (old_msg_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Yesterday's attachment should NOT count
    assert db.get_user_group_attachment_count_today(alice_uid) == 0

    # A new message today should count
    msg_id = db.send_group_message(group["id"], alice_uid, "today")
    db.add_group_attachment(msg_id, "t.txt", "t.txt", 50)
    assert db.get_user_group_attachment_count_today(alice_uid) == 1


def test_chat_cleanup_banner_hidden_when_disabled(alice_uid, bob_uid):
    """Cleanup countdown banner should be hidden when cleanup is disabled."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(
        chat_cleanup_enabled=0,
        chat_dm_auto_clear_messages=0,
        chat_dm_auto_clear_attachments=0,
        chat_auto_clear_messages=0,
    )
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}")
        assert b"dm-cleanup-" not in resp.data


def test_group_chat_cleanup_banner_hidden_when_disabled(alice_uid, bob_uid):
    """Group cleanup countdown banner should be hidden when cleanup is disabled."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(
        chat_cleanup_enabled=0,
        chat_group_auto_clear_messages=0,
        chat_group_auto_clear_attachments=0,
        chat_auto_clear_messages=0,
    )
    group = db.create_group_chat("CleanupBannerTest", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/groups/{group['id']}")
        assert b"group-cleanup-" not in resp.data


def test_group_chat_cleanup_banner_honors_legacy_attachment_disable(alice_uid, bob_uid):
    """Legacy attachment cleanup settings should keep the group cleanup banner hidden when disabled."""
    import db
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(
        chat_cleanup_enabled=1,
        chat_auto_clear_messages=0,
        chat_auto_clear_attachments=0,
        chat_attachment_retention_days=14,
        chat_group_auto_clear_messages=0,
        chat_group_auto_clear_attachments=1,
        chat_group_attachment_retention_days=7,
    )
    group = db.create_group_chat("LegacyCleanupBannerTest", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/groups/{group['id']}")

    assert b"group-cleanup-" not in resp.data


def test_chat_attachment_ui_hidden_when_disabled(alice_uid, bob_uid):
    """Attachment upload UI should be hidden when attachments are disabled."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(chat_attachments_enabled=0)
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}")
        assert b"chat-input-file" not in resp.data


def test_group_chat_attachment_ui_hidden_when_disabled(alice_uid, bob_uid):
    """Group attachment upload UI should be hidden when attachments are disabled."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(chat_attachments_enabled=0)
    group = db.create_group_chat("AttachDisabledTest", alice_uid)
    db.add_group_member(group["id"], bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/groups/{group['id']}")
        assert b"chat-input-file" not in resp.data


def test_chat_message_maxlength_from_settings(alice_uid, bob_uid):
    """Chat textarea maxlength should reflect the configured max message length."""
    import db
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(chat_max_message_length=3000)
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}")
        assert b'maxlength="3000"' in resp.data
