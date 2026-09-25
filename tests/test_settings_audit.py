"""Tests for the three audited settings: public mode, open signup, and PDF export.

This module provides thorough test coverage for:
1. Public mode: unauthenticated visitors can read wiki pages
2. Open signup: users can create accounts without an invite code
3. PDF export: pages can be exported as PDF files (extends existing tests)
"""

import io
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

import db


def _create_page(user_id, title="Test Page", content="# Hello\n\nWorld"):
    """Create a wiki page and return its slug."""
    from helpers._text import slugify
    slug = slugify(title)
    db.create_page(title, slug, content, user_id=user_id)
    return slug


def _settings_post_data(**overrides):
    """Return a base admin settings POST dict with required colour fields."""
    data = {
        "site_name": "Test Wiki",
        "timezone": "UTC",
        "default_theme_mode": "dark",
        "bg_color": "#16161f",
        "sidebar_color": "#1a1a24",
        "secondary_color": "#1e1e2c",
        "text_color": "#c8ccd8",
        "primary_color": "#8fa0d4",
        "accent_color": "#7e9ada",
        "light_bg_color": "#f6f7fb",
        "light_sidebar_color": "#e9edf5",
        "light_secondary_color": "#ffffff",
        "light_text_color": "#202534",
        "light_primary_color": "#4b63b6",
        "light_accent_color": "#3553c7",
    }
    data.update(overrides)
    return data


# ===========================================================================
#  1.  PUBLIC MODE
# ===========================================================================

class TestPublicModeDefault:
    """Public mode should be disabled by default."""

    def test_setting_defaults_to_disabled(self, isolated_db):
        settings = db.get_site_settings()
        assert settings["public_mode"] == 0

    def test_unauthenticated_redirect_when_disabled(self, client, admin_user):
        slug = _create_page(admin_user, "Private Page", "Secret stuff")
        resp = client.get(f"/page/{slug}")
        assert resp.status_code == 302  # redirect to login

    def test_home_redirects_when_disabled(self, client, admin_user):
        resp = client.get("/")
        assert resp.status_code == 302


