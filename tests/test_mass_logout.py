"""
Tests for the admin mass-logout feature and auto-logout scheduler settings.
"""

import pytest
from werkzeug.security import generate_password_hash
import db


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_user():
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def regular_user():
    uid = db.create_user("alice", generate_password_hash("alice123"), role="user")
    return uid


@pytest.fixture
def client(admin_user):
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        c.post("/login", data={"username": "admin", "password": "admin123"})
        yield c


# ---------------------------------------------------------------------------
# DB layer: mass_logout_all_users()
# ---------------------------------------------------------------------------

class TestMassLogoutDB:
    def test_clears_all_session_tokens(self, admin_user, regular_user):
        """mass_logout_all_users() clears every session_token."""
        db.update_user(admin_user, session_token="tok-admin")
        db.update_user(regular_user, session_token="tok-user")

        count = db.mass_logout_all_users()
        assert count == 2

        admin = db.get_user_by_id(admin_user)
        user = db.get_user_by_id(regular_user)
        assert admin["session_token"] is None
        assert user["session_token"] is None

    def test_returns_zero_when_no_tokens(self, admin_user):
        """Returns 0 when no user has an active session token."""
        db.update_user(admin_user, session_token=None)
        count = db.mass_logout_all_users()
        assert count == 0

    def test_only_clears_non_null_tokens(self, admin_user, regular_user):
        """Only rows with a non-NULL session_token are counted."""
        db.update_user(admin_user, session_token="tok-admin")
        # regular_user has no token

        count = db.mass_logout_all_users()
        assert count == 1


# ---------------------------------------------------------------------------
# HTTP route: POST /admin/mass-logout
# ---------------------------------------------------------------------------

