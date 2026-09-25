"""Tests for the API Service token system and banana mode endpoints."""

import pytest
from werkzeug.security import generate_password_hash

import db


@pytest.fixture(autouse=True)
def enable_api_service_settings():
    """Banana Mode API tests exercise API Service endpoints."""
    db.update_site_settings(api_service_enabled=1)


def _make_admin_token(admin_user):
    """Create an API service token with admin scope for testing."""
    return db.create_api_token(
        admin_user, name="test",
        permissions={"read": True, "write": True, "scopes": ["admin"]}
    )


class TestAPITokenGeneration:
    """Tests for API token generation and management."""

    def test_generate_api_token_returns_raw_token(self, admin_user):
        """create_api_token returns a raw token string."""
        token = db.create_api_token(admin_user)
        assert token is not None
        assert len(token) > 20

    def test_generate_api_token_creates_record(self, admin_user):
        """create_api_token creates a record visible via list_user_tokens."""
        token = db.create_api_token(admin_user)
        assert token is not None
        tokens = db.list_user_tokens(admin_user)
        assert len(tokens) >= 1
        assert tokens[0]["user_id"] == admin_user
        assert tokens[0]["created_at"] is not None

    def test_generate_multiple_tokens(self, admin_user):
        """Multiple tokens can be created for the same user."""
        token1 = db.create_api_token(admin_user, name="first")
        token2 = db.create_api_token(admin_user, name="second")
        assert token1 != token2
        tokens = db.list_user_tokens(admin_user)
        assert len(tokens) >= 2

    def test_revoke_api_token(self, admin_user):
        """revoke_api_service_token deactivates the token."""
        raw = db.create_api_token(admin_user)
        tokens = db.list_user_tokens(admin_user)
        token_id = tokens[0]["id"]
        result = db.revoke_api_service_token(token_id)
        assert result is True
        token_row, user_row = db.verify_api_service_token(raw)
        assert token_row is None

    def test_revoke_nonexistent_token(self, admin_user):
        """revoke_api_service_token returns False for nonexistent id."""
        result = db.revoke_api_service_token(999999)
        assert result is False


class TestAPITokenVerification:
    """Tests for API token verification."""

    def test_verify_valid_admin_token(self, admin_user):
        """verify_api_service_token returns rows for valid admin token."""
        token = db.create_api_token(
            admin_user, permissions={"read": True, "write": True, "scopes": ["admin"]}
        )
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None
        assert user_row is not None
        assert user_row["id"] == admin_user
        assert user_row["role"] == "admin"

    def test_verify_invalid_token(self):
        """verify_api_service_token returns None for invalid token."""
        token_row, user_row = db.verify_api_service_token("invalid_token_12345")
        assert token_row is None
        assert user_row is None

    def test_verify_token_suspended_user(self, admin_user):
        """verify_api_service_token returns None if user is suspended."""
        token = db.create_api_token(admin_user)
        db.update_user(admin_user, suspended=1)
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is None

    def test_verify_token_restored_user(self, admin_user):
        """verify_api_service_token works again after suspension is lifted."""
        token = db.create_api_token(admin_user)
        db.update_user(admin_user, suspended=1)
        assert db.verify_api_service_token(token) == (None, None)
        db.update_user(admin_user, suspended=0)
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None

    def test_verify_token_demoted_user_still_verifies(self, admin_user):
        """verify_api_service_token succeeds even if user lost admin role (scope check is separate)."""
        token = db.create_api_token(
            admin_user, permissions={"read": True, "write": True, "scopes": ["admin"]}
        )
        db.update_user(admin_user, role="editor")
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None
        assert user_row["role"] == "editor"

    def test_admin_token_keeps_its_configured_limits(self, admin_user):
        """An administrator's token cannot exceed its deliberately empty scope list."""
        token = db.create_api_token(
            admin_user, permissions={"read": True, "write": False, "scopes": []}
        )
        token_row, user_row = db.verify_api_service_token(token)
        assert db.token_has_permission(token_row, "admin") is False
        assert db.token_has_permission(token_row, "nonexistent_scope") is False

    def test_token_has_permission_non_admin(self, regular_user):
        """token_has_permission checks scopes for non-admin users."""
        token = db.create_api_token(
            regular_user,
            permissions={"read": True, "write": True, "scopes": ["pages"]}
        )
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None
        assert db.token_has_permission(token_row, "pages") is True
        assert db.token_has_permission(token_row, "admin") is False

    def test_owner_token_verifies(self):
        """verify_api_service_token works for owner users."""
        uid = db.create_user("protadmin", generate_password_hash("test"), role="owner")
        db.update_site_settings(setup_done=1)
        token = db.create_api_token(
            uid, permissions={"read": True, "write": True, "scopes": ["admin"]}
        )
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None
        assert user_row["role"] == "owner"