class TestPublicModeEnabled:
    """When public mode is on, unauthenticated visitors can read pages."""

    def test_admin_can_enable_public_mode(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/global-settings",
                           data=_settings_post_data(public_mode="1"),
                           follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["public_mode"] == 1

    def test_unauthenticated_can_view_page(self, client, admin_user):
        slug = _create_page(admin_user, "Public Page", "Visible content")
        db.update_site_settings(public_mode=1)
        resp = client.get(f"/page/{slug}")
        assert resp.status_code == 200
        assert b"Visible content" in resp.data

    def test_unauthenticated_can_view_home(self, client, admin_user):
        db.update_site_settings(public_mode=1)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_unauthenticated_cannot_edit(self, client, admin_user):
        """Public visitors should not be able to edit pages."""
        slug = _create_page(admin_user, "No Edit", "Content")
        db.update_site_settings(public_mode=1)
        resp = client.get(f"/edit/{slug}")
        # Should redirect to login or return 404 (editor_required gate)
        assert resp.status_code in (302, 404)

    def test_unauthenticated_cannot_create_page(self, client, admin_user):
        """Public visitors should not be able to create pages."""
        db.update_site_settings(public_mode=1)
        resp = client.get("/create")
        # Should redirect to login or return 404 (editor_required gate)
        assert resp.status_code in (302, 404)

    def test_unauthenticated_cannot_access_admin(self, client, admin_user):
        """Public visitors should not be able to access admin panel."""
        db.update_site_settings(public_mode=1)
        resp = client.get("/admin/users")
        assert resp.status_code == 302

    def test_public_mode_with_message(self, client, admin_user):
        """Public mode message should appear for visitors."""
        db.update_site_settings(
            public_mode=1,
            public_mode_show_message=1,
            public_mode_message="Welcome visitors!"
        )
        slug = _create_page(admin_user, "Msg Page", "Content")
        resp = client.get(f"/page/{slug}")
        assert resp.status_code == 200

    def test_public_visitor_lands_on_home_not_login(self, client, admin_user):
        """The public entry point should render the wiki shell, not the login form."""
        db.update_site_settings(public_mode=1)
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"/login" in resp.data
        assert b"login-form" not in resp.data

    def test_public_kanban_requires_explicit_setting(self, client, admin_user):
        board_id = db.kanban_create_board("Public Board", "Visible board", admin_user)
        db.kanban_set_board_visibility(board_id, "public")
        db.update_site_settings(public_mode=1, kanban_public_access_enabled=0)

        resp = client.get("/kanban")
        assert resp.status_code == 302

        db.update_site_settings(kanban_public_access_enabled=1)
        resp = client.get("/kanban")
        assert resp.status_code == 200
        assert b"Public Board" in resp.data

    def test_public_canvas_requires_explicit_setting(self, client, admin_user):
        layout_id = db.canvas_create_layout("Public Canvas", admin_user)
        db.canvas_update_layout(layout_id, visibility="public")
        db.update_site_settings(public_mode=1, canvas_public_access_enabled=0)

        resp = client.get("/canvas")
        assert resp.status_code == 302

        db.update_site_settings(canvas_public_access_enabled=1)
        resp = client.get("/canvas")
        assert resp.status_code == 200
        assert b"Public Canvas" in resp.data

    def test_public_kanban_closes_when_public_mode_expires(self, client, admin_user):
        board_id = db.kanban_create_board("Expired Public Board", "Visible board", admin_user)
        db.kanban_set_board_visibility(board_id, "public")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_site_settings(
            public_mode=1,
            public_mode_until=past,
            kanban_public_access_enabled=1,
        )

        resp = client.get("/kanban")
        assert resp.status_code == 302

    def test_public_canvas_closes_when_public_mode_expires(self, client, admin_user):
        layout_id = db.canvas_create_layout("Expired Public Canvas", admin_user)
        db.canvas_update_layout(layout_id, visibility="public")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_site_settings(
            public_mode=1,
            public_mode_until=past,
            canvas_public_access_enabled=1,
        )

        resp = client.get("/canvas")
        assert resp.status_code == 302

    def test_public_accessibility_uses_cookie(self, client, admin_user):
        db.update_site_settings(public_mode=1)
        resp = client.post("/api/accessibility", json={"theme_mode": "light", "font_scale": 1.2})
        assert resp.status_code == 200
        assert "bw_public_accessibility" in resp.headers.get("Set-Cookie", "")

        prefs = client.get("/api/accessibility").get_json()
        assert prefs["theme_mode"] == "light"
        assert prefs["font_scale"] == 1.2

    def test_public_page_with_contribution_approval_does_not_crash(self, client, admin_user):
        db.enable_plugin("page_governance")
        db.update_site_settings(public_mode=1, contribution_approval_enabled=1)
        slug = _create_page(admin_user, "Public Contribution Page", "Visible content")

        resp = client.get(f"/page/{slug}")

        assert resp.status_code == 200
        assert b"Visible content" in resp.data

    def test_public_mode_keeps_account_routes_private(self, client, admin_user):
        db.update_site_settings(public_mode=1)

        for path in ("/settings", "/settings/export", "/settings/api-tokens", "/users"):
            resp = client.get(path)
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]

    def test_public_mode_allows_public_search_helpers(self, client, admin_user):
        slug = _create_page(admin_user, "Public Searchable Page", "Visible content")
        db.update_site_settings(public_mode=1)

        search_resp = client.get("/api/pages/search?q=Searchable")
        assert search_resp.status_code == 200
        assert any(row["slug"] == slug for row in search_resp.get_json())

        preview_resp = client.get(f"/api/pages/preview-by-slug?slug={slug}")
        assert preview_resp.status_code == 200
        assert preview_resp.get_json()["slug"] == slug

        sidebar_resp = client.get("/api/sidebar/search?q=Searchable")
        assert sidebar_resp.status_code == 200
        assert any(row["slug"] == slug for row in sidebar_resp.get_json()["pages"])

    def test_admin_can_disable_public_mode(self, client, admin_user):
        db.update_site_settings(public_mode=1)
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/global-settings",
                           data=_settings_post_data(),  # no public_mode key
                           follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["public_mode"] == 0

    def test_authenticated_user_still_works(self, client, admin_user):
        """Logged-in users should still work normally in public mode."""
        db.update_site_settings(public_mode=1)
        slug = _create_page(admin_user, "Auth Page", "For logged in")
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.get(f"/page/{slug}")
        assert resp.status_code == 200
        assert b"For logged in" in resp.data