class TestMassLogoutRoute:
    def test_mass_logout_redirects_to_admin_users(self, client):
        resp = client.post("/admin/mass-logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/admin/users" in resp.headers["Location"]

    def test_mass_logout_flashes_success(self, client):
        resp = client.post("/admin/mass-logout", follow_redirects=True)
        assert resp.status_code == 200
        assert b"logged out" in resp.data.lower()

    def test_mass_logout_requires_admin(self, admin_user):
        from app import app
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            # Log in as a regular user
            db.create_user("bob", generate_password_hash("bob12345"), role="user")
            c.post("/login", data={"username": "bob", "password": "bob12345"})
            resp = c.post("/admin/mass-logout", follow_redirects=False)
            # Should be forbidden or redirected away from admin
            assert resp.status_code in (302, 403)
            if resp.status_code == 302:
                assert "/admin/users" not in resp.headers.get("Location", "")

    def test_mass_logout_requires_login(self):
        from app import app
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            resp = c.post("/admin/mass-logout", follow_redirects=False)
            assert resp.status_code in (302, 403)

    def test_mass_logout_clears_other_sessions(self, client, admin_user, regular_user):
        """After mass logout, regular_user's session token is cleared."""
        db.update_user(regular_user, session_token="tok-b")

        client.post("/admin/mass-logout")

        u = db.get_user_by_id(regular_user)
        assert u["session_token"] is None


# ---------------------------------------------------------------------------
# Settings: auto_logout_enabled and auto_logout_hour
# ---------------------------------------------------------------------------

class TestAutoLogoutSettings:
    def test_settings_defaults(self, admin_user):
        """auto_logout_enabled defaults to 0 and auto_logout_hour to 0."""
        settings = db.get_site_settings()
        assert settings["auto_logout_enabled"] == 0
        assert settings["auto_logout_hour"] == 0

    def test_save_auto_logout_settings(self, admin_user):
        db.update_site_settings(auto_logout_enabled=1, auto_logout_hour=3)
        settings = db.get_site_settings()
        assert settings["auto_logout_enabled"] == 1
        assert settings["auto_logout_hour"] == 3

    def test_settings_page_renders_auto_logout_section(self, client):
        resp = client.get("/global-settings")
        assert resp.status_code == 200
        assert b"Automatic Mass Logout" in resp.data
        assert b"auto_logout_enabled" in resp.data
        assert b"auto_logout_hour" in resp.data

    def test_save_auto_logout_via_form(self, client):
        """Submitting the settings form persists auto-logout settings."""
        settings_before = db.get_site_settings()
        resp = client.post("/global-settings", data={
            "site_name": settings_before["site_name"] or "BananaWiki",
            "timezone": settings_before["timezone"] or "UTC",
            "default_theme_mode": "dark",
            "auto_logout_enabled": "1",
            "auto_logout_hour": "0",
            # supply required colour fields
            "primary_color": settings_before["primary_color"] or "#8fa0d4",
            "secondary_color": settings_before["secondary_color"] or "#151520",
            "accent_color": settings_before["accent_color"] or "#7c8dc6",
            "text_color": settings_before["text_color"] or "#c8ccd8",
            "sidebar_color": settings_before["sidebar_color"] or "#1a1a24",
            "bg_color": settings_before["bg_color"] or "#16161f",
            "light_primary_color": settings_before.get("light_primary_color") or "#4b63b6",
            "light_secondary_color": settings_before.get("light_secondary_color") or "#ffffff",
            "light_accent_color": settings_before.get("light_accent_color") or "#3553c7",
            "light_text_color": settings_before.get("light_text_color") or "#202534",
            "light_sidebar_color": settings_before.get("light_sidebar_color") or "#e9edf5",
            "light_bg_color": settings_before.get("light_bg_color") or "#f6f7fb",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings_after = db.get_site_settings()
        assert settings_after["auto_logout_enabled"] == 1
        assert settings_after["auto_logout_hour"] == 0

    def test_auto_logout_disabled_via_form(self, client):
        """Omitting auto_logout_enabled checkbox sets it to 0."""
        db.update_site_settings(auto_logout_enabled=1, auto_logout_hour=12)
        settings_before = db.get_site_settings()
        client.post("/global-settings", data={
            "site_name": settings_before["site_name"] or "BananaWiki",
            "timezone": "UTC",
            "default_theme_mode": "dark",
            # auto_logout_enabled intentionally omitted → unchecked
            "auto_logout_hour": "12",
            "primary_color": "#8fa0d4",
            "secondary_color": "#151520",
            "accent_color": "#7c8dc6",
            "text_color": "#c8ccd8",
            "sidebar_color": "#1a1a24",
            "bg_color": "#16161f",
            "light_primary_color": "#4b63b6",
            "light_secondary_color": "#ffffff",
            "light_accent_color": "#3553c7",
            "light_text_color": "#202534",
            "light_sidebar_color": "#e9edf5",
            "light_bg_color": "#f6f7fb",
        }, follow_redirects=True)
        settings_after = db.get_site_settings()
        assert settings_after["auto_logout_enabled"] == 0

    def test_auto_logout_hour_clamped(self, client):
        """auto_logout_hour is clamped to 0–23."""
        settings_before = db.get_site_settings()
        client.post("/global-settings", data={
            "site_name": settings_before["site_name"] or "BananaWiki",
            "timezone": "UTC",
            "default_theme_mode": "dark",
            "auto_logout_enabled": "1",
            "auto_logout_hour": "99",  # out of range
            "primary_color": "#8fa0d4",
            "secondary_color": "#151520",
            "accent_color": "#7c8dc6",
            "text_color": "#c8ccd8",
            "sidebar_color": "#1a1a24",
            "bg_color": "#16161f",
            "light_primary_color": "#4b63b6",
            "light_secondary_color": "#ffffff",
            "light_accent_color": "#3553c7",
            "light_text_color": "#202534",
            "light_sidebar_color": "#e9edf5",
            "light_bg_color": "#f6f7fb",
        }, follow_redirects=True)
        settings_after = db.get_site_settings()
        assert settings_after["auto_logout_hour"] == 23

    def test_users_page_shows_mass_logout_button(self, client):
        """Admin users page has the mass-logout form and button."""
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        assert b"Mass Logout" in resp.data
        assert b"admin_mass_logout" in resp.data or b"/admin/mass-logout" in resp.data
