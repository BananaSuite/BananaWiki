import pytest
from conftest import PASSWORD, csrf_token_from
from flask import request, url_for

from bananawiki.wiki import registry
from tests.features.pages_support import in_app


def test_setup_requires_token(app_factory):
    app = app_factory(setup_done=False)
    client = app.test_client()
    assert client.get("/_probe/private").headers["Location"].endswith("/setup")
    response = client.post("/setup", data={"step": "create", "username": "owner", "password": PASSWORD,
                                           "confirm_password": PASSWORD})
    assert b"Setup token" in response.data or response.status_code in (200, 403)
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


def test_head_requests_never_run_a_form_submission(app, client, make_user, login, db):
    owner = make_user("owner1", role="owner")
    login(client, owner)
    client.post("/create", data={"title": "Lesson one", "content": "Important body text"})
    app.config["CSRF_DISABLED"] = False
    response = client.head("/page/lesson-one/edit")
    assert response.status_code == 200 and response.data == b""
    assert response.headers["Content-Type"].startswith("text/html")
    page = db.execute("SELECT content, revision FROM pages WHERE slug = 'lesson-one'").fetchone()
    assert (page["content"], page["revision"]) == ("Important body text", 1)
    assert client.head("/logout").status_code == 200
    assert client.get("/_probe/private").data == b"private"


def test_views_accepting_get_and_post_see_head_as_get(app, client, make_user, login):
    from werkzeug.routing import IntegerConverter

    login(client, make_user("owner1", role="owner"))
    app.config["CSRF_DISABLED"] = False
    seen: dict[str, str] = {}

    def recording(endpoint, view):
        def record(*args, **kwargs):
            seen[endpoint] = request.method
            return view(*args, **kwargs)
        return record

    rules = [rule for rule in app.url_map.iter_rules() if {"GET", "POST"} <= (rule.methods or set())]
    assert rules
    for rule in rules:
        app.view_functions[rule.endpoint] = recording(rule.endpoint, app.view_functions[rule.endpoint])
    for rule in rules:
        values = {name: 1 if isinstance(rule._converters[name], IntegerConverter) else "x"
                  for name in rule.arguments}
        with app.test_request_context():
            target = url_for(rule.endpoint, **values)
        client.head(target)
    assert seen and set(seen.values()) == {"GET"}


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


def test_scripts_get_a_translated_reason_and_a_code_from_the_gates(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    json_headers = {"Accept": "application/json"}
    db.execute("UPDATE site_settings SET maintenance_mode = 1")
    response = client.get("/_probe/private?lang=it", headers=json_headers)
    assert response.status_code == 503 and response.json == {
        "error": "La wiki non è disponibile per qualche momento mentre gli amministratori ci lavorano.",
        "code": "maintenance"}
    db.execute("UPDATE site_settings SET maintenance_mode = 0")
    db.execute("UPDATE users SET suspended = 1 WHERE id = ?", (user["id"],))
    response = client.get("/_probe/private?lang=de", headers=json_headers)
    assert response.status_code == 403 and response.json == {
        "error": "Ein Administrator hat dein Konto gesperrt.", "code": "suspended"}
    db.execute("UPDATE users SET suspended = 0, force_password_change = 1 WHERE id = ?", (user["id"],))
    response = client.post("/_probe/private", headers=json_headers)
    assert response.status_code == 403 and response.json["code"] == "account_step_required"
    assert response.json["step"] == "auth.force_password_change"
    assert response.json["error"].startswith("Für dein Konto fehlt noch ein Schritt")


def test_failed_sign_in_logs_known_accounts_but_not_unknown_names(client, make_user, caplog):
    make_user("real_user")
    with caplog.at_level("INFO"):
        client.post("/login", data={"username": "real_user", "password": "wrong-password-1"})
        client.post("/login", data={"username": "hunter2-typed-by-mistake", "password": "x"})
    text = caplog.text
    assert "Failed sign-in for 'real_user'" in text
    assert "hunter2-typed-by-mistake" not in text
    assert "Failed sign-in for an unknown account" in text


@pytest.mark.parametrize("method, url", [
    ("POST", "/api/preview"),
    ("POST", "/api/code/highlight"),
    ("GET", "/api/accessibility"),
    ("POST", "/api/accessibility"),
    ("POST", "/api/draft/save"),
    ("GET", "/api/draft/mine"),
])
def test_bearer_header_does_not_skip_sign_in_outside_the_token_api(client, method, url):
    """Only the token API reads ``Authorization: Bearer``; the rest of /api/ needs a session."""
    response = client.open(url, method=method, json={"content": "x"}, headers={"Authorization": "Bearer junk"})
    assert response.status_code == 401


def test_bearer_calls_reach_the_token_api(app, client):
    in_app(app, lambda: registry.set_enabled("api_service", True))
    response = client.get("/api/v1/pages", headers={"Authorization": "Bearer junk"})
    assert response.status_code in (401, 503)  # the API's own answer, not the sign-in redirect
    assert response.get_json()["error"]
