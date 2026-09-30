"""Sign-in extras: setup token, administrator sign-in, session limit, app selector, account status."""

from __future__ import annotations

from conftest import PASSWORD


def test_setup_token_is_not_accepted_from_the_query_string(app_factory):
    client = app_factory(setup_done=False).test_client()
    response = client.get("/setup?setup_token=test-setup-token")
    assert b'name="setup_token"' in response.data
    response = client.post("/setup", data={"step": "create", "username": "owner", "password": PASSWORD,
                                           "confirm_password": PASSWORD})
    assert response.status_code == 403
    client.post("/setup", data={"setup_token": "test-setup-token"})
    response = client.post("/setup", data={"step": "create", "username": "owner", "password": PASSWORD,
                                           "confirm_password": PASSWORD})
    assert response.headers["Location"].endswith("/onboarding")


def test_admin_login_accepts_only_administrators(client, make_user, admin):
    make_user("bob")
    response = client.post("/admin", data={"username": "bob", "password": PASSWORD})
    assert response.status_code == 403
    assert client.get("/_probe/private").status_code == 302  # not signed in
    response = client.post("/admin", data={"username": admin["username"], "password": "wrong password"})
    assert response.status_code == 401
    response = client.post("/admin", data={"username": admin["username"], "password": PASSWORD})
    assert response.status_code == 302
    assert client.get("/_probe/admin").data == b"admin"


def test_admin_login_works_during_maintenance(client, db, admin, make_user):
    db.execute("UPDATE site_settings SET maintenance_mode = 1")
    assert client.get("/admin/sign-in").status_code == 200
    assert b"/admin/sign-in" in client.get("/maintenance").data
    response = client.post("/admin", data={"username": admin["username"], "password": PASSWORD})
    assert response.status_code == 302
    assert client.get("/_probe/private").data == b"private"


def test_regular_login_during_maintenance_keeps_users_out(client, db, make_user):
    user = make_user("bob")
    db.execute("UPDATE site_settings SET maintenance_mode = 1")
    response = client.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.headers["Location"].endswith("/maintenance")
    assert db.scalar("SELECT COUNT(*) FROM user_sessions") == 0


def test_session_limit_shows_conflict_to_the_older_session(app, db, make_user, login):
    db.execute("UPDATE site_settings SET session_limit_enabled = 1")
    user = make_user("bob")
    first, second = app.test_client(), app.test_client()
    login(first, user)
    login(second, user)
    response = first.get("/_probe/private")
    assert "/session-conflict" in response.headers["Location"]
    page = first.get("/session-conflict")
    assert page.status_code == 200 and b'value="bob"' in page.data
    # Signing in here anyway ends the other session.
    response = first.post("/session-conflict/force", data={"username": "bob", "password": PASSWORD})
    assert response.status_code == 302
    assert first.get("/_probe/private").data == b"private"
    assert "/session-conflict" in second.get("/_probe/private").headers["Location"]


def test_session_conflict_page_needs_a_conflict(client, db, make_user):
    assert client.get("/session-conflict").headers["Location"].startswith("/login")


def test_session_conflict_force_is_rate_limited_and_checks_password(app, db, make_user, login):
    db.execute("UPDATE site_settings SET session_limit_enabled = 1")
    make_user("bob")
    client = app.test_client()
    assert client.post("/session-conflict/force", data={"username": "bob", "password": "nope-nope"}).status_code == 401
    for _ in range(8):
        client.post("/session-conflict/force", data={"username": "bob", "password": "nope-nope"})
    response = client.post("/session-conflict/force", data={"username": "bob", "password": PASSWORD})
    assert response.status_code == 429


def test_revoked_session_without_limit_goes_to_login(client, db, make_user, login):
    login(client, make_user("bob"))
    db.execute("UPDATE user_sessions SET revoked_at = '2000-01-01 00:00:00'")
    assert client.get("/_probe/private").headers["Location"].startswith("/login")


def test_app_selector_after_login(client, db, make_user, login):
    db.execute("UPDATE site_settings SET login_app_selector = 1")
    response = login(client, make_user("bob"))
    assert response.headers["Location"].endswith("/app-selector")
    response = client.get("/app-selector")
    assert response.status_code in (200, 302)
    if response.status_code == 200:
        assert b"auth-apps" in response.data


