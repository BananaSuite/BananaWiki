"""Account administration: access control, hierarchy rules and account actions."""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.core.timeutil import sql_in

NEW_PASSWORD = "another secret phrase"


def edit(client, user, **data):
    return client.post(f"/admin/users/{user['id']}/edit", data=data)


def fetch(db, user):
    return db.one("SELECT * FROM users WHERE id = ?", (user["id"],))


@pytest.fixture
def owner(make_user):
    return make_user("the_owner", role="owner", is_superuser=1)


# ── Access ───────────────────────────────────────────────────────────────────

ADMIN_URLS = ["/admin/dashboard", "/admin/users", "/admin/users/create", "/admin/roles", "/admin/roles/create",
              "/admin/sessions"]


@pytest.mark.parametrize("url", ADMIN_URLS)
@pytest.mark.parametrize("role", ["user", "editor"])
def test_non_admins_are_refused(client, make_user, login, url, role):
    login(client, make_user("someone", role=role))
    assert client.get(url).status_code == 403


def test_non_admins_cannot_post_account_actions(client, make_user, login, db):
    victim = make_user("victim")
    login(client, make_user("sneaky", role="editor"))
    assert edit(client, victim, action="suspend").status_code == 403
    assert client.post(f"/admin/users/{victim['id']}/impersonate").status_code == 403
    assert client.post("/admin/mass-logout").status_code == 403
    assert client.post(f"/admin/users/{victim['id']}/assign-role", data={"identity": "std:admin"}).status_code == 403
    assert fetch(db, victim)["suspended"] == 0


def test_anonymous_visitors_are_sent_to_sign_in(client):
    assert "/login" in client.get("/admin/users").headers["Location"]
    response = client.get("/admin")
    assert "/login" in response.headers["Location"] and "dashboard" in response.headers["Location"]


def test_admin_entry_redirects_admins_to_dashboard(admin_client):
    assert admin_client.get("/admin").headers["Location"].endswith("/admin/dashboard")


def test_unknown_user_is_404(admin_client):
    assert admin_client.get("/admin/users/nobody00").status_code == 404
    assert admin_client.post("/admin/users/nobody00/edit", data={"action": "suspend"}).status_code == 404


def test_unknown_action_is_400(admin_client, make_user):
    assert edit(admin_client, make_user("bob"), action="explode").status_code == 400


def test_csrf_is_required(app, admin_client, make_user, db):
    bob = make_user("bob")
    app.config["CSRF_DISABLED"] = False
    assert edit(admin_client, bob, action="suspend").status_code == 400
    assert fetch(db, bob)["suspended"] == 0


# ── List and create ──────────────────────────────────────────────────────────


def test_list_search_filter_and_pagination(admin_client, make_user):
    for i in range(55):
        make_user(f"member{i:02d}")
    make_user("zed_editor", role="editor")
    page_one = admin_client.get("/admin/users").data
    assert b"member00" in page_one and b"Page 1 of 2" in page_one
    assert b"zed_editor" in admin_client.get("/admin/users?page=2").data
    only_editors = admin_client.get("/admin/users?role=editor").data
    assert b"zed_editor" in only_editors and b"member00" not in only_editors
    search = admin_client.get("/admin/users?q=member05").data
    assert b"member05" in search and b"member06" not in search
    assert admin_client.get("/admin/users?q=%25_").status_code == 200


def test_create_user(admin_client, db):
    response = admin_client.post("/admin/users/create", data={
        "username": "newbie", "password": PASSWORD, "confirm_password": PASSWORD, "role": "editor",
        "force_password_change": "1"})
    assert response.status_code == 302
    row = db.one("SELECT * FROM users WHERE username = 'newbie'")
    assert row["role"] == "editor" and row["force_password_change"] == 1


@pytest.mark.parametrize("data", [
    {"username": "x", "password": PASSWORD, "confirm_password": PASSWORD, "role": "user"},
    {"username": "valid_name", "password": "short", "confirm_password": "short", "role": "user"},
    {"username": "valid_name", "password": PASSWORD, "confirm_password": "different!!", "role": "user"},
    {"username": "valid_name", "password": PASSWORD, "confirm_password": PASSWORD, "role": "owner"},
])
def test_create_user_validation(admin_client, db, data):
    assert admin_client.post("/admin/users/create", data=data).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'valid_name'") == 0