class TestTokenDeletionOnAccountDelete:
    """Tests for token deletion when account is deleted."""

    def test_tokens_deleted_when_user_deleted(self, admin_user):
        """API tokens are deleted when user account is deleted."""
        raw = db.create_api_token(admin_user)
        tokens = db.list_user_tokens(admin_user)
        assert len(tokens) >= 1
        db.delete_user(admin_user)
        token_row, user_row = db.verify_api_service_token(raw)
        assert token_row is None


class TestAccountAPIPage:
    """Tests for legacy API redirects and the unified API Service page."""

    def test_user_settings_api_requires_login(self, client, admin_user):
        """/settings/api redirects to login when not authenticated."""
        response = client.get("/settings/api")
        assert response.status_code == 302
        assert "/login" in response.location

    def test_user_settings_api_redirects_logged_in_users(self, client, admin_user, editor_user):
        """/settings/api redirects logged-in users to the API token page."""
        client.post("/login", data={"username": "editor", "password": "editor123"})
        response = client.get("/settings/api")
        assert response.status_code == 301
        assert "/settings/api-tokens" in response.location

    def test_user_settings_api_get_redirects_to_tokens(self, logged_in_admin):
        """GET /settings/api redirects to the API token settings page."""
        response = logged_in_admin.get("/settings/api")
        assert response.status_code == 301
        assert "/settings/api-tokens" in response.location

    def test_legacy_banana_page_redirects_to_api_service(self, logged_in_admin):
        """/admin/banana redirects to the unified API Service page."""
        response = logged_in_admin.get("/admin/banana")
        assert response.status_code == 301
        assert "/admin/api-service" in response.location

    def test_api_service_page_shows_banana_mode(self, logged_in_admin):
        """/admin/api-service shows the Banana Mode controls."""
        response = logged_in_admin.get("/admin/api-service")
        assert response.status_code == 200
        assert b"Banana Mode" in response.data

    def test_api_service_page_shows_banana_endpoint_documentation(self, logged_in_admin):
        """/admin/api-service shows Banana Mode API endpoint documentation."""
        response = logged_in_admin.get("/admin/api-service")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Banana Mode" in body
        assert "/api/v1/banana-mode" in body


