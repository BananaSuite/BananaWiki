"""Bearer API requests work without cookies while browser forms retain CSRF checks."""

from datetime import datetime, timedelta, timezone
import json

import pytest

import db


@pytest.fixture
def protected_client(client, admin_user, monkeypatch):
    """Use the real CSRF extension with a fresh, unauthenticated client."""
    db.update_site_settings(api_service_enabled=1)
    monkeypatch.setitem(client.application.config, "WTF_CSRF_ENABLED", True)
    return client


def token_headers(user_id, *, write=True, scopes=None, origin=None):
    """Create a scoped fixture token without establishing a browser session."""
    token = db.create_api_token(
        user_id,
        name="CSRF fixture",
        permissions={
            "read": True,
            "write": write,
            "scopes": scopes or ["pages"],
        },
    )
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    return headers


def test_bearer_write_needs_no_session_or_csrf_token(protected_client, admin_user):
    """A command-line API client can create a page with its scoped bearer token."""
    response = protected_client.post(
        "/api/v1/pages",
        json={
            "title": "API fixture",
            "content": "Created without a browser session",
        },
        headers=token_headers(admin_user),
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    assert db.get_page_by_slug("api-fixture") is not None


def test_api_write_without_bearer_remains_unauthorized(protected_client):
    """Exemption from browser CSRF checks does not grant write access."""
    response = protected_client.post(
        "/api/v1/pages", json={"title": "Rejected fixture"}
    )
    assert response.status_code == 401
    assert db.get_page_by_slug("rejected-fixture") is None


def test_browser_admin_form_still_requires_csrf(
    logged_in_admin, admin_user, monkeypatch
):
    """A bearer header cannot bypass CSRF protection on a cookie-based admin form."""
    monkeypatch.setitem(logged_in_admin.application.config, "WTF_CSRF_ENABLED", True)
    response = logged_in_admin.post(
        "/global-settings",
        json={"site_name": "Rejected fixture"},
        headers=token_headers(admin_user),
    )
    assert response.status_code == 400
    assert db.get_site_settings()["site_name"] != "Rejected fixture"


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil-localhost",
        "http://localhost.evil.invalid",
        "http://localhost:81",
        "http://localhost:0",
        "https://localhost",
        "http://evil@localhost",
        "http://localhost/private",
        "null",
    ],
)
def test_api_rejects_a_different_browser_origin(protected_client, admin_user, origin):
    """Lookalike hosts and different ports or schemes are not the same origin."""
    response = protected_client.get(
        "/api/v1/pages", headers=token_headers(admin_user, origin=origin)
    )
    assert response.status_code == 403


@pytest.mark.parametrize("origin", ["http://localhost", "http://localhost:80"])
def test_api_accepts_same_origin(protected_client, admin_user, origin):
    """An explicit default port and its implicit form identify the same origin."""
    response = protected_client.get(
        "/api/v1/pages", headers=token_headers(admin_user, origin=origin)
    )
    assert response.status_code == 200


@pytest.mark.parametrize("csrf_enabled", [True, False])
def test_userbot_read_only_token_cannot_change_profile(
    protected_client, admin_user, monkeypatch, csrf_enabled
):
    """The userbot scope does not turn a read-only token into a profile-write token."""
    monkeypatch.setitem(
        protected_client.application.config, "WTF_CSRF_ENABLED", csrf_enabled
    )
    response = protected_client.post(
        "/api/v1/userbot/profile",
        json={"bio": "Rejected fixture"},
        headers=token_headers(admin_user, write=False, scopes=["userbot"]),
    )
    assert response.status_code == 403
    assert (db.get_user_profile(admin_user) or {}).get("bio") != "Rejected fixture"


def test_userbot_write_token_can_change_profile_without_session(
    protected_client, admin_user
):
    """A correctly scoped automation token can update its own profile."""
    response = protected_client.post(
        "/api/v1/userbot/profile",
        json={"bio": "Updated fixture"},
        headers=token_headers(admin_user, scopes=["userbot"]),
    )
    assert response.status_code == 200
    assert db.get_user_profile(admin_user)["bio"] == "Updated fixture"