# ── Hierarchy (1.4 audit M4) ─────────────────────────────────────────────────


def test_admin_cannot_reset_another_admins_password(admin_client, make_user, db):
    peer = make_user("peer_admin", role="admin")
    before = fetch(db, peer)["password"]
    edit(admin_client, peer, action="change_password", password=NEW_PASSWORD, confirm_password=NEW_PASSWORD)
    assert fetch(db, peer)["password"] == before


@pytest.mark.parametrize("data", [
    {"action": "change_role", "role": "user"},
    {"action": "suspend"},
    {"action": "delete"},
    {"action": "change_username", "username": "renamed"},
])
def test_admin_cannot_change_another_admin(admin_client, make_user, db, data):
    peer = make_user("peer_admin", role="admin")
    edit(admin_client, peer, **data)
    row = fetch(db, peer)
    assert row is not None and row["role"] == "admin" and row["suspended"] == 0 and row["username"] == "peer_admin"


def test_owner_can_manage_admins(client, owner, login, make_user, db):
    login(client, owner)
    peer = make_user("peer_admin", role="admin")
    edit(client, peer, action="change_password", password=NEW_PASSWORD, confirm_password=NEW_PASSWORD)
    edit(client, peer, action="change_role", role="editor")
    assert fetch(db, peer)["role"] == "editor"


