"""
Tests for the public profile chat group badges feature.

Covers:
  - Admin global toggle (profile_group_badges_enabled)
  - User toggle visibility for specific group badges
  - Badges hidden by default
  - State reset on leave and rejoin
  - Profile page shows/hides group badges based on settings
  - Account settings page shows toggle UI when feature enabled
  - Admin toggle hidden when groups/user_profiles plugins disabled
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Fresh temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr(config, "UPLOAD_FOLDER", upload_dir)
    import db as db_mod
    db_mod.init_db()
    # Enable all first-party plugins so feature-level tests work
    from plugin_loader import discover_plugins
    for _dir, manifest, is_builtin in discover_plugins():
        if is_builtin:
            db_mod.register_plugin(
                manifest["id"],
                name=manifest.get("name", manifest["id"]),
                version=manifest.get("version", "0.0.0"),
                author=manifest.get("author", "BananaWiki"),
                description=manifest.get("description", ""),
                builtin=True,
                enabled=True,
            )
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear rate limiter between tests."""
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def client():
    """Flask test client with CSRF disabled."""
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_uid():
    """Create an admin user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def editor_uid():
    """Create an editor user."""
    from werkzeug.security import generate_password_hash
    import db
    return db.create_user("editor", generate_password_hash("editor123"), role="editor")


@pytest.fixture
def regular_uid():
    """Create a regular user."""
    from werkzeug.security import generate_password_hash
    import db
    return db.create_user("alice", generate_password_hash("alice123"), role="user")


@pytest.fixture
def logged_in_admin(client, admin_uid):
    """Log in as admin and return the client."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, admin_uid, editor_uid):
    """Log in as editor and return the client."""
    client.post("/login", data={"username": "editor", "password": "editor123"})
    return client


@pytest.fixture
def logged_in_user(client, admin_uid, regular_uid):
    """Log in as regular user and return the client."""
    client.post("/login", data={"username": "alice", "password": "alice123"})
    return client


# ---------------------------------------------------------------------------
# Database-level tests
# ---------------------------------------------------------------------------

class TestDbProfileGroupBadges:
    """Tests for the db-level profile group badge functions."""

    def test_badges_hidden_by_default(self, admin_uid):
        """Group badges should be hidden by default (no rows)."""
        import db
        db.create_group_chat("TestGroup", admin_uid)
        badges = db.get_profile_group_badges(admin_uid)
        assert badges == []

    def test_set_visible_and_retrieve(self, admin_uid):
        """Setting a group badge to visible should make it retrievable."""
        import db
        group = db.create_group_chat("TestGroup", admin_uid)
        db.set_profile_group_badge_visible(admin_uid, group["id"], True)
        badges = db.get_profile_group_badges(admin_uid)
        assert len(badges) == 1
        assert badges[0]["group_name"] == "TestGroup"

    def test_set_hidden_after_visible(self, admin_uid):
        """Toggling a badge back to hidden should remove it from visible list."""
        import db
        group = db.create_group_chat("TestGroup", admin_uid)
        db.set_profile_group_badge_visible(admin_uid, group["id"], True)
        db.set_profile_group_badge_visible(admin_uid, group["id"], False)
        badges = db.get_profile_group_badges(admin_uid)
        assert badges == []

    def test_clear_on_leave_and_rejoin(self, admin_uid, editor_uid):
        """Leaving a group should clear badge; rejoining defaults to private."""
        import db
        group = db.create_group_chat("TestGroup", admin_uid)
        db.add_group_member(group["id"], editor_uid)
        db.set_profile_group_badge_visible(editor_uid, group["id"], True)
        # Verify visible
        assert len(db.get_profile_group_badges(editor_uid)) == 1
        # Leave group
        db.remove_group_member(group["id"], editor_uid)
        # Badge entry should be cleared
        assert len(db.get_profile_group_badges(editor_uid)) == 0
        # Rejoin
        db.add_group_member(group["id"], editor_uid)
        # Should default to private (no visible badges)
        assert len(db.get_profile_group_badges(editor_uid)) == 0

    def test_get_user_profile_group_settings(self, admin_uid, editor_uid):
        """Profile group settings should list all groups with visibility flags."""
        import db
        g1 = db.create_group_chat("Alpha", admin_uid)
        g2 = db.create_group_chat("Beta", admin_uid)
        db.add_group_member(g1["id"], editor_uid)
        db.add_group_member(g2["id"], editor_uid)
        db.set_profile_group_badge_visible(editor_uid, g1["id"], True)
        settings = db.get_user_profile_group_settings(editor_uid)
        assert len(settings) == 2
        by_name = {r["group_name"]: r["visible"] for r in settings}
        assert by_name["Alpha"] == 1
        assert by_name["Beta"] == 0

    def test_only_member_groups_shown(self, admin_uid, editor_uid):
        """Settings should only include groups the user is a member of."""
        import db
        g1 = db.create_group_chat("Members", admin_uid)
        db.create_group_chat("NotMember", admin_uid)
        db.add_group_member(g1["id"], editor_uid)
        # editor is NOT a member of g2
        settings = db.get_user_profile_group_settings(editor_uid)
        names = [r["group_name"] for r in settings]
        assert "Members" in names
        assert "NotMember" not in names

    def test_banned_member_not_shown(self, admin_uid, editor_uid):
        """Banned members should not have visible group badges."""
        import db
        group = db.create_group_chat("TestGroup", admin_uid)
        db.add_group_member(group["id"], editor_uid)
        db.set_profile_group_badge_visible(editor_uid, group["id"], True)
        db.ban_group_member(group["id"], editor_uid)
        badges = db.get_profile_group_badges(editor_uid)
        assert badges == []
        settings = db.get_user_profile_group_settings(editor_uid)
        assert settings == []


