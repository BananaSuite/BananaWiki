"""Invite codes: administrators and editors with invite permissions."""

from __future__ import annotations

import re

import pytest

from bananawiki.core.timeutil import sql_in


def generate(client, **data):
    payload = {"max_uses": "1", "expiry_mode": "48h"}
    payload.update(data)
    return client.post("/admin/codes/generate", data=payload)


def grant(db, user, *keys):
    from bananawiki.wiki import permissions as perms

    for key in sorted(perms.defaults(user["role"]) | set(keys)):
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (user["id"], key))
    for access in ("read", "write"):
        db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, 0)",
                   (user["id"], access))


def test_admin_generates_code(admin_client, db):
    response = generate(admin_client, max_uses="3", expiry_mode="7d", assigned_role="admin")
    assert response.status_code == 302
    code = db.one("SELECT * FROM invite_codes")
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code["code"])
    assert code["max_uses"] == 3 and code["assigned_role"] == "admin" and code["expires_at"] > sql_in(days=6)
    assert code["code"].encode() in admin_client.get("/admin/codes").data


@pytest.mark.parametrize("data", [
    {"max_uses": "-1"}, {"max_uses": "abc"}, {"expiry_mode": "custom", "custom_expiry": ""},
    {"expiry_mode": "custom", "custom_expiry": "2001-01-01T00:00"}, {"expiry_mode": "sometime"},
    {"custom_code": "ab1"}, {"custom_code": "bad code!"}, {"assigned_role": "owner"},
    {"assigned_custom_role_id": "999"},
])
def test_invalid_codes_are_refused(admin_client, db, data):
    generate(admin_client, **data)
    assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 0


def test_custom_code_is_unique_and_uppercased(admin_client, db):
    generate(admin_client, custom_code="welcome2026", expiry_mode="never")
    generate(admin_client, custom_code="WELCOME2026")
    rows = db.all("SELECT code, expires_at FROM invite_codes")
    assert rows == [{"code": "WELCOME2026", "expires_at": None}]


def test_deactivate_then_purge(admin_client, db):
    generate(admin_client)
    code_id = db.scalar("SELECT id FROM invite_codes")
    assert admin_client.post(f"/admin/codes/expired/{code_id}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 1  # still active: not purged
    admin_client.post(f"/admin/codes/{code_id}/delete")
    assert db.scalar("SELECT deleted FROM invite_codes") == 1
    assert b"Deactivated" in admin_client.get("/admin/codes/expired").data
    admin_client.post(f"/admin/codes/expired/{code_id}/delete")
    assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 0


def test_used_and_expired_codes_are_listed_as_expired(admin_client, admin, db):
    db.execute("INSERT INTO invite_codes (code, created_by, max_uses, use_count) VALUES ('USED-UP1', ?, 1, 1)",
               (admin["id"],))
    db.execute("INSERT INTO invite_codes (code, created_by, expires_at) VALUES ('OLD-CODE', ?, ?)",
               (admin["id"], sql_in(hours=-1)))
    active = admin_client.get("/admin/codes").data
    expired = admin_client.get("/admin/codes/expired").data
    assert b"USED-UP1" not in active and b"OLD-CODE" not in active
    assert b"USED-UP1" in expired and b"OLD-CODE" in expired


def test_plain_users_and_editors_are_refused(client, make_user, login, db):
    login(client, make_user("editor_one", role="editor"))
    assert client.get("/admin/codes").status_code == 403
    assert generate(client).status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 0


def test_editor_with_invite_permissions(client, admin, make_user, login, db):
    editor = make_user("inviter", role="editor")
    grant(db, editor, "invite.generate", "invite.view", "invite.delete")
    db.execute("INSERT INTO invite_codes (code, created_by, assigned_role) VALUES ('ADMN-CODE', ?, 'admin')",
               (admin["id"],))
    admin_code = db.scalar("SELECT id FROM invite_codes")
    login(client, editor)
    page = client.get("/admin/codes")
    assert page.status_code == 200 and b"ADMN-CODE" not in page.data
    generate(client, assigned_role="admin")
    generate(client, assigned_role="editor")
    own = db.all("SELECT * FROM invite_codes WHERE created_by = ?", (editor["id"],))
    assert [c["assigned_role"] for c in own] == ["editor"]
    assert client.post(f"/admin/codes/{admin_code}/delete").status_code == 404
    assert db.scalar("SELECT deleted FROM invite_codes WHERE id = ?", (admin_code,)) == 0
    client.post(f"/admin/codes/{own[0]['id']}/delete")
    assert db.scalar("SELECT deleted FROM invite_codes WHERE id = ?", (own[0]["id"],)) == 1
    assert client.get("/admin/users").status_code == 403


def test_view_only_editor_cannot_generate(client, make_user, login, db):
    editor = make_user("viewer", role="editor")
    grant(db, editor, "invite.view")
    login(client, editor)
    assert client.get("/admin/codes").status_code == 200
    assert generate(client).status_code == 403


# Delegated invites stay within the inviter's own rights


def _custom_role(db, name, base_role, keys, *, write_ids=None):
    from bananawiki.core.timeutil import now_sql

    role_id = db.insert("custom_roles", {"name": name, "base_role": base_role, "created_at": now_sql(),
                                         "updated_at": now_sql(), "write_restricted": int(write_ids is not None),
                                         "read_restricted": int(write_ids is not None)})
    for key in keys:
        db.execute("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (?, ?)", (role_id, key))
    for category_id in write_ids or ():
        for access in ("read", "write"):
            db.execute("INSERT INTO custom_role_categories (role_id, category_id, access_type) VALUES (?, ?, ?)",
                       (role_id, category_id, access))
    return role_id


def _restricted_inviter(db, make_user, category_id):
    from bananawiki.wiki import permissions as perms

    editor = make_user("limited_inviter", role="editor")
    keys = (perms.defaults("editor") - {"category.delete"}) | {"invite.generate", "invite.view"}
    for key in sorted(keys):
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (editor["id"], key))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (editor["id"],))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', 1)",
               (editor["id"],))
    db.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'write')",
               (editor["id"], category_id))
    return editor