def test_owners_are_only_edited_by_themselves(admin_client, owner, db):
    for data in ({"action": "change_role", "role": "user"}, {"action": "suspend"}, {"action": "delete"},
                 {"action": "change_password", "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}):
        edit(admin_client, owner, **data)
    row = fetch(db, owner)
    assert row["role"] == "owner" and row["suspended"] == 0 and row["password"] == owner["password"]


def test_superusers_are_protected_from_others(client, make_user, login, db):
    first = make_user("first_owner", role="owner")
    protected = make_user("protected_one", role="user", is_superuser=1)
    login(client, first)
    edit(client, protected, action="suspend")
    edit(client, protected, action="change_role", role="editor")
    row = fetch(db, protected)
    assert row["suspended"] == 0 and row["role"] == "user"


def test_only_superusers_toggle_superuser(admin_client, client, owner, login, make_user, db):
    bob = make_user("bob")
    edit(admin_client, bob, action="toggle_superuser")
    assert fetch(db, bob)["is_superuser"] == 0
    client.post("/logout")
    login(client, owner)
    edit(client, bob, action="toggle_superuser")
    assert fetch(db, bob)["is_superuser"] == 1
    edit(client, owner, action="toggle_superuser")
    assert fetch(db, owner)["is_superuser"] == 1


def test_admin_cannot_grant_owner(admin_client, make_user, db):
    bob = make_user("bob")
    edit(admin_client, bob, action="change_role", role="owner")
    assert fetch(db, bob)["role"] == "user"


def test_cannot_change_own_role(admin_client, admin, db):
    edit(admin_client, admin, action="change_role", role="user")
    assert fetch(db, admin)["role"] == "admin"


def test_last_owner_cannot_step_down(client, owner, login, make_user, db):
    login(client, owner)
    edit(client, owner, action="change_role", role="admin")
    assert fetch(db, owner)["role"] == "owner"
    make_user("second_owner", role="owner")
    edit(client, owner, action="change_role", role="admin")
    assert fetch(db, owner)["role"] == "admin"


def test_role_change_records_history_and_resets_overrides(admin_client, admin, make_user, db):
    bob = make_user("bob", role="editor")
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', 1)",
               (bob["id"],))
    admin_client.post(f"/admin/users/{bob['id']}/assign-role", data={"identity": "std:user"})
    assert fetch(db, bob)["role"] == "user"
    history = db.one("SELECT * FROM role_history WHERE user_id = ?", (bob["id"],))
    assert history["old_role"] == "editor" and history["changed_by"] == admin["id"]
    assert db.scalar("SELECT COUNT(*) FROM user_category_access WHERE user_id = ?", (bob["id"],)) == 0


# ── Suspension (1.4 audit H2) ────────────────────────────────────────────────


def test_suspension_revokes_sessions_and_is_audited(app, admin_client, make_user, login, db):
    bob = make_user("bob")
    bob_client = app.test_client()
    login(bob_client, bob)
    assert bob_client.get("/_probe/private").data == b"private"
    edit(admin_client, bob, action="suspend", suspend_duration="24", suspend_reason="spam",
         suspend_reason_visible="1", suspend_time_visible="1")
    row = fetch(db, bob)
    assert row["suspended"] == 1 and row["suspended_until"] and row["suspend_reason_visible"] == 1
    assert db.scalar("SELECT COUNT(*) FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL",
                     (bob["id"],)) == 0
    assert "/login" in bob_client.get("/_probe/private").headers["Location"]
    audit = db.one("SELECT * FROM suspension_audit WHERE user_id = ?", (bob["id"],))
    assert audit["action"] == "suspend" and audit["reason"] == "spam"
    edit(admin_client, bob, action="unsuspend")
    row = fetch(db, bob)
    assert row["suspended"] == 0 and row["suspend_reason"] is None
    assert db.scalar("SELECT COUNT(*) FROM suspension_audit WHERE user_id = ?", (bob["id"],)) == 2


def test_suspension_visibility_needs_reason_and_end(admin_client, make_user, db):
    bob = make_user("bob")
    edit(admin_client, bob, action="suspend", suspend_duration="permanent", suspend_reason_visible="1",
         suspend_time_visible="1")
    row = fetch(db, bob)
    assert row["suspended"] == 1 and row["suspended_until"] is None
    assert row["suspend_reason_visible"] == 0 and row["suspend_time_visible"] == 0


@pytest.mark.parametrize("data", [
    {"suspend_duration": "custom_datetime", "suspend_custom_datetime": ""},
    {"suspend_duration": "custom_datetime", "suspend_custom_datetime": "2001-01-01T10:00"},
    {"suspend_duration": "custom_datetime", "suspend_custom_datetime": "not a date"},
    {"suspend_duration": "custom_relative", "suspend_rel_hours": "0", "suspend_rel_minutes": "0"},
    {"suspend_duration": "custom_relative", "suspend_rel_hours": "x"},
    {"suspend_duration": "banana"},
])
def test_invalid_suspension_is_refused(admin_client, make_user, db, data):
    bob = make_user("bob")
    edit(admin_client, bob, action="suspend", **data)
    assert fetch(db, bob)["suspended"] == 0


def test_suspension_custom_relative_and_reason_truncated(admin_client, make_user, db):
    bob = make_user("bob")
    edit(admin_client, bob, action="suspend", suspend_duration="custom_relative", suspend_rel_hours="2",
         suspend_rel_minutes="30", suspend_reason="x" * 800)
    row = fetch(db, bob)
    assert row["suspended"] == 1 and len(row["suspend_reason"]) == 500


def test_cannot_suspend_self(admin_client, admin, db):
    edit(admin_client, admin, action="suspend")
    assert fetch(db, admin)["suspended"] == 0


# ── Passwords ────────────────────────────────────────────────────────────────


def test_password_reset_forces_change_and_signs_out(app, admin_client, make_user, login, db):
    bob = make_user("bob")
    bob_client = app.test_client()
    login(bob_client, bob)
    edit(admin_client, bob, action="change_password", password=NEW_PASSWORD, confirm_password=NEW_PASSWORD,
         force_password_change="1")
    assert fetch(db, bob)["force_password_change"] == 1
    assert "/login" in bob_client.get("/_probe/private").headers["Location"]
    login(bob_client, bob, password=NEW_PASSWORD)


def test_temporary_password_and_restore(app, admin_client, make_user, login, db):
    bob = make_user("bob")
    original = fetch(db, bob)["password"]
    edit(admin_client, bob, action="set_temp_password", password=NEW_PASSWORD, confirm_password=NEW_PASSWORD)
    assert fetch(db, bob)["original_password_backup"] == original
    edit(admin_client, bob, action="revert_password")
    row = fetch(db, bob)
    assert row["password"] == original and row["original_password_backup"] is None
    login(app.test_client(), bob)


def test_password_mismatch_is_refused(admin_client, make_user, db):
    bob = make_user("bob")
    edit(admin_client, bob, action="change_password", password=NEW_PASSWORD, confirm_password="nope nope nope")
    assert fetch(db, bob)["password"] == bob["password"]


# ── Approval ─────────────────────────────────────────────────────────────────


def test_approve_and_deny(admin_client, admin, make_user, db):
    alice = make_user("alice", approval_status="pending")
    carol = make_user("carol", approval_status="pending")
    edit(admin_client, alice, action="approve_user")
    edit(admin_client, carol, action="deny_user")
    a, c = fetch(db, alice), fetch(db, carol)
    assert a["approval_status"] == "approved" and a["approved_by"] == admin["id"]
    assert c["approval_status"] == "denied" and c["denied_by"] == admin["id"] and c["denied_at"]
    edit(admin_client, alice, action="deny_user")
    assert fetch(db, alice)["approval_status"] == "approved"


def test_signup_cleanup_job(app, make_user, db):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.admin import jobs

    old_denied = make_user("old_denied", approval_status="denied")
    new_denied = make_user("new_denied", approval_status="denied")
    old_pending = make_user("old_pending", approval_status="pending")
    db.execute("UPDATE users SET denied_at = ? WHERE id = ?", (sql_in(hours=-30), old_denied["id"]))
    db.execute("UPDATE users SET denied_at = ? WHERE id = ?", (sql_in(hours=-1), new_denied["id"]))
    db.execute("UPDATE users SET created_at = ? WHERE id = ?", (sql_in(hours=-100), old_pending["id"]))
    with app.app_context(), connection_scope():
        jobs.account_cleanup()
    assert fetch(db, old_denied) is None and fetch(db, new_denied) is not None
    assert fetch(db, old_pending) is not None  # pending timeout 0 = never
    db.execute("UPDATE site_settings SET approval_pending_timeout_hours = 48 WHERE id = 1")
    with app.app_context(), connection_scope():
        jobs.account_cleanup()
    assert fetch(db, old_pending) is None


def test_expired_suspensions_are_lifted(app, make_user, db):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.admin import jobs

    bob = make_user("bob", suspended=1, suspended_until=sql_in(hours=-1), suspend_reason="old")
    with app.app_context(), connection_scope():
        jobs.account_cleanup()
    row = fetch(db, bob)
    assert row["suspended"] == 0 and row["suspend_reason"] is None


# ── Other actions ────────────────────────────────────────────────────────────


def test_rename_and_toggle_chat(admin_client, make_user, db):
    bob = make_user("bob")
    edit(admin_client, bob, action="change_username", username="robert")
    assert fetch(db, bob)["username"] == "robert"
    assert db.scalar("SELECT COUNT(*) FROM username_history WHERE user_id = ?", (bob["id"],)) == 1
    admin_client.post(f"/admin/users/{bob['id']}/toggle_chat")
    assert fetch(db, bob)["chat_disabled"] == 1


def test_delete_user(admin_client, admin, make_user, db):
    bob = make_user("bob")
    response = edit(admin_client, bob, action="delete")
    assert response.headers["Location"].endswith("/admin/users")
    assert fetch(db, bob) is None
    edit(admin_client, admin, action="delete")
    assert fetch(db, admin) is not None


def test_next_redirect_must_be_local(admin_client, make_user):
    bob = make_user("bob", approval_status="pending")
    response = edit(admin_client, bob, action="approve_user", next="https://evil.example/")
    assert "evil.example" not in response.headers["Location"]


# ── Impersonation ────────────────────────────────────────────────────────────


def test_impersonation_start_and_stop(admin_client, admin, make_user, db):
    bob = make_user("bob")
    assert admin_client.post(f"/admin/users/{bob['id']}/impersonate").status_code == 302
    log = db.one("SELECT * FROM impersonation_logs WHERE target_user_id = ?", (bob["id"],))
    assert log["admin_id"] == admin["id"] and log["ended_at"] is None
    assert admin_client.get("/admin/users").status_code == 403  # now acting as bob
    response = admin_client.post("/admin/stop-impersonating")
    assert response.status_code == 307
    admin_client.post(response.headers["Location"])
    assert admin_client.get("/admin/users").status_code == 200
    assert db.one("SELECT ended_at FROM impersonation_logs WHERE id = ?", (log["id"],))["ended_at"]


@pytest.mark.parametrize("target_kwargs", [
    {"role": "admin"}, {"role": "owner"}, {"role": "user", "is_superuser": 1}, {"role": "user", "suspended": 1},
    {"role": "user", "approval_status": "pending"},
])
def test_impersonation_restrictions(admin_client, make_user, db, target_kwargs):
    target = make_user("target_account", **target_kwargs)
    admin_client.post(f"/admin/users/{target['id']}/impersonate")
    assert db.scalar("SELECT COUNT(*) FROM impersonation_logs") == 0


def test_superuser_may_impersonate_admin(client, owner, login, make_user, db):
    login(client, owner)
    peer = make_user("peer_admin", role="admin")
    client.post(f"/admin/users/{peer['id']}/impersonate")
    assert db.scalar("SELECT COUNT(*) FROM impersonation_logs") == 1


def test_cannot_impersonate_self_or_nest(admin_client, admin, make_user, db):
    admin_client.post(f"/admin/users/{admin['id']}/impersonate")
    assert db.scalar("SELECT COUNT(*) FROM impersonation_logs") == 0


# ── Sessions ─────────────────────────────────────────────────────────────────


def test_revoke_user_sessions(app, admin_client, make_user, login, db):
    bob = make_user("bob")
    bob_client = app.test_client()
    login(bob_client, bob)
    session_id = db.scalar("SELECT id FROM user_sessions WHERE user_id = ?", (bob["id"],))
    admin_client.post(f"/admin/users/{bob['id']}/sessions/revoke", data={"session_id": session_id})
    assert "/login" in bob_client.get("/_probe/private").headers["Location"]


def test_revoke_session_belongs_to_user(app, admin_client, make_user, login, db):
    bob, carol = make_user("bob"), make_user("carol")
    carol_client = app.test_client()
    login(carol_client, carol)
    carol_session = db.scalar("SELECT id FROM user_sessions WHERE user_id = ?", (carol["id"],))
    admin_client.post(f"/admin/users/{bob['id']}/sessions/revoke", data={"session_id": carol_session})
    assert carol_client.get("/_probe/private").data == b"private"


def test_mass_logout_keeps_admin_session(app, admin_client, make_user, login):
    bob_client = app.test_client()
    login(bob_client, make_user("bob"))
    assert admin_client.post("/admin/mass-logout").status_code == 302
    assert "/login" in bob_client.get("/_probe/private").headers["Location"]
    assert admin_client.get("/admin/users").status_code == 200


def test_auto_logout_job(app, admin_client, make_user, login, db):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.admin import jobs

    bob_client = app.test_client()
    login(bob_client, make_user("bob"))
    admin_client.post("/admin/sessions/auto-logout", data={"auto_logout_enabled": "1", "auto_logout_hour": "3"})
    assert db.one("SELECT auto_logout_enabled, auto_logout_hour FROM site_settings")["auto_logout_hour"] == 3
    with app.app_context(), connection_scope():
        jobs.auto_logout()  # first run: no previous run, nothing happens
    assert bob_client.get("/_probe/private").data == b"private"
    db.execute("UPDATE user_sessions SET created_at = ?", (sql_in(days=-3),))
    db.execute("INSERT INTO job_runs (name, last_run_at) VALUES ('admin.auto_logout', ?)", (sql_in(days=-2),))
    with app.app_context(), connection_scope():
        jobs.auto_logout()
    assert "/login" in bob_client.get("/_probe/private").headers["Location"]
