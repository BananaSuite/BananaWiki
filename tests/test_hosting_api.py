"""The hosting portal's REST API under /api/v1 and the tokens it accepts."""

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers._passwords import generate_password_hash
from hosting import config as hosting_config
from hosting import instance_manager
from hosting.app import create_hosting_app
from hosting.db import (
    MAX_ACTIVE_API_TOKENS,
    add_collaborator,
    change_account_password,
    consume_password_reset,
    create_account,
    create_api_token,
    create_instance,
    delete_account,
    get_account_by_id,
    get_hosting_api_enabled,
    get_hosting_db_context,
    init_hosting_db,
    issue_password_reset,
    list_api_tokens,
    revoke_api_token,
    set_account_admin,
    set_pending_deletion,
    suspend_account,
    update_hosting_account,
    update_hosting_settings,
    update_instance_status,
)
from hosting.db._events import list_events
from hosting.routes import api as api_routes
from hosting.routes.auth import API_TOKEN_FORM_NONCE_KEY

PASSWORD = "password123"
TOKEN_SHAPE = re.compile(r"bwh_[A-Za-z0-9_-]{40}")
INSTANCE_FIELDS = {
    "id", "subdomain", "url", "status", "role", "created_at", "expires_at",
    "storage_used_bytes", "storage_limit_bytes",
}


@pytest.fixture(autouse=True)
def isolated_hosting_db(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    monkeypatch.setattr(hosting_config, "INSTANCES_DIR", str(tmp_path / "instances"))
    init_hosting_db()


@pytest.fixture
def app():
    application = create_hosting_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def api_on():
    update_hosting_settings(api_enabled=1)


@pytest.fixture
def lifecycle(monkeypatch):
    """Replace the dashboard's lifecycle functions, as the API imports them.

    The identity checks prove the API calls the same functions the
    dashboard does; the stand-ins only flip the status, with no containers.
    """
    assert api_routes.stop_instance is instance_manager.stop_instance
    assert api_routes.restart_instance is instance_manager.restart_instance
    calls = []

    def fake_stop(instance_id):
        calls.append(("stop", instance_id))
        update_instance_status(instance_id, "stopped")
        return True, ""

    def fake_restart(instance_id):
        calls.append(("restart", instance_id))
        update_instance_status(instance_id, "running")
        return True, ""

    monkeypatch.setattr(api_routes, "stop_instance", fake_stop)
    monkeypatch.setattr(api_routes, "restart_instance", fake_restart)
    return calls


def _account(username, *, admin=False, **fields):
    account_id = create_account(username, generate_password_hash(PASSWORD), is_admin=admin)
    if fields:
        update_hosting_account(account_id, **fields)
    return account_id


def _token(account_id, *scopes, expires_at=None, name="script"):
    _token_id, raw = create_api_token(account_id, name, scopes or ("account:read",), expires_at)
    return raw


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def _login(client, username):
    response = client.post("/login", data={"username": username, "password": PASSWORD})
    assert response.status_code == 302, response.get_data(as_text=True)


def _instance(account_id, subdomain):
    return create_instance(account_id, subdomain, "wikiadmin", "one-time-secret")


def _token_rows(account_id):
    with get_hosting_db_context() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM hosting_api_tokens WHERE account_id=? ORDER BY id", (account_id,)
        )]


def _create_form(client, **overrides):
    """Fill in the token form, with the nonce the Account page would have rendered."""
    with client.session_transaction() as stored:
        nonce = stored.setdefault(API_TOKEN_FORM_NONCE_KEY, "rendered-nonce")
    form = {
        "name": "deploy script",
        "scopes": ["account:read", "instances:read"],
        "expires_in": "90",
        "current_password": PASSWORD,
        "form_nonce": nonce,
    }
    form.update(overrides)
    return form


# The switch


def test_every_api_route_is_a_json_404_while_the_api_is_off(client):
    owner = _account("owner")
    raw = _token(owner, "account:read", "instances:manage")
    inst = _instance(owner, "offwiki")
    assert not get_hosting_api_enabled()

    for method, path, headers in (
        ("get", "/api/v1/status", {}),
        ("get", "/api/v1/me", _auth(raw)),
        ("post", f"/api/v1/instances/{inst['id']}/pause", _auth(raw)),
        ("get", "/api/v1/no-such-endpoint", {}),
        ("delete", "/api/v1/me", _auth(raw)),
        ("options", "/api/v1/me", {}),
    ):
        response = getattr(client, method)(path, headers=headers)
        assert response.status_code == 404, (method, path)
        assert response.is_json and response.get_json()["error"], (method, path)


