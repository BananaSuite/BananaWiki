from conftest import PASSWORD, csrf_token_from


def test_setup_requires_token(app_factory):
    app = app_factory(setup_done=False)
    client = app.test_client()
    assert client.get("/_probe/private").headers["Location"].endswith("/setup")
    response = client.post("/setup", data={"step": "create", "username": "owner", "password": PASSWORD,
                                           "confirm_password": PASSWORD})
    assert b"Installation token" in response.data or response.status_code in (200, 403)
    client.post("/setup", data={"setup_token": "test-setup-token"})
    response = client.post("/setup", data={"step": "create", "username": "owner", "password": PASSWORD,
                                           "confirm_password": PASSWORD})
    assert response.headers["Location"].endswith("/onboarding")


def test_login_and_logout(client, make_user, login):
    user = make_user("bob")
    login(client, user)
    assert client.get("/_probe/private").data == b"private"
    client.post("/logout")
    assert "/login" in client.get("/_probe/private").headers["Location"]


def test_wrong_password(client, make_user):
    make_user("bob")
    response = client.post("/login", data={"username": "bob", "password": "nope-nope"})
    assert response.status_code == 401


def test_account_lockout_is_per_account(client, make_user, login):
    victim = make_user("victim")
    attacker = make_user("attacker")
    for _ in range(8):
        client.post("/login", data={"username": "victim", "password": "wrong-password"})
    # A successful sign-in to another account must not reset the victim's counter.
    login(client, attacker)
    client.post("/logout")
    response = client.post("/login", data={"username": "victim", "password": PASSWORD})
    assert response.status_code == 429
    assert victim


def test_pending_accounts_cannot_use_the_wiki(client, make_user, login):
    user = make_user("newbie", approval_status="pending")
    login(client, user)
    response = client.get("/_probe/private")
    assert response.headers["Location"].endswith("/account-status")
    assert b"Waiting for approval" in client.get("/account-status").data


def test_revoked_session_is_rejected(app, client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    assert client.get("/_probe/private").data == b"private"
    db.execute("UPDATE user_sessions SET revoked_at = '2000-01-01 00:00:00'")
    response = client.get("/_probe/private")
    assert "/login" in response.headers["Location"]


def test_csrf_is_enforced(csrf_client, make_user):
    make_user("bob")
    response = csrf_client.post("/login", data={"username": "bob", "password": PASSWORD})
    assert response.status_code == 400
    token = csrf_token_from(csrf_client.get("/login"))
    response = csrf_client.post("/login", data={"username": "bob", "password": PASSWORD, "csrf_token": token})
    assert response.status_code == 302


def test_forced_password_change(client, make_user, login):
    user = make_user("bob", force_password_change=1)
    login(client, user)
    assert client.get("/_probe/private").headers["Location"].endswith("/force-change-password")
    assert client.post("/_probe/private").status_code == 403
    response = client.post("/force-change-password", data={
        "current_password": PASSWORD, "new_password": "brand new secret", "confirm_password": "brand new secret"})
    assert response.status_code == 302


def test_health_needs_no_session(client):
    assert client.get("/health").json == {"status": "ok"}


def test_security_headers(client):
    response = client.get("/login")
    assert "nonce-" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_public_read_needs_public_mode(client, db):
    assert "/login" in client.get("/_probe/public-read").headers["Location"]
    db.execute("UPDATE site_settings SET public_mode = 1")
    assert client.get("/_probe/public-read").data == b"public-read"


def test_admin_required(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/_probe/admin").status_code == 403
    client.post("/logout")
    login(client, make_user("chief", role="admin"))
    assert client.get("/_probe/admin").data == b"admin"


def test_suspended_user_blocked_then_released(client, make_user, login, db):
    user = make_user("bob", suspended=1, suspended_until="2000-01-01 00:00:00")
    login(client, user)
    assert client.get("/_probe/private").data == b"private"
    db.execute("UPDATE users SET suspended = 1, suspended_until = NULL WHERE id = ?", (user["id"],))
    assert client.get("/_probe/private").headers["Location"].endswith("/account-status")


def test_maintenance_mode(client, make_user, login, db):
    login(client, make_user("bob"))
    db.execute("UPDATE site_settings SET maintenance_mode = 1")
    assert client.get("/_probe/private").headers["Location"].endswith("/maintenance")
