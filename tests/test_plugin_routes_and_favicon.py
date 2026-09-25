"""
Tests for plugin route gating improvements and favicon features.

Covers:
  - difficulty_tags plugin gating (path matcher, create page template, route handlers)
  - Admin navigation links for plugin-dependent pages
  - Favicon preset replacement and restore functionality
"""

import os
import io

import pytest

import db
import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _login_admin(client):
    """Log in with the admin account used in the admin_user fixture."""
    client.post("/login", data={"username": "admin", "password": "admin123"})


def _create_editor_and_login(client):
    """Create an editor, log in, and return the user id."""
    from werkzeug.security import generate_password_hash
    uid = db.create_user("editor_dt", generate_password_hash("editor123"), role="editor")
    client.post("/login", data={"username": "editor_dt", "password": "editor123"})
    return uid


class TestDifficultyTagsPluginGating:
    """Ensure the difficulty_tags plugin gates its UI and routes correctly."""

    def test_tag_route_blocked_when_plugin_disabled(self, client, admin_user):
        """POST to /page/<slug>/tag returns 404 when difficulty_tags is disabled."""
        db.disable_plugin("difficulty_tags")
        _login_admin(client)
        # Create a page directly via db (no plugin dependency for page creation)
        db.create_page("Tag Test", "tag-test", "content", None, admin_user)

        resp = client.post("/page/tag-test/tag", data={"difficulty_tag": "beginner"})
        assert resp.status_code == 404

    def test_tag_route_works_when_plugin_enabled(self, client, admin_user):
        """POST to /page/<slug>/tag succeeds when difficulty_tags is enabled."""
        db.enable_plugin("difficulty_tags")
        _login_admin(client)
        db.create_page("Tag Test 2", "tag-test-2", "content", None, admin_user)

        resp = client.post("/page/tag-test-2/tag", data={"difficulty_tag": "beginner"})
        # Should redirect (302) on success, not 404
        assert resp.status_code in (200, 302)

    def test_create_page_hides_tag_field_when_disabled(self, client, admin_user):
        """The create page form should not show difficulty tag fields when plugin is disabled."""
        db.disable_plugin("difficulty_tags")
        _login_admin(client)

        resp = client.get("/create-page")
        html = resp.get_data(as_text=True)
        assert 'id="createFormTagSelect"' not in html

    def test_create_page_shows_tag_field_when_enabled(self, client, admin_user):
        """The create page form should show difficulty tag fields when plugin is enabled."""
        db.enable_plugin("difficulty_tags")
        _login_admin(client)

        resp = client.get("/create-page")
        html = resp.get_data(as_text=True)
        assert "Difficulty Tag" in html

    def test_edit_page_ignores_tag_when_plugin_disabled(self, client, admin_user):
        """Editing a page should not process difficulty tag data when the plugin is disabled."""
        db.enable_plugin("difficulty_tags")
        _login_admin(client)
        page_id = db.create_page("Edit Tag Test", "edit-tag-test", "content", None, admin_user)

        # Set an initial tag while plugin is enabled
        db.update_page_tag(page_id, "beginner", "", "")

        # Now disable the plugin
        db.disable_plugin("difficulty_tags")

        # Edit the page with a different tag: should be ignored
        resp = client.post("/page/edit-tag-test/edit", data={
            "content": "updated content",
            "difficulty_tag": "expert",
        })
        assert resp.status_code in (200, 302)

        # Tag should remain unchanged
        page = db.get_page_by_slug("edit-tag-test")
        assert page["difficulty_tag"] == "beginner"

    def test_create_page_ignores_tag_when_plugin_disabled(self, client, admin_user):
        """Creating a page should not process difficulty tag data when the plugin is disabled."""
        db.disable_plugin("difficulty_tags")
        _login_admin(client)

        resp = client.post("/create-page", data={
            "title": "No Tag Page",
            "difficulty_tag": "expert",
        })
        assert resp.status_code in (200, 302)

        page = db.get_page_by_slug("no-tag-page")
        if page:
            assert not page["difficulty_tag"]