class TestPublicModeExpiration:
    """Public mode with expiration datetime."""

    def test_public_mode_active_before_expiry(self, client, admin_user):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.update_site_settings(public_mode=1, public_mode_until=future)
        slug = _create_page(admin_user, "Timed Page", "Timed content")
        resp = client.get(f"/page/{slug}")
        assert resp.status_code == 200
        assert b"Timed content" in resp.data

    def test_public_mode_inactive_after_expiry(self, client, admin_user):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_site_settings(public_mode=1, public_mode_until=past)
        slug = _create_page(admin_user, "Expired Page", "Hidden")
        resp = client.get(f"/page/{slug}")
        assert resp.status_code == 302  # redirect to login

    def test_is_public_mode_active_helper(self, isolated_db):
        from helpers._auth import is_public_mode_active
        from app import app
        with app.test_request_context():
            # Disabled by default
            assert not is_public_mode_active()

            # Enabled without expiry
            db.update_site_settings(public_mode=1)
            assert is_public_mode_active()

            # Enabled with future expiry
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db.update_site_settings(public_mode=1, public_mode_until=future)
            assert is_public_mode_active()

            # Enabled with past expiry
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            db.update_site_settings(public_mode=1, public_mode_until=past)
            assert not is_public_mode_active()


class TestPublicModeDeindexedPages:
    """Deindexed pages should stay hidden from public visitors."""

    def test_deindexed_page_hidden_from_public(self, client, admin_user):
        slug = _create_page(admin_user, "Deindexed Page", "Secret info")
        page = db.get_page_by_slug(slug)
        db.set_page_deindexed(page["id"], True)
        db.update_site_settings(public_mode=1)
        resp = client.get(f"/page/{slug}")
        # Deindexed pages return 403 or 404 for non-logged-in users
        assert resp.status_code in (403, 404)


# ===========================================================================
#  2.  OPEN SIGNUP (no invite code required)
# ===========================================================================