# ---------------------------------------------------------------------------
# Admin toggle tests
# ---------------------------------------------------------------------------

class TestAdminToggle:
    """Tests for the admin global toggle for profile group badges."""

    def test_setting_default_disabled(self, admin_uid):
        """The profile_group_badges_enabled setting should be disabled by default."""
        import db
        settings = db.get_site_settings()
        assert settings["profile_group_badges_enabled"] == 0

    def test_admin_can_enable(self, logged_in_admin, admin_uid):
        """Admin should be able to enable profile group badges."""
        import db
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "Test",
            "primary_color": "#8fa0d4",
            "secondary_color": "#1e1e2c",
            "accent_color": "#7e9ada",
            "text_color": "#c8ccd8",
            "sidebar_color": "#1a1a24",
            "bg_color": "#16161f",
            "light_primary_color": "#4b63b6",
            "light_secondary_color": "#ffffff",
            "light_accent_color": "#3553c7",
            "light_text_color": "#202534",
            "light_sidebar_color": "#e9edf5",
            "light_bg_color": "#f6f7fb",
            "default_theme_mode": "dark",
            "timezone": "UTC",
            "profile_group_badges_enabled": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["profile_group_badges_enabled"] == 1

    def test_admin_can_disable(self, logged_in_admin, admin_uid):
        """Admin should be able to disable profile group badges."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "Test",
            "primary_color": "#8fa0d4",
            "secondary_color": "#1e1e2c",
            "accent_color": "#7e9ada",
            "text_color": "#c8ccd8",
            "sidebar_color": "#1a1a24",
            "bg_color": "#16161f",
            "light_primary_color": "#4b63b6",
            "light_secondary_color": "#ffffff",
            "light_accent_color": "#3553c7",
            "light_text_color": "#202534",
            "light_sidebar_color": "#e9edf5",
            "light_bg_color": "#f6f7fb",
            "default_theme_mode": "dark",
            "timezone": "UTC",
            # profile_group_badges_enabled NOT submitted = disabled
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["profile_group_badges_enabled"] == 0

    def test_admin_settings_shows_toggle_when_plugins_enabled(self, logged_in_admin):
        """Toggle should be visible on admin settings when groups and user_profiles plugins are enabled."""
        resp = logged_in_admin.get("/global-settings")
        assert b"profile_group_badges_enabled" in resp.data

    def test_admin_settings_hides_toggle_when_chat_disabled(self, logged_in_admin):
        """Toggle should be hidden when chat plugin is disabled (groups is merged into chat)."""
        import db
        db.disable_plugin("chat")
        resp = logged_in_admin.get("/global-settings")
        assert b"profile_group_badges_enabled" not in resp.data

    def test_admin_settings_hides_toggle_when_user_profiles_disabled(self, logged_in_admin):
        """Toggle should be hidden when user_profiles plugin is disabled."""
        import db
        db.disable_plugin("user_profiles")
        resp = logged_in_admin.get("/global-settings")
        assert b"profile_group_badges_enabled" not in resp.data


# ---------------------------------------------------------------------------
# User toggle tests (account settings)
# ---------------------------------------------------------------------------

class TestUserToggle:
    """Tests for user toggling group badge visibility."""

    def test_toggle_group_badge_visible(self, logged_in_user, admin_uid, regular_uid):
        """User should be able to set a group badge to visible."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        group = db.create_group_chat("TestGroup", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        resp = logged_in_user.post("/settings", data={
            "action": "toggle_group_badge",
            "group_id": str(group["id"]),
            "visible": "1",
        }, follow_redirects=True)
        assert b"successfully updated" in resp.data
        badges = db.get_profile_group_badges(regular_uid)
        assert len(badges) == 1

    def test_toggle_group_badge_hidden(self, logged_in_user, admin_uid, regular_uid):
        """User should be able to hide a group badge."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        group = db.create_group_chat("TestGroup", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        db.set_profile_group_badge_visible(regular_uid, group["id"], True)
        resp = logged_in_user.post("/settings", data={
            "action": "toggle_group_badge",
            "group_id": str(group["id"]),
            "visible": "0",
        }, follow_redirects=True)
        assert b"successfully updated" in resp.data
        badges = db.get_profile_group_badges(regular_uid)
        assert badges == []

    def test_toggle_requires_feature_enabled(self, logged_in_user, admin_uid, regular_uid):
        """Toggle should fail when feature is globally disabled."""
        import db
        group = db.create_group_chat("TestGroup", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        resp = logged_in_user.post("/settings", data={
            "action": "toggle_group_badge",
            "group_id": str(group["id"]),
            "visible": "1",
        }, follow_redirects=True)
        assert b"not enabled" in resp.data

    def test_toggle_requires_group_membership(self, logged_in_user, admin_uid, regular_uid):
        """Toggle should fail when user is not a member of the group."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        group = db.create_group_chat("TestGroup", admin_uid)
        # regular_uid is NOT a member
        resp = logged_in_user.post("/settings", data={
            "action": "toggle_group_badge",
            "group_id": str(group["id"]),
            "visible": "1",
        }, follow_redirects=True)
        assert b"not a member" in resp.data

    def test_user_settings_shows_group_badges_section(self, logged_in_user, admin_uid, regular_uid):
        """Account settings should show group badges section when feature is enabled."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        group = db.create_group_chat("TestGroup", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        resp = logged_in_user.get("/settings")
        assert b"Profile Group Badges" in resp.data
        assert b"TestGroup" in resp.data

    def test_user_settings_hides_group_badges_when_disabled(self, logged_in_user, admin_uid, regular_uid):
        """Account settings should not show group badges section when feature is disabled."""
        import db
        group = db.create_group_chat("TestGroup", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        resp = logged_in_user.get("/settings")
        assert b'id="profile-group-badges"' not in resp.data

    def test_user_settings_shows_no_groups_message(self, logged_in_user, admin_uid, regular_uid):
        """Account settings should show 'no groups' message when user has no groups."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        resp = logged_in_user.get("/settings")
        assert b"not a member of any chat groups" in resp.data

    def test_toggle_invalid_group_id(self, logged_in_user, admin_uid, regular_uid):
        """Toggle with an invalid group_id should show error."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        resp = logged_in_user.post("/settings", data={
            "action": "toggle_group_badge",
            "group_id": "invalid",
            "visible": "1",
        }, follow_redirects=True)
        assert b"Invalid group" in resp.data


# ---------------------------------------------------------------------------
# Profile display tests
# ---------------------------------------------------------------------------

class TestProfileDisplay:
    """Tests for group badges on the user profile page."""

    def test_profile_shows_group_badges(self, logged_in_admin, admin_uid, regular_uid):
        """Profile should show group badges when feature is enabled and user has visible badges."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        db.upsert_user_profile(regular_uid, page_published=True)
        group = db.create_group_chat("DevTeam", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        db.set_profile_group_badge_visible(regular_uid, group["id"], True)
        resp = logged_in_admin.get("/users/alice")
        assert b"DevTeam" in resp.data
        assert b"Groups" in resp.data

    def test_profile_hides_badges_when_feature_disabled(self, logged_in_admin, admin_uid, regular_uid):
        """Profile should not show group badges when feature is globally disabled."""
        import db
        db.upsert_user_profile(regular_uid, page_published=True)
        group = db.create_group_chat("DevTeam", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        db.set_profile_group_badge_visible(regular_uid, group["id"], True)
        resp = logged_in_admin.get("/users/alice")
        # The "Groups" heading should not appear in profile badges
        assert b'DevTeam' not in resp.data

    def test_profile_hides_private_badges(self, logged_in_admin, admin_uid, regular_uid):
        """Profile should not show group badges that are set to private."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        db.upsert_user_profile(regular_uid, page_published=True)
        group = db.create_group_chat("SecretGroup", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        # Badge is private by default
        resp = logged_in_admin.get("/users/alice")
        assert b"SecretGroup" not in resp.data

    def test_leave_and_rejoin_resets_badge(self, logged_in_admin, admin_uid, regular_uid):
        """Leaving and rejoining should reset badge to private."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        db.upsert_user_profile(regular_uid, page_published=True)
        group = db.create_group_chat("DevTeam", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        db.set_profile_group_badge_visible(regular_uid, group["id"], True)
        # Verify visible
        resp = logged_in_admin.get("/users/alice")
        assert b"DevTeam" in resp.data
        # Leave
        db.remove_group_member(group["id"], regular_uid)
        # Rejoin
        db.add_group_member(group["id"], regular_uid)
        # Should be hidden again
        resp = logged_in_admin.get("/users/alice")
        assert b'DevTeam' not in resp.data

    def test_multiple_group_badges(self, logged_in_admin, admin_uid, regular_uid):
        """Profile should show multiple group badges."""
        import db
        db.update_site_settings(profile_group_badges_enabled=1)
        db.upsert_user_profile(regular_uid, page_published=True)
        g1 = db.create_group_chat("Alpha", admin_uid)
        g2 = db.create_group_chat("Beta", admin_uid)
        g3 = db.create_group_chat("Gamma", admin_uid)
        db.add_group_member(g1["id"], regular_uid)
        db.add_group_member(g2["id"], regular_uid)
        db.add_group_member(g3["id"], regular_uid)
        db.set_profile_group_badge_visible(regular_uid, g1["id"], True)
        db.set_profile_group_badge_visible(regular_uid, g2["id"], True)
        # g3 remains hidden
        resp = logged_in_admin.get("/users/alice")
        assert b"Alpha" in resp.data
        assert b"Beta" in resp.data
        assert b"Gamma" not in resp.data


# ---------------------------------------------------------------------------
# State logic (leave via route)
# ---------------------------------------------------------------------------

class TestLeaveViaRoute:
    """Test that leaving a group via the HTTP route resets badge visibility."""

    def test_leave_route_clears_badge(self, admin_uid, regular_uid):
        """Leaving via /groups/<id>/leave should clear the profile badge."""
        import db
        from app import app

        db.update_site_settings(profile_group_badges_enabled=1)
        group = db.create_group_chat("RouteGroup", admin_uid)
        db.add_group_member(group["id"], regular_uid)
        db.set_profile_group_badge_visible(regular_uid, group["id"], True)
        assert len(db.get_profile_group_badges(regular_uid)) == 1

        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            c.post("/login", data={"username": "alice", "password": "alice123"})
            resp = c.post(f"/groups/{group['id']}/leave", follow_redirects=True)
            assert b"You left the group" in resp.data

        assert len(db.get_profile_group_badges(regular_uid)) == 0