def _category(db, name):
    return db.insert("categories", {"name": name})


def test_delegated_invites_cannot_grant_more_than_the_inviter_holds(client, make_user, login, db):
    mine, other = _category(db, "Mine"), _category(db, "Other")
    editor = _restricted_inviter(db, make_user, mine)
    destructive = _custom_role(db, "Cleaners", "editor", ["page.view_all", "page.delete", "history.delete"])
    unrestricted = _custom_role(db, "Writers", "editor", ["page.view_all", "page.edit_all"])
    wider = _custom_role(db, "Wider", "editor", ["page.view_all", "page.edit_all"], write_ids=[mine, other])
    narrow = _custom_role(db, "Narrow", "editor", ["page.view_all", "page.edit_all"], write_ids=[mine])
    login(client, editor)
    page = client.get("/admin/codes").get_data(as_text=True)
    assert "Narrow" in page and "Cleaners" not in page and "Writers" not in page and "Wider" not in page
    assert 'value="editor"' not in page, "a plain editor writes everywhere, unlike this inviter"
    for role_id in (destructive, unrestricted, wider):
        generate(client, assigned_custom_role_id=str(role_id))
    generate(client, assigned_role="editor")
    assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 0
    generate(client, assigned_custom_role_id=str(narrow))
    generate(client, assigned_role="user")
    assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 2


def test_invites_follow_their_creator_rights_at_sign_up(app, admin, make_user, db):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.auth import invites

    demoted = make_user("former_admin", role="editor")
    db.execute("INSERT INTO invite_codes (code, created_by, assigned_role) VALUES ('ADMN-CODE', ?, 'admin')",
               (demoted["id"],))
    db.execute("INSERT INTO invite_codes (code, created_by, assigned_role) VALUES ('GOOD-CODE', ?, 'admin')",
               (admin["id"],))
    with app.test_request_context(), connection_scope():
        assert invites.assigned_role(invites.find_valid("ADMN-CODE")) == ("user", None)
        assert invites.assigned_role(invites.find_valid("GOOD-CODE")) == ("admin", None)