class TestBananaAPIEndpoint:
    """Tests for POST /api/v1/banana-mode (toggle)."""

    def test_banana_api_no_auth_toggle(self, client, admin_user):
        """POST /api/v1/banana-mode without auth returns 401."""
        response = client.post("/api/v1/banana-mode")
        assert response.status_code == 401
        assert b"Missing or invalid Authorization header" in response.data

    def test_banana_api_invalid_token(self, client, admin_user):
        """POST /api/v1/banana-mode with invalid token returns 401."""
        response = client.post("/api/v1/banana-mode", headers={
            "Authorization": "Bearer invalid_token"
        })
        assert response.status_code == 401
        assert b"Invalid or expired" in response.data

    def test_banana_api_toggle_switches_off_to_on(self, client, admin_user):
        """POST /api/v1/banana-mode switches mode from off to on."""
        db.update_site_settings(banana_mode=0)
        token = _make_admin_token(admin_user)
        response = client.post("/api/v1/banana-mode", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["banana_mode"] is True
        assert data["changed"] is True
        assert "enabled" in data["message"]

    def test_banana_api_toggle_switches_on_to_off(self, client, admin_user):
        """POST /api/v1/banana-mode switches mode from on to off."""
        db.update_site_settings(banana_mode=1)
        token = _make_admin_token(admin_user)
        response = client.post("/api/v1/banana-mode", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["banana_mode"] is False
        assert data["changed"] is True
        assert "disabled" in data["message"]

    def test_banana_api_toggle_repeatedly(self, client, admin_user):
        """POST /api/v1/banana-mode always switches state."""
        db.update_site_settings(banana_mode=0)
        token = _make_admin_token(admin_user)
        resp1 = client.post("/api/v1/banana-mode", headers={"Authorization": f"Bearer {token}"})
        assert resp1.get_json()["banana_mode"] is True
        assert resp1.get_json()["changed"] is True
        resp2 = client.post("/api/v1/banana-mode", headers={"Authorization": f"Bearer {token}"})
        assert resp2.get_json()["banana_mode"] is False
        assert resp2.get_json()["changed"] is True

    def test_banana_api_suspended_user_rejected(self, client, admin_user):
        """POST /api/v1/banana-mode rejects suspended user's token."""
        token = _make_admin_token(admin_user)
        db.update_user(admin_user, suspended=1)
        response = client.post("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 401

    def test_banana_api_demoted_user_rejected(self, client, admin_user):
        """POST /api/v1/banana-mode rejects demoted user's token."""
        token = _make_admin_token(admin_user)
        db.update_user(admin_user, role="editor")
        response = client.post("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 403


class TestBananaModeEffect:
    """Tests for the banana mode rendering effect."""

    def test_banana_mode_non_admin_sees_banana(self, client, admin_user, regular_user):
        """Non-admin users see banana when mode is enabled."""
        db.update_site_settings(banana_mode=1)
        client.post("/login", data={"username": "user", "password": "user123"})
        response = client.get("/")
        assert response.status_code == 200
        assert "🍌" in response.data.decode("utf-8")
        assert b"BananaWiki" not in response.data

    def test_banana_mode_admin_sees_banana_with_buttons(self, logged_in_admin):
        """Admins see banana page with navigation buttons when banana mode is enabled."""
        db.update_site_settings(banana_mode=1)
        response = logged_in_admin.get("/")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "🍌" in body
        assert "API Service Settings" in body
        assert "Logout" in body

    def test_banana_mode_logged_out_sees_banana(self, client, admin_user):
        """Logged-out users see banana with a Login button when mode is enabled."""
        db.update_site_settings(banana_mode=1)
        response = client.get("/page/home")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "🍌" in body
        assert "Login" in body
        assert 'href="/login"' in body

    def test_banana_mode_logged_out_no_logout_button(self, client, admin_user):
        """Logged-out users should NOT see a Logout button on banana screen."""
        db.update_site_settings(banana_mode=1)
        response = client.get("/page/home")
        body = response.data.decode("utf-8")
        assert "Logout" not in body

    def test_banana_mode_api_returns_banana(self, client, admin_user, regular_user):
        """API endpoints return banana error when mode is enabled."""
        db.update_site_settings(banana_mode=1)
        client.post("/login", data={"username": "user", "password": "user123"})
        response = client.get("/api/pages/search?q=test")
        assert response.status_code == 503
        assert "🍌" in response.get_json()["error"]

    def test_banana_mode_off_normal_rendering(self, client, admin_user, regular_user):
        """Site renders normally when banana mode is off."""
        db.update_site_settings(banana_mode=0)
        client.post("/login", data={"username": "user", "password": "user123"})
        response = client.get("/")
        assert response.status_code == 200
        assert b"Home" in response.data or b"Welcome" in response.data

    def test_banana_mode_persists_across_requests(self, client, admin_user):
        """Banana mode state persists in database."""
        token = _make_admin_token(admin_user)
        client.post("/api/v1/banana-mode", headers={"Authorization": f"Bearer {token}"})
        settings = db.get_site_settings()
        assert settings["banana_mode"] == 1
        client.post("/api/v1/banana-mode", headers={"Authorization": f"Bearer {token}"})
        settings = db.get_site_settings()
        assert settings["banana_mode"] == 0


class TestBananaAPIGetStatus:
    """Tests for GET /api/v1/banana-mode (read-only status endpoint)."""

    def test_get_status_no_auth(self, client, admin_user):
        """GET /api/v1/banana-mode without auth returns 401."""
        response = client.get("/api/v1/banana-mode")
        assert response.status_code == 401
        assert b"Missing or invalid Authorization header" in response.data

    def test_get_status_invalid_token(self, client, admin_user):
        """GET /api/v1/banana-mode with invalid token returns 401."""
        response = client.get("/api/v1/banana-mode", headers={
            "Authorization": "Bearer invalid_token"
        })
        assert response.status_code == 401

    def test_get_status_returns_off(self, client, admin_user):
        """GET /api/v1/banana-mode returns false when banana mode is off."""
        db.update_site_settings(banana_mode=0)
        token = _make_admin_token(admin_user)
        response = client.get("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["banana_mode"] is False

    def test_get_status_returns_on(self, client, admin_user):
        """GET /api/v1/banana-mode returns true when banana mode is on."""
        db.update_site_settings(banana_mode=1)
        token = _make_admin_token(admin_user)
        response = client.get("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["banana_mode"] is True

    def test_get_status_no_side_effects(self, client, admin_user):
        """GET /api/v1/banana-mode does not change the state."""
        db.update_site_settings(banana_mode=0)
        token = _make_admin_token(admin_user)
        for _ in range(3):
            response = client.get("/api/v1/banana-mode", headers={
                "Authorization": f"Bearer {token}"
            })
            assert response.get_json()["banana_mode"] is False
        settings = db.get_site_settings()
        assert settings["banana_mode"] == 0

    def test_get_status_during_maintenance(self, client, admin_user):
        """GET /api/v1/banana-mode works during maintenance with valid admin token."""
        db.update_site_settings(maintenance_mode=1, banana_mode=1)
        token = _make_admin_token(admin_user)
        response = client.get("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        assert response.get_json()["banana_mode"] is True

    def test_get_status_during_banana_mode(self, client, admin_user):
        """GET /api/v1/banana-mode is accessible even when banana mode is active."""
        db.update_site_settings(banana_mode=1)
        token = _make_admin_token(admin_user)
        response = client.get("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        assert response.get_json()["banana_mode"] is True

    def test_get_status_suspended_user_rejected(self, client, admin_user):
        """GET /api/v1/banana-mode with suspended admin's token returns 401."""
        token = _make_admin_token(admin_user)
        db.update_user(admin_user, suspended=1)
        response = client.get("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 401


class TestBananaModeAdminAccess:
    """Tests ensuring admins can access banana settings page when banana mode is on."""

    def test_admin_can_access_banana_page_in_banana_mode(self, logged_in_admin):
        """Admin can access the unified API Service page from /admin/banana in banana mode."""
        db.update_site_settings(banana_mode=1)
        response = logged_in_admin.get("/admin/banana", follow_redirects=True)
        assert response.status_code == 200
        assert b"API Service" in response.data
        assert b"Banana Mode" in response.data

    def test_admin_can_access_user_settings_api_in_banana_mode(self, logged_in_admin):
        """Admin can GET /settings/api for token ops even in banana mode."""
        db.update_site_settings(banana_mode=1)
        response = logged_in_admin.get("/settings/api")
        assert response.status_code == 301
        assert "/settings/api-tokens" in response.location

    def test_admin_sees_banana_on_regular_pages(self, logged_in_admin):
        """Admin sees banana page (with buttons) on regular pages when mode is on."""
        db.update_site_settings(banana_mode=1)
        response = logged_in_admin.get("/")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "🍌" in body
        assert "API Service Settings" in body

    def test_admin_sees_banana_on_admin_pages(self, logged_in_admin):
        """Admin sees banana page on admin settings too (except /admin/banana)."""
        db.update_site_settings(banana_mode=1)
        response = logged_in_admin.get("/global-settings")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "🍌" in body

    def test_no_banana_banner_anywhere(self, logged_in_admin):
        """The old 'Banana Mode is ACTIVE' banner should not appear."""
        db.update_site_settings(banana_mode=1)
        response = logged_in_admin.get("/admin/api-service")
        assert response.status_code == 200
        assert b"Banana Mode is ACTIVE" not in response.data

    def test_login_page_not_affected_by_banana_mode(self, client, admin_user):
        """Login page should be accessible even when banana mode is on."""
        db.update_site_settings(banana_mode=1)
        response = client.get("/login")
        assert response.status_code == 200
        assert b"Login" in response.data or b"login" in response.data.lower()
        assert b"banana-wrap" not in response.data

    def test_signup_page_not_affected_by_banana_mode(self, client, admin_user):
        """Signup page should be accessible even when banana mode is on."""
        db.update_site_settings(banana_mode=1)
        response = client.get("/signup")
        assert response.status_code in (200, 302)
        if response.status_code == 200:
            assert b"banana-wrap" not in response.data

    def test_session_conflict_not_affected_by_banana_mode(self, client, admin_user):
        """Session conflict page should be accessible even when banana mode is on."""
        db.update_site_settings(banana_mode=1)
        response = client.get("/session-conflict")
        assert response.status_code in (200, 302)
        if response.status_code == 200:
            assert b"banana-wrap" not in response.data

    def test_maintenance_not_affected_by_banana_mode(self, client, admin_user):
        """Maintenance page should be accessible even when banana mode is on."""
        db.update_site_settings(banana_mode=1, maintenance_mode=1)
        response = client.get("/maintenance")
        assert response.status_code == 200
        assert b"banana-wrap" not in response.data


class TestEdgeCase:
    """Edge case tests."""

    def test_editor_can_access_user_settings_api_redirect(self, client, admin_user, editor_user):
        """Editors can use the unified API token redirect."""
        client.post("/login", data={"username": "editor", "password": "editor123"})
        response = client.get("/settings/api")
        assert response.status_code == 301
        assert "/settings/api-tokens" in response.location

    def test_regular_user_can_access_user_settings_api_redirect(self, client, admin_user, regular_user):
        """Regular users can use the unified API token redirect."""
        client.post("/login", data={"username": "user", "password": "user123"})
        response = client.get("/settings/api")
        assert response.status_code == 301
        assert "/settings/api-tokens" in response.location

    def test_no_token_for_non_admin(self, regular_user):
        """Non-admin user tokens verify but lack admin scope."""
        token = db.create_api_token(regular_user)
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None
        assert db.token_has_permission(token_row, "admin") is False


class TestBananaAPIMaintenance:
    """Tests for banana API during maintenance mode."""

    def test_banana_api_toggle_works_during_maintenance(self, client, admin_user):
        """POST /api/v1/banana-mode with valid admin token succeeds during maintenance."""
        db.update_site_settings(maintenance_mode=1)
        token = _make_admin_token(admin_user)
        response = client.post("/api/v1/banana-mode", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

    def test_banana_api_invalid_token_during_maintenance(self, client, admin_user):
        """POST /api/v1/banana-mode with invalid token returns 401 during maintenance."""
        db.update_site_settings(maintenance_mode=1)
        response = client.post("/api/v1/banana-mode", headers={
            "Authorization": "Bearer invalid_token"
        })
        assert response.status_code == 401

    def test_banana_api_no_auth_during_maintenance(self, client, admin_user):
        """POST /api/v1/banana-mode without auth returns 401 during maintenance."""
        db.update_site_settings(maintenance_mode=1)
        response = client.post("/api/v1/banana-mode")
        assert response.status_code == 401

    def test_other_api_still_blocked_during_maintenance(self, client, admin_user):
        """Non-banana API endpoints still return 403 during maintenance."""
        db.update_site_settings(maintenance_mode=1)
        response = client.post(
            "/api/draft/save",
            json={"page_id": 1, "title": "t", "content": "c"},
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 403
        data = response.get_json()
        assert "maintenance" in data["error"].lower()


class TestTokenHMACKeyDerivation:
    """Tests that API token HMAC uses a per-instance key derived from SECRET_KEY."""

    def test_hash_uses_secret_key(self, monkeypatch):
        """_hash_token output changes when config.SECRET_KEY changes."""
        import config
        from db._api_service import _hash_token

        monkeypatch.setattr(config, "SECRET_KEY", "secret-key-aaa")
        hash_a = _hash_token("test-token")

        monkeypatch.setattr(config, "SECRET_KEY", "secret-key-bbb")
        hash_b = _hash_token("test-token")

        assert hash_a != hash_b

    def test_token_not_verifiable_after_key_change(self, admin_user, monkeypatch):
        """A token generated with one SECRET_KEY cannot verify with another."""
        import config

        token = db.create_api_token(admin_user)
        assert db.verify_api_service_token(token) != (None, None)

        monkeypatch.setattr(config, "SECRET_KEY", "completely-different-key")
        assert db.verify_api_service_token(token) == (None, None)

    def test_derive_hmac_key_is_deterministic(self, monkeypatch):
        """Same SECRET_KEY always produces the same HMAC key."""
        import config
        from db._api_service import _derive_hmac_key

        monkeypatch.setattr(config, "SECRET_KEY", "fixed-key-123")
        key1 = _derive_hmac_key()
        key2 = _derive_hmac_key()
        assert key1 == key2


class TestAPIV1StatusEndpoint:
    """Tests for the public /api/v1/status endpoint."""

    def test_status_returns_200(self, client, admin_user):
        """/api/v1/status is public and returns 200."""
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "version" in data
        assert "service" in data

    def test_status_no_auth_required(self, client, admin_user):
        """/api/v1/status works without any Authorization header."""
        response = client.get("/api/v1/status")
        assert response.status_code == 200