class TestOpenSignupDefault:
    """Open signup should be disabled by default (invite code required)."""

    def test_setting_defaults_to_disabled(self, isolated_db):
        settings = db.get_site_settings()
        assert settings["open_signup"] == 0

    def test_signup_page_shows_invite_field(self, client, admin_user):
        resp = client.get("/signup")
        assert resp.status_code == 200
        assert b"invite_code" in resp.data
        assert b"Invite Code" in resp.data

    def test_signup_requires_invite_code(self, client, admin_user):
        resp = client.post("/signup", data={
            "username": "newuser",
            "password": "password123",
            "confirm_password": "password123",
            # No invite code
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"required" in resp.data.lower() or b"All fields" in resp.data


class TestOpenSignupEnabled:
    """When open signup is on, users can register without an invite code."""

    def test_admin_can_enable_open_signup(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/global-settings",
                           data=_settings_post_data(open_signup="1"),
                           follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["open_signup"] == 1

    def test_signup_page_hides_invite_field(self, client, admin_user):
        db.update_site_settings(open_signup=1)
        resp = client.get("/signup")
        assert resp.status_code == 200
        assert b"invite_code" not in resp.data

    def test_signup_succeeds_without_invite_code(self, client, admin_user):
        db.update_site_settings(open_signup=1)
        resp = client.post("/signup", data={
            "username": "openuser",
            "password": "password123",
            "confirm_password": "password123",
        }, follow_redirects=True)
        assert resp.status_code == 200
        # Should redirect to login with success message
        assert b"Account created" in resp.data or b"Sign in" in resp.data.lower() or b"sign in" in resp.data
        # Verify user was actually created
        user = db.get_user_by_username("openuser")
        assert user is not None

    def test_signup_still_validates_username(self, client, admin_user):
        db.update_site_settings(open_signup=1)
        # Too short username
        resp = client.post("/signup", data={
            "username": "ab",
            "password": "password123",
            "confirm_password": "password123",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"at least 3" in resp.data

    def test_signup_still_validates_password(self, client, admin_user):
        db.update_site_settings(open_signup=1)
        # Too short password
        resp = client.post("/signup", data={
            "username": "newuser",
            "password": "short",
            "confirm_password": "short",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"at least 8" in resp.data

    def test_signup_still_checks_password_match(self, client, admin_user):
        db.update_site_settings(open_signup=1)
        resp = client.post("/signup", data={
            "username": "newuser",
            "password": "password123",
            "confirm_password": "password456",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"do not match" in resp.data

    def test_signup_still_checks_duplicate_username(self, client, admin_user):
        db.update_site_settings(open_signup=1)
        resp = client.post("/signup", data={
            "username": "admin",
            "password": "password123",
            "confirm_password": "password123",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"already taken" in resp.data

    def test_admin_can_disable_open_signup(self, client, admin_user):
        db.update_site_settings(open_signup=1)
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/global-settings",
                           data=_settings_post_data(),  # no open_signup key
                           follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["open_signup"] == 0


class TestOpenSignupExpiration:
    """Open signup with expiration datetime."""

    def test_open_signup_active_before_expiry(self, client, admin_user):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.update_site_settings(open_signup=1, open_signup_until=future)
        resp = client.get("/signup")
        assert b"invite_code" not in resp.data

    def test_open_signup_inactive_after_expiry(self, client, admin_user):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_site_settings(open_signup=1, open_signup_until=past)
        resp = client.get("/signup")
        assert b"invite_code" in resp.data

    def test_is_open_signup_active_helper(self, isolated_db):
        from helpers._auth import is_open_signup_active
        from app import app
        with app.test_request_context():
            # Disabled by default
            assert not is_open_signup_active()

            # Enabled without expiry
            db.update_site_settings(open_signup=1)
            assert is_open_signup_active()

            # Enabled with future expiry
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db.update_site_settings(open_signup=1, open_signup_until=future)
            assert is_open_signup_active()

            # Enabled with past expiry
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            db.update_site_settings(open_signup=1, open_signup_until=past)
            assert not is_open_signup_active()


class TestTemporaryPublicSettingsPersistence:
    """Admin settings should save normalized future expiry dates only."""

    def test_settings_rejects_past_public_mode_until(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")

        resp = client.post(
            "/global-settings",
            data=_settings_post_data(public_mode="1", public_mode_until=past),
            follow_redirects=True,
        )

        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["public_mode"] == 0
        assert not settings["public_mode_until"]

    def test_settings_rejects_invalid_open_signup_until(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})

        resp = client.post(
            "/global-settings",
            data=_settings_post_data(open_signup="1", open_signup_until="not-a-date"),
            follow_redirects=True,
        )

        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["open_signup"] == 0
        assert not settings["open_signup_until"]

    def test_settings_normalizes_public_and_open_signup_until(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")

        resp = client.post(
            "/global-settings",
            data=_settings_post_data(
                public_mode="1",
                public_mode_until=future,
                open_signup="1",
                open_signup_until=future,
            ),
            follow_redirects=True,
        )

        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["public_mode"] == 1
        assert settings["open_signup"] == 1
        assert settings["public_mode_until"].endswith(":00")
        assert settings["open_signup_until"].endswith(":00")

    def test_settings_page_persists_expired_public_access_cleanup(self, client, admin_user):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_site_settings(
            public_mode=1,
            public_mode_until=past,
            open_signup=1,
            open_signup_until=past,
        )
        client.post("/login", data={"username": "admin", "password": "admin123"})

        resp = client.get("/global-settings")

        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["public_mode"] == 0
        assert settings["public_mode_until"] == ""
        assert settings["open_signup"] == 0
        assert settings["open_signup_until"] == ""


class TestOpenSignupSettingsUI:
    """Admin settings page should show the open signup toggle."""

    def test_settings_page_shows_open_signup_toggle(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.get("/global-settings")
        assert resp.status_code == 200
        assert b"open_signup" in resp.data


# ===========================================================================
#  3.  PDF EXPORT: supplemental tests
# ===========================================================================

class TestPDFExportSettingsIntegration:
    """Integration tests that verify the full admin → export → download flow."""

    def test_full_enable_and_export_flow(self, client, admin_user):
        """Enable via admin settings, then export a page."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        # Enable PDF export through admin settings
        client.post("/global-settings",
                    data=_settings_post_data(pdf_export_enabled="1"),
                    follow_redirects=True)
        # Create a page
        slug = _create_page(admin_user, "Full Flow", "# Title\n\nParagraph text")
        # Export it
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"
        assert resp.data[:5] == b"%PDF-"

    def test_export_link_appears_on_page(self, client, admin_user):
        """The 'Export as PDF' link should be visible when feature is enabled."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        db.update_site_settings(pdf_export_enabled=1)
        slug = _create_page(admin_user, "Link Test", "Content here")
        resp = client.get(f"/page/{slug}")
        assert b"Export as PDF" in resp.data

    def test_export_link_hidden_when_disabled(self, client, admin_user):
        """The 'Export as PDF' link should NOT appear when disabled."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        db.update_site_settings(pdf_export_enabled=0)
        slug = _create_page(admin_user, "No Link", "Content here")
        resp = client.get(f"/page/{slug}")
        assert b"/export-pdf" not in resp.data

    def test_pdf_contains_valid_data(self, client, admin_user):
        """The generated PDF should contain valid PDF structure."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        db.update_site_settings(pdf_export_enabled=1)
        slug = _create_page(admin_user, "Valid PDF",
                            "# Section One\n\nSome paragraph.\n\n## Section Two\n\n- Item 1\n- Item 2")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 200
        # Valid PDF header
        assert resp.data[:5] == b"%PDF-"
        # PDF should have reasonable size
        assert len(resp.data) > 100

    def test_pdf_rate_limiting(self, client, admin_user):
        """PDF export should be rate limited."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        db.update_site_settings(pdf_export_enabled=1)
        slug = _create_page(admin_user, "Rate Test", "Content")
        # Export 5 times (the limit)
        for _ in range(5):
            resp = client.get(f"/page/{slug}/export-pdf")
            assert resp.status_code == 200
        # 6th request should be rate limited
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 429


class TestSettingsCombined:
    """Test interactions between public mode, open signup, and PDF export."""

    def test_public_mode_visitor_cannot_export_pdf(self, client, admin_user):
        """Even with PDF export enabled, public visitors can't download PDFs."""
        db.update_site_settings(public_mode=1, pdf_export_enabled=1)
        slug = _create_page(admin_user, "Public PDF", "Content")
        resp = client.get(f"/page/{slug}/export-pdf")
        # Unauthenticated users should not see export link / get redirected
        assert resp.status_code == 302

    def test_public_mode_page_no_export_link(self, client, admin_user):
        """Public visitors should not see the PDF export link on pages."""
        db.update_site_settings(public_mode=1, pdf_export_enabled=1)
        slug = _create_page(admin_user, "No Export Link", "Content")
        resp = client.get(f"/page/{slug}")
        assert resp.status_code == 200
        assert b"/export-pdf" not in resp.data

    def test_all_three_enabled_simultaneously(self, client, admin_user):
        """All three features can be enabled at the same time."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/global-settings",
                           data=_settings_post_data(
                               public_mode="1",
                               open_signup="1",
                               pdf_export_enabled="1",
                           ),
                           follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["public_mode"] == 1
        assert settings["open_signup"] == 1
        assert settings["pdf_export_enabled"] == 1

    def test_open_signup_user_can_export_pdf_with_permission(self, client, admin_user):
        """A user who signed up via open signup can export PDFs if granted permission."""
        db.update_site_settings(open_signup=1, pdf_export_enabled=1)
        # Sign up without invite code
        client.post("/signup", data={
            "username": "opensignupuser",
            "password": "password123",
            "confirm_password": "password123",
        })
        user = db.get_user_by_username("opensignupuser")
        assert user is not None
        # Grant PDF permission
        perms = db.get_user_permissions(user["id"])
        enabled = set(perms["enabled_permissions"])
        enabled.add("page.export_pdf")
        db.set_user_permissions(user["id"], list(enabled))
        # Log in and export
        client.post("/login", data={"username": "opensignupuser", "password": "password123"})
        slug = _create_page(admin_user, "Export After Signup", "Content")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"


def test_far_future_expiry_west_of_utc_is_a_value_error():
    """Year 9999 read in a zone behind UTC must not escape as OverflowError (a 500)."""
    from routes.admin_common import _normalize_future_settings_datetime

    db.update_site_settings(timezone="America/New_York")
    with pytest.raises(ValueError):
        _normalize_future_settings_datetime("9999-12-31T23:59")