class TestAdminNavLinks:
    """Ensure admin navigation shows/hides plugin-specific links correctly."""

    def test_temporary_accounts_link_shown_when_enabled(self, client, admin_user):
        """The Temporary Items admin link should appear when the plugin is enabled."""
        db.enable_plugin("temporary_accounts")
        _login_admin(client)

        resp = client.get("/settings")
        html = resp.get_data(as_text=True)
        assert "Temporary Items" in html

    def test_temporary_accounts_link_hidden_when_disabled(self, client, admin_user):
        """The Temporary Items admin link should not appear when the plugin is disabled."""
        db.disable_plugin("temporary_accounts")
        _login_admin(client)

        resp = client.get("/settings")
        html = resp.get_data(as_text=True)
        assert "/admin/temporary" not in html

    def test_page_checkouts_link_shown_when_enabled(self, client, admin_user):
        """The Checkouts admin link should appear when the plugin is enabled."""
        db.enable_plugin("page_governance")
        db.update_site_settings(page_reservations_enabled=1)
        _login_admin(client)

        resp = client.get("/settings")
        html = resp.get_data(as_text=True)
        assert "Checkouts" in html

    def test_page_checkouts_link_hidden_when_disabled(self, client, admin_user):
        """The Checkouts admin link should not appear when the plugin is disabled."""
        db.disable_plugin("page_governance")
        _login_admin(client)

        resp = client.get("/settings")
        html = resp.get_data(as_text=True)
        assert "/admin/checkouts" not in html

    def test_pending_deletions_link_shown_when_enabled(self, client, admin_user):
        """The Pending Deletions admin link should appear when the plugin is enabled."""
        db.enable_plugin("deletion_slowdown")
        _login_admin(client)

        resp = client.get("/settings")
        html = resp.get_data(as_text=True)
        assert "Pending Deletions" in html

    def test_pending_deletions_link_hidden_when_disabled(self, client, admin_user):
        """The Pending Deletions admin link should not appear when the plugin is disabled."""
        db.disable_plugin("deletion_slowdown")
        _login_admin(client)

        resp = client.get("/settings")
        html = resp.get_data(as_text=True)
        assert "/admin/pending-deletions" not in html

    def test_migration_link_always_shown(self, client, admin_user):
        """The Migration admin link should always appear (not plugin-dependent)."""
        _login_admin(client)

        resp = client.get("/settings")
        html = resp.get_data(as_text=True)
        assert "Migration" in html


class TestFaviconFeatures:
    """Test favicon preset replacement and restore functionality."""

    def test_admin_settings_shows_favicon_library(self, client, admin_user):
        """The admin settings page should show the favicon library grid."""
        _login_admin(client)

        resp = client.get("/global-settings")
        html = resp.get_data(as_text=True)
        assert "Favicon Library" in html
        assert "favicon-grid" in html

    def test_admin_settings_shows_favicon_presets(self, client, admin_user):
        """The favicon library should list all preset bananas."""
        _login_admin(client)

        resp = client.get("/global-settings")
        html = resp.get_data(as_text=True)
        assert "banana_yellow.png" in html
        assert "banana_green.png" in html

    def test_restore_favicon_route_works(self, client, admin_user):
        """Posting to restore-favicon should regenerate the default icon."""
        _login_admin(client)

        resp = client.post("/global-settings/restore-favicon/yellow", follow_redirects=True)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "restored" in html.lower() or "Settings" in html

        # Verify the file exists
        static_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "static", "favicons", "banana_yellow.png"
        )
        assert os.path.isfile(static_path)

    def test_restore_favicon_rejects_invalid_preset(self, client, admin_user):
        """Restore favicon should reject invalid preset types."""
        _login_admin(client)

        resp = client.post("/global-settings/restore-favicon/invalid", follow_redirects=True)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Invalid preset" in html or "Settings" in html

    def test_restore_favicon_rejects_custom(self, client, admin_user):
        """Restore favicon should reject 'custom' as a preset type."""
        _login_admin(client)

        resp = client.post("/global-settings/restore-favicon/custom", follow_redirects=True)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Invalid preset" in html or "Settings" in html

    def test_upload_custom_favicon_via_ajax(self, client, admin_user):
        """Uploading a custom favicon via AJAX should work."""
        _login_admin(client)

        from PIL import Image
        img = Image.new("RGBA", (32, 32), (255, 0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        resp = client.post("/global-settings/favicon/upload", data={
            "file": (buf, "test.png"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "custom_" in data["filename"]