def test_app_selector_is_skipped_with_next(client, db, make_user):
    db.execute("UPDATE site_settings SET login_app_selector = 1")
    make_user("bob")
    response = client.post("/login", data={"username": "bob", "password": PASSWORD, "next": "/_probe/private"})
    assert response.headers["Location"].endswith("/_probe/private")


def test_login_redirects_to_intro_when_required(client, make_user):
    make_user("bob", intro_required=1)
    response = client.post("/login", data={"username": "bob", "password": PASSWORD, "next": "/_probe/private"})
    assert "/intro" in response.headers["Location"]


def test_denied_account_status_shows_deletion_time(client, db, make_user, login):
    user = make_user("bob", approval_status="denied", denied_at="2030-01-01 00:00:00")
    login(client, user)
    page = client.get("/account-status")
    assert b"2030-01-02" in page.data
    assert db.scalar("SELECT denied_notified FROM users WHERE id = ?", (user["id"],)) == 1


# ── Suspended users deleting their own account ───────────────────────────────


def _suspend(db, user):
    db.execute("UPDATE users SET suspended = 1 WHERE id = ?", (user["id"],))


def test_suspended_deletion_disabled_by_default(client, db, make_user, login):
    user = make_user("bob")
    login(client, user)
    _suspend(db, user)
    assert b"account-suspended/delete" not in client.get("/account-status").data
    assert client.post("/account-suspended/delete", data={"password": PASSWORD}).status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)) == 1


def test_suspended_user_deletes_own_account(client, db, make_user, login):
    db.execute("UPDATE site_settings SET suspended_account_deletion_enabled = 1")
    user = make_user("bob")
    login(client, user)
    _suspend(db, user)
    assert b"account-suspended/delete" in client.get("/account-status").data
    response = client.post("/account-suspended/delete", data={"password": "wrong password"})
    assert response.headers["Location"].endswith("/account-status")
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)) == 1
    response = client.post("/account-suspended/delete", data={"password": PASSWORD})
    assert response.headers["Location"].endswith("/login")
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)) == 0


def test_active_users_cannot_use_suspended_deletion(client, db, make_user, login):
    db.execute("UPDATE site_settings SET suspended_account_deletion_enabled = 1")
    user = make_user("bob")
    login(client, user)
    assert client.post("/account-suspended/delete", data={"password": PASSWORD}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)) == 1
    client.post("/logout")
    assert "/login" in client.post("/account-suspended/delete", data={"password": PASSWORD}).headers["Location"]


def test_suspended_superuser_and_last_admin_cannot_delete(client, db, make_user, login):
    db.execute("UPDATE site_settings SET suspended_account_deletion_enabled = 1")
    root = make_user("rooty", role="admin", is_superuser=1)
    login(client, root)
    _suspend(db, root)
    assert client.post("/account-suspended/delete", data={"password": PASSWORD}).status_code == 403
    client.post("/logout")
    chief = make_user("chief", role="admin")
    login(client, chief)
    db.execute("UPDATE users SET suspended = 1 WHERE role IN ('admin', 'owner')")
    client.post("/account-suspended/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (chief["id"],)) == 1


def test_csrf_protects_suspended_deletion(csrf_client, db, make_user):
    db.execute("UPDATE site_settings SET suspended_account_deletion_enabled = 1")
    make_user("bob", suspended=1)
    from conftest import csrf_token_from

    token = csrf_token_from(csrf_client.get("/login"))
    csrf_client.post("/login", data={"username": "bob", "password": PASSWORD, "csrf_token": token})
    assert csrf_client.post("/account-suspended/delete", data={"password": PASSWORD}).status_code == 400


def test_failures_from_one_address_do_not_lock_the_account_elsewhere(app, db, make_user):
    make_user("bob")
    attacker = app.test_client()
    for _ in range(9):
        attacker.post("/login", data={"username": "bob", "password": "nope-nope"},
                      environ_base={"REMOTE_ADDR": "203.0.113.5"})
    refused = attacker.post("/login", data={"username": "bob", "password": PASSWORD},
                            environ_base={"REMOTE_ADDR": "203.0.113.5"})
    assert refused.status_code == 429
    owner = app.test_client()
    response = owner.post("/login", data={"username": "bob", "password": PASSWORD},
                          environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert response.status_code == 302