def test_double_slash_paths_are_a_json_404_while_the_api_is_off(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    for method, path in (("get", "/api/v1//me"), ("post", "/api/v1//instances/x/pause"), ("get", "/api/v1/")):
        response = getattr(client, method)(path)
        assert response.status_code == 404, (method, path)
        assert response.is_json, (method, path)


def test_status_answers_once_an_administrator_switches_the_api_on(client, api_on):
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.get_json() == {"api": "v1"}
    assert response.headers["Cache-Control"] == "no-store"


def test_unknown_paths_and_wrong_methods_answer_in_json(client, api_on):
    missing = client.get("/api/v1/no-such-endpoint")
    assert missing.status_code == 404 and missing.is_json
    wrong_method = client.delete("/api/v1/me")
    assert wrong_method.status_code == 405 and wrong_method.is_json
    assert "GET" in wrong_method.headers["Allow"]
    # Pages outside the API keep the portal's own error page.
    assert not client.get("/no-such-page").is_json


def test_the_account_page_hides_tokens_while_the_api_is_off(client):
    _account("owner")
    _login(client, "owner")
    page = client.get("/account").get_data(as_text=True)
    assert 'id="api-tokens"' not in page
    assert client.post("/account/api-tokens", data=_create_form(client)).status_code == 404

    update_hosting_settings(api_enabled=1)
    page = client.get("/account").get_data(as_text=True)
    assert 'id="api-tokens"' in page
    assert 'value="admin:read"' not in page


def test_platform_admins_switch_the_api_in_platform_settings(client):
    _account("root", admin=True)
    _login(client, "root")
    assert 'name="api_enabled"' in client.get("/admin/settings").get_data(as_text=True)

    client.post("/admin/settings", data={"action": "save_api", "api_enabled": "1"})
    assert get_hosting_api_enabled()
    client.post("/admin/settings", data={"action": "save_api"})
    assert not get_hosting_api_enabled()


def test_customers_cannot_switch_the_api_on(client):
    _account("root", admin=True)
    _account("customer")
    _login(client, "customer")
    client.post("/admin/settings", data={"action": "save_api", "api_enabled": "1"})
    assert not get_hosting_api_enabled()


def test_an_existing_database_gains_the_setting_and_the_token_table(tmp_path, monkeypatch):
    from hosting.db.migrations.legacy import _upgrade_legacy
    from sqlite_migrations import apply_migrations

    path = tmp_path / "before-api.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        apply_migrations(conn, 0x42574850, (_upgrade_legacy,))
        conn.execute(
            "INSERT INTO accounts (id, username, password, created_at) "
            "VALUES ('kept', 'kept', 'hash', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(path))
    init_hosting_db()
    init_hosting_db()

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT api_enabled FROM hosting_settings").fetchone()[0] == 0
        columns = {row[1] for row in conn.execute("PRAGMA table_info(hosting_api_tokens)")}
        assert columns == {
            "id", "account_id", "name", "prefix", "token_hash", "scopes",
            "created_at", "expires_at", "last_used_at", "revoked_at",
        }
        indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list(hosting_api_tokens)")}
        assert indexes["ix_hosting_api_tokens_token_hash"] == 1
        assert conn.execute("SELECT username FROM accounts WHERE id='kept'").fetchone()[0] == "kept"
    assert not get_hosting_api_enabled()


def _schema(path):
    with closing(sqlite3.connect(path)) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {
            table: [tuple(row) for row in conn.execute(f'PRAGMA table_info("{table}")')]
            for table in tables
        }


def test_a_released_version_1_database_upgrades_to_the_current_schema(tmp_path, monkeypatch):
    fresh = hosting_config.HOSTING_DATABASE_PATH
    released = tmp_path / "released-v1.db"
    fixture = Path(__file__).parent / "fixtures" / "hosting_v1.sql"
    with closing(sqlite3.connect(released)) as conn:
        conn.executescript(fixture.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA application_id={0x42574850}")
        conn.execute("PRAGMA user_version=1")
        conn.commit()

    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(released))
    init_hosting_db()

    with closing(sqlite3.connect(released)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute(
            "SELECT approval_notify_email, approval_notify_mode, approval_notify_digest_hours, "
            "approval_notify_last_digest_at FROM hosting_settings"
        ).fetchone() == ("", "digest", 6, None)
    assert _schema(released) == _schema(fresh)


# Creating and revoking tokens


def test_creating_a_token_needs_the_current_password(client, api_on):
    owner = _account("owner")
    _login(client, "owner")
    response = client.post("/account/api-tokens", data=_create_form(client, current_password="wrong-password1"),
                           follow_redirects=True)
    assert "The current password is incorrect." in response.get_data(as_text=True)
    assert _token_rows(owner) == []


def test_a_new_token_is_shown_once_and_only_its_hash_is_kept(client, api_on):
    owner = _account("owner")
    _login(client, "owner")
    response = client.post("/account/api-tokens", data=_create_form(client))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    shown = TOKEN_SHAPE.findall(response.get_data(as_text=True))
    assert len(set(shown)) == 1
    raw = shown[0]

    [row] = _token_rows(owner)
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert row["prefix"] == raw[:12]
    assert row["name"] == "deploy script"
    assert row["scopes"] == "account:read instances:read"
    assert not any(raw in str(value) for value in row.values())

    # Not in the session, and not on the page any more.
    with client.session_transaction() as stored:
        assert raw not in repr(dict(stored))
    later = client.get("/account").get_data(as_text=True)
    assert raw not in later
    assert raw[:12] in later

    events = [dict(event) for event in list_events("account", owner)]
    created = [event for event in events if event["action"] == "api.token.created"]
    assert created and row["prefix"] in created[0]["reason"]
    assert all(raw not in event["reason"] for event in events)


def test_token_creation_is_refused_while_an_administrator_impersonates(client, api_on):
    admin = _account("root", admin=True)
    customer = _account("customer")
    _login(client, "root")
    with client.session_transaction() as stored:
        stored["hosting_impersonator_account_id"] = admin
        stored["hosting_account_id"] = customer

    page = client.get("/account").get_data(as_text=True)
    assert 'action="/account/api-tokens"' not in page
    response = client.post("/account/api-tokens", data=_create_form(client))
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account#api-tokens")
    assert _token_rows(customer) == [] and _token_rows(admin) == []


def test_an_administrator_impersonating_can_revoke_and_is_recorded(client, api_on):
    admin = _account("root", admin=True)
    customer = _account("customer")
    raw = _token(customer)
    token_id = _token_rows(customer)[0]["id"]
    _login(client, "root")
    with client.session_transaction() as stored:
        stored["hosting_impersonator_account_id"] = admin
        stored["hosting_account_id"] = customer

    assert client.post(f"/account/api-tokens/{token_id}/revoke").status_code == 302
    assert client.get("/api/v1/me", headers=_auth(raw)).status_code == 401
    revoked = [e for e in list_events("account", customer) if e["action"] == "api.token.revoked"]
    assert revoked[0]["actor_id"] == admin


def test_an_account_holds_at_most_ten_active_tokens(client, api_on):
    owner = _account("owner")
    for number in range(MAX_ACTIVE_API_TOKENS):
        _token(owner, name=f"token {number}")
    _login(client, "owner")

    assert client.post("/account/api-tokens", data=_create_form(client)).status_code == 302
    assert len(_token_rows(owner)) == MAX_ACTIVE_API_TOKENS
    with pytest.raises(ValueError, match="api_token_limit"):
        create_api_token(owner, "one too many", ["account:read"])

    # Expired and revoked tokens make room again.
    first, second = _token_rows(owner)[:2]
    with get_hosting_db_context() as conn:
        conn.execute("UPDATE hosting_api_tokens SET expires_at=? WHERE id=?",
                     ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), first["id"]))
        conn.commit()
    revoke_api_token(owner, second["id"])
    assert client.post("/account/api-tokens", data=_create_form(client)).status_code == 200
    assert client.post("/account/api-tokens", data=_create_form(client)).status_code == 200
    assert client.post("/account/api-tokens", data=_create_form(client)).status_code == 302


def test_customers_are_not_offered_admin_read(client, api_on):
    owner = _account("owner")
    _login(client, "owner")
    response = client.post("/account/api-tokens", data=_create_form(client, scopes=["account:read", "admin:read"]))
    assert response.status_code == 302
    assert _token_rows(owner) == []


def test_administrators_are_offered_admin_read(client, api_on):
    _account("root", admin=True)
    _login(client, "root")
    assert 'value="admin:read"' in client.get("/account").get_data(as_text=True)
    assert client.post("/account/api-tokens", data=_create_form(client, scopes=["admin:read"])).status_code == 200


@pytest.mark.parametrize("overrides", [
    {"name": ""},
    {"name": "x" * 61},
    {"name": "tab\tinside"},
    {"scopes": []},
    {"scopes": ["instances:delete"]},
    {"expires_in": "7"},
    {"expires_in": ""},
])
def test_bad_token_forms_are_rejected(client, api_on, overrides):
    owner = _account("owner")
    _login(client, "owner")
    assert client.post("/account/api-tokens", data=_create_form(client, **overrides)).status_code == 302
    assert _token_rows(owner) == []


@pytest.mark.parametrize("choice, days", [("30", 30), ("90", 90), ("365", 365), ("never", None)])
def test_the_expiry_choice_sets_the_expiry(client, api_on, choice, days):
    owner = _account("owner")
    _login(client, "owner")
    client.post("/account/api-tokens", data=_create_form(client, expires_in=choice))
    [row] = _token_rows(owner)
    if days is None:
        assert row["expires_at"] is None
    else:
        expires = datetime.fromisoformat(row["expires_at"])
        expected = datetime.now(timezone.utc) + timedelta(days=days)
        assert abs((expires - expected).total_seconds()) < 60


def _rendered_nonce(page):
    return re.search(r'name="form_nonce" value="([^"]+)"', page).group(1)


def test_reloading_the_new_token_page_does_not_create_another(client, api_on):
    owner = _account("owner")
    _login(client, "owner")
    page = client.get("/account").get_data(as_text=True)
    form = _create_form(client, form_nonce=_rendered_nonce(page))

    shown = client.post("/account/api-tokens", data=form)
    assert shown.status_code == 200
    text = shown.get_data(as_text=True)
    assert "history.replaceState(null, '', " in text
    assert 'action="/account" autocomplete="off"' in text
    # The page with the token carries a fresh nonce for a deliberate second token.
    assert _rendered_nonce(text) != form["form_nonce"]

    again = client.post("/account/api-tokens", data=form)
    assert again.status_code == 302
    assert again.headers["Location"].endswith("/account#api-tokens")
    assert len(_token_rows(owner)) == 1
    assert "That form was already used." in client.get("/account").get_data(as_text=True)

    assert client.post("/account/api-tokens", data=_create_form(client, form_nonce="")).status_code == 302
    assert len(_token_rows(owner)) == 1


def test_a_get_on_the_token_form_url_goes_to_the_account_page(client, api_on):
    _account("owner")
    _login(client, "owner")
    response = client.get("/account/api-tokens")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account#api-tokens")


def test_a_password_change_during_creation_cancels_the_token(client, api_on, monkeypatch):
    owner = _account("owner")
    _login(client, "owner")

    def check_then_change(stored, password):
        # The password is right, but it changes before the token is stored.
        change_account_password(owner, generate_password_hash("newpassword456"))
        return True

    monkeypatch.setattr(api_routes, "check_password_hash", check_then_change)
    response = client.post("/account/api-tokens", data=_create_form(client))
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert _token_rows(owner) == []
    with client.session_transaction() as stored:
        assert "hosting_account_id" not in stored


def test_create_api_token_refuses_a_stale_session_version(api_on):
    owner = _account("owner")
    version = get_account_by_id(owner)["session_version"]
    with pytest.raises(ValueError, match="api_token_stale"):
        create_api_token(owner, "late", ["account:read"], expected_session_version=int(version) + 1)
    create_api_token(owner, "on time", ["account:read"], expected_session_version=version)
    assert [row["name"] for row in _token_rows(owner)] == ["on time"]


def test_tokens_stay_listed_and_revocable_while_the_api_is_off(client):
    owner = _account("owner")
    update_hosting_settings(api_enabled=1)
    _token(owner, name="left over")
    update_hosting_settings(api_enabled=0)
    _login(client, "owner")

    page = client.get("/account").get_data(as_text=True)
    assert 'id="api-tokens"' in page
    assert "left over" in page
    assert "/revoke" in page
    assert 'action="/account/api-tokens"' not in page
    assert "The hosting API is switched off on this service" in page

    token_id = _token_rows(owner)[0]["id"]
    assert client.post(f"/account/api-tokens/{token_id}/revoke").status_code == 302
    assert _token_rows(owner)[0]["revoked_at"]
    assert 'id="api-tokens"' not in client.get("/account").get_data(as_text=True)


def test_an_unverified_email_hides_the_form_but_not_revoking(client, api_on, monkeypatch):
    from hosting.routes import auth as auth_routes

    owner = _account("owner", email="owner@example.org")
    _token(owner, name="older token")
    _login(client, "owner")
    monkeypatch.setattr(auth_routes, "_email_policy_active", lambda: True)
    monkeypatch.setattr(auth_routes, "_email_verification_required", lambda settings=None: True)

    page = client.get("/account").get_data(as_text=True)
    assert "older token" in page
    assert 'action="/account/api-tokens"' not in page
    assert "Verify your contact email before creating an API token." in page

    token_id = _token_rows(owner)[0]["id"]
    response = client.post(f"/account/api-tokens/{token_id}/revoke")
    assert response.headers["Location"].endswith("/account#api-tokens")
    assert _token_rows(owner)[0]["revoked_at"]


def test_revoking_a_token_on_the_account_page(client, api_on):
    owner = _account("owner")
    other = _account("other")
    raw = _token(owner)
    others = _token(other)
    _login(client, "owner")
    token_id = _token_rows(owner)[0]["id"]
    other_id = _token_rows(other)[0]["id"]

    assert client.post(f"/account/api-tokens/{other_id}/revoke").status_code == 404
    assert client.get("/api/v1/me", headers=_auth(others)).status_code == 200

    assert client.post(f"/account/api-tokens/{token_id}/revoke").status_code == 302
    assert client.get("/api/v1/me", headers=_auth(raw)).status_code == 401
    assert list_api_tokens(owner) == []
    assert client.post(f"/account/api-tokens/{token_id}/revoke").status_code == 404


# Authentication


def _expired_token(account_id):
    return _token(account_id, expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())


def _revoked_token(account_id):
    raw = _token(account_id)
    revoke_api_token(account_id, _token_rows(account_id)[-1]["id"])
    return raw


@pytest.mark.parametrize("header", [
    None,
    "Basic b3duZXI6cGFzc3dvcmQxMjM=",
    "Bearer",
    "Bearer not-a-token",
    "Bearer bwh_" + "A" * 40,
    "Bearer bwh_" + "A" * 41,
    "revoked",
    "expired",
])
def test_bad_credentials_get_401(client, api_on, header):
    owner = _account("owner")
    if header == "revoked":
        header = "Bearer " + _revoked_token(owner)
    elif header == "expired":
        header = "Bearer " + _expired_token(owner)
    headers = {"Authorization": header} if header else {}
    response = client.get("/api/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.get_json()["error"]
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_a_session_cookie_alone_never_authenticates(client, api_on, lifecycle):
    owner = _account("owner")
    inst = _instance(owner, "cookiewiki")
    _login(client, "owner")
    assert client.get("/account").status_code == 200

    assert client.get("/api/v1/me").status_code == 401
    assert client.post(f"/api/v1/instances/{inst['id']}/pause").status_code == 401
    assert lifecycle == []


def test_each_endpoint_needs_its_scope(client, api_on):
    owner = _account("owner")
    account_only = _token(owner, "account:read")
    instances_only = _token(owner, "instances:read")
    inst = _instance(owner, "scopewiki")

    assert client.get("/api/v1/me", headers=_auth(account_only)).status_code == 200
    assert client.get("/api/v1/instances", headers=_auth(account_only)).status_code == 403
    assert client.get(f"/api/v1/instances/{inst['id']}", headers=_auth(account_only)).status_code == 403
    assert client.get("/api/v1/me", headers=_auth(instances_only)).status_code == 403
    assert client.get("/api/v1/instances", headers=_auth(instances_only)).status_code == 200
    assert client.post(f"/api/v1/instances/{inst['id']}/pause", headers=_auth(instances_only)).status_code == 403
    assert client.get("/api/v1/admin/instances", headers=_auth(instances_only)).status_code == 403


def test_me_describes_the_account_and_token(client, api_on):
    owner = _account("owner", email="owner@example.org")
    raw = _token(owner, "account:read", "instances:read", name="nightly")
    body = client.get("/api/v1/me", headers=_auth(raw)).get_json()
    assert body["account"] == {
        "id": owner, "username": "owner", "email": "owner@example.org",
        "is_admin": False, "created_at": get_account_by_id(owner)["created_at"],
    }
    assert body["token"]["name"] == "nightly"
    assert body["token"]["scopes"] == ["account:read", "instances:read"]
    assert body["token"]["prefix"] == raw[:12]


def test_use_is_recorded_as_last_used(client, api_on):
    owner = _account("owner")
    raw = _token(owner)
    assert _token_rows(owner)[0]["last_used_at"] is None
    client.get("/api/v1/me", headers=_auth(raw))
    assert _token_rows(owner)[0]["last_used_at"] is not None


def test_no_cors_headers_are_sent(client, api_on):
    owner = _account("owner")
    raw = _token(owner)
    for response in (
        client.get("/api/v1/me", headers={**_auth(raw), "Origin": "https://evil.example"}),
        client.options("/api/v1/me", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
        }),
    ):
        assert not [name for name in response.headers.keys() if name.lower().startswith("access-control-")]


@pytest.mark.parametrize("message, status", [("database is locked", 503), ("no such column: nope", 500)])
def test_database_errors_are_answered_in_json(client, api_on, monkeypatch, message, status):
    owner = _account("owner")
    raw = _token(owner)

    def broken(_raw):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(api_routes, "get_active_api_token", broken)
    response = client.get("/api/v1/me", headers=_auth(raw))
    assert response.status_code == status
    assert response.is_json and response.get_json()["error"]
    assert response.headers["Cache-Control"] == "no-store"
    if status == 503:
        assert response.headers["Retry-After"] == "30"
        assert response.get_json()["retry_after"] == 30
    else:
        assert "request_id" in response.get_json()


# Account state


def _past(minutes=5):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.mark.parametrize("state", [
    "suspended", "pending_deletion", "email_flagged", "pending_approval", "denied",
])
def test_account_state_gates_refuse_the_token(client, api_on, state):
    owner = _account("owner")
    raw = _token(owner)
    if state == "suspended":
        suspend_account(owner, reason="abuse")
    elif state == "pending_deletion":
        set_pending_deletion(owner, seconds=3600)
    elif state == "email_flagged":
        update_hosting_account(owner, email_flagged_invalid=1)
    elif state == "pending_approval":
        update_hosting_account(owner, approval_status="pending")
    else:
        update_hosting_account(owner, approval_status="denied")
    response = client.get("/api/v1/me", headers=_auth(raw))
    assert response.status_code == 403
    assert response.get_json()["error"]


def test_an_expired_timed_suspension_is_lifted_on_use(client, api_on):
    owner = _account("owner")
    raw = _token(owner)
    suspend_account(owner, reason="cooling off", suspended_until=_past())
    assert client.get("/api/v1/me", headers=_auth(raw)).status_code == 200
    assert not get_account_by_id(owner)["suspended"]


def test_a_deleted_account_loses_its_tokens(client, api_on):
    owner = _account("owner")
    raw = _token(owner)
    delete_account(owner)
    assert _token_rows(owner) == []
    assert client.get("/api/v1/me", headers=_auth(raw)).status_code == 401


# Instances


def test_an_owner_lists_and_views_their_wikis(client, api_on):
    owner = _account("owner")
    raw = _token(owner, "instances:read")
    inst = _instance(owner, "ownerwiki")

    listed = client.get("/api/v1/instances", headers=_auth(raw)).get_json()["instances"]
    assert [item["id"] for item in listed] == [inst["id"]]
    assert set(listed[0]) == INSTANCE_FIELDS
    assert listed[0]["role"] == "owner"
    assert listed[0]["status"] == "running"
    assert listed[0]["subdomain"] == "ownerwiki"
    assert listed[0]["storage_used_bytes"] == 0
    assert listed[0]["storage_limit_bytes"] == hosting_config.INSTANCE_STORAGE_LIMIT_MB * 1024 * 1024

    detail = client.get(f"/api/v1/instances/{inst['id']}", headers=_auth(raw)).get_json()["instance"]
    assert detail == listed[0]


def test_terminated_wikis_are_not_returned(client, api_on):
    owner = _account("owner")
    raw = _token(owner, "instances:read")
    inst = _instance(owner, "gonewiki")
    update_instance_status(inst["id"], "terminated")
    assert client.get("/api/v1/instances", headers=_auth(raw)).get_json() == {"instances": []}
    assert client.get(f"/api/v1/instances/{inst['id']}", headers=_auth(raw)).status_code == 404


def test_collaborators_need_view_to_see_and_start_stop_to_act(client, api_on, lifecycle):
    owner = _account("owner")
    viewer = _account("viewer")
    operator = _account("operator")
    blind = _account("blind")
    full = _account("full")
    inst = _instance(owner, "sharedwiki")
    add_collaborator(inst["id"], viewer, owner, permissions=["view"])
    add_collaborator(inst["id"], operator, owner, permissions=["view", "start_stop"])
    add_collaborator(inst["id"], blind, owner, permissions=["start_stop"])
    add_collaborator(inst["id"], full, owner, role="full_access")
    scopes = ("instances:read", "instances:manage")
    tokens = {name: _token(account, *scopes) for name, account in (
        ("viewer", viewer), ("operator", operator), ("blind", blind), ("full", full),
    )}
    detail = f"/api/v1/instances/{inst['id']}"

    listed = client.get("/api/v1/instances", headers=_auth(tokens["viewer"])).get_json()["instances"]
    assert [(item["id"], item["role"]) for item in listed] == [(inst["id"], "collaborator")]
    assert client.get(detail, headers=_auth(tokens["viewer"])).status_code == 200
    assert client.post(detail + "/pause", headers=_auth(tokens["viewer"])).status_code == 403

    assert client.get("/api/v1/instances", headers=_auth(tokens["blind"])).get_json() == {"instances": []}
    assert client.get(detail, headers=_auth(tokens["blind"])).status_code == 404
    assert lifecycle == []

    assert client.post(detail + "/pause", headers=_auth(tokens["operator"])).status_code == 200
    assert client.post(detail + "/resume", headers=_auth(tokens["full"])).status_code == 200
    assert lifecycle == [("stop", inst["id"]), ("restart", inst["id"])]


def test_other_peoples_wikis_are_indistinguishable_from_missing_ones(client, api_on, lifecycle):
    owner = _account("owner")
    admin = _account("root", admin=True)
    stranger = _account("stranger")
    inst = _instance(owner, "privatewiki")
    scopes = ("instances:read", "instances:manage")

    for raw in (_token(stranger, *scopes), _token(admin, *scopes)):
        for method, suffix in (("get", ""), ("post", "/pause"), ("post", "/resume")):
            theirs = getattr(client, method)(f"/api/v1/instances/{inst['id']}{suffix}", headers=_auth(raw))
            missing = getattr(client, method)(f"/api/v1/instances/nosuchinstance00{suffix}", headers=_auth(raw))
            assert theirs.status_code == missing.status_code == 404
            assert theirs.get_json() == missing.get_json()
        assert client.get("/api/v1/instances", headers=_auth(raw)).get_json() == {"instances": []}
    assert lifecycle == []


def test_pause_and_resume_use_the_dashboard_functions_and_are_audited(client, api_on, lifecycle):
    owner = _account("owner")
    raw = _token(owner, "instances:manage")
    row = _token_rows(owner)[0]
    inst = _instance(owner, "busywiki")

    paused = client.post(f"/api/v1/instances/{inst['id']}/pause", headers=_auth(raw))
    assert paused.status_code == 200
    assert paused.get_json()["instance"]["status"] == "stopped"
    resumed = client.post(f"/api/v1/instances/{inst['id']}/resume", headers=_auth(raw))
    assert resumed.status_code == 200
    assert resumed.get_json()["instance"]["status"] == "running"
    assert lifecycle == [("stop", inst["id"]), ("restart", inst["id"])]

    events = [dict(event) for event in list_events("instance", inst["id"])]
    assert [event["action"] for event in events] == ["api.instance.resumed", "api.instance.paused"]
    for event in events:
        assert event["actor_id"] == owner
        assert f"Token {row['id']} ({row['prefix']})" in event["reason"]
        assert raw not in event["reason"]


def test_the_wrong_state_is_a_409(client, api_on, lifecycle):
    owner = _account("owner")
    raw = _token(owner, "instances:manage")
    inst = _instance(owner, "statewiki")

    resume = client.post(f"/api/v1/instances/{inst['id']}/resume", headers=_auth(raw))
    assert resume.status_code == 409
    assert resume.get_json()["status"] == "running"
    update_instance_status(inst["id"], "stopped")
    pause = client.post(f"/api/v1/instances/{inst['id']}/pause", headers=_auth(raw))
    assert pause.status_code == 409
    assert pause.get_json()["status"] == "stopped"
    assert lifecycle == []


def test_an_owner_is_locked_out_of_a_suspended_wiki(client, api_on, lifecycle):
    owner = _account("owner")
    raw = _token(owner, "instances:read", "instances:manage")
    inst = _instance(owner, "lockedwiki")
    update_instance_status(inst["id"], "suspended")

    for action in ("pause", "resume"):
        response = client.post(f"/api/v1/instances/{inst['id']}/{action}", headers=_auth(raw))
        assert response.status_code == 403
        assert "suspended" in response.get_json()["error"]
    assert client.get(f"/api/v1/instances/{inst['id']}", headers=_auth(raw)).get_json()["instance"]["status"] == "suspended"
    assert lifecycle == []


def test_a_stopped_wiki_that_carries_a_suspension_cannot_be_resumed(client, api_on, lifecycle):
    owner = _account("owner")
    operator = _account("operator")
    inst = _instance(owner, "markedwiki")
    add_collaborator(inst["id"], operator, owner, permissions=["view", "start_stop"])
    update_instance_status(inst["id"], "stopped")
    with get_hosting_db_context() as conn:
        conn.execute("UPDATE instances SET suspended_at=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), inst["id"]))
        conn.commit()

    for account in (owner, operator):
        response = client.post(f"/api/v1/instances/{inst['id']}/resume",
                               headers=_auth(_token(account, "instances:manage")))
        assert response.status_code == 403
        assert "suspended" in response.get_json()["error"]
    assert lifecycle == []


def test_a_resume_that_fails_to_start_is_a_500(client, api_on, monkeypatch):
    owner = _account("owner")
    raw = _token(owner, "instances:manage")
    inst = _instance(owner, "brokenwiki")
    update_instance_status(inst["id"], "stopped")
    monkeypatch.setattr(api_routes, "restart_instance", lambda _id: (False, "The process did not start."))

    response = client.post(f"/api/v1/instances/{inst['id']}/resume", headers=_auth(raw))
    assert response.status_code == 500
    assert response.get_json()["error"] == "The process did not start."
    assert list_events("instance", inst["id"]) == []


# Administrators


def test_admin_read_serves_the_queue_and_the_counts(client, api_on):
    admin = _account("root", admin=True)
    raw = _token(admin, "admin:read")
    _account("applicant", approval_status="pending", email="applicant@example.org",
             signup_use_case="Club documentation")
    customer = _account("customer")
    for number, status in enumerate(("running", "running", "stopped", "suspended", "terminated")):
        inst = _instance(customer, f"countwiki{number}")
        update_instance_status(inst["id"], status)

    pending = client.get("/api/v1/admin/pending-accounts", headers=_auth(raw)).get_json()["accounts"]
    assert [(item["username"], item["email"], item["use_case"]) for item in pending] == [
        ("applicant", "applicant@example.org", "Club documentation"),
    ]
    assert set(pending[0]) == {"id", "username", "email", "email_verified", "created_at", "use_case"}

    counts = client.get("/api/v1/admin/instances", headers=_auth(raw)).get_json()
    assert counts == {
        "total": 5,
        "by_status": {"running": 2, "stopped": 1, "suspended": 1, "terminated": 1},
    }


def test_admin_read_is_refused_to_customers(client, api_on):
    customer = _account("customer")
    # The form never offers the scope to a customer; this token stands in
    # for one that got it some other way.
    raw = _token(customer, "admin:read")
    for path in ("/api/v1/admin/pending-accounts", "/api/v1/admin/instances"):
        assert client.get(path, headers=_auth(raw)).status_code == 403


def test_admin_read_stops_working_when_the_admin_is_demoted(client, api_on):
    _account("other-root", admin=True)
    admin = _account("root", admin=True)
    raw = _token(admin, "admin:read", "account:read")
    assert client.get("/api/v1/admin/instances", headers=_auth(raw)).status_code == 200

    set_account_admin(admin, False)
    assert client.get("/api/v1/admin/instances", headers=_auth(raw)).status_code == 403
    assert client.get("/api/v1/admin/pending-accounts", headers=_auth(raw)).status_code == 403
    assert client.get("/api/v1/me", headers=_auth(raw)).status_code == 200


# Rate limits


def test_the_request_limit_answers_429_with_retry_after(client, api_on):
    owner = _account("owner")
    raw = _token(owner)
    other = _token(owner)
    for _ in range(60):
        assert client.get("/api/v1/me", headers=_auth(raw)).status_code == 200

    limited = client.get("/api/v1/me", headers=_auth(raw))
    assert limited.status_code == 429
    retry_after = int(limited.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert limited.get_json()["retry_after"] == retry_after
    # The limit belongs to the token, not the account or the address.
    assert client.get("/api/v1/me", headers=_auth(other)).status_code == 200


def test_pause_and_resume_have_a_tighter_limit(client, api_on, lifecycle):
    owner = _account("owner")
    raw = _token(owner, "instances:manage", "instances:read")
    inst = _instance(owner, "limitwiki")
    path = f"/api/v1/instances/{inst['id']}"

    statuses = [client.post(path + ("/pause" if n % 2 == 0 else "/resume"), headers=_auth(raw)).status_code
                for n in range(10)]
    assert statuses == [200] * 10
    limited = client.post(path + "/pause", headers=_auth(raw))
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.get(path, headers=_auth(raw)).status_code == 200


def test_api_traffic_leaves_longer_rate_limit_windows_intact(client, api_on):
    """API calls share rate-limit storage with the five-minute second-factor
    limit, and pruning their one-minute buckets must not shorten it."""
    from hosting.db import check_and_record_hosting_rate_limit

    for _ in range(10):
        assert check_and_record_hosting_rate_limit("someone", "mfa-login", 10, 300)
    with get_hosting_db_context() as conn:
        conn.execute("UPDATE hosting_rate_limit_hits SET hit_at=? WHERE bucket='mfa-login'", (_past(2),))
        conn.commit()
    owner = _account("owner")
    assert client.get("/api/v1/me", headers=_auth(_token(owner))).status_code == 200
    assert not check_and_record_hosting_rate_limit("someone", "mfa-login", 10, 300)


# Passwords


def test_changing_the_password_revokes_every_token(client, api_on):
    owner = _account("owner")
    raws = [_token(owner), _token(owner, "instances:read")]
    _login(client, "owner")
    client.post("/account/change-password", data={
        "current_password": PASSWORD,
        "new_password": "newpassword456",
        "confirm_new_password": "newpassword456",
    })
    for raw in raws:
        assert client.get("/api/v1/me", headers=_auth(raw)).status_code == 401
    assert all(row["revoked_at"] for row in _token_rows(owner))
    revoked = [e for e in list_events("account", owner) if e["action"] == "api.token.revoked"]
    assert revoked and "Password changed" in revoked[0]["reason"]


def test_a_reset_password_revokes_every_token(client, api_on):
    owner = _account("owner", email="owner@example.org",
                     email_verified_at=datetime.now(timezone.utc).isoformat())
    by_email = _token(owner)
    issue_password_reset(owner, "reset-token", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    assert consume_password_reset("reset-token", generate_password_hash("newpassword456"))
    assert client.get("/api/v1/me", headers=_auth(by_email)).status_code == 401

    by_admin = _token(owner)
    change_account_password(owner, generate_password_hash("anotherpass789"))
    assert client.get("/api/v1/me", headers=_auth(by_admin)).status_code == 401


# Secrets and CSRF


def test_responses_carry_no_secrets(client, api_on, lifecycle):
    admin = _account("root", admin=True, email="root@example.org")
    owner = _account("owner", email="owner@example.org")
    _account("bystander", email="bystander@example.org")
    inst = _instance(owner, "secretwiki")
    add_collaborator(inst["id"], admin, owner, role="full_access")
    with get_hosting_db_context() as conn:
        conn.execute("UPDATE instances SET oauth_client_id='client-id-value', "
                     "oauth_client_secret_hash='client-secret-hash' WHERE id=?", (inst["id"],))
        conn.commit()
    raw = _token(owner, "account:read", "instances:read", "instances:manage")
    admin_raw = _token(admin, "admin:read", "instances:read")
    token_hash = _token_rows(owner)[0]["token_hash"]

    bodies = [
        client.get("/api/v1/me", headers=_auth(raw)),
        client.get("/api/v1/instances", headers=_auth(raw)),
        client.get(f"/api/v1/instances/{inst['id']}", headers=_auth(raw)),
        client.post(f"/api/v1/instances/{inst['id']}/pause", headers=_auth(raw)),
        client.get("/api/v1/instances", headers=_auth(admin_raw)),
        client.get("/api/v1/admin/pending-accounts", headers=_auth(admin_raw)),
        client.get("/api/v1/admin/instances", headers=_auth(admin_raw)),
    ]
    for response in bodies:
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        for secret in (
            raw, admin_raw, token_hash, "one-time-secret", "wikiadmin", "client-id-value",
            "client-secret-hash", "admin_password", hosting_config.INSTANCES_DIR,
            "bystander@example.org", "root@example.org",
        ):
            assert secret not in text, (response.request.path, secret)
    shared = bodies[4].get_json()["instances"]
    assert [(item["id"], item["role"]) for item in shared] == [(inst["id"], "collaborator")]
    assert "owner@example.org" not in bodies[4].get_data(as_text=True)


def test_only_the_api_is_exempt_from_csrf(app, client, api_on, lifecycle):
    owner = _account("owner")
    raw = _token(owner, "instances:manage")
    inst = _instance(owner, "csrfwiki")
    _login(client, "owner")
    app.config["WTF_CSRF_ENABLED"] = True

    response = client.post(f"/api/v1/instances/{inst['id']}/pause", headers=_auth(raw))
    assert response.status_code == 200
    assert lifecycle == [("stop", inst["id"])]

    form = client.post("/account/api-tokens", data=_create_form(client))
    assert form.status_code == 302 and "/login" in form.headers["Location"]
    assert len(_token_rows(owner)) == 1

    csrf = app.extensions["csrf"]
    assert [blueprint.name for blueprint in csrf._exempt_blueprints] == ["hosting_api"]
    assert not csrf._exempt_views