def test_admin_token_cannot_exceed_selected_scope_or_read_only_flag(
    protected_client, admin_user
):
    """An admin can issue a genuinely limited credential for a read-only integration."""
    headers = token_headers(admin_user, write=False)
    assert protected_client.get("/api/v1/pages", headers=headers).status_code == 200
    assert protected_client.get("/api/v1/users", headers=headers).status_code == 403
    assert (
        protected_client.post(
            "/api/v1/pages", json={"title": "Rejected fixture"}, headers=headers
        ).status_code
        == 403
    )
    assert db.get_page_by_slug("rejected-fixture") is None


def test_token_cannot_issue_broader_scopes(protected_client, admin_user):
    """A token-management credential cannot mint an administrator-scoped child."""
    headers = token_headers(admin_user, scopes=["tokens"])
    response = protected_client.post(
        "/api/v1/tokens",
        headers=headers,
        json={
            "name": "Rejected child",
            "permissions": {"read": True, "write": True, "scopes": ["admin"]},
        },
    )
    assert response.status_code == 403
    assert db.count_user_tokens(admin_user) == 1


def test_child_token_inherits_expiry_and_cannot_extend_it(protected_client, admin_user):
    """A token with a deadline cannot turn itself into a longer-lived credential."""
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    parent = db.create_api_token(
        admin_user,
        permissions={"read": True, "write": True, "scopes": ["tokens", "pages"]},
        expires_at=deadline.isoformat(),
    )
    headers = {"Authorization": "Bearer " + parent}
    payload = {
        "name": "Limited child",
        "permissions": {"read": True, "write": False, "scopes": ["pages"]},
    }
    response = protected_client.post("/api/v1/tokens", headers=headers, json=payload)
    assert response.status_code == 201
    child, _ = db.verify_api_service_token(response.get_json()["token"])
    assert child["expires_at"] == deadline.isoformat()
    payload["expires_at"] = (deadline + timedelta(hours=1)).isoformat()
    response = protected_client.post("/api/v1/tokens", headers=headers, json=payload)
    assert response.status_code == 403
    assert db.count_user_tokens(admin_user) == 2


@pytest.mark.parametrize(
    "permissions",
    [
        {"read": True, "write": "false", "scopes": ["pages"]},
        {"read": True, "write": True, "scopes": "pages"},
        {"read": True, "write": True, "scopes": [42]},
    ],
)
def test_malformed_token_permissions_do_not_authorize_writes(
    protected_client, admin_user, permissions
):
    """Incorrect JSON types cannot turn strings or invalid scope lists into grants."""
    raw = db.create_api_token(admin_user, permissions=permissions)
    response = protected_client.post(
        "/api/v1/pages",
        json={"title": "Rejected fixture"},
        headers={"Authorization": "Bearer " + raw},
    )
    assert response.status_code == 403
    assert db.get_page_by_slug("rejected-fixture") is None


@pytest.mark.parametrize("payload", [[], "not an object"])
def test_non_object_api_payload_returns_validation_error(
    protected_client, admin_user, payload
):
    """Malformed API bodies receive a client error instead of raising an exception."""
    response = protected_client.post(
        "/api/v1/pages", json=payload, headers=token_headers(admin_user)
    )
    assert response.status_code == 400


def test_api_audit_does_not_store_password_payloads(protected_client, admin_user):
    """Account API calls retain useful audit metadata without plaintext passwords."""
    password = "Fixture-secret-kept-out-of-audit-123"
    response = protected_client.post(
        "/api/v1/users",
        headers=token_headers(admin_user, scopes=["users"]),
        json={"username": "audit-fixture", "password": password},
    )
    assert response.status_code == 201
    entry = db.get_audit_log()[0]
    assert entry["endpoint"] == "/api/v1/users"
    assert entry["status_code"] == 201
    assert entry["request_body"] == ""
    assert password not in json.dumps(entry)
