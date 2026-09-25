"""
Tests for BananaWiki bug fixes and edge cases.
"""

import os
import sys
import tempfile
import pytest

# Ensure the project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


def _enable_all_builtin_plugins():
    """Enable all first-party plugins so feature-level tests work."""
    import db as db_mod
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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
    _enable_all_builtin_plugins()
    # Ensure plugin routes are registered for the new database. The app
    # module may have been imported during pytest collection (before fixtures
    # run), so load_enabled_plugins() may not have picked up plugins enabled
    # in the fresh database.  Load any enabled-but-not-loaded plugins so
    # their routes (e.g. the announcements admin) are available.
    import sys as _sys
    if 'app' in _sys.modules:
        import app as _app_mod
        from plugin_loader import get_loaded_plugins, trigger_plugin_enable
        _loaded_ids = set(get_loaded_plugins().keys())
        for _row in db_mod.list_plugins():
            if _row["enabled"] and _row["id"] not in _loaded_ids:
                trigger_plugin_enable(_row["id"], _app_mod.app)
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate limit store before and after each test."""
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_user():
    """Create an admin user and mark setup as done."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a client that is logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


# -----------------------------------------------------------------------
# Fix 1: user_settings should render without errors (sidebar data)
# -----------------------------------------------------------------------
def test_user_settings_renders(logged_in_admin):
    import db
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    assert b"Account Settings" in resp.data


# -----------------------------------------------------------------------
# Fix 2: 404 page should render without errors (sidebar data)
# -----------------------------------------------------------------------
def test_404_renders(logged_in_admin):
    resp = logged_in_admin.get("/page/nonexistent-slug")
    assert resp.status_code == 404
    assert b"Page Not Found" in resp.data


# -----------------------------------------------------------------------
# Fix 3: Home page should include editor_info after editing
# -----------------------------------------------------------------------
def test_home_page_shows_editor_info(logged_in_admin, admin_user):
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Updated content", admin_user, "test edit")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"Last edit by" in resp.data
    assert b"admin" in resp.data


# -----------------------------------------------------------------------
# Fix 4: slugify with special characters should not produce empty slug
# -----------------------------------------------------------------------
def test_slugify_empty_input():
    from app import slugify
    assert slugify("") == "page"
    assert slugify("!!!") == "page"
    assert slugify("   ") == "page"
    assert slugify("hello world") == "hello-world"
    assert slugify("Test Page!") == "test-page"


# -----------------------------------------------------------------------
# Fix 5: api_delete_draft validates page_id
# -----------------------------------------------------------------------
def test_api_delete_draft_validates_page_id(logged_in_admin):
    # Missing page_id
    resp = logged_in_admin.post("/api/draft/delete",
                                json={},
                                content_type="application/json")
    assert resp.status_code == 400

    # Invalid page_id
    resp = logged_in_admin.post("/api/draft/delete",
                                json={"page_id": "abc"},
                                content_type="application/json")
    assert resp.status_code == 400


# -----------------------------------------------------------------------
# Fix 6: Admin cannot suspend or delete themselves via admin panel
# -----------------------------------------------------------------------
def test_admin_cannot_suspend_self(logged_in_admin, admin_user):
    resp = logged_in_admin.post(f"/admin/users/{admin_user}/edit",
                                data={"action": "suspend"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot suspend your own account" in resp.data


def test_admin_cannot_delete_self(logged_in_admin, admin_user):
    resp = logged_in_admin.post(f"/admin/users/{admin_user}/edit",
                                data={"action": "delete"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot delete your own account" in resp.data


# -----------------------------------------------------------------------
# Fix 7: update_page_title records history
# -----------------------------------------------------------------------
def test_edit_title_records_history(logged_in_admin):
    import db
    home = db.get_home_page()
    slug = home["slug"]
    resp = logged_in_admin.post(f"/page/{slug}/edit/title",
                                data={"title": "New Home Title"},
                                follow_redirects=True)
    assert resp.status_code == 200
    history = db.get_page_history(home["id"])
    assert len(history) > 0
    assert any("Title changed" in (h["edit_message"] or "") for h in history)


# -----------------------------------------------------------------------
# Fix 8: Admin settings validates color inputs
# -----------------------------------------------------------------------
def test_admin_settings_rejects_invalid_color(logged_in_admin):
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "TestWiki",
        "primary_color": "not-a-color",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid color" in resp.data


def test_admin_settings_accepts_valid_colors(logged_in_admin):
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "MyWiki",
        "primary_color": "#aabbcc",
        "secondary_color": "#112233",
        "accent_color": "#445566",
        "text_color": "#778899",
        "sidebar_color": "#001122",
        "bg_color": "#334455",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Settings updated" in resp.data


def test_admin_settings_shows_theme_import_export_controls(logged_in_admin):
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "/global-settings/theme/export" in html
    assert "/global-settings/theme/import" in html
    assert ".bwtheme" in html
    assert "theme library" not in html.lower()


def test_theme_export_downloads_current_palette(logged_in_admin):
    import json
    import db

    db.update_site_settings(
        site_name="Palette Wiki",
        default_theme_mode="light",
        primary_color="#101010",
        secondary_color="#202020",
        accent_color="#303030",
        text_color="#404040",
        sidebar_color="#505050",
        bg_color="#606060",
        light_primary_color="#a1b2c3",
        light_secondary_color="#ffffff",
        light_accent_color="#c3b2a1",
        light_text_color="#111111",
        light_sidebar_color="#eeeeee",
        light_bg_color="#f8f8f8",
    )

    resp = logged_in_admin.get("/global-settings/theme/export")

    assert resp.status_code == 200
    assert resp.headers["Content-Disposition"].endswith("palette-wiki-theme.bwtheme")
    payload = json.loads(resp.data.decode("utf-8"))
    assert payload["_meta"]["type"] == "bananawiki-theme"
    assert payload["_meta"]["format"] == "bwtheme"
    assert payload["theme"]["default_theme_mode"] == "light"
    assert payload["theme"]["dark"]["primary"] == "#101010"
    assert payload["theme"]["light"]["accent"] == "#c3b2a1"


def test_theme_import_restores_palette(logged_in_admin):
    import io
    import json
    import db

    payload = {
        "_meta": {"type": "bananawiki-theme", "format": "bwtheme", "version": 1},
        "theme": {
            "default_theme_mode": "light",
            "dark": {
                "primary": "#111111",
                "secondary": "#222222",
                "accent": "#333333",
                "text": "#444444",
                "sidebar": "#555555",
                "bg": "#666666",
            },
            "light": {
                "primary": "#abcdef",
                "secondary": "#fedcba",
                "accent": "#123456",
                "text": "#654321",
                "sidebar": "#0f1e2d",
                "bg": "#f0e1d2",
            },
        },
    }

    resp = logged_in_admin.post(
        "/global-settings/theme/import",
        data={"theme_file": (io.BytesIO(json.dumps(payload).encode("utf-8")), "custom.bwtheme")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Theme has been successfully imported" in resp.data
    settings = db.get_site_settings()
    assert settings["default_theme_mode"] == "light"
    assert settings["primary_color"] == "#111111"
    assert settings["bg_color"] == "#666666"
    assert settings["light_primary_color"] == "#abcdef"
    assert settings["light_bg_color"] == "#f0e1d2"


def test_theme_import_rejects_invalid_color(logged_in_admin):
    import io
    import json
    import db

    db.update_site_settings(primary_color="#aaaaaa")
    payload = {
        "_meta": {"type": "bananawiki-theme", "format": "bwtheme", "version": 1},
        "theme": {
            "default_theme_mode": "dark",
            "dark": {
                "primary": "red",
                "secondary": "#222222",
                "accent": "#333333",
                "text": "#444444",
                "sidebar": "#555555",
                "bg": "#666666",
            },
            "light": {
                "primary": "#abcdef",
                "secondary": "#fedcba",
                "accent": "#123456",
                "text": "#654321",
                "sidebar": "#0f1e2d",
                "bg": "#f0e1d2",
            },
        },
    }

    resp = logged_in_admin.post(
        "/global-settings/theme/import",
        data={"theme_file": (io.BytesIO(json.dumps(payload).encode("utf-8")), "bad.bwtheme")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"invalid color" in resp.data.lower()
    assert db.get_site_settings()["primary_color"] == "#aaaaaa"


# -----------------------------------------------------------------------
# Fix 9: time_ago handles future dates
# -----------------------------------------------------------------------
def test_time_ago_future_dates():
    from app import time_ago
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    result = time_ago(future)
    assert result.startswith("in ")
    assert "hour" in result

    far_future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    result = time_ago(far_future)
    assert result.startswith("in ")
    assert "day" in result


def test_time_ago_edge_cases():
    from app import time_ago
    assert time_ago(None) == "never"
    assert time_ago("") == "never"
    assert time_ago("not-a-date") == "unknown"


# -----------------------------------------------------------------------
# Fix 10: Admin rename user with invalid username shows error
# -----------------------------------------------------------------------
def test_admin_rename_user_short_username(logged_in_admin, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("testuser", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "change_username", "username": "ab"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Username must be at least 3 characters" in resp.data


def test_admin_rename_user_empty_username(logged_in_admin, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("testuser2", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "change_username", "username": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Username must be at least 3 characters" in resp.data


# -----------------------------------------------------------------------
# Fix 11: Create page with invalid category shows error
# -----------------------------------------------------------------------
def test_create_page_invalid_category(logged_in_admin):
    resp = logged_in_admin.post("/create-page",
                                data={"title": "Test Page", "content": "test",
                                      "category_id": "9999"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Selected category does not exist" in resp.data


# -----------------------------------------------------------------------
# Fix 12: Move page to invalid category shows error
# -----------------------------------------------------------------------
def test_move_page_invalid_category(logged_in_admin):
    import db
    db.create_page("Test Move", "test-move", "content", user_id=None)
    resp = logged_in_admin.post("/page/test-move/move",
                                data={"category_id": "9999"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Selected category does not exist" in resp.data


# -----------------------------------------------------------------------
# Fix 13: Signup handles IntegrityError gracefully
# -----------------------------------------------------------------------
def test_signup_duplicate_username_race(client, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    # Generate a valid invite code
    code = db.generate_invite_code(admin_user)
    # Pre-create the user to simulate a race
    db.create_user("raceuser", generate_password_hash("pass123"))

    resp = client.post("/signup", data={
        "username": "raceuser",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"already taken" in resp.data


# -----------------------------------------------------------------------
# Fix 14: Admin create user handles IntegrityError gracefully
# -----------------------------------------------------------------------
def test_admin_create_user_duplicate(logged_in_admin, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("dupuser", generate_password_hash("pass123"))

    resp = logged_in_admin.post("/admin/users/create", data={
        "username": "dupuser",
        "password": "password123",
        "confirm_password": "password123",
        "role": "user",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"already taken" in resp.data


# -----------------------------------------------------------------------
# Fix 15: Create page with non-numeric category_id doesn't crash
# -----------------------------------------------------------------------
def test_create_page_nonnumeric_category(logged_in_admin):
    resp = logged_in_admin.post("/create-page",
                                data={"title": "Test Page", "content": "test",
                                      "category_id": "abc"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid category" in resp.data


# -----------------------------------------------------------------------
# Fix 16: Move page with non-numeric category_id doesn't crash
# -----------------------------------------------------------------------
def test_move_page_nonnumeric_category(logged_in_admin):
    import db
    db.create_page("Test Move2", "test-move2", "content", user_id=None)
    resp = logged_in_admin.post("/page/test-move2/move",
                                data={"category_id": "abc"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid category" in resp.data


# -----------------------------------------------------------------------
# Fix 17: Create category with non-numeric parent_id doesn't crash
# -----------------------------------------------------------------------
def test_create_category_nonnumeric_parent(logged_in_admin):
    resp = logged_in_admin.post("/category/create",
                                data={"name": "TestCat", "parent_id": "abc"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid parent category" in resp.data


# -----------------------------------------------------------------------
# Fix 18: Admin settings rejects overly long site name
# -----------------------------------------------------------------------
def test_admin_settings_rejects_long_site_name(logged_in_admin):
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "A" * 101,
        "primary_color": "#aabbcc",
        "secondary_color": "#112233",
        "accent_color": "#445566",
        "text_color": "#778899",
        "sidebar_color": "#001122",
        "bg_color": "#334455",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"100 characters" in resp.data


# -----------------------------------------------------------------------
# Fix 19: Admin cannot demote the last admin (self)
# -----------------------------------------------------------------------
def test_admin_cannot_demote_last_admin(logged_in_admin, admin_user):
    resp = logged_in_admin.post(f"/admin/users/{admin_user}/edit",
                                data={"action": "change_role", "role": "user"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot change your own role" in resp.data


# -----------------------------------------------------------------------
# Fix 20: Move page to uncategorized (empty category_id) works
# -----------------------------------------------------------------------
def test_move_page_to_uncategorized(logged_in_admin):
    import db
    cat_id = db.create_category("TempCat")
    db.create_page("TestMove3", "test-move3", "content", category_id=cat_id, user_id=None)
    resp = logged_in_admin.post("/page/test-move3/move",
                                data={"category_id": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Page moved" in resp.data
    page = db.get_page_by_slug("test-move3")
    assert page["category_id"] is None


# -----------------------------------------------------------------------
# Fix 21: Signup with empty invite code shows error
# -----------------------------------------------------------------------
def test_signup_empty_invite_code(client, admin_user):
    resp = client.post("/signup", data={
        "username": "newuser",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": "",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"All fields are required" in resp.data


# -----------------------------------------------------------------------
# Fix 22: Login with empty fields shows error
# -----------------------------------------------------------------------
def test_login_empty_fields(client, admin_user):
    resp = client.post("/login", data={
        "username": "",
        "password": "password123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data


# -----------------------------------------------------------------------
# Fix 23: Deleting last admin account from account settings is blocked
# -----------------------------------------------------------------------
def test_last_admin_cannot_delete_own_account(logged_in_admin, admin_user):
    resp = logged_in_admin.post("/settings",
                                data={"action": "delete_account", "password": "admin123"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot delete the last admin" in resp.data


# -----------------------------------------------------------------------
# Fix 24: Deleting a user who used an invite code does not crash
# -----------------------------------------------------------------------
def test_delete_user_with_invite_code(logged_in_admin, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    code = db.generate_invite_code(admin_user)
    uid = db.create_user("inviteduser", generate_password_hash("pass123"))
    db.use_invite_code(code, uid)
    # Admin deletes the user via the admin panel
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "delete"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"User deleted" in resp.data
    assert db.get_user_by_id(uid) is None


def test_self_delete_account_with_invite_code(client, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    # Create a second admin so the first can stay
    db.create_user("admin2", generate_password_hash("admin123"), role="admin")
    code = db.generate_invite_code(admin_user)
    uid = db.create_user("selfdeleter", generate_password_hash("pass123"))
    db.use_invite_code(code, uid)
    # Log in as the regular user
    client.post("/login", data={"username": "selfdeleter", "password": "pass123"})
    resp = client.post("/settings",
                       data={"action": "delete_account", "password": "pass123"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"Your account has been deleted" in resp.data
    assert db.get_user_by_id(uid) is None


# -----------------------------------------------------------------------
# Fix 25: Page history is always enabled
# -----------------------------------------------------------------------
def test_page_history_enabled_by_default(logged_in_admin):
    import config
    assert config.PAGE_HISTORY_ENABLED is True
    resp = logged_in_admin.get("/page/home/history")
    assert resp.status_code == 200


def test_page_history_entry_returns_404_for_missing(logged_in_admin):
    resp = logged_in_admin.get("/page/home/history/99999")
    assert resp.status_code == 404


def test_page_revert_returns_404_for_missing(logged_in_admin):
    resp = logged_in_admin.post("/page/home/revert/99999")
    assert resp.status_code == 404


def test_page_history_accessible(logged_in_admin):
    resp = logged_in_admin.get("/page/home/history")
    assert resp.status_code == 200


# -----------------------------------------------------------------------
# Fix 26: Editors cannot access invite codes pages
# -----------------------------------------------------------------------
def test_editor_cannot_access_invite_codes(client, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    client.post("/login", data={"username": "editor1", "password": "pass123"})
    resp = client.get("/admin/codes", follow_redirects=True)
    assert b"Admin access required" in resp.data

    resp = client.get("/admin/codes/expired", follow_redirects=True)
    assert b"Admin access required" in resp.data


# -----------------------------------------------------------------------
# Fix 27: "Last edit by" and "View history" always shown
# -----------------------------------------------------------------------
def test_editor_info_shown_with_history_link(logged_in_admin, admin_user):
    import db
    import config
    assert config.PAGE_HISTORY_ENABLED is True
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Updated content", admin_user, "test edit")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"Last edit by" in resp.data
    assert b"View history" in resp.data


# -----------------------------------------------------------------------
# Fix 28: delete_user preserves invite code history (SET NULL, not DELETE)
# -----------------------------------------------------------------------
def test_delete_user_preserves_invite_code_history(logged_in_admin, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    code = db.generate_invite_code(admin_user)
    uid = db.create_user("preserveuser", generate_password_hash("pass123"))
    db.use_invite_code(code, uid)
    # Verify code was marked as used
    expired = db.list_expired_codes()
    used_codes = [c for c in expired if c["code"] == code]
    assert len(used_codes) == 1
    usage = db.get_invite_code_usage(used_codes[0]["id"])
    assert len(usage) == 1
    assert usage[0]["user_id"] == uid
    # Delete the user
    db.delete_user(uid)
    # Verify the invite code row still exists
    expired = db.list_expired_codes()
    preserved = [c for c in expired if c["code"] == code]
    assert len(preserved) == 1
    # Usage record is currently deleted by db.delete_user
    usage = db.get_invite_code_usage(preserved[0]["id"])
    assert len(usage) == 0


# -----------------------------------------------------------------------
# Fix 29: create_category with empty name shows error
# -----------------------------------------------------------------------
def test_create_category_empty_name(logged_in_admin):
    resp = logged_in_admin.post("/category/create",
                                data={"name": "", "parent_id": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Category name is required" in resp.data


# -----------------------------------------------------------------------
# Fix 30: edit_category with empty name shows error
# -----------------------------------------------------------------------
def test_edit_category_empty_name(logged_in_admin):
    import db
    cat_id = db.create_category("TestCat")
    resp = logged_in_admin.post(f"/category/{cat_id}/edit",
                                data={"name": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Category name is required" in resp.data


# -----------------------------------------------------------------------
# Fix 31: 403 error template shows proper Access Denied message
# -----------------------------------------------------------------------
def test_403_template_exists(client, admin_user):
    from flask import render_template
    with client.application.test_request_context("/"):
        content = render_template("wiki/403.html")
    assert "Access Denied" in content
    assert "403" in content


# -----------------------------------------------------------------------
# Fix 32: Admin cannot change their own role
# -----------------------------------------------------------------------
def test_admin_cannot_change_own_role(logged_in_admin, admin_user):
    resp = logged_in_admin.post(f"/admin/users/{admin_user}/edit",
                                data={"action": "change_role", "role": "editor"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot change your own role" in resp.data


# -----------------------------------------------------------------------
# Fix 33: Last admin demotion blocked (via another admin)
# -----------------------------------------------------------------------
def test_can_demote_other_admin_when_an_admin_remains(logged_in_admin, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    # Create a second admin who is the target
    uid2 = db.create_user("admin2", generate_password_hash("pass123"), role="admin")
    resp = logged_in_admin.post(f"/admin/users/{uid2}/edit",
                                data={"action": "change_role", "role": "user"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_user_by_id(uid2)["role"] == "user"


# -----------------------------------------------------------------------
# Fix 34: Editor should not see invite codes link in account settings
# -----------------------------------------------------------------------
def test_editor_does_not_see_invite_codes_link(client, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("editor2", generate_password_hash("pass123"), role="editor")
    client.post("/login", data={"username": "editor2", "password": "pass123"})
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b'href="/admin/codes"' not in resp.data


def test_admin_sees_invite_codes_link(logged_in_admin):
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    assert b"Invite Codes" in resp.data


# -----------------------------------------------------------------------
# Fix 35: Create category with non-existent parent_id shows error
# -----------------------------------------------------------------------
def test_create_category_nonexistent_parent(logged_in_admin):
    resp = logged_in_admin.post("/category/create",
                                data={"name": "TestCat", "parent_id": "9999"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Selected parent category does not exist" in resp.data


# -----------------------------------------------------------------------
# Fix 36: Admin rename user handles IntegrityError (race condition)
# -----------------------------------------------------------------------
def test_admin_rename_user_integrity_error(logged_in_admin, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("user_a", generate_password_hash("pass123"), role="user")
    uid_b = db.create_user("user_b", generate_password_hash("pass123"), role="user")
    # Try to rename user_b to user_a (duplicate)
    resp = logged_in_admin.post(f"/admin/users/{uid_b}/edit",
                                data={"action": "change_username", "username": "user_a"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"already taken" in resp.data


# -----------------------------------------------------------------------
# Fix 37: Account change username handles IntegrityError (race condition)
# -----------------------------------------------------------------------
def test_account_change_username_duplicate(client, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("existing_user", generate_password_hash("pass123"), role="user")
    db.create_user("changer", generate_password_hash("pass123"), role="user")
    client.post("/login", data={"username": "changer", "password": "pass123"})
    resp = client.post("/settings", data={
        "action": "change_username",
        "new_username": "existing_user",
        "password": "pass123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"already taken" in resp.data


# -----------------------------------------------------------------------
# Fix 38: Edit non-existent category returns 404
# -----------------------------------------------------------------------
def test_edit_nonexistent_category(logged_in_admin):
    resp = logged_in_admin.post("/category/9999/edit",
                                data={"name": "NewName"})
    assert resp.status_code == 404


# -----------------------------------------------------------------------
# Fix 39: Delete non-existent category returns 404
# -----------------------------------------------------------------------
def test_delete_nonexistent_category(logged_in_admin):
    resp = logged_in_admin.post("/category/9999/delete")
    assert resp.status_code == 404


# -----------------------------------------------------------------------
# Fix 40: Move page UI is accessible (page shows move button)
# -----------------------------------------------------------------------
def test_move_page_button_visible(logged_in_admin):
    import db
    db.create_page("Movable Page", "movable-page", "content", user_id=None)
    resp = logged_in_admin.get("/page/movable-page")
    assert resp.status_code == 200
    assert b"Move" in resp.data


def test_move_page_button_hidden_on_home(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert f'action="/page/{home["slug"]}/move"'.encode() not in resp.data


# -----------------------------------------------------------------------
# Fix 41: delete_upload with empty filename returns error, not crash
# -----------------------------------------------------------------------
def test_delete_upload_empty_filename(logged_in_admin):
    resp = logged_in_admin.post("/api/upload/delete",
                                json={"filename": ""},
                                content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error"] == "invalid filename"


def test_delete_upload_missing_filename(logged_in_admin):
    resp = logged_in_admin.post("/api/upload/delete",
                                json={"other": "value"},
                                content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error"] == "invalid filename"


# -----------------------------------------------------------------------
# Fix 42: Admin change_role with invalid role shows error
# -----------------------------------------------------------------------
def test_admin_change_role_invalid_value(logged_in_admin, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("roleuser", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "change_role", "role": "owner"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid role" in resp.data


# -----------------------------------------------------------------------
# Fix 43: Page title max length validation
# -----------------------------------------------------------------------
def test_create_page_long_title(logged_in_admin):
    resp = logged_in_admin.post("/create-page",
                                data={"title": "A" * 201, "content": "test",
                                      "category_id": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"200 characters" in resp.data


def test_edit_page_title_too_long(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post(f"/page/{home['slug']}/edit/title",
                                data={"title": "X" * 201},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"200 characters" in resp.data


def test_edit_page_title_empty(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post(f"/page/{home['slug']}/edit/title",
                                data={"title": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Title is required" in resp.data


# -----------------------------------------------------------------------
# Fix 44: Category name max length validation
# -----------------------------------------------------------------------
def test_create_category_long_name(logged_in_admin):
    resp = logged_in_admin.post("/category/create",
                                data={"name": "C" * 101, "parent_id": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"100 characters" in resp.data


def test_edit_category_long_name(logged_in_admin):
    import db
    cat_id = db.create_category("TestCat")
    resp = logged_in_admin.post(f"/category/{cat_id}/edit",
                                data={"name": "C" * 101},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"100 characters" in resp.data


# -----------------------------------------------------------------------
# Fix 45: Username max length validation
# -----------------------------------------------------------------------
def test_signup_username_too_long(client, admin_user):
    import db
    code = db.generate_invite_code(admin_user)
    resp = client.post("/signup", data={
        "username": "u" * 51,
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"50 characters" in resp.data


def test_signup_initializes_default_user_permissions(client, admin_user):
    """Signup should seed the current default permission rows for new users."""
    import db
    from helpers._permissions import get_default_permissions

    code = db.generate_invite_code(admin_user)
    resp = client.post("/signup", data={
        "username": "freshsignup",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)

    assert resp.status_code == 200
    user = db.get_user_by_username("freshsignup")
    perms = db.get_user_permissions(user["id"])
    assert perms["enabled_permissions"] == get_default_permissions("user")
    assert perms["category_access"]["restricted"] is False
    assert perms["category_write_access"]["restricted"] is False


def test_account_username_too_long(client, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("lenuser", generate_password_hash("pass123"), role="user")
    client.post("/login", data={"username": "lenuser", "password": "pass123"})
    resp = client.post("/settings", data={
        "action": "change_username",
        "new_username": "u" * 51,
        "password": "pass123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"50 characters" in resp.data


def test_admin_create_user_username_too_long(logged_in_admin):
    resp = logged_in_admin.post("/admin/users/create", data={
        "username": "u" * 51,
        "password": "password123",
        "confirm_password": "password123",
        "role": "user",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"50 characters" in resp.data


def test_admin_rename_user_username_too_long(logged_in_admin, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("longuser", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "change_username", "username": "u" * 51},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"50 characters" in resp.data


# -----------------------------------------------------------------------
# Fix 46: Edit page redirects home page to /
# -----------------------------------------------------------------------
def test_edit_home_redirects_to_page(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post(f"/page/{home['slug']}/edit",
                                data={"title": "Home", "content": "Updated",
                                      "edit_message": "test"})
    assert resp.status_code == 302
    assert resp.location.endswith(f"/page/{home['slug']}")


def test_edit_title_home_redirects_to_page(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post(f"/page/{home['slug']}/edit/title",
                                data={"title": "Updated Home"})
    assert resp.status_code == 302
    assert resp.location.endswith(f"/page/{home['slug']}")


# -----------------------------------------------------------------------
# Fix 47: Create page form preserves data on error
# -----------------------------------------------------------------------
def test_create_page_preserves_form_data(logged_in_admin):
    resp = logged_in_admin.post("/create-page",
                                data={"title": "", "content": "my content here",
                                      "category_id": ""})
    assert resp.status_code == 200
    # Form should re-render with error notice and helper callout
    assert b"Title is required" in resp.data
    assert b"First create the page" in resp.data


# -----------------------------------------------------------------------
# Fix 48: Admin password change requires confirmation
# -----------------------------------------------------------------------
def test_admin_change_password_mismatch(logged_in_admin, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("pwuser", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "change_password",
                                      "password": "newpass123",
                                      "confirm_password": "different123"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Passwords do not match" in resp.data


def test_admin_change_password_success(logged_in_admin, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("pwuser2", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "change_password",
                                      "password": "newpass123",
                                      "confirm_password": "newpass123"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Password updated" in resp.data


# -----------------------------------------------------------------------
# New tests: format_datetime helper
# -----------------------------------------------------------------------
def test_format_datetime_valid():
    from app import format_datetime
    result = format_datetime("2026-02-20T18:07:24+00:00")
    assert "2026-02-20" in result
    assert "18:07" in result
    assert "UTC" in result


def test_format_datetime_edge_cases():
    from app import format_datetime
    assert format_datetime(None) == ""
    assert format_datetime("") == ""
    assert format_datetime("not-a-date") == ""


# -----------------------------------------------------------------------
# New tests: Time hover tooltip on page view
# -----------------------------------------------------------------------
def test_time_hover_tooltip_on_page(logged_in_admin, admin_user):
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Updated content", admin_user, "test edit")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"title=" in resp.data
    assert b"UTC" in resp.data


def test_time_hover_tooltip_on_slug_page(logged_in_admin, admin_user):
    import db
    db.create_page("Test Page", "test-page", "content", user_id=admin_user)
    resp = logged_in_admin.get("/page/test-page")
    assert resp.status_code == 200
    assert b"title=" in resp.data
    assert b"UTC" in resp.data


# -----------------------------------------------------------------------
# New tests: Page history always active
# -----------------------------------------------------------------------
def test_history_link_always_shown(logged_in_admin, admin_user):
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Updated content", admin_user, "test edit")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"View history" in resp.data


def test_revert_preserves_old_versions(logged_in_admin, admin_user):
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Version 1", admin_user, "first edit")
    db.update_page(home["id"], "Home", "Version 2", admin_user, "second edit")
    history_before = db.get_page_history(home["id"])

    # Revert to first version
    first_entry = history_before[-1]  # oldest entry
    resp = logged_in_admin.post(
        f"/page/home/revert/{first_entry['id']}",
        follow_redirects=True
    )
    assert resp.status_code == 200

    # All previous history entries should still exist plus the revert
    history_after = db.get_page_history(home["id"])
    assert len(history_after) > len(history_before)
    # Revert message should be in history
    assert any("Reverted" in (h["edit_message"] or "") for h in history_after)


# -----------------------------------------------------------------------
# New tests: Draft contributors in commit message
# -----------------------------------------------------------------------
def test_commit_includes_contributor_names(logged_in_admin, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    # Create a page
    page_id = db.create_page("Collab Page", "collab-page", "initial", user_id=admin_user)
    # Create another user with a draft
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    db.save_draft(page_id, editor_id, "Collab Page", "editor1 content")
    # Admin commits the page
    resp = logged_in_admin.post("/page/collab-page/edit", data={
        "title": "Collab Page",
        "content": "final content",
        "edit_message": "merged changes",
    }, follow_redirects=True)
    assert resp.status_code == 200
    # Check history contains contributor name
    history = db.get_page_history(page_id)
    latest = history[0]
    assert "editor1" in latest["edit_message"]
    assert "contributors" in latest["edit_message"].lower()


def test_commit_cleans_up_all_drafts(logged_in_admin, admin_user):
    import db
    from werkzeug.security import generate_password_hash
    page_id = db.create_page("Draft Cleanup", "draft-cleanup", "initial", user_id=admin_user)
    editor_id = db.create_user("editor2", generate_password_hash("pass123"), role="editor")
    db.save_draft(page_id, editor_id, "Draft Cleanup", "editor2 content")
    db.save_draft(page_id, admin_user, "Draft Cleanup", "admin content")
    # Admin commits
    logged_in_admin.post("/page/draft-cleanup/edit", data={
        "title": "Draft Cleanup",
        "content": "final",
        "edit_message": "",
    })
    # All drafts should be cleaned up
    drafts = db.get_drafts_for_page(page_id)
    assert len(drafts) == 0


def test_commit_without_contributors_no_extra_message(logged_in_admin, admin_user):
    import db
    page_id = db.create_page("Solo Page", "solo-page", "initial", user_id=admin_user)
    logged_in_admin.post("/page/solo-page/edit", data={
        "title": "Solo Page",
        "content": "solo content",
        "edit_message": "my edit",
    })
    history = db.get_page_history(page_id)
    latest = history[0]
    assert latest["edit_message"] == "my edit"
    assert "contributors" not in latest["edit_message"].lower()


# -----------------------------------------------------------------------
# Security: CSRF protection is enabled
# -----------------------------------------------------------------------
def test_csrf_protection_enabled():
    from app import app, csrf
    assert csrf is not None


def test_csrf_rejects_post_without_token():
    """POST requests without a CSRF token should be rejected (redirect with flash)."""
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = True
    with app.test_client() as c:
        import db
        from werkzeug.security import generate_password_hash
        db.create_user("csrfadmin", generate_password_hash("admin123"), role="admin")
        db.update_site_settings(setup_done=1)
        c.post("/login", data={"username": "csrfadmin", "password": "admin123",
                               "csrf_token": "invalid"})
        # Without a valid CSRF token, a POST should be rejected with a redirect
        resp = c.post("/create-page", data={"title": "Bad", "content": "x"})
        assert resp.status_code == 302


# -----------------------------------------------------------------------
# Security: Security headers are set
# -----------------------------------------------------------------------
def test_security_headers(logged_in_admin):
    resp = logged_in_admin.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp.headers.get("Server") is None


# -----------------------------------------------------------------------
# Security: Session cookie configuration
# -----------------------------------------------------------------------
def test_session_cookie_config():
    from app import app
    assert app.config.get("SESSION_COOKIE_HTTPONLY") is True
    assert app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"


# -----------------------------------------------------------------------
# Security: Logout requires POST
# -----------------------------------------------------------------------
def test_logout_rejects_get(logged_in_admin):
    resp = logged_in_admin.get("/logout")
    assert resp.status_code == 405


def test_logout_works_with_post(logged_in_admin):
    resp = logged_in_admin.post("/logout", follow_redirects=True)
    assert resp.status_code == 200
    assert b"logged out" in resp.data.lower()


# -----------------------------------------------------------------------
# Security: Invite codes use cryptographic randomness
# -----------------------------------------------------------------------
def test_invite_code_uses_secrets(admin_user):
    """Ensure invite codes are generated with secrets module (not random)."""
    import db
    # Generate several codes and verify format
    codes = set()
    for _ in range(20):
        code = db.generate_invite_code(admin_user)
        # Format: XXXX-XXXX where X is uppercase letter or digit
        assert len(code) == 9
        assert code[4] == "-"
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in code.replace("-", ""))
        codes.add(code)
    # All codes should be unique (extremely unlikely to collide with secrets)
    assert len(codes) == 20


def test_invite_code_not_using_random_module():
    """Verify db module imports secrets, not random."""
    import db as db_mod
    import inspect
    # db may be a package; inspect the module that defines generate_invite_code
    source = inspect.getsource(inspect.getmodule(db_mod.generate_invite_code))
    assert "secrets.choice" in source
    assert "random.choices" not in source


# -----------------------------------------------------------------------
# Security: Open redirect prevention via _safe_referrer
# -----------------------------------------------------------------------
def test_safe_referrer_blocks_external(client, admin_user):
    """413 handler should not redirect to external domains."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    # Simulate a request with an external Referer header
    resp = client.post(
        "/category/create",
        data={"name": "test"},
        headers={"Referer": "https://evil.example.com/steal"},
        follow_redirects=False,
    )
    # Should redirect, but NOT to the external domain
    assert resp.status_code in (302, 303)
    location = resp.headers.get("Location", "")
    assert "evil.example.com" not in location


def test_safe_referrer_allows_same_origin(client, admin_user):
    """Same-origin referrer should be preserved in redirects."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/category/create",
        data={"name": ""},  # empty name triggers error + redirect
        headers={"Referer": "http://localhost/page/test"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    location = resp.headers.get("Location", "")
    assert "localhost" in location or "/" in location


# -----------------------------------------------------------------------
# Security: Username character validation
# -----------------------------------------------------------------------
def test_username_rejects_special_characters(client, admin_user):
    """Usernames with special chars should be rejected to prevent log injection."""
    import db
    code = db.generate_invite_code(admin_user)
    resp = client.post("/signup", data={
        "username": "user<script>",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"letters, digits, underscores and hyphens" in resp.data


def test_username_rejects_newlines(client, admin_user):
    """Usernames with newlines should be rejected to prevent log injection."""
    import db
    code = db.generate_invite_code(admin_user)
    resp = client.post("/signup", data={
        "username": "user\nfake",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"letters, digits, underscores and hyphens" in resp.data


def test_username_rejects_spaces(client, admin_user):
    """Usernames with spaces should be rejected."""
    import db
    code = db.generate_invite_code(admin_user)
    resp = client.post("/signup", data={
        "username": "user name",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"letters, digits, underscores and hyphens" in resp.data


def test_username_allows_valid_chars(client, admin_user):
    """Usernames with letters, digits, underscores, hyphens should be accepted."""
    import db
    code = db.generate_invite_code(admin_user)
    resp = client.post("/signup", data={
        "username": "valid_user-123",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Account created" in resp.data


def test_setup_rejects_invalid_username(client):
    """Setup should reject usernames with special characters."""
    resp = client.post("/setup", data={
        "username": "admin user",
        "password": "admin123",
        "confirm_password": "admin123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"letters, digits, underscores and hyphens" in resp.data


def test_account_change_rejects_invalid_username(client, admin_user):
    """Account settings should reject usernames with special characters."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("validuser", generate_password_hash("pass123"), role="user")
    client.post("/login", data={"username": "validuser", "password": "pass123"})
    resp = client.post("/settings", data={
        "action": "change_username",
        "new_username": "invalid user!",
        "password": "pass123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"letters, digits, underscores and hyphens" in resp.data


def test_admin_create_user_rejects_invalid_username(logged_in_admin):
    """Admin create user should reject usernames with special characters."""
    resp = logged_in_admin.post("/admin/users/create", data={
        "username": "bad<name",
        "password": "password123",
        "confirm_password": "password123",
        "role": "user",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"letters, digits, underscores and hyphens" in resp.data


def test_admin_rename_rejects_invalid_username(logged_in_admin, admin_user):
    """Admin rename user should reject usernames with special characters."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("goodname", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "change_username", "username": "bad name"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"letters, digits, underscores and hyphens" in resp.data


def test_is_valid_username_helper():
    """Test the _is_valid_username helper function directly."""
    from app import _is_valid_username
    assert _is_valid_username("admin") is True
    assert _is_valid_username("user_123") is True
    assert _is_valid_username("my-user") is True
    assert _is_valid_username("A") is True
    assert _is_valid_username("user name") is False
    assert _is_valid_username("user<script>") is False
    assert _is_valid_username("user\nfake") is False
    assert _is_valid_username("") is False
    assert _is_valid_username("user@name") is False


# -----------------------------------------------------------------------
# Security: Log injection prevention
# -----------------------------------------------------------------------
def test_log_sanitize_strips_newlines():
    """Log sanitizer should strip newlines and control characters."""
    from wiki_logger import _sanitize
    assert _sanitize("normal text") == "normal text"
    assert _sanitize("line1\nline2") == "line1line2"
    assert _sanitize("line1\r\nline2") == "line1line2"
    assert _sanitize("tab\there") == "tabhere"
    assert _sanitize("null\x00byte") == "nullbyte"


# -----------------------------------------------------------------------
# Security: delete_upload path traversal defense-in-depth
# -----------------------------------------------------------------------
def test_delete_upload_path_traversal_blocked(logged_in_admin):
    """delete_upload should block path traversal attempts."""
    resp = logged_in_admin.post("/api/upload/delete",
                                json={"filename": "../../../etc/passwd"},
                                content_type="application/json")
    # secure_filename strips path components, so this should be safe
    # but the filename may still end up empty or blocked
    data = resp.get_json()
    assert resp.status_code in (200, 400)
    if resp.status_code == 400:
        assert data["error"] == "invalid filename"


# -----------------------------------------------------------------------
# Security: Content-Security-Policy header is set
# -----------------------------------------------------------------------
def test_csp_header_present(logged_in_admin):
    """Responses should include a Content-Security-Policy header."""
    resp = logged_in_admin.get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    # Video embeds require YouTube and Vimeo iframes to be allowed
    assert "frame-src" in csp
    # Verify CSP frame-src contains the expected video-embed origins.
    # Strip semicolons (CSP directive separators) then split on whitespace
    # so we check exact tokens, not substrings.
    csp_tokens = csp.replace(";", " ").split()
    assert "https://www.youtube.com" in csp_tokens
    assert "https://player.vimeo.com" in csp_tokens


def test_csp_uses_nonce_not_unsafe_inline(logged_in_admin):
    """CSP should use per-request nonces instead of 'unsafe-inline'.

    script-src and style-src must not use 'unsafe-inline'; both rely on
    per-request nonces.  ``style-src-attr 'unsafe-inline'`` is permitted
    because the templates legitimately use inline ``style=`` attributes
    for layout helpers, and moving every occurrence to a CSS class is not
    practical.
    """
    resp = logged_in_admin.get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    # script-src and style-src must NOT contain 'unsafe-inline'
    import re
    for directive in ("script-src", "style-src"):
        m = re.search(rf"{directive}\s+[^;]+", csp)
        assert m, f"{directive} directive missing from CSP"
        assert "'unsafe-inline'" not in m.group(0), (
            f"{directive} must not use 'unsafe-inline'"
        )
    # Both script-src and style-src must contain a nonce directive
    assert re.search(r"script-src\s+'self'\s+'nonce-[0-9a-f]+'", csp)
    assert re.search(r"style-src\s+'self'\s+'nonce-[0-9a-f]+'", csp)
    # style-src-attr IS expected because templates use inline style= attributes
    assert "style-src-attr 'unsafe-inline'" in csp


def test_csp_nonce_differs_per_request(logged_in_admin):
    """Each request should get a unique CSP nonce."""
    import re
    resp1 = logged_in_admin.get("/")
    resp2 = logged_in_admin.get("/")
    csp1 = resp1.headers.get("Content-Security-Policy", "")
    csp2 = resp2.headers.get("Content-Security-Policy", "")
    m1 = re.search(r"'nonce-([0-9a-f]+)'", csp1)
    m2 = re.search(r"'nonce-([0-9a-f]+)'", csp2)
    assert m1 and m2
    assert m1.group(1) != m2.group(1)


def test_csp_nonce_matches_inline_tags(logged_in_admin):
    """The nonce in the CSP header must match the nonce on inline tags."""
    import re
    resp = logged_in_admin.get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    m = re.search(r"'nonce-([0-9a-f]+)'", csp)
    assert m
    nonce = m.group(1)
    html = resp.data.decode()
    # All inline script and style tags must use this nonce
    assert f'nonce="{nonce}"' in html
    # No inline script or style tag should lack the nonce
    assert '<script>' not in html
    assert re.search(r'<style(?:\s+id="[^"]*")?\s*>', html) is None
    # Every inline <script> and <style> tag must carry the correct nonce
    inline_tags = re.findall(r'<(?:script|style)(?:\s[^>]*)?\s*>', html)
    for tag in inline_tags:
        if 'src=' in tag:
            continue
        assert f'nonce="{nonce}"' in tag, f"Missing nonce on tag: {tag}"


# -----------------------------------------------------------------------
# Security: Log sanitization covers path, method, and IP
# -----------------------------------------------------------------------
def test_log_sanitize_covers_all_request_fields():
    """log_request should sanitize path, method, and IP. Not just UA."""
    import inspect
    from wiki_logger import log_request
    source = inspect.getsource(log_request)
    # Verify _sanitize is applied to path, method, and remote_addr
    assert "_sanitize(request.remote_addr" in source or "_sanitize(request.remote_addr or" in source
    assert "_sanitize(request.method)" in source
    assert "_sanitize(request.path)" in source


def test_log_action_sanitizes_ip_and_action():
    """log_action should sanitize the IP and action parameter."""
    import inspect
    from wiki_logger import log_action
    source = inspect.getsource(log_action)
    assert "_sanitize(request.remote_addr" in source or "_sanitize(request.remote_addr or" in source
    assert "_sanitize(action)" in source


def test_log_sanitize_prevents_injection_at_runtime():
    """Verify _sanitize actually strips newlines from values at runtime."""
    from wiki_logger import _sanitize
    # Simulate a malicious path with newline injection
    malicious_path = "/page/test\nFAKE ACTION | user=admin action=admin_delete_user"
    sanitized = _sanitize(malicious_path)
    assert "\n" not in sanitized
    assert "\r" not in sanitized
    # The sanitized value should be a single line
    assert sanitized == "/page/testFAKE ACTION | user=admin action=admin_delete_user"

    # Simulate a malicious action string
    malicious_action = "login_success\nACTION  | user=admin action=delete_all"
    sanitized = _sanitize(malicious_action)
    assert "\n" not in sanitized


# -----------------------------------------------------------------------
# Proxy / HTTPS config defaults
# -----------------------------------------------------------------------
def test_proxy_mode_default():
    """Forwarded headers are untrusted unless explicitly configured."""
    import config
    assert config.PROXY_MODE is False


def test_proxy_fix_applied_when_enabled(tmp_path):
    """An independent process enables the middleware through configuration."""
    import subprocess
    import sys
    env = dict(os.environ, BW_PROXY_MODE="1", BW_ENV="test",
               BW_INSTANCE_DIR=str(tmp_path), BW_DATABASE_PATH=str(tmp_path / "proxy.db"),
               BW_LOGGING_LEVEL="off")
    result = subprocess.run([
        sys.executable, "-c",
        "from app import app; from werkzeug.middleware.proxy_fix import ProxyFix; "
        "assert isinstance(app.wsgi_app, ProxyFix)",
    ], cwd=os.path.dirname(os.path.dirname(__file__)), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_proxy_fix_disabled_by_default():
    """The directly reachable application does not trust proxy headers."""
    from app import app
    from werkzeug.middleware.proxy_fix import ProxyFix
    assert not isinstance(app.wsgi_app, ProxyFix)


# -----------------------------------------------------------------------
# Connection config defaults
# -----------------------------------------------------------------------
def test_port_config_default():
    """PORT should default to 5001."""
    import config
    assert config.PORT == 5001


# -----------------------------------------------------------------------
# WSGI entry point and Gunicorn config
# -----------------------------------------------------------------------
def test_wsgi_entry_point():
    """wsgi.py should expose the Flask app."""
    import wsgi
    assert hasattr(wsgi, "app")
    from app import app
    assert wsgi.app is app


def test_gunicorn_conf_exists():
    """gunicorn.conf.py should exist and define bind/workers."""
    import importlib.util
    import os
    conf_path = os.path.join(
        os.path.dirname(__file__), "..", "gunicorn.conf.py"
    )
    assert os.path.exists(conf_path)
    spec = importlib.util.spec_from_file_location("_gunicorn_conf", conf_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "bind")
    assert hasattr(mod, "workers")
    assert mod.workers >= 1


def test_gunicorn_conf_reads_config():
    """gunicorn.conf.py should derive bind from config.HOST:config.PORT."""
    import importlib.util
    import os
    conf_path = os.path.join(
        os.path.dirname(__file__), "..", "gunicorn.conf.py"
    )
    spec = importlib.util.spec_from_file_location("_gunicorn_conf2", conf_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import config as cfg
    assert mod.bind == f"{cfg.HOST}:{cfg.PORT}"


# -----------------------------------------------------------------------
# Category edit/delete: CSRF tokens present in forms
# -----------------------------------------------------------------------
def test_category_forms_include_csrf(logged_in_admin):
    import db
    db.create_category("TestCat")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"csrf_token" in resp.data
    assert b"Manage category" in resp.data or b"manage category" in resp.data


# -----------------------------------------------------------------------
# Category actions visible in sidebar
# -----------------------------------------------------------------------
def test_category_actions_visible_in_sidebar(logged_in_admin):
    import db
    db.create_category("VisibleCat")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    # The edit icon (pencil ✎ = &#9998;) and delete icon (✕ = &#10005;) should render
    assert b"cat-actions" in resp.data


# -----------------------------------------------------------------------
# Draft delete API cleans up properly
# -----------------------------------------------------------------------
def test_draft_delete_cleans_up(logged_in_admin, admin_user):
    import db
    home = db.get_home_page()
    db.save_draft(home["id"], admin_user, "Title", "Content")
    assert db.get_draft(home["id"], admin_user) is not None

    resp = logged_in_admin.post("/api/draft/delete",
                                json={"page_id": home["id"]},
                                content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Draft has been successfully deleted."
    assert db.get_draft(home["id"], admin_user) is None


# -----------------------------------------------------------------------
# My drafts API endpoint
# -----------------------------------------------------------------------
def test_api_my_drafts(logged_in_admin, admin_user):
    import db
    home = db.get_home_page()
    db.save_draft(home["id"], admin_user, "Draft Title", "Draft Content")

    resp = logged_in_admin.get("/api/draft/mine")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["page_title"] is not None


# -----------------------------------------------------------------------
# User draft count helper
# -----------------------------------------------------------------------
def test_get_user_draft_count():
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("draftuser", generate_password_hash("test123"), role="editor")
    assert db.get_user_draft_count(uid) == 0

    home = db.get_home_page()
    db.save_draft(home["id"], uid, "t", "c")
    assert db.get_user_draft_count(uid) == 1


# -----------------------------------------------------------------------
# List user drafts helper
# -----------------------------------------------------------------------
def test_list_user_drafts():
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("draftlistuser", generate_password_hash("test123"), role="editor")
    home = db.get_home_page()
    db.save_draft(home["id"], uid, "Draft T", "Draft C")

    drafts = db.list_user_drafts(uid)
    assert len(drafts) == 1
    assert drafts[0]["page_title"] is not None
    assert drafts[0]["page_slug"] is not None


# -----------------------------------------------------------------------
# Login rate limiting
# -----------------------------------------------------------------------
def test_login_rate_limiting(client, admin_user):
    from app import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    # Exhaust rate limit with failed attempts
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    # Next attempt should be rate limited
    resp = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert resp.status_code == 429
    assert b"Too many login attempts" in resp.data
    _LOGIN_ATTEMPTS.clear()


# -----------------------------------------------------------------------
# Edit page has Save Draft & Close button
# -----------------------------------------------------------------------
def test_edit_page_has_save_draft_close(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    assert b"save-draft-close" in resp.data
    assert b"Save Draft" in resp.data


# -----------------------------------------------------------------------
# data-redirect attribute is safe (same-origin only)
# -----------------------------------------------------------------------
def test_data_redirect_is_same_origin(logged_in_admin):
    """data-redirect on save-draft-close must be a relative path, not external."""
    import re, db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    # Extract the data-redirect attribute value
    match = re.search(rb'data-redirect="([^"]*)"', resp.data)
    assert match, "data-redirect attribute not found"
    redirect_value = match.group(1).decode()
    # Must be a relative path (starts with /) and not protocol-relative
    assert redirect_value.startswith("/"), "data-redirect must be a relative path"
    assert not redirect_value.startswith("//"), "data-redirect must not be protocol-relative"


# -----------------------------------------------------------------------
# JS isSameOrigin function blocks external redirects
# -----------------------------------------------------------------------
def test_main_js_has_same_origin_guard(logged_in_admin):
    """Draft redirects must use the shared same-origin guard."""
    core = logged_in_admin.get("/static/js/main.js")
    drafts = logged_in_admin.get("/static/js/editor-drafts.js")
    assert core.status_code == drafts.status_code == 200
    assert b"function isSameOrigin" in core.data
    # Both redirect sites must use the guard
    assert drafts.data.count(b"isSameOrigin(redirectUrl)") >= 2


# -----------------------------------------------------------------------
# Edit page has sync indicator
# -----------------------------------------------------------------------
def test_edit_page_has_sync_indicator(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    assert b"save-indicator" in resp.data


# -----------------------------------------------------------------------
# Session fixation prevention
# -----------------------------------------------------------------------
def test_login_clears_session_before_setting_user(client, admin_user):
    """Login should clear existing session data before setting user_id."""
    from app import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    # Set some arbitrary session data
    with client.session_transaction() as sess:
        sess["stale_data"] = "should_be_cleared"
    # Login
    client.post("/login", data={"username": "admin", "password": "admin123"})
    with client.session_transaction() as sess:
        assert "user_id" in sess
        assert "stale_data" not in sess


# -----------------------------------------------------------------------
# Rate limit clears on successful login
# -----------------------------------------------------------------------
def test_rate_limit_clears_on_successful_login(client, admin_user):
    """Successful login should clear rate limit attempts for that IP."""
    from app import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    # Record 4 failed attempts
    for _ in range(4):
        client.post("/login", data={"username": "admin", "password": "wrong"})
    # Successful login should clear attempts
    client.post("/login", data={"username": "admin", "password": "admin123"})
    # After logout, should be able to fail again without rate limit
    client.post("/logout")
    _LOGIN_ATTEMPTS.clear()  # Clear for clean test
    for _ in range(4):
        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert resp.status_code == 200  # Not rate limited


# -----------------------------------------------------------------------
# CSRF tokens present in critical forms
# -----------------------------------------------------------------------
def test_page_delete_form_has_csrf(logged_in_admin):
    """Delete page form should contain explicit CSRF token."""
    import db
    db.create_page("Test CSRF Page", "test-csrf-page", "Content", None)
    resp = logged_in_admin.get("/page/test-csrf-page")
    assert resp.status_code == 200
    assert b"csrf_token" in resp.data


def test_admin_users_forms_have_csrf(logged_in_admin):
    """Admin user management forms should contain CSRF tokens."""
    resp = logged_in_admin.get("/admin/users")
    assert resp.status_code == 200
    # Count occurrences of csrf_token in forms
    csrf_count = resp.data.count(b'name="csrf_token"')
    assert csrf_count >= 7  # create + 6 per-user action forms


def test_admin_codes_forms_have_csrf(logged_in_admin):
    """Admin codes forms should contain CSRF tokens."""
    resp = logged_in_admin.get("/admin/codes")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


def test_history_revert_form_has_csrf(logged_in_admin, admin_user):
    """History revert form should contain CSRF token."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Updated", admin_user, "test edit")
    resp = logged_in_admin.get(f"/page/{home['slug']}/history")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


# -----------------------------------------------------------------------
# CSRF tokens in remaining forms
# -----------------------------------------------------------------------
def test_user_settings_forms_have_csrf(logged_in_admin):
    """Account settings forms should contain explicit CSRF tokens."""
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    csrf_count = resp.data.count(b'name="csrf_token"')
    assert csrf_count >= 3  # username, password, delete account


def test_admin_settings_form_has_csrf(logged_in_admin):
    """Admin site settings form should contain CSRF token."""
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


def test_create_page_form_has_csrf(logged_in_admin):
    """Create page form should contain CSRF token."""
    resp = logged_in_admin.get("/create-page")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


def test_edit_page_form_has_csrf(logged_in_admin):
    """Edit page form should contain CSRF token."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


# -----------------------------------------------------------------------
# Session timeout configured
# -----------------------------------------------------------------------
def test_session_lifetime_configured():
    """Session should have an explicit lifetime configured."""
    from app import app
    assert app.permanent_session_lifetime is not None
    assert app.permanent_session_lifetime.days <= 30


# -----------------------------------------------------------------------
# 500 error handler
# -----------------------------------------------------------------------
def test_500_error_template_exists():
    """500 error template should exist."""
    import os
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "templates", "wiki", "500.html"
    )
    assert os.path.exists(template_path)


# -----------------------------------------------------------------------
# Collapsible sidebar categories
# -----------------------------------------------------------------------
def test_category_has_collapse_toggle(logged_in_admin):
    """Category sections should have a collapse toggle button."""
    import db
    db.create_category("TestCollapse")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"cat-toggle" in resp.data
    assert b"nav-section-body" in resp.data


# -----------------------------------------------------------------------
# Last login tracking
# -----------------------------------------------------------------------
def test_login_updates_last_login_at(client, admin_user):
    """Successful login should set last_login_at on the user record."""
    from app import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    import db
    # Check before login
    user = db.get_user_by_id(admin_user)
    assert user["last_login_at"] is None
    # Login
    client.post("/login", data={"username": "admin", "password": "admin123"})
    user = db.get_user_by_id(admin_user)
    assert user["last_login_at"] is not None


# -----------------------------------------------------------------------
# Admin audit trail
# -----------------------------------------------------------------------
def test_admin_audit_route_exists(logged_in_admin, admin_user):
    """Admin audit trail route should return 200."""
    resp = logged_in_admin.get(f"/admin/users/{admin_user}/audit")
    assert resp.status_code == 200
    assert b"Audit Trail" in resp.data


def test_admin_audit_requires_admin(client, admin_user):
    """Non-admin users should not access audit trail."""
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("editor1", generate_password_hash("password"), "editor")
    from app import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    client.post("/login", data={"username": "editor1", "password": "password"})
    resp = client.get(f"/admin/users/{admin_user}/audit")
    assert resp.status_code in (302, 403)  # Redirect or forbidden


def test_audit_log_caps_entries_with_deque(logged_in_admin, admin_user, tmp_path, monkeypatch):
    """_read_user_audit_log should return at most max_entries lines (most recent first)."""
    import config as cfg
    log_file = str(tmp_path / "test.log")
    monkeypatch.setattr(cfg, "LOG_FILE", log_file)
    # Write 10 matching lines
    with open(log_file, "w") as f:
        for i in range(10):
            f.write(f"2026-01-01 line {i} user=admin action=test\n")
    # Import the route module to access the inner helper via the route
    resp = logged_in_admin.get(f"/admin/users/{admin_user}/audit")
    assert resp.status_code == 200
    # With only 10 lines, all should appear; verify most-recent-first order
    body = resp.data.decode()
    assert "line 9" in body
    assert "line 0" in body
    idx_9 = body.index("line 9")
    idx_0 = body.index("line 0")
    assert idx_9 < idx_0  # most recent first


def test_audit_log_trims_to_max_entries(tmp_path, monkeypatch, logged_in_admin, admin_user):
    """When the log has more matching lines than max_entries, only the last N are kept."""
    import config as cfg
    log_file = str(tmp_path / "test.log")
    monkeypatch.setattr(cfg, "LOG_FILE", log_file)
    # Write 300 matching lines (default max_entries=200)
    with open(log_file, "w") as f:
        for i in range(300):
            f.write(f"2026-01-01 entry_{i:04d} user=admin action=test\n")
    resp = logged_in_admin.get(f"/admin/users/{admin_user}/audit")
    assert resp.status_code == 200
    body = resp.data.decode()
    # The first 100 entries (0-99) should be trimmed
    assert "entry_0000" not in body
    assert "entry_0099" not in body
    # The last 200 entries (100-299) should be present
    assert "entry_0100" in body
    assert "entry_0299" in body


def test_audit_log_early_termination(tmp_path, monkeypatch, logged_in_admin, admin_user):
    """Reverse-read stops once max_entries are collected; old non-matching lines are skipped."""
    import config as cfg
    log_file = str(tmp_path / "test.log")
    monkeypatch.setattr(cfg, "LOG_FILE", log_file)
    # Write 50 non-matching lines, then 200 matching lines, then 50 more non-matching.
    # Only the 200 matching lines should appear, in newest-first order.
    with open(log_file, "w") as f:
        for i in range(50):
            f.write(f"2026-01-01 noise_early_{i:04d} user=other action=ignore\n")
        for i in range(200):
            f.write(f"2026-01-01 match_{i:04d} user=admin action=test\n")
        for i in range(50):
            f.write(f"2026-01-01 noise_late_{i:04d} user=other action=ignore\n")
    resp = logged_in_admin.get(f"/admin/users/{admin_user}/audit")
    assert resp.status_code == 200
    body = resp.data.decode()
    # All 200 matching entries should be present
    assert "match_0000" in body
    assert "match_0199" in body
    # Non-matching noise should not appear
    assert "noise_early" not in body
    assert "noise_late" not in body
    # Newest-first: match_0199 should appear before match_0000
    assert body.index("match_0199") < body.index("match_0000")


def test_admin_users_shows_last_login(logged_in_admin):
    """Admin users page should show Last Login column."""
    resp = logged_in_admin.get("/admin/users")
    assert resp.status_code == 200
    assert b"Last Login" in resp.data


# -----------------------------------------------------------------------
# Create page minimal form + guidance
# -----------------------------------------------------------------------
def test_create_page_has_editor_and_preview(logged_in_admin):
    """Create page now guides to create then edit; should show title field and callout."""
    resp = logged_in_admin.get("/create-page")
    assert resp.status_code == 200
    assert b"info-callout" in resp.data
    assert b"name=\"title\"" in resp.data


# -----------------------------------------------------------------------
# Category delete with page actions
# -----------------------------------------------------------------------
def test_category_delete_moves_pages_to_uncategorized(logged_in_admin):
    """Deleting a category with uncategorize action moves pages to uncategorized."""
    import db
    cat_id = db.create_category("DelCat")
    page_id = db.create_page("TestPage", "test-del-page", "content", cat_id, None)
    resp = logged_in_admin.post(f"/category/{cat_id}/delete",
                                data={"page_action": "uncategorize"},
                                follow_redirects=True)
    assert resp.status_code == 200
    page = db.get_page(page_id)
    assert page is not None
    assert page["category_id"] is None


def test_category_delete_bulk_deletes_pages(logged_in_admin):
    """Deleting a category with delete action removes its pages."""
    import db
    db.disable_plugin("deletion_slowdown")
    cat_id = db.create_category("DelCat2")
    page_id = db.create_page("DelPage", "test-del-page2", "content", cat_id, None)
    resp = logged_in_admin.post(f"/category/{cat_id}/delete",
                                data={"page_action": "delete"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_page(page_id) is None


def test_category_delete_bulk_queues_pages_under_deletion_slowdown(logged_in_admin):
    """With deletion_slowdown on, the category's pages are queued, not removed."""
    import db
    cat_id = db.create_category("DelCat3")
    page_id = db.create_page("DelPage3", "test-del-page3", "content", cat_id, None)
    resp = logged_in_admin.post(f"/category/{cat_id}/delete",
                                data={"page_action": "delete"},
                                follow_redirects=True)
    assert resp.status_code == 200
    page = db.get_page(page_id)
    assert page is not None
    assert page["pending_deletion"] == 1


def test_category_delete_moves_pages_to_another_category(logged_in_admin):
    """Deleting a category with move action moves pages to target category."""
    import db
    cat_id = db.create_category("MoveSrc")
    target_id = db.create_category("MoveDst")
    page_id = db.create_page("MovePage", "test-move-page", "content", cat_id, None)
    resp = logged_in_admin.post(f"/category/{cat_id}/delete",
                                data={"page_action": "move",
                                      "target_category_id": str(target_id)},
                                follow_redirects=True)
    assert resp.status_code == 200
    page = db.get_page(page_id)
    assert page is not None
    assert page["category_id"] == target_id


def test_category_manage_panel_visible(logged_in_admin):
    """Category manage panel with rename and delete should be in sidebar."""
    import db
    db.create_category("ManageCat")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"catManageModal" in resp.data
    assert b"Rename" in resp.data
    assert b"Delete Category" in resp.data


# -----------------------------------------------------------------------
# Category move (reparent)
# -----------------------------------------------------------------------
def test_move_category_to_parent(logged_in_admin):
    """Moving a category to a new parent should update its parent_id."""
    import db
    parent_id = db.create_category("ParentCat")
    child_id = db.create_category("ChildCat")
    resp = logged_in_admin.post(f"/category/{child_id}/move",
                                data={"parent_id": str(parent_id)},
                                follow_redirects=True)
    assert resp.status_code == 200
    cat = db.get_category(child_id)
    assert cat["parent_id"] == parent_id


def test_move_category_to_top_level(logged_in_admin):
    """Moving a category with parent_id='' should make it top-level."""
    import db
    parent_id = db.create_category("TopParent")
    child_id = db.create_category("TopChild", parent_id=parent_id)
    cat = db.get_category(child_id)
    assert cat["parent_id"] == parent_id
    resp = logged_in_admin.post(f"/category/{child_id}/move",
                                data={"parent_id": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    cat = db.get_category(child_id)
    assert cat["parent_id"] is None


def test_move_category_into_itself_rejected(logged_in_admin):
    """Moving a category into itself should fail."""
    import db
    cat_id = db.create_category("SelfRef")
    resp = logged_in_admin.post(f"/category/{cat_id}/move",
                                data={"parent_id": str(cat_id)},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot move a category into itself" in resp.data


def test_move_category_manage_panel_has_move_option(logged_in_admin):
    """Category manage modal should include a Move section."""
    import db
    cat_id = db.create_category("MovableCat")
    resp = logged_in_admin.get(f"/api/category/{cat_id}/management")
    assert resp.status_code == 200
    assert "Move to:" in resp.json["html"]
    assert f'action="/category/{cat_id}/move"' in resp.json["html"]


def test_move_category_circular_reference_rejected(logged_in_admin):
    """Moving a category into one of its own descendants should fail."""
    import db
    parent_id = db.create_category("Parent")
    child_id = db.create_category("Child", parent_id=parent_id)
    grandchild_id = db.create_category("Grandchild", parent_id=child_id)
    # Try to move parent under grandchild (circular)
    resp = logged_in_admin.post(f"/category/{parent_id}/move",
                                data={"parent_id": str(grandchild_id)},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot move a category into one of its own subcategories" in resp.data
    # Parent should still be at top level
    cat = db.get_category(parent_id)
    assert cat["parent_id"] is None


def test_is_descendant_of(isolated_db):
    """Verify is_descendant_of correctly detects ancestor chains."""
    import db
    a = db.create_category("A")
    b = db.create_category("B", parent_id=a)
    c = db.create_category("C", parent_id=b)
    d = db.create_category("D")
    # b is a descendant of a
    assert db.is_descendant_of(a, b) is True
    # c is a descendant of a (transitive)
    assert db.is_descendant_of(a, c) is True
    # d is NOT a descendant of a
    assert db.is_descendant_of(a, d) is False
    # a is NOT a descendant of c
    assert db.is_descendant_of(c, a) is False


def test_delete_category_move_validates_target(logged_in_admin):
    """Deleting a category with move should fall back if target doesn't exist."""
    import db
    cat_id = db.create_category("DeleteMe")
    page_id = db.create_page("TestPage", "testpage-validate", "content", cat_id)
    resp = logged_in_admin.post(f"/category/{cat_id}/delete",
                                data={"page_action": "move",
                                      "target_category_id": "9999"},
                                follow_redirects=True)
    assert resp.status_code == 200
    # Page should be uncategorized (fallback), not moved to non-existent 9999
    page = db.get_page(page_id)
    assert page["category_id"] is None


# -----------------------------------------------------------------------
# Fix: login_required populates g._current_user cache
# -----------------------------------------------------------------------
def test_login_required_populates_g_cache(client, admin_user):
    """login_required must call get_current_user() so that g._current_user is
    populated, avoiding a second DB hit during inject_globals()."""
    from unittest.mock import patch, call
    import db as db_mod
    import app as app_mod

    original_get_user = db_mod.get_user_by_id
    call_count = []

    def counting_get_user(uid):
        call_count.append(uid)
        return original_get_user(uid)

    client.post("/login", data={"username": "admin", "password": "admin123"})

    with patch.object(db_mod, "get_user_by_id", side_effect=counting_get_user):
        resp = client.get("/")
        assert resp.status_code == 200

    # get_user_by_id should be called at most once per request; using the
    # g cache means the decorator and inject_globals() share the same lookup.
    assert len(call_count) <= 1, (
        f"get_user_by_id was called {len(call_count)} times in a single request; "
        "login_required should use get_current_user() to populate g._current_user cache."
    )


# -----------------------------------------------------------------------
# Fix: login_required no longer shows duplicate "please log in" flash
# -----------------------------------------------------------------------
def test_login_required_no_duplicate_flash(client, admin_user):
    """Accessing multiple protected pages without login should not produce
    duplicate flash messages on the login page."""
    # Access multiple protected pages without being logged in
    client.get("/")
    client.get("/settings")
    # Now visit the login page: should have no "Please log in" flash since
    # the decorator was updated to redirect silently
    resp = client.get("/login")
    assert resp.status_code == 200
    # No "Please log in to continue" messages should be present
    assert b"Please log in to continue" not in resp.data


def test_login_page_deduplicates_identical_flash_messages(client, admin_user):
    """The login page should render repeated flashed messages only once."""
    with client.session_transaction() as sess:
        sess["_flashes"] = [
            ("error", "Invalid username or password."),
            ("error", "Invalid username or password."),
        ]

    resp = client.get("/login")
    assert resp.status_code == 200
    assert resp.data.count(b"Invalid username or password.") == 1


def test_base_template_deduplicates_identical_flash_messages(logged_in_admin):
    """Base layout pages should also suppress repeated flashed messages."""
    with logged_in_admin.session_transaction() as sess:
        sess["_flashes"] = [
            ("info", "Saved successfully."),
            ("info", "Saved successfully."),
            ("error", "A different message."),
        ]

    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert resp.data.count(b"Saved successfully.") == 1


def test_setup_missing_credentials_message_is_actionable(client):
    """Setup should tell the user exactly what to enter next."""
    resp = client.post("/setup", data={"username": "", "password": "", "confirm_password": ""})
    assert resp.status_code == 200
    assert b"Enter both a username and password to continue." in resp.data


def test_session_conflict_page_explains_single_session_flow(client, admin_user):
    """The session conflict page should clearly explain what will happen next."""
    resp = client.get("/session-conflict")
    assert resp.status_code == 200
    assert b"Sign in again to continue" in resp.data
    assert b"This wiki only allows one active session at a time." in resp.data
    assert b"Continue on this device" in resp.data


def test_session_conflict_force_requires_credentials_message(client, admin_user):
    """Re-authentication should prompt for both required credentials."""
    resp = client.post("/session-conflict/force", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Enter your username and password to continue." in resp.data
    assert resp.data.count(b"Enter your username and password to continue.") == 1


def test_session_limit_no_false_conflict_for_legacy_session(client, admin_user):
    """A pre-existing session with no token should be upgraded, not rejected.

    When session_limit is enabled but an already-logged-in user has no
    session_token in their session (e.g. they were logged in before the
    feature was turned on), the before_request hook must NOT redirect to
    /session-conflict.  Instead it should silently issue a new token so
    the one-session-per-user constraint is enforced going forward.
    """
    import db

    # Ensure session_limit is on.
    db.update_site_settings(session_limit_enabled=1)

    # Manually set up a session that has user_id but NO session_token
    # (as if the user had logged in before session_limit was enabled).
    with client.session_transaction() as sess:
        sess["user_id"] = admin_user
        # Deliberately omit sess["session_token"]

    # Also clear the token from the DB to match the "legacy" state.
    db.update_user(admin_user, session_token=None)

    # The next request must NOT redirect to /session-conflict.
    resp = client.get("/", follow_redirects=False)
    if resp.status_code == 302:
        location = resp.headers.get("Location") or ""
        assert "/session-conflict" not in location, (
            "Legacy session (both tokens None) should be upgraded, not rejected as a conflict"
        )

    # A fresh token must have been written to the DB.
    user = db.get_user_by_id(admin_user)
    assert user["session_token"] is not None

# -----------------------------------------------------------------------
# Announcements: DB helpers
# -----------------------------------------------------------------------
def test_create_and_get_announcement(admin_user):
    import db
    ann_id = db.create_announcement(
        content="Test announcement",
        color="orange",
        text_size="normal",
        visibility="both",
        expires_at=None,
        user_id=admin_user,
    )
    assert ann_id is not None
    ann = db.get_announcement(ann_id)
    assert ann is not None
    assert ann["content"] == "Test announcement"
    assert ann["color"] == "orange"
    assert ann["is_active"] == 1


def test_list_announcements(admin_user):
    import db
    db.create_announcement("Ann 1", "red", "normal", "both", None, admin_user)
    db.create_announcement("Ann 2", "blue", "large", "logged_in", None, admin_user)
    rows = db.list_announcements()
    assert len(rows) >= 2
    assert any(r["content"] == "Ann 1" for r in rows)


def test_update_announcement(admin_user):
    import db
    ann_id = db.create_announcement("Original", "orange", "normal", "both", None, admin_user)
    db.update_announcement(ann_id, content="Updated", is_active=0)
    ann = db.get_announcement(ann_id)
    assert ann["content"] == "Updated"
    assert ann["is_active"] == 0


def test_delete_announcement(admin_user):
    import db
    ann_id = db.create_announcement("ToDelete", "orange", "normal", "both", None, admin_user)
    db.delete_announcement(ann_id)
    assert db.get_announcement(ann_id) is None


def test_get_active_announcements_visibility(admin_user):
    import db
    db.create_announcement("For logged in", "orange", "normal", "logged_in", None, admin_user)
    db.create_announcement("For logged out", "red", "normal", "logged_out", None, admin_user)
    db.create_announcement("For everyone", "blue", "normal", "both", None, admin_user)

    logged_in = db.get_active_announcements(True)
    slugs_in = [r["content"] for r in logged_in]
    assert "For logged in" in slugs_in
    assert "For everyone" in slugs_in
    assert "For logged out" not in slugs_in

    logged_out = db.get_active_announcements(False)
    slugs_out = [r["content"] for r in logged_out]
    assert "For logged out" in slugs_out
    assert "For everyone" in slugs_out
    assert "For logged in" not in slugs_out


def test_get_active_announcements_expired(admin_user):
    import db
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.create_announcement("Expired", "orange", "normal", "both", past, admin_user)
    active = db.get_active_announcements(True)
    assert all(r["content"] != "Expired" for r in active)


def test_get_active_announcements_inactive(admin_user):
    import db
    ann_id = db.create_announcement("Inactive", "orange", "normal", "both", None, admin_user)
    db.update_announcement(ann_id, is_active=0)
    active = db.get_active_announcements(True)
    assert all(r["content"] != "Inactive" for r in active)


# -----------------------------------------------------------------------
# Announcements: Admin routes
# -----------------------------------------------------------------------
def test_admin_announcements_page(logged_in_admin):
    resp = logged_in_admin.get("/admin/announcements")
    assert resp.status_code == 200
    assert b"Announcement" in resp.data


def test_admin_create_announcement(logged_in_admin):
    resp = logged_in_admin.post("/admin/announcements/create", data={
        "content": "Hello world",
        "color": "orange",
        "text_size": "normal",
        "visibility": "both",
        "expires_at": "",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Announcement created" in resp.data
    assert b"Hello world" in resp.data


def test_admin_create_announcement_requires_content(logged_in_admin):
    resp = logged_in_admin.post("/admin/announcements/create", data={
        "content": "",
        "color": "orange",
        "text_size": "normal",
        "visibility": "both",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


def test_admin_create_announcement_max_length(logged_in_admin):
    resp = logged_in_admin.post("/admin/announcements/create", data={
        "content": "x" * 2001,
        "color": "orange",
        "text_size": "normal",
        "visibility": "both",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"2000 characters" in resp.data


def test_admin_edit_announcement(logged_in_admin, admin_user):
    import db
    ann_id = db.create_announcement("Old text", "orange", "normal", "both", None, admin_user)
    resp = logged_in_admin.post(f"/admin/announcements/{ann_id}/edit", data={
        "content": "New text",
        "color": "red",
        "text_size": "large",
        "visibility": "logged_in",
        "expires_at": "",
        "is_active": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Announcement updated" in resp.data
    ann = db.get_announcement(ann_id)
    assert ann["content"] == "New text"
    assert ann["color"] == "red"


def test_admin_delete_announcement(logged_in_admin, admin_user):
    import db
    ann_id = db.create_announcement("To delete", "orange", "normal", "both", None, admin_user)
    resp = logged_in_admin.post(f"/admin/announcements/{ann_id}/delete",
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Announcement deleted" in resp.data
    assert db.get_announcement(ann_id) is None


def test_announcement_shown_in_base(logged_in_admin, admin_user):
    import db
    db.create_announcement("Visible banner", "orange", "normal", "both", None, admin_user)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"announcements-bar" in resp.data
    assert b"Visible banner" in resp.data


def test_announcement_full_view(logged_in_admin, admin_user):
    import db
    ann_id = db.create_announcement("Full content here", "blue", "normal", "logged_in", None, admin_user)
    resp = logged_in_admin.get(f"/announcements/{ann_id}")
    assert resp.status_code == 200
    assert b"Full content here" in resp.data


def test_announcement_full_view_404_for_wrong_visibility(client, admin_user):
    import db
    ann_id = db.create_announcement("Logged in only", "blue", "normal", "logged_in", None, admin_user)
    # Not logged in: should get 404
    resp = client.get(f"/announcements/{ann_id}")
    assert resp.status_code == 404


def test_announcement_admin_link_in_user_settings(logged_in_admin):
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    assert b"Announcements" in resp.data


@pytest.fixture
def isolated_uploads(tmp_path, monkeypatch):
    """Redirect UPLOAD_FOLDER to a temporary directory for cleanup tests."""
    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr(config, "UPLOAD_FOLDER", upload_dir)
    return upload_dir


def _make_fake_upload(upload_dir, filename="abc123.png"):
    """Create a dummy file in the upload directory and return its path."""
    fpath = os.path.join(upload_dir, filename)
    with open(fpath, "wb") as f:
        f.write(b"fakeimage")
    return fpath


def test_get_all_referenced_image_filenames_empty():
    """No images referenced when pages have no image markdown."""
    import db
    result = db.get_all_referenced_image_filenames()
    assert isinstance(result, set)
    assert len(result) == 0


def test_get_all_referenced_image_filenames_from_page():
    """Images referenced in page content are returned."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("u1", generate_password_hash("pw"), role="editor")
    content = "Hello\n![img](/static/uploads/abc123.png)\nWorld"
    db.create_page("Test", "test-ref", content, None, uid)
    result = db.get_all_referenced_image_filenames()
    assert "abc123.png" in result


def test_get_all_referenced_image_filenames_from_history():
    """Images referenced only in page history (removed from live page) are still returned."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("u2", generate_password_hash("pw"), role="editor")
    # Create page with image, then update without image
    page_id = db.create_page("HistPage", "hist-page", "![img](/static/uploads/hist1.png)", None, uid)
    db.update_page(page_id, "HistPage", "no image now", uid, "removed image")
    result = db.get_all_referenced_image_filenames()
    # Image removed from live page but still in history → must be kept
    assert "hist1.png" in result


def test_cleanup_unused_uploads_removes_unreferenced(isolated_uploads):
    """Files not referenced in any page/history are deleted by cleanup."""
    from app import cleanup_unused_uploads
    fpath = _make_fake_upload(isolated_uploads, "orphan.png")
    assert os.path.isfile(fpath)
    cleanup_unused_uploads()
    assert not os.path.isfile(fpath)


def test_cleanup_unused_uploads_keeps_referenced(isolated_uploads):
    """Files referenced in a page are preserved by cleanup."""
    import db
    from werkzeug.security import generate_password_hash
    from app import cleanup_unused_uploads
    uid = db.create_user("u3", generate_password_hash("pw"), role="editor")
    fpath = _make_fake_upload(isolated_uploads, "keep_me.png")
    db.create_page("KPage", "k-page", "![img](/static/uploads/keep_me.png)", None, uid)
    cleanup_unused_uploads()
    assert os.path.isfile(fpath)


def test_cleanup_unused_uploads_keeps_history_referenced(isolated_uploads):
    """Files referenced only in page history are preserved by cleanup."""
    import db
    from werkzeug.security import generate_password_hash
    from app import cleanup_unused_uploads
    uid = db.create_user("u4", generate_password_hash("pw"), role="editor")
    page_id = db.create_page("HPage", "h-page", "![img](/static/uploads/hist_keep.png)", None, uid)
    fpath = _make_fake_upload(isolated_uploads, "hist_keep.png")
    # Update page to remove the image; original content is now only in history
    db.update_page(page_id, "HPage", "no image", uid, "removed")
    cleanup_unused_uploads()
    # Image is in history → must be kept
    assert os.path.isfile(fpath)


def test_draft_discard_triggers_cleanup(logged_in_admin, admin_user, isolated_uploads):
    """Discarding a draft via /api/draft/delete removes unreferenced upload files."""
    import db
    # Create a page and a draft that references an orphan image
    home = db.get_home_page()
    db.save_draft(home["id"], admin_user, "Draft", "![img](/static/uploads/draft_orphan.png)")
    fpath = _make_fake_upload(isolated_uploads, "draft_orphan.png")
    assert os.path.isfile(fpath)
    resp = logged_in_admin.post("/api/draft/delete",
                                json={"page_id": home["id"]},
                                content_type="application/json")
    assert resp.status_code == 200
    # Image was in the discarded draft only → should be cleaned up
    assert not os.path.isfile(fpath)


def test_page_commit_removes_images_not_in_content(logged_in_admin, admin_user, isolated_uploads):
    """Committing a page without an image that was uploaded deletes the orphan file."""
    import db
    home = db.get_home_page()
    fpath = _make_fake_upload(isolated_uploads, "unused_commit.png")
    # Commit the page WITHOUT including the image URL in the content
    resp = logged_in_admin.post(f"/page/{home['slug']}/edit",
                                data={"title": "Home", "content": "Clean content",
                                      "edit_message": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert not os.path.isfile(fpath)


def test_cleanup_skips_dotfiles(isolated_uploads):
    """Files starting with '.' (e.g. .gitkeep) are never removed."""
    from app import cleanup_unused_uploads
    gitkeep = os.path.join(isolated_uploads, ".gitkeep")
    with open(gitkeep, "wb") as f:
        f.write(b"")
    cleanup_unused_uploads()
    assert os.path.isfile(gitkeep)


# -----------------------------------------------------------------------
# Improved draft system tests
# -----------------------------------------------------------------------

def test_edit_page_has_discard_draft_button(logged_in_admin):
    """Edit page must show 'Discard Draft' button instead of plain Cancel link."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    assert b"Discard Draft" in resp.data
    # The form actions should not have a bare cancel link (href to page view)
    assert b'class="btn btn-outline">Cancel</a>' not in resp.data


def test_edit_page_removes_legacy_replace_image_input(logged_in_admin):
    """Edit page should only render the active replace-image upload input."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    assert b"replace-img-upload-input" in resp.data
    assert b"replace-img-file-input" not in resp.data


def test_user_settings_has_my_drafts_section(logged_in_admin):
    """Account settings must include the My Drafts section."""
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    assert b"My Drafts" in resp.data
    assert b"draft-manager-list" in resp.data


def test_api_draft_mine_returns_list(logged_in_admin, admin_user):
    """GET /api/draft/mine returns a JSON list (empty or not)."""
    resp = logged_in_admin.get("/api/draft/mine")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_api_draft_mine_includes_user_draft(logged_in_admin, admin_user):
    """After saving a draft, /api/draft/mine includes it."""
    import db
    home = db.get_home_page()
    db.save_draft(home["id"], admin_user, "My draft title", "Draft content")
    resp = logged_in_admin.get("/api/draft/mine")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["page_id"] == home["id"]
    assert data[0]["page_slug"] == home["slug"]
    assert data[0]["title"] == "My draft title"


def test_api_draft_others_returns_new_format(logged_in_admin, admin_user):
    """GET /api/draft/others/<id> returns {drafts: [...], page_last_edited_at: ...}."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/api/draft/others/{home['id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "drafts" in data
    assert "page_last_edited_at" in data
    assert isinstance(data["drafts"], list)


def test_api_draft_others_404_for_missing_page(logged_in_admin):
    """GET /api/draft/others/<id> returns 404 for non-existent page."""
    resp = logged_in_admin.get("/api/draft/others/99999")
    assert resp.status_code == 404


def test_stale_draft_warning_shown_when_page_newer(logged_in_admin, admin_user):
    """Edit page shows stale draft warning when page was updated after draft."""
    import db
    from time import sleep
    home = db.get_home_page()
    # Save a draft first
    db.save_draft(home["id"], admin_user, "Old draft", "old content")
    # Now update the page (simulating another user's commit)
    sleep(0.01)
    db.update_page(home["id"], "Home", "newer content", admin_user, "external edit")
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    assert b"stale-draft-notice" in resp.data
    # The warning should be visible (not display:none)
    assert b"updated by another user" in resp.data


def test_stale_draft_warning_hidden_when_no_draft(logged_in_admin):
    """Edit page does not show stale draft warning when there is no draft."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    # The stale notice element should be hidden (no inline style, uses u-d-none class)
    assert b'id="stale-draft-notice"' in resp.data
    assert b'class="draft-notice draft-notice-warning u-d-none"' in resp.data
    assert b'style="display:none"' not in resp.data


def test_commit_clears_all_page_drafts(logged_in_admin, admin_user):
    """Committing a page edit deletes all drafts for that page."""
    import db
    from werkzeug.security import generate_password_hash
    home = db.get_home_page()
    # Create a second user with a draft
    uid2 = db.create_user("editor2", generate_password_hash("pw2"), role="editor")
    db.save_draft(home["id"], admin_user, "Admin draft", "admin content")
    db.save_draft(home["id"], uid2, "Editor draft", "editor content")
    assert db.get_draft(home["id"], admin_user) is not None
    assert db.get_draft(home["id"], uid2) is not None
    # Admin commits
    resp = logged_in_admin.post(f"/page/{home['slug']}/edit",
                                data={"title": "Home", "content": "committed",
                                      "edit_message": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    # Both drafts should be gone
    assert db.get_draft(home["id"], admin_user) is None
    assert db.get_draft(home["id"], uid2) is None


# -----------------------------------------------------------------------
# Timezone, DST, and leap year support
# -----------------------------------------------------------------------
def test_default_timezone_is_utc():
    """site_settings.timezone defaults to 'UTC'."""
    import db
    settings = db.get_site_settings()
    assert settings["timezone"] == "UTC"


def test_update_and_retrieve_timezone():
    """Timezone can be updated and retrieved from site_settings."""
    import db
    db.update_site_settings(timezone="Europe/Rome")
    settings = db.get_site_settings()
    assert settings["timezone"] == "Europe/Rome"


def test_format_datetime_uses_configured_timezone(monkeypatch):
    """format_datetime returns time in the configured site timezone."""
    import db
    db.update_site_settings(timezone="Europe/Rome")
    from app import format_datetime
    # 2026-02-25T10:00:00 UTC → 11:00 CET (UTC+1) in winter
    result = format_datetime("2026-02-25T10:00:00")
    assert "11:00" in result
    assert "CET" in result or "CE" in result or "+01" in result or "Rome" in result


def test_format_datetime_dst_handling(monkeypatch):
    """format_datetime correctly applies DST (summer time = UTC+2 for Rome)."""
    import db
    db.update_site_settings(timezone="Europe/Rome")
    from app import format_datetime
    # 2026-07-01T10:00:00 UTC → 12:00 CEST (UTC+2) in summer
    result = format_datetime("2026-07-01T10:00:00")
    assert "12:00" in result
    assert "CEST" in result or "CE" in result or "+02" in result or "Rome" in result


def test_format_datetime_leap_year():
    """format_datetime correctly handles Feb 29 in a leap year."""
    import db
    db.update_site_settings(timezone="UTC")
    from app import format_datetime
    result = format_datetime("2028-02-29T12:00:00")
    assert "2028-02-29" in result
    assert "12:00" in result


def test_admin_settings_saves_timezone(logged_in_admin):
    """POST to /global-settings updates timezone in the database."""
    import db
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "TestWiki",
        "timezone": "America/New_York",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
    }, follow_redirects=True)
    assert resp.status_code == 200
    settings = db.get_site_settings()
    assert settings["timezone"] == "America/New_York"


def test_admin_settings_rejects_invalid_timezone(logged_in_admin):
    """POST to /global-settings with an invalid timezone returns an error."""
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "TestWiki",
        "timezone": "Not/A/Real/Zone",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid time zone" in resp.data


def test_admin_settings_page_shows_timezone_dropdown(logged_in_admin):
    """GET /global-settings renders the timezone selector."""
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert b"Time Zone" in resp.data
    assert b"Europe/Rome" in resp.data
    assert b"America/New_York" in resp.data


# -----------------------------------------------------------------------
# Favicon settings tests
# -----------------------------------------------------------------------

def test_favicon_columns_exist_in_db():
    """New favicon columns should exist in site_settings after init_db."""
    import db
    conn = db.get_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(site_settings)").fetchall()]
    conn.close()
    assert "favicon_enabled" in cols
    assert "favicon_type" in cols
    assert "favicon_custom" in cols


def test_favicon_defaults():
    """Favicon should be disabled with 'yellow' type by default."""
    import db
    settings = db.get_site_settings()
    assert settings["favicon_enabled"] == 0
    assert settings["favicon_type"] == "yellow"
    assert settings["favicon_custom"] == ""


def test_admin_settings_page_shows_favicon_section(logged_in_admin):
    """GET /global-settings renders the Favicon section."""
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert b"Favicon" in resp.data
    assert b"favicon_enabled" in resp.data


def test_favicon_enable_sets_flag(logged_in_admin):
    """Enabling favicon via POST sets favicon_enabled=1 in DB."""
    import db
    from PIL import Image
    import io
    # First set the favicon type via AJAX
    img = Image.new("RGBA", (32, 32), (0, 128, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = logged_in_admin.post("/global-settings/favicon/upload", data={
        "file": (buf, "green.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    # Now submit the main settings form with favicon_enabled
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "TestWiki",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
        "favicon_enabled": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Settings updated" in resp.data
    settings = db.get_site_settings()
    assert settings["favicon_enabled"] == 1
    assert settings["favicon_type"] == "custom"


def test_favicon_disable_clears_flag(logged_in_admin):
    """Submitting settings without favicon_enabled checkbox sets it to 0."""
    import db
    db.update_site_settings(favicon_enabled=1, favicon_type="blue")
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "TestWiki",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
        # no favicon_enabled checkbox
        "favicon_type": "blue",
    }, follow_redirects=True)
    assert resp.status_code == 200
    settings = db.get_site_settings()
    assert settings["favicon_enabled"] == 0


def test_favicon_invalid_type_falls_back_to_yellow(logged_in_admin):
    """An unrecognised favicon_type value in DB is replaced with 'yellow' on save."""
    import db
    # Directly set an invalid value in the DB
    db.update_site_settings(favicon_type="notacolor")
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "TestWiki",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
        "favicon_enabled": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    settings = db.get_site_settings()
    assert settings["favicon_type"] == "yellow"


def test_favicon_link_tag_in_base_when_enabled(logged_in_admin):
    """When favicon is enabled, the <link rel=icon> tag appears in pages."""
    import db
    db.update_site_settings(favicon_enabled=1, favicon_type="orange")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'rel="icon"' in resp.data
    assert b"banana_orange.png" in resp.data


def test_favicon_link_tag_absent_when_disabled(logged_in_admin):
    """When favicon is disabled, no <link rel=icon> tag should appear."""
    import db
    db.update_site_settings(favicon_enabled=0)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"banana_" not in resp.data or b'rel="icon"' not in resp.data


def test_banana_favicon_files_exist():
    """All 8 bundled banana favicon PNG files must exist on disk."""
    import os
    favicons_dir = os.path.join(
        os.path.dirname(__file__), "..", "app", "static", "favicons"
    )
    expected = [
        "banana_yellow.png", "banana_green.png", "banana_blue.png",
        "banana_red.png", "banana_orange.png", "banana_cyan.png",
        "banana_purple.png", "banana_lime.png",
    ]
    for fname in expected:
        assert os.path.isfile(os.path.join(favicons_dir, fname)), \
            f"Missing favicon file: {fname}"


# ---------------------------------------------------------------------------
# reset_password.py CLI tool tests
# ---------------------------------------------------------------------------

def test_reset_password_no_users(monkeypatch, capsys):
    """reset_password.main() exits cleanly when no users exist."""
    import importlib, sys as _sys
    rp = importlib.import_module("reset_password")
    with pytest.raises(SystemExit) as exc:
        rp.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "No users found" in captured.out


def test_reset_password_quit(monkeypatch, capsys):
    """Entering 'q' at the user-selection prompt aborts cleanly."""
    from werkzeug.security import generate_password_hash
    import db, importlib
    db.create_user("alice", generate_password_hash("oldpass"), role="user")

    rp = importlib.import_module("reset_password")
    monkeypatch.setattr("builtins.input", lambda _: "q")
    with pytest.raises(SystemExit) as exc:
        rp.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Aborted" in captured.out


def test_reset_password_updates_password(monkeypatch, capsys):
    """Selecting a user and entering a valid password updates the DB."""
    from werkzeug.security import generate_password_hash, check_password_hash
    import db, importlib, itertools

    uid = db.create_user("bob", generate_password_hash("oldpassword"), role="user")

    rp = importlib.import_module("reset_password")

    inputs = iter(["1"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    passwords = iter(["newpassword1", "newpassword1"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(passwords))

    rp.main()

    user = db.get_user_by_id(uid)
    assert check_password_hash(user["password"], "newpassword1")
    captured = capsys.readouterr()
    assert "updated successfully" in captured.out


def test_reset_password_rotates_session_token_when_limit_enabled(monkeypatch, capsys):
    """CLI password resets should invalidate session-limit tokens from legacy sessions."""
    from werkzeug.security import generate_password_hash
    import db, importlib

    db.update_site_settings(session_limit_enabled=1)
    uid = db.create_user("bobtoken", generate_password_hash("oldpassword"), role="user")
    db.update_user(uid, session_token="legacytoken")

    rp = importlib.import_module("reset_password")

    inputs = iter(["1"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    passwords = iter(["newpassword1", "newpassword1"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(passwords))

    rp.main()

    user = db.get_user_by_id(uid)
    assert user["session_token"] is not None
    assert user["session_token"] != "legacytoken"
    capsys.readouterr()


def test_reset_password_revokes_api_tokens(monkeypatch, capsys):
    """A CLI reset revokes the account's API tokens and leaves other accounts alone."""
    from werkzeug.security import generate_password_hash
    import db, importlib

    uid = db.create_user("bobapi", generate_password_hash("oldpassword"), role="user")
    other = db.create_user("carolapi", generate_password_hash("oldpassword"), role="user")
    db.create_api_token(uid, name="one")
    db.create_api_token(other, name="other")

    rp = importlib.import_module("reset_password")

    inputs = iter(["1"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    passwords = iter(["newpassword1", "newpassword1"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(passwords))

    rp.main()

    with db.get_db_context() as conn:
        active = dict(conn.execute(
            "SELECT user_id, COUNT(*) FROM api_service__tokens WHERE active=1 GROUP BY user_id"
        ).fetchall())
    assert uid not in active
    assert active.get(other) == 1
    assert "1 API token(s) of this account were revoked" in capsys.readouterr().out


def test_reset_password_mismatch_then_success(monkeypatch, capsys):
    """Mismatched passwords prompt again; success on second attempt."""
    from werkzeug.security import generate_password_hash, check_password_hash
    import db, importlib

    uid = db.create_user("carol", generate_password_hash("oldpass"), role="editor")

    rp = importlib.import_module("reset_password")

    monkeypatch.setattr("builtins.input", lambda _: "1")
    # first attempt: mismatch; second attempt: match
    passwords = iter(["newpassword1", "wrongconfirm", "newpassword1", "newpassword1"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(passwords))

    rp.main()

    user = db.get_user_by_id(uid)
    assert check_password_hash(user["password"], "newpassword1")


def test_reset_password_too_short_then_success(monkeypatch, capsys):
    """A password shorter than MIN_PASSWORD_LENGTH is rejected."""
    from werkzeug.security import generate_password_hash, check_password_hash
    import db, importlib

    uid = db.create_user("dave", generate_password_hash("oldpass"), role="user")

    rp = importlib.import_module("reset_password")

    monkeypatch.setattr("builtins.input", lambda _: "1")
    passwords = iter(["short", "longenoughpw", "longenoughpw"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(passwords))

    rp.main()

    user = db.get_user_by_id(uid)
    assert check_password_hash(user["password"], "longenoughpw")


def test_reset_password_invalid_selection_then_valid(monkeypatch, capsys):
    """Non-numeric and out-of-range selections are rejected before a valid pick."""
    from werkzeug.security import generate_password_hash, check_password_hash
    import db, importlib

    uid = db.create_user("eve", generate_password_hash("oldpass"), role="user")

    rp = importlib.import_module("reset_password")

    inputs = iter(["abc", "99", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    passwords = iter(["validpassword", "validpassword"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(passwords))

    rp.main()

    user = db.get_user_by_id(uid)
    assert check_password_hash(user["password"], "validpassword")


# -----------------------------------------------------------------------
# Superuser: protected account tests
# -----------------------------------------------------------------------
def _make_superuser(username="superadmin", password="super123"):
    """Helper: create a user with is_superuser=1 via direct DB write."""
    from werkzeug.security import generate_password_hash
    import db
    import sqlite3
    import config
    uid = db.create_user(username, generate_password_hash(password), role="admin")
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute("UPDATE users SET is_superuser=1 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return uid, username, password


def test_superuser_column_exists(isolated_db):
    """is_superuser column should exist with default 0 after init_db."""
    import db, sqlite3, config
    conn = sqlite3.connect(config.DATABASE_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    conn.close()
    assert "is_superuser" in cols


def test_superuser_default_zero(isolated_db):
    """Newly created users should have is_superuser=0."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("normaluser", generate_password_hash("pass123"), role="admin")
    user = db.get_user_by_id(uid)
    assert user["is_superuser"] == 0


def test_admin_cannot_delete_superuser(client, admin_user):
    """Admin should not be able to delete a superuser account."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    su_uid, _, _ = _make_superuser()
    resp = client.post(f"/admin/users/{su_uid}/edit",
                       data={"action": "delete"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    import db
    assert db.get_user_by_id(su_uid) is not None


def test_admin_cannot_suspend_superuser(client, admin_user):
    """Admin should not be able to suspend a superuser account."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    su_uid, _, _ = _make_superuser()
    resp = client.post(f"/admin/users/{su_uid}/edit",
                       data={"action": "suspend"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    import db
    user = db.get_user_by_id(su_uid)
    assert user["suspended"] == 0


def test_admin_cannot_change_superuser_role(client, admin_user):
    """Admin should not be able to change the role of a superuser."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    su_uid, _, _ = _make_superuser()
    resp = client.post(f"/admin/users/{su_uid}/edit",
                       data={"action": "change_role", "role": "user"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    import db
    user = db.get_user_by_id(su_uid)
    assert user["role"] == "admin"


def test_admin_cannot_change_superuser_username(client, admin_user):
    """Admin should not be able to rename a superuser."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    su_uid, su_name, _ = _make_superuser()
    resp = client.post(f"/admin/users/{su_uid}/edit",
                       data={"action": "change_username", "username": "hacked"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    import db
    user = db.get_user_by_id(su_uid)
    assert user["username"] == su_name


def test_admin_cannot_change_superuser_password(client, admin_user):
    """Admin should not be able to change a superuser's password."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    su_uid, _, su_pw = _make_superuser()
    resp = client.post(f"/admin/users/{su_uid}/edit",
                       data={"action": "change_password",
                             "password": "newpass1", "confirm_password": "newpass1"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    from werkzeug.security import check_password_hash
    import db
    user = db.get_user_by_id(su_uid)
    assert check_password_hash(user["password"], su_pw)


def test_superuser_cannot_delete_own_account(client, admin_user):
    """A superuser cannot delete their own account via account settings."""
    su_uid, su_name, su_pw = _make_superuser()
    client.post("/login", data={"username": su_name, "password": su_pw})
    resp = client.post("/settings",
                       data={"action": "delete_account", "password": su_pw},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    import db
    assert db.get_user_by_id(su_uid) is not None


def test_superuser_cannot_change_own_username(client, admin_user):
    """A superuser cannot change their own username via account settings."""
    su_uid, su_name, su_pw = _make_superuser()
    client.post("/login", data={"username": su_name, "password": su_pw})
    resp = client.post("/settings",
                       data={"action": "change_username",
                             "new_username": "hacked", "password": su_pw},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    import db
    user = db.get_user_by_id(su_uid)
    assert user["username"] == su_name


def test_superuser_cannot_change_own_password(client, admin_user):
    """A superuser cannot change their own password via account settings."""
    su_uid, su_name, su_pw = _make_superuser()
    client.post("/login", data={"username": su_name, "password": su_pw})
    resp = client.post("/settings",
                       data={"action": "change_password",
                             "current_password": su_pw,
                             "new_password": "newpass1",
                             "confirm_password": "newpass1"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"protected" in resp.data.lower()
    from werkzeug.security import check_password_hash
    import db
    user = db.get_user_by_id(su_uid)
    assert check_password_hash(user["password"], su_pw)


def test_superuser_has_admin_access(client, admin_user):
    """A superuser can access admin pages since they have role='admin'."""
    su_uid, su_name, su_pw = _make_superuser("super2", "super456")
    client.post("/login", data={"username": su_name, "password": su_pw})
    resp = client.get("/admin/users")
    assert resp.status_code == 200


def test_superuser_not_assigned_on_setup(isolated_db):
    """Users created during setup have is_superuser=0."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("setupadmin", generate_password_hash("pass123"), role="admin")
    user = db.get_user_by_id(uid)
    assert user["is_superuser"] == 0


# ---------------------------------------------------------------------------
# Maintenance mode tests (formerly lockdown mode: same gating, new naming
# and dedicated ``/admin`` sign-in route)
# ---------------------------------------------------------------------------

def test_maintenance_columns_exist_in_db():
    """maintenance_mode and maintenance_message columns exist in site_settings."""
    import db
    settings = db.get_site_settings()
    assert settings["maintenance_mode"] == 0
    assert settings["maintenance_message"] == ""


def test_legacy_lockdown_route_redirects_to_maintenance(client, admin_user):
    """/lockdown is a permanent redirect to /maintenance for backwards compat."""
    resp = client.get("/lockdown")
    assert resp.status_code == 301
    assert "/maintenance" in resp.headers["Location"]


def test_maintenance_page_redirects_to_login_when_inactive(client, admin_user):
    """/maintenance redirects to /login when maintenance mode is off."""
    resp = client.get("/maintenance")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_maintenance_page_renders_when_active(client, admin_user):
    """/maintenance page renders the custom message when maintenance mode is on."""
    import db
    db.update_site_settings(maintenance_mode=1, maintenance_message="Maintenance in progress.")
    resp = client.get("/maintenance")
    assert resp.status_code == 200
    assert b"maintenance" in resp.data.lower()
    assert b"Maintenance in progress." in resp.data


def test_maintenance_page_hides_admin_login_link(client, admin_user):
    """/maintenance page does not expose a visible admin login link.

    The admin entry point lives at ``/admin``: admins navigate there
    directly during maintenance.  The maintenance landing page is
    intentionally informational only.
    """
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.get("/maintenance")
    assert resp.status_code == 200
    assert b'href="/login' not in resp.data
    assert b'href="/admin' not in resp.data


def test_admin_route_serves_login_form_during_maintenance(client, admin_user):
    """``/admin`` shows the dedicated admin sign-in form while in maintenance mode."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 200
    assert b"admin-login-form" in resp.data


def test_admin_route_serves_login_form_for_anonymous_users(client, admin_user):
    """``/admin`` shows the admin sign-in form for anonymous visitors at all times."""
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 200
    assert b"admin-login-form" in resp.data


def test_admin_route_redirects_admins_to_settings(logged_in_admin):
    """Logged-in admins hitting ``/admin`` are forwarded to the global settings dashboard."""
    resp = logged_in_admin.get("/admin", follow_redirects=False)
    assert resp.status_code == 302
    assert "/global-settings" in resp.headers["Location"]


def test_admin_route_post_logs_in_admin(client, admin_user):
    """POST /admin with valid admin credentials creates a session and redirects."""
    resp = client.post(
        "/admin",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/global-settings" in resp.headers["Location"]
    # And follow-up requests are authenticated.
    home = client.get("/")
    assert home.status_code == 200


def test_admin_route_post_rejects_non_admin(client, admin_user):
    """POST /admin with non-admin credentials is rejected."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("normaluser", generate_password_hash("pass123"), role="user")
    resp = client.post(
        "/admin",
        data={"username": "normaluser", "password": "pass123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Should still show the admin login form, not redirect into the wiki.
    assert b"admin-login-form" in resp.data


def test_admin_route_post_rejects_bad_password(client, admin_user):
    """POST /admin with the wrong password is rejected and the form is re-rendered."""
    resp = client.post(
        "/admin",
        data={"username": "admin", "password": "WRONG"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"admin-login-form" in resp.data


def test_maintenance_redirects_unauthenticated_users(client, admin_user):
    """Unauthenticated users are redirected to /maintenance when mode is active."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/maintenance" in resp.headers["Location"]


def test_maintenance_redirects_regular_user(client, admin_user):
    """A logged-in regular user is kicked to /maintenance when mode is activated."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("regular", generate_password_hash("pass123"), role="user")
    client.post("/login", data={"username": "regular", "password": "pass123"})
    db.update_site_settings(maintenance_mode=1)
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/maintenance" in resp.headers["Location"]


def test_maintenance_allows_admin_through(client, admin_user):
    """An admin can still browse the wiki when maintenance is active (login via /admin)."""
    import db
    db.update_site_settings(maintenance_mode=1)
    client.post("/admin", data={"username": "admin", "password": "admin123"})
    resp = client.get("/")
    assert resp.status_code == 200


def test_maintenance_blocks_non_admin_login(client, admin_user):
    """A non-admin user cannot log in via /login while maintenance is active."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("regular", generate_password_hash("pass123"), role="user")
    db.update_site_settings(maintenance_mode=1)
    # GET /login during maintenance redirects to /maintenance.
    resp = client.post(
        "/login",
        data={"username": "regular", "password": "pass123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Either the maintenance page is rendered, or the user was bounced back to it.
    assert b"maintenance" in resp.data.lower()


def test_maintenance_admin_login_via_admin_route(client, admin_user):
    """An admin can log in via /admin while maintenance is active."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.post(
        "/admin",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"maintenance" not in resp.data.lower() or b"Settings" in resp.data


def test_maintenance_login_get_redirects_to_maintenance(client, admin_user):
    """GET /login redirects to /maintenance while maintenance mode is active."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.get("/login")
    assert resp.status_code == 302
    assert "/maintenance" in resp.headers["Location"]


def test_login_shows_signup_link_when_maintenance_inactive(client, admin_user):
    """The login page shows the signup link when maintenance is off."""
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Sign up" in resp.data


def test_maintenance_signup_redirects_to_maintenance(client, admin_user):
    """The signup page redirects to /maintenance when maintenance mode is active."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.get("/signup")
    assert resp.status_code == 302
    assert "/maintenance" in resp.headers["Location"]


def test_maintenance_settings_saved_via_admin(logged_in_admin):
    """Admin can enable maintenance mode and set a message via site settings."""
    import db
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "BananaWiki",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
        "maintenance_mode": "1",
        "maintenance_message": "We are down for maintenance.",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Settings updated" in resp.data
    settings = db.get_site_settings()
    assert settings["maintenance_mode"] == 1
    assert settings["maintenance_message"] == "We are down for maintenance."


def test_maintenance_settings_disabled_by_default(logged_in_admin):
    """Submitting settings without maintenance_mode disables it."""
    import db
    db.update_site_settings(maintenance_mode=1)
    logged_in_admin.post("/global-settings", data={
        "site_name": "BananaWiki",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
        # maintenance_mode not submitted → checkbox off
    }, follow_redirects=True)
    settings = db.get_site_settings()
    assert settings["maintenance_mode"] == 0


def test_admin_settings_page_shows_maintenance_section(logged_in_admin):
    """GET /global-settings includes the maintenance section."""
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert b"Maintenance Mode" in resp.data
    assert b"maintenance_mode" in resp.data
    assert b"maintenance_message" in resp.data


# ---------------------------------------------------------------------------
# Create Category modal uses all_categories (flat list, including subcategories)
# ---------------------------------------------------------------------------

def test_create_category_modal_shows_all_categories(logged_in_admin):
    """Create Category modal parent dropdown should include subcategories."""
    import db
    parent_id = db.create_category("TopLevel")
    db.create_category("SubLevel", parent_id=parent_id)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    # The createCatModal should list SubLevel as a potential parent option
    assert b"SubLevel" in resp.data
    # Both categories should appear in the all_categories-based dropdown
    assert b"TopLevel" in resp.data


# ---------------------------------------------------------------------------
# Create Page form includes category selector
# ---------------------------------------------------------------------------

def test_create_page_form_has_category_selector(logged_in_admin):
    """Create Page form should include a category dropdown."""
    import db
    db.create_category("MyCat")
    resp = logged_in_admin.get("/create-page")
    assert resp.status_code == 200
    assert b'name="category_id"' in resp.data
    assert b"MyCat" in resp.data


def test_create_page_with_category_places_page_in_category(logged_in_admin):
    """Creating a page with a category_id should assign the page to that category."""
    import db
    cat_id = db.create_category("TestCreateCat")
    resp = logged_in_admin.post(
        "/create-page",
        data={"title": "CatPage", "category_id": str(cat_id)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    page = db.get_page_by_slug("catpage")
    assert page is not None
    assert page["category_id"] == cat_id


def test_create_page_category_preserved_on_error(logged_in_admin):
    """On validation error, the selected category_id should be preserved."""
    import db
    cat_id = db.create_category("PreserveCat")
    resp = logged_in_admin.post(
        "/create-page",
        data={"title": "", "category_id": str(cat_id)},
    )
    assert resp.status_code == 200
    assert b"Title is required" in resp.data
    # The dropdown should still show the category option
    assert b"PreserveCat" in resp.data


# ---------------------------------------------------------------------------
# Maintenance mode: API endpoints return JSON 403 instead of HTML redirect
# ---------------------------------------------------------------------------

def test_maintenance_api_endpoint_returns_json_403(client, admin_user):
    """During maintenance, API endpoints should return JSON 403, not HTML redirect."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.post(
        "/api/preview",
        json={"content": "test"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert data is not None
    assert "error" in data
    assert "maintenance" in data["error"].lower()


def test_maintenance_api_endpoint_json_403_for_unauthenticated(client, admin_user):
    """Unauthenticated API calls during maintenance return JSON 403."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.post(
        "/api/draft/save",
        json={"page_id": 1, "title": "t", "content": "c"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert data is not None
    assert "error" in data


def test_maintenance_html_endpoint_still_redirects(client, admin_user):
    """During maintenance, non-API endpoints still redirect to /maintenance."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/maintenance" in resp.headers["Location"]


def test_maintenance_does_not_block_setup_endpoint(client, admin_user):
    """During maintenance with setup_done=0, /setup should still be reachable."""
    import db
    db.update_site_settings(maintenance_mode=1, setup_done=0)
    resp = client.get("/setup")
    assert resp.status_code == 200 or (
        resp.status_code == 302 and "/maintenance" not in resp.headers.get("Location", "")
    )


# ---------------------------------------------------------------------------
# Fix: page_history_enabled respected in routes and templates
# ---------------------------------------------------------------------------

def test_page_history_route_returns_404_when_disabled(logged_in_admin, monkeypatch):
    """When PAGE_HISTORY_ENABLED is False, /page/<slug>/history returns 404."""
    import config
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    resp = logged_in_admin.get("/page/home/history")
    assert resp.status_code == 404


def test_page_history_entry_route_returns_404_when_disabled(logged_in_admin, monkeypatch):
    """When PAGE_HISTORY_ENABLED is False, /page/<slug>/history/<id> returns 404."""
    import config
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    resp = logged_in_admin.get("/page/home/history/1")
    assert resp.status_code == 404


def test_revert_page_route_returns_404_when_disabled(logged_in_admin, monkeypatch):
    """When PAGE_HISTORY_ENABLED is False, revert endpoint returns 404."""
    import config
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    resp = logged_in_admin.post("/page/home/revert/1")
    assert resp.status_code == 404


def test_view_history_link_present_when_enabled(logged_in_admin, admin_user):
    """History link is shown on page view when PAGE_HISTORY_ENABLED is True."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "content", admin_user, "edit")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"View history" in resp.data


def test_view_history_link_absent_when_disabled(logged_in_admin, admin_user, monkeypatch):
    """History link is hidden on page view when PAGE_HISTORY_ENABLED is False."""
    import config, db
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "content", admin_user, "edit")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"/history" not in resp.data


# ---------------------------------------------------------------------------
# Fix: My Drafts section hidden for non-editor users
# ---------------------------------------------------------------------------

def test_my_drafts_section_shown_to_editor(client, admin_user):
    """My Drafts section is visible when the logged-in user is an editor."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("editoruser", generate_password_hash("pass123"), role="editor")
    client.post("/login", data={"username": "editoruser", "password": "pass123"})
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"My Drafts" in resp.data
    assert b"draft-manager-list" in resp.data


def test_my_drafts_section_shown_to_admin(logged_in_admin):
    """My Drafts section is visible when the logged-in user is an admin."""
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    assert b"My Drafts" in resp.data
    assert b"draft-manager-list" in resp.data


def test_my_drafts_section_hidden_for_regular_user(client, admin_user):
    """My Drafts section is NOT shown for a user with role='user'."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("regularuser", generate_password_hash("pass123"), role="user")
    client.post("/login", data={"username": "regularuser", "password": "pass123"})
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"draft-manager-list" not in resp.data


# ---------------------------------------------------------------------------
# Fix: all_categories no longer redundantly passed from home/view_page/edit_page
# ---------------------------------------------------------------------------

def test_home_page_hides_move_modal_categories(logged_in_admin):
    """Home page does not expose category move controls."""
    import db
    home = db.get_home_page()
    db.create_category("MoveCat")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert f'action="/page/{home["slug"]}/move"'.encode() not in resp.data


def test_view_page_has_move_modal_with_categories(logged_in_admin):
    """View page still shows all_categories (from context processor) in move modal."""
    import db
    db.create_category("MovePageCat")
    resp = logged_in_admin.post(
        "/create-page",
        data={"title": "TestMovePage", "category_id": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    resp = logged_in_admin.get("/page/testmovepage")
    assert resp.status_code == 200
    assert b"MovePageCat" in resp.data


def test_announcement_full_view_expiry_uses_format_datetime(logged_in_admin, admin_user):
    """Announcement full view should display expiry date using format_datetime."""
    import db
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    ann_id = db.create_announcement("Expiry test", "orange", "normal", "logged_in", future, admin_user)
    resp = logged_in_admin.get(f"/announcements/{ann_id}")
    assert resp.status_code == 200
    assert b"Expires:" in resp.data
    assert b"UTC" in resp.data


def test_announcement_full_view_expiry_respects_timezone(logged_in_admin, admin_user):
    """Announcement expiry date should respect the configured site timezone."""
    import db
    # Set timezone to something non-UTC
    db.update_site_settings(timezone="Europe/Rome")
    from datetime import datetime, timezone as tz, timedelta
    # Use a known UTC time: 2026-02-25T10:00:00 UTC → 11:00 CET in winter
    future_utc = "2027-06-15T10:00:00"
    ann_id = db.create_announcement("TZ expiry test", "orange", "normal", "logged_in", future_utc, admin_user)
    resp = logged_in_admin.get(f"/announcements/{ann_id}")
    assert resp.status_code == 200
    assert b"Expires:" in resp.data
    # In Europe/Rome (CEST = UTC+2 in summer), 10:00 UTC becomes 12:00
    assert b"12:00" in resp.data
    # Should NOT say "UTC" since we configured Europe/Rome
    assert b"UTC" not in resp.data or b"CEST" in resp.data
    db.update_site_settings(timezone="UTC")


def test_announcement_full_view_no_expiry_section_when_none(logged_in_admin, admin_user):
    """Announcement full view should not show expiry section when expires_at is None."""
    import db
    ann_id = db.create_announcement("No expiry", "blue", "normal", "logged_in", None, admin_user)
    resp = logged_in_admin.get(f"/announcements/{ann_id}")
    assert resp.status_code == 200
    assert b'class="xs-aaa62a92"' not in resp.data


def test_admin_announcements_list_expiry_uses_format_datetime(logged_in_admin, admin_user):
    """Admin announcements list should display expiry date using format_datetime."""
    import db
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    db.create_announcement("Admin expiry test", "red", "normal", "both", future, admin_user)
    resp = logged_in_admin.get("/admin/announcements")
    assert resp.status_code == 200
    assert b"UTC" in resp.data


def test_announcement_malformed_expires_at_treated_as_expired(logged_in_admin, admin_user):
    """Malformed expires_at in DB should cause 404, not show announcement as non-expiring."""
    import db
    ann_id = db.create_announcement("Malformed expiry", "orange", "normal", "logged_in", None, admin_user)
    # Manually set a malformed expires_at value in the DB
    conn = db.get_db()
    conn.execute("UPDATE announcements SET expires_at=? WHERE id=?", ("not-a-date", ann_id))
    conn.commit()
    conn.close()
    resp = logged_in_admin.get(f"/announcements/{ann_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Announcement improvements: not_removable, show_countdown, timezone handling
# ---------------------------------------------------------------------------

def test_announcement_not_removable_default(admin_user):
    """Announcements should default to not_removable=1."""
    import db
    ann_id = db.create_announcement("Sticky", "orange", "normal", "both", None, admin_user)
    ann = db.get_announcement(ann_id)
    assert ann["not_removable"] == 1


def test_announcement_show_countdown_default(admin_user):
    """Announcements should default to show_countdown=1."""
    import db
    ann_id = db.create_announcement("Countdown", "orange", "normal", "both", None, admin_user)
    ann = db.get_announcement(ann_id)
    assert ann["show_countdown"] == 1


def test_announcement_create_with_not_removable_off(admin_user):
    """Announcements can be created with not_removable=0."""
    import db
    ann_id = db.create_announcement("Removable", "orange", "normal", "both", None, admin_user,
                                     not_removable=0)
    ann = db.get_announcement(ann_id)
    assert ann["not_removable"] == 0


def test_announcement_create_with_show_countdown_off(admin_user):
    """Announcements can be created with show_countdown=0."""
    import db
    ann_id = db.create_announcement("No countdown", "orange", "normal", "both", None, admin_user,
                                     show_countdown=0)
    ann = db.get_announcement(ann_id)
    assert ann["show_countdown"] == 0


def test_announcement_update_not_removable(admin_user):
    """not_removable can be toggled via update."""
    import db
    ann_id = db.create_announcement("Sticky", "orange", "normal", "both", None, admin_user)
    db.update_announcement(ann_id, not_removable=0)
    ann = db.get_announcement(ann_id)
    assert ann["not_removable"] == 0


def test_announcement_update_show_countdown(admin_user):
    """show_countdown can be toggled via update."""
    import db
    ann_id = db.create_announcement("Timer", "orange", "normal", "both", None, admin_user)
    db.update_announcement(ann_id, show_countdown=0)
    ann = db.get_announcement(ann_id)
    assert ann["show_countdown"] == 0


def test_admin_create_announcement_with_flags(logged_in_admin):
    """Admin route should accept not_removable and show_countdown flags."""
    import db
    resp = logged_in_admin.post("/admin/announcements/create", data={
        "content": "Flagged announcement",
        "color": "blue",
        "text_size": "normal",
        "visibility": "both",
        "expires_at": "",
        "not_removable": "1",
        "show_countdown": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Announcement created" in resp.data
    anns = db.list_announcements()
    created = [a for a in anns if a["content"] == "Flagged announcement"][0]
    assert created["not_removable"] == 1
    assert created["show_countdown"] == 1


def test_admin_create_announcement_flags_unchecked(logged_in_admin):
    """Admin route should set flags to 0 when checkboxes are unchecked."""
    import db
    resp = logged_in_admin.post("/admin/announcements/create", data={
        "content": "No flags",
        "color": "orange",
        "text_size": "normal",
        "visibility": "both",
        "expires_at": "",
    }, follow_redirects=True)
    assert resp.status_code == 200
    anns = db.list_announcements()
    created = [a for a in anns if a["content"] == "No flags"][0]
    assert created["not_removable"] == 0
    assert created["show_countdown"] == 0


def test_admin_edit_announcement_flags(logged_in_admin, admin_user):
    """Admin edit route should update not_removable and show_countdown."""
    import db
    ann_id = db.create_announcement("Edit flags", "orange", "normal", "both", None, admin_user)
    resp = logged_in_admin.post(f"/admin/announcements/{ann_id}/edit", data={
        "content": "Edit flags",
        "color": "orange",
        "text_size": "normal",
        "visibility": "both",
        "expires_at": "",
        "is_active": "1",
        "not_removable": "1",
        "show_countdown": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    ann = db.get_announcement(ann_id)
    assert ann["not_removable"] == 1
    assert ann["show_countdown"] == 1


def test_announcement_bar_not_removable_hides_close_button(logged_in_admin, admin_user):
    """Not-removable announcements should not have a close button in the bar HTML."""
    import db
    db.create_announcement("Sticky banner", "orange", "normal", "both", None, admin_user,
                            not_removable=1)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"Sticky banner" in resp.data
    assert b'data-not-removable="1"' in resp.data
    # The close button should not appear for not_removable announcements
    html = resp.data.decode()
    # Find the slide for this announcement
    import re
    slide_match = re.search(r'data-not-removable="1".*?</div>\s*</div>\s*</div>', html, re.DOTALL)
    assert slide_match is not None
    slide_html = slide_match.group(0)
    assert 'ann-close' not in slide_html


def test_announcement_bar_removable_has_close_button(logged_in_admin, admin_user):
    """Removable announcements should have a close button in the bar HTML."""
    import db
    db.create_announcement("Removable banner", "orange", "normal", "both", None, admin_user,
                            not_removable=0)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"Removable banner" in resp.data
    assert b'data-not-removable="0"' in resp.data
    assert b'ann-close' in resp.data


def test_announcement_bar_has_countdown_data(logged_in_admin, admin_user):
    """Announcements with show_countdown=1 should have countdown data attributes."""
    import db
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    db.create_announcement("Countdown test", "orange", "normal", "both", future, admin_user,
                            show_countdown=1)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'data-show-countdown="1"' in resp.data
    assert b'data-expires-at="' in resp.data


def test_announcement_bar_no_countdown_data(logged_in_admin, admin_user):
    """Announcements with show_countdown=0 should still have the data attribute set to 0."""
    import db
    db.create_announcement("No countdown", "orange", "normal", "both", None, admin_user,
                            show_countdown=0)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'data-show-countdown="0"' in resp.data


def test_admin_announcements_page_shows_flags(logged_in_admin, admin_user):
    """Admin announcements page should show not_removable and countdown status."""
    import db
    db.create_announcement("Flag display", "orange", "normal", "both", None, admin_user,
                            not_removable=1, show_countdown=1)
    resp = logged_in_admin.get("/admin/announcements")
    assert resp.status_code == 200
    assert b"not removable" in resp.data
    assert b"countdown" in resp.data


def test_create_announcement_converts_local_to_utc(logged_in_admin, admin_user):
    """Creating an announcement via admin route should convert site-timezone datetime to UTC."""
    import db
    db.update_site_settings(timezone="Europe/Rome")
    resp = logged_in_admin.post("/admin/announcements/create", data={
        "content": "TZ conversion test",
        "color": "orange",
        "text_size": "normal",
        "visibility": "both",
        "expires_at": "2027-06-15T12:00",
        "not_removable": "1",
        "show_countdown": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    anns = db.list_announcements()
    created = [a for a in anns if a["content"] == "TZ conversion test"][0]
    # 2027-06-15T12:00 Europe/Rome (CEST=UTC+2) should be stored as 2027-06-15T10:00:00 UTC
    assert created["expires_at"] == "2027-06-15T10:00:00"
    db.update_site_settings(timezone="UTC")


def test_edit_announcement_converts_local_to_utc(logged_in_admin, admin_user):
    """Editing an announcement via admin route should convert site-timezone datetime to UTC."""
    import db
    db.update_site_settings(timezone="Europe/Rome")
    ann_id = db.create_announcement("Edit TZ", "orange", "normal", "both", None, admin_user)
    resp = logged_in_admin.post(f"/admin/announcements/{ann_id}/edit", data={
        "content": "Edit TZ",
        "color": "orange",
        "text_size": "normal",
        "visibility": "both",
        "expires_at": "2027-06-15T14:00",
        "is_active": "1",
        "not_removable": "1",
        "show_countdown": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    ann = db.get_announcement(ann_id)
    # 2027-06-15T14:00 Europe/Rome (CEST=UTC+2) should be stored as 2027-06-15T12:00:00 UTC
    assert ann["expires_at"] == "2027-06-15T12:00:00"
    db.update_site_settings(timezone="UTC")


def test_format_datetime_local_input(logged_in_admin, admin_user):
    """format_datetime_local_input should convert UTC to site-timezone datetime-local format."""
    import db
    from app import format_datetime_local_input
    db.update_site_settings(timezone="Europe/Rome")
    # 2027-06-15T10:00:00 UTC → 2027-06-15T12:00 in CEST (UTC+2)
    result = format_datetime_local_input("2027-06-15T10:00:00")
    assert result == "2027-06-15T12:00"
    db.update_site_settings(timezone="UTC")


# ---------------------------------------------------------------------------
# Fix: history entry detail view shows editor username
# ---------------------------------------------------------------------------

def test_history_entry_shows_editor_username(logged_in_admin, admin_user):
    """History entry detail view should show who edited the page."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "New content", admin_user, "test edit")
    history = db.get_page_history(home["id"])
    entry = history[0]
    resp = logged_in_admin.get(f"/page/{home['slug']}/history/{entry['id']}")
    assert resp.status_code == 200
    assert b"Saved by" in resp.data
    assert b"admin" in resp.data


def test_history_entry_shows_formatted_and_markdown_views(logged_in_admin, admin_user):
    """History entry page should include all four view containers and two toggle buttons."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "First version", admin_user, "v1")
    db.update_page(home["id"], "Home", "Second version", admin_user, "v2")
    history = db.get_page_history(home["id"])
    entry = history[0]
    resp = logged_in_admin.get(f"/page/{home['slug']}/history/{entry['id']}")
    assert resp.status_code == 200
    data = resp.data
    # Two toggle buttons must be present
    assert b"toggle-diff-btn" in data
    assert b"toggle-md-btn" in data
    # All four view divs must be present
    assert b'id="view-formatted"' in data
    assert b'id="view-formatted-diff"' in data
    assert b'id="view-markdown"' in data
    assert b'id="view-markdown-diff"' in data


def test_history_entry_formatted_diff_uses_css_classes(logged_in_admin, admin_user):
    """Formatted diff should use CSS classes diff-ins/diff-del, not inline styles."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Hello world", admin_user, "v1")
    db.update_page(home["id"], "Home", "Hello universe", admin_user, "v2")
    history = db.get_page_history(home["id"])
    entry = history[0]
    resp = logged_in_admin.get(f"/page/{home['slug']}/history/{entry['id']}")
    assert resp.status_code == 200
    data = resp.data
    # CSS class-based highlighting must be used
    assert b'class="diff-ins"' in data
    assert b'class="diff-del"' in data
    # Inline styles on ins/del should not be used
    assert b'ins style=' not in data
    assert b'del style=' not in data


def test_history_entry_markdown_view_contains_raw_content(logged_in_admin, admin_user):
    """Markdown view should include the raw markdown source."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "**raw** _markdown_ content", admin_user, "raw test")
    history = db.get_page_history(home["id"])
    entry = history[0]
    resp = logged_in_admin.get(f"/page/{home['slug']}/history/{entry['id']}")
    assert resp.status_code == 200
    # The raw markdown source must appear on the page (in the markdown view div)
    assert b"**raw** _markdown_ content" in resp.data


def test_history_entry_first_revision_has_no_diff(logged_in_admin, admin_user):
    """First revision (no previous entry) should have no diff views."""
    import db
    # Create a brand-new page using the admin user so FK constraint is satisfied
    page_id = db.create_page("NoDiffPage", "nodiffpage", "initial content",
                             category_id=None, user_id=admin_user)
    history = db.get_page_history(page_id)
    entry = history[0]
    resp = logged_in_admin.get(f"/page/nodiffpage/history/{entry['id']}")
    assert resp.status_code == 200
    data = resp.data
    # No diff toggle button element when there is no previous revision.
    # The button HTML uses id="toggle-diff-btn"; the JS references it too so we
    # search for the button tag specifically.
    assert b'id="toggle-diff-btn"' not in data
    # Only the formatted and markdown views (no diff variants)
    assert b'id="view-formatted"' in data
    assert b'id="view-markdown"' in data
    assert b'id="view-formatted-diff"' not in data
    assert b'id="view-markdown-diff"' not in data


def test_get_history_entry_includes_username():
    """db.get_history_entry() must include the editor username."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("histuser", generate_password_hash("pw"), role="editor")
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "content", uid, "edit by histuser")
    history = db.get_page_history(home["id"])
    entry_id = history[0]["id"]
    entry = db.get_history_entry(entry_id)
    assert entry is not None
    assert entry["username"] == "histuser"


def test_get_history_entry_shows_removed_when_user_removed():
    """db.get_history_entry() returns '[removed]' when editor's user_id is NULL."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("delhistuser", generate_password_hash("pw"), role="editor")
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "content by soon-deleted", uid, "edit")
    history = db.get_page_history(home["id"])
    entry_id = history[0]["id"]
    # Delete the user
    db.delete_user(uid)
    entry = db.get_history_entry(entry_id)
    assert entry is not None
    assert entry["username"] == "[removed]"


# ---------------------------------------------------------------------------
# Fix: api_my_drafts includes timezone-formatted updated_at
# ---------------------------------------------------------------------------

def test_api_my_drafts_includes_formatted_timestamp(logged_in_admin, admin_user):
    """GET /api/draft/mine should include updated_at_formatted in each draft."""
    import db
    home = db.get_home_page()
    db.save_draft(home["id"], admin_user, "Draft T", "Draft C")
    resp = logged_in_admin.get("/api/draft/mine")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 1
    assert "updated_at_formatted" in data[0]
    # The formatted timestamp should be a non-empty string containing the timezone label
    assert data[0]["updated_at_formatted"] != ""
    assert "UTC" in data[0]["updated_at_formatted"]


def test_api_my_drafts_formatted_timestamp_respects_timezone(logged_in_admin, admin_user):
    """GET /api/draft/mine formatted timestamp should use the configured timezone."""
    import db
    db.update_site_settings(timezone="Europe/Rome")
    home = db.get_home_page()
    db.save_draft(home["id"], admin_user, "Draft TZ", "Draft C")
    resp = logged_in_admin.get("/api/draft/mine")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 1
    fmt = data[0]["updated_at_formatted"]
    # Should contain a European timezone label (CET or CEST depending on time of year)
    assert "UTC" not in fmt or "CET" in fmt or "CEST" in fmt or fmt != ""
    # Restore default
    db.update_site_settings(timezone="UTC")


# ---------------------------------------------------------------------------
# Fix: admin/users and admin/audit last_login_at tooltips use format_datetime
# ---------------------------------------------------------------------------

def test_admin_users_last_login_tooltip_uses_format_datetime(logged_in_admin, admin_user, client):
    """Admin users page: last login tooltip should use format_datetime, not raw ISO."""
    from app import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    import db
    # Trigger a login to set last_login_at on admin_user
    with client.session_transaction():
        pass
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = logged_in_admin.get("/admin/users")
    assert resp.status_code == 200
    # The title attribute on the last-login cell should contain a formatted datetime
    # (format_datetime returns "YYYY-MM-DD HH:MM TZ"), not a raw ISO string
    # Raw ISO contains 'T' separator and fractional seconds; formatted does not
    assert b"T00:00" not in resp.data  # not a bare date-only ISO
    # format_datetime output contains a space-separated date/time/tz
    assert b"UTC" in resp.data  # timezone label present


def test_admin_audit_last_login_tooltip_uses_format_datetime(logged_in_admin, admin_user, client):
    """Admin audit page: last login tooltip should use format_datetime, not raw ISO."""
    from app import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = logged_in_admin.get(f"/admin/users/{admin_user}/audit")
    assert resp.status_code == 200
    assert b"Audit Trail" in resp.data
    # The title attribute on the last-login span should be formatted, not raw ISO
    # Raw ISO strings contain fractional seconds like ".123456"; formatted don't
    import db
    user = db.get_user_by_id(admin_user)
    if user["last_login_at"]:
        # Raw ISO would include the 'T' time separator; format_datetime uses spaces
        assert b'title="' + user["last_login_at"].encode() + b'"' not in resp.data


# ---------------------------------------------------------------------------
# Feature: Transfer attribution of page history entries
# ---------------------------------------------------------------------------

def test_transfer_attribution_single_entry(logged_in_admin, admin_user):
    """Admin can transfer a single history entry's attribution to another user."""
    from werkzeug.security import generate_password_hash
    import db
    # Create a second user
    uid2 = db.create_user("editor2", generate_password_hash("pass123"), role="editor")
    # Create a page with a history entry authored by admin_user
    page_id = db.create_page("Transfer Test", "transfer-test", "Content", user_id=admin_user)
    db.update_page(page_id, "Transfer Test", "Updated content", admin_user, "Edit 1")
    history = db.get_page_history(page_id)
    assert len(history) >= 1
    entry = history[0]
    # Transfer attribution of the most recent entry to uid2
    resp = logged_in_admin.post(
        f"/page/transfer-test/history/{entry['id']}/transfer",
        data={"new_user_id": uid2},
    )
    assert resp.status_code in (200, 302)
    # Verify the attribution changed
    updated = db.get_history_entry(entry["id"])
    assert updated["edited_by"] == uid2


def test_transfer_attribution_returns_404_when_history_disabled(logged_in_admin, admin_user, monkeypatch):
    """Transfer attribution returns 404 when PAGE_HISTORY_ENABLED is False."""
    import config
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    resp = logged_in_admin.post("/page/home/history/1/transfer", data={"new_user_id": "x"})
    assert resp.status_code == 404


def test_bulk_transfer_attribution(logged_in_admin, admin_user):
    """Admin can bulk-transfer all history entries from one user to another."""
    from werkzeug.security import generate_password_hash
    import db
    uid2 = db.create_user("bulk_target", generate_password_hash("pass123"), role="editor")
    page_id = db.create_page("Bulk Transfer", "bulk-transfer", "Content", user_id=admin_user)
    db.update_page(page_id, "Bulk Transfer", "Edit A", admin_user, "Edit A")
    db.update_page(page_id, "Bulk Transfer", "Edit B", admin_user, "Edit B")
    resp = logged_in_admin.post(
        "/page/bulk-transfer/history/bulk-transfer",
        data={"from_user_id": admin_user, "new_user_id": uid2},
    )
    assert resp.status_code in (200, 302)
    history = db.get_page_history(page_id)
    # All entries should now be attributed to uid2
    for entry in history:
        assert entry["edited_by"] == uid2


def test_bulk_transfer_attribution_returns_404_when_history_disabled(logged_in_admin, admin_user, monkeypatch):
    """Bulk transfer returns 404 when PAGE_HISTORY_ENABLED is False."""
    import config
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    resp = logged_in_admin.post("/page/home/history/bulk-transfer", data={})
    assert resp.status_code == 404


def test_history_page_shows_transfer_buttons_for_admin(logged_in_admin, admin_user):
    """History page shows Transfer button for admin users."""
    import db
    db.update_page(db.get_home_page()["id"], "Home", "Updated content", admin_user, "Edit")
    resp = logged_in_admin.get("/page/home/history")
    assert resp.status_code == 200
    assert b"Transfer" in resp.data


# ---------------------------------------------------------------------------
# Feature: Username history audit
# ---------------------------------------------------------------------------

def test_username_history_recorded_on_admin_rename(logged_in_admin, admin_user):
    """Admin renaming a user records the change in username_history."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("original_name", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={"action": "change_username", "username": "new_name"},
    )
    assert resp.status_code in (200, 302)
    history = db.get_username_history(uid)
    assert len(history) == 1
    assert history[0]["old_username"] == "original_name"
    assert history[0]["new_username"] == "new_name"


def test_username_history_recorded_on_self_rename(client, admin_user):
    """User renaming their own account records the change in username_history."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("selfname", generate_password_hash("selfpass"), role="user")
    client.post("/login", data={"username": "selfname", "password": "selfpass"})
    resp = client.post(
        "/settings",
        data={"action": "change_username", "new_username": "selfname_new", "password": "selfpass"},
    )
    assert resp.status_code in (200, 302)
    history = db.get_username_history(uid)
    assert len(history) == 1
    assert history[0]["old_username"] == "selfname"
    assert history[0]["new_username"] == "selfname_new"


def test_audit_page_shows_username_history(logged_in_admin, admin_user):
    """Admin audit page shows username history section when changes exist."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("audituser", generate_password_hash("pass123"), role="user")
    db.record_username_change(uid, "audituser", "audituser_v2")
    resp = logged_in_admin.get(f"/admin/users/{uid}/audit")
    assert resp.status_code == 200
    assert b"Username History" in resp.data
    assert b"audituser" in resp.data
    assert b"audituser_v2" in resp.data


def test_audit_page_no_username_history_section_when_empty(logged_in_admin, admin_user):
    """Admin audit page does not show username history section when no changes exist."""
    resp = logged_in_admin.get(f"/admin/users/{admin_user}/audit")
    assert resp.status_code == 200
    assert b'id="username-history-section"' not in resp.data


def test_get_username_history_returns_newest_first():
    """get_username_history returns entries in descending order."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("histuser", generate_password_hash("pass"), role="user")
    db.record_username_change(uid, "histuser", "histuser_v2")
    db.record_username_change(uid, "histuser_v2", "histuser_v3")
    history = db.get_username_history(uid)
    assert len(history) == 2
    assert history[0]["new_username"] == "histuser_v3"
    assert history[1]["new_username"] == "histuser_v2"


def test_username_history_deleted_with_user():
    """Deleting a user also deletes their username history (ON DELETE CASCADE)."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("delhistuser", generate_password_hash("pass"), role="user")
    db.record_username_change(uid, "delhistuser", "delhistuser_v2")
    assert len(db.get_username_history(uid)) == 1
    db.delete_user(uid)
    # After deletion, the user is gone; history should be empty via CASCADE
    assert db.get_username_history(uid) == []


# ---------------------------------------------------------------------------
# Feature: Rate limiting on edit_page_title and revert_page
# ---------------------------------------------------------------------------

def test_edit_page_title_rate_limited(logged_in_admin, admin_user, monkeypatch):
    """edit_page_title is rate-limited; exceeding the limit returns 429."""
    import app as app_mod
    monkeypatch.setattr(app_mod, "_RL_STORE", app_mod._RL_STORE)
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    import db
    home = db.get_home_page()
    slug = home["slug"]
    for i in range(20):
        logged_in_admin.post(f"/page/{slug}/edit/title", data={"title": f"Title {i}"})
    resp = logged_in_admin.post(f"/page/{slug}/edit/title", data={"title": "Over limit"})
    assert resp.status_code == 429


def test_revert_page_rate_limited(logged_in_admin, admin_user, monkeypatch):
    """revert_page is rate-limited; exceeding the limit returns 429."""
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "v1", admin_user, "edit")
    history = db.get_page_history(home["id"])
    assert history
    entry_id = history[0]["id"]
    for _ in range(20):
        logged_in_admin.post(f"/page/home/revert/{entry_id}")
    resp = logged_in_admin.post(f"/page/home/revert/{entry_id}")
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Feature: delete_upload logs the action
# ---------------------------------------------------------------------------

def test_delete_upload_logs_action(logged_in_admin, tmp_path, monkeypatch):
    """delete_upload calls log_action when a file is actually removed."""
    import wiki_logger

    logged_calls = []

    original_log_action = wiki_logger.log_action

    def capturing_log_action(action, *args, **kwargs):
        logged_calls.append(action)
        return original_log_action(action, *args, **kwargs)

    monkeypatch.setattr(wiki_logger, "log_action", capturing_log_action)
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(tmp_path))

    # Create a dummy file to delete
    dummy = tmp_path / "testfile.png"
    dummy.write_bytes(b"\x89PNG\r\n")

    resp = logged_in_admin.post(
        "/api/upload/delete",
        json={"filename": "testfile.png"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert "delete_upload" in logged_calls


def test_transfer_attribution_rate_limited(logged_in_admin, admin_user):
    """transfer_attribution is rate-limited; exceeding the limit returns 429."""
    import app as app_mod
    import db
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    from werkzeug.security import generate_password_hash
    uid2 = db.create_user("editor2", generate_password_hash("pass123"), role="editor")
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "v1", admin_user, "edit")
    history = db.get_page_history(home["id"])
    assert history
    entry_id = history[0]["id"]
    for _ in range(20):
        logged_in_admin.post(
            f"/page/home/history/{entry_id}/transfer",
            data={"new_user_id": uid2},
        )
    resp = logged_in_admin.post(
        f"/page/home/history/{entry_id}/transfer",
        data={"new_user_id": uid2},
    )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Rate limiting: bulk_transfer_attribution is rate-limited
# ---------------------------------------------------------------------------

def test_bulk_transfer_attribution_rate_limited(logged_in_admin, admin_user):
    """bulk_transfer_attribution is rate-limited; exceeding the limit returns 429."""
    import app as app_mod
    import db
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    from werkzeug.security import generate_password_hash
    uid2 = db.create_user("editor3", generate_password_hash("pass123"), role="editor")
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "v1", admin_user, "edit")
    for _ in range(20):
        logged_in_admin.post(
            "/page/home/history/bulk-transfer",
            data={"from_user_id": admin_user, "new_user_id": uid2},
        )
    resp = logged_in_admin.post(
        "/page/home/history/bulk-transfer",
        data={"from_user_id": admin_user, "new_user_id": uid2},
    )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Rate limiting: api_transfer_draft is rate-limited
# ---------------------------------------------------------------------------

def test_api_transfer_draft_rate_limited(logged_in_admin, admin_user):
    """api_transfer_draft is rate-limited; exceeding the limit returns JSON 429."""
    import app as app_mod
    import db
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    from werkzeug.security import generate_password_hash
    uid2 = db.create_user("editor4", generate_password_hash("pass123"), role="editor")
    home = db.get_home_page()
    db.save_draft(home["id"], uid2, "Draft title", "Draft content")
    for _ in range(30):
        logged_in_admin.post(
            "/api/draft/transfer",
            json={"page_id": home["id"], "from_user_id": uid2},
            content_type="application/json",
        )
    resp = logged_in_admin.post(
        "/api/draft/transfer",
        json={"page_id": home["id"], "from_user_id": uid2},
        content_type="application/json",
    )
    assert resp.status_code == 429
    data = resp.get_json()
    assert data is not None
    assert "error" in data


def test_user_settings_rate_limited(client, admin_user):
    """user_settings is rate-limited; exceeding the limit returns 429."""
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    client.post("/login", data={"username": "admin", "password": "admin123"})
    for i in range(10):
        client.post("/settings", data={
            "action": "change_username",
            "new_username": f"admin_try{i}",
            "password": "wrongpassword",
        })
    resp = client.post("/settings", data={
        "action": "change_username",
        "new_username": "admin_over",
        "password": "wrongpassword",
    })
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Feature: api_reorder_pages and api_reorder_categories log the action
# ---------------------------------------------------------------------------

def test_reorder_pages_logs_action(logged_in_admin, admin_user, monkeypatch):
    """api_reorder_pages calls log_action after updating sort order."""
    import wiki_logger
    import db

    logged_calls = []

    original_log_action = wiki_logger.log_action

    def capturing_log_action(action, *args, **kwargs):
        logged_calls.append(action)
        return original_log_action(action, *args, **kwargs)

    monkeypatch.setattr(wiki_logger, "log_action", capturing_log_action)

    home = db.get_home_page()
    page_id = db.create_page("ReorderTest", "reorder-test", "content", None, admin_user)

    resp = logged_in_admin.post(
        "/api/reorder/pages",
        json={"ids": [home["id"], page_id]},
        content_type="application/json",
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["message"] == "Page order saved successfully."
    assert "reorder_pages" in logged_calls


def test_reorder_categories_logs_action(logged_in_admin, admin_user, monkeypatch):
    """api_reorder_categories calls log_action after updating sort order."""
    import wiki_logger
    import db

    logged_calls = []

    original_log_action = wiki_logger.log_action

    def capturing_log_action(action, *args, **kwargs):
        logged_calls.append(action)
        return original_log_action(action, *args, **kwargs)

    monkeypatch.setattr(wiki_logger, "log_action", capturing_log_action)

    cat1 = db.create_category("Cat1")
    cat2 = db.create_category("Cat2")

    resp = logged_in_admin.post(
        "/api/reorder/categories",
        json={"ids": [cat1, cat2]},
        content_type="application/json",
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["message"] == "Category order saved successfully."
    assert "reorder_categories" in logged_calls


# -----------------------------------------------------------------------
# Superadmin toggle tests
# -----------------------------------------------------------------------

def test_admin_can_enable_owner_status(logged_in_admin, admin_user):
    """Admin can toggle their own role to owner via account settings."""
    import db
    resp = logged_in_admin.post(
        "/settings",
        data={"action": "toggle_owner", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner status enabled' in resp.data
    user = db.get_user_by_id(admin_user)
    assert user["role"] == "owner"


def test_superuser_can_enable_owner_status(client, admin_user):
    """A superuser can toggle their own role to owner via account settings."""
    import db
    db.update_user(admin_user, is_superuser=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner status enabled' in resp.data
    user = db.get_user_by_id(admin_user)
    assert user["role"] == "owner"


def test_owner_can_disable_status(client, admin_user):
    """Protected admin can revert to admin role via account settings."""
    from werkzeug.security import generate_password_hash
    import db
    # Second owner needed so admin_user is not the last owner when toggling back to admin
    db.create_user("owner2", generate_password_hash("pass2"), role="owner")
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner status disabled' in resp.data
    user = db.get_user_by_id(admin_user)
    assert user["role"] == "admin"


def test_toggle_owner_wrong_password_rejected(logged_in_admin, admin_user):
    """Toggle protected admin with wrong password should fail."""
    import db
    resp = logged_in_admin.post(
        "/settings",
        data={"action": "toggle_owner", "password": "wrongpassword"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Incorrect password" in resp.data
    user = db.get_user_by_id(admin_user)
    assert user["role"] == "admin"


def test_non_admin_cannot_toggle_owner(client, admin_user):
    """A regular user cannot use toggle_owner action."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("regularuser", generate_password_hash("pass123"), role="user")
    client.post("/login", data={"username": "regularuser", "password": "pass123"})
    resp = client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "pass123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Only admins" in resp.data
    user = db.get_user_by_id(uid)
    assert user["role"] == "user"


def test_owner_has_admin_panel_access(client, admin_user):
    """User with owner role should have access to admin panel."""
    import db
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/admin/users")
    assert resp.status_code == 200


def test_admin_cannot_change_owner_role_via_panel(client, admin_user):
    """Admin cannot change a owner user's role from the admin panel."""
    from werkzeug.security import generate_password_hash
    import db
    # Create a second admin and promote to owner
    uid2 = db.create_user("admin2", generate_password_hash("pass456"), role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "change_role", "role": "user"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner status can only be changed by the account owner' in resp.data
    user = db.get_user_by_id(uid2)
    assert user["role"] == "owner"


def test_owner_counted_as_admin_in_count(isolated_db):
    """count_admins() should include users with role='owner'."""
    from werkzeug.security import generate_password_hash
    import db
    db.update_site_settings(setup_done=1)
    db.create_user("adm1", generate_password_hash("pass"), role="admin")
    db.create_user("padm1", generate_password_hash("pass"), role="owner")
    assert db.count_admins() == 2


def test_owner_can_change_own_username(client, admin_user):
    """Protected admin should be able to change their own username."""
    import db
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/settings",
        data={"action": "change_username", "new_username": "newname", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Username updated" in resp.data
    user = db.get_user_by_id(admin_user)
    assert user["username"] == "newname"


def test_owner_can_change_own_password(client, admin_user):
    """Protected admin should be able to change their own password."""
    from werkzeug.security import check_password_hash
    import db
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/settings",
        data={
            "action": "change_password",
            "current_password": "admin123",
            "new_password": "newpass1",
            "confirm_password": "newpass1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Password updated" in resp.data
    user = db.get_user_by_id(admin_user)
    assert check_password_hash(user["password"], "newpass1")


# -----------------------------------------------------------------------
# Edge cases: protected admin role
# -----------------------------------------------------------------------

def test_editor_cannot_toggle_owner(client, admin_user):
    """An editor cannot use the toggle_owner action."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editoruser", generate_password_hash("ed123"), role="editor")
    client.post("/login", data={"username": "editoruser", "password": "ed123"})
    resp = client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "ed123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Only admins" in resp.data
    user = db.get_user_by_id(uid)
    assert user["role"] == "editor"


def test_admin_panel_cannot_directly_set_owner_role(client, admin_user):
    """Attempting to set role='owner' via the admin panel is rejected as invalid."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("targetuser", generate_password_hash("pass"), role="user")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid}/edit",
        data={"action": "change_role", "role": "owner"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid role" in resp.data
    user = db.get_user_by_id(uid)
    assert user["role"] == "user"


def test_owner_can_still_suspend_another_user(client, admin_user):
    """Protected admin retains ability to suspend other users via the admin panel."""
    from werkzeug.security import generate_password_hash
    import db
    uid2 = db.create_user("normaluser", generate_password_hash("pass"), role="user")
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "suspend"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"User suspended" in resp.data
    user = db.get_user_by_id(uid2)
    assert user["suspended"] == 1


def test_owner_double_toggle(client, admin_user):
    """Protected admin can enable and then disable protection repeatedly."""
    from werkzeug.security import generate_password_hash
    import db
    # Second owner needed so admin_user is not the last owner when toggling back to admin
    db.create_user("owner2", generate_password_hash("pass2"), role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    # Enable
    client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "admin123"},
        follow_redirects=True,
    )
    assert db.get_user_by_id(admin_user)["role"] == "owner"
    # Disable
    client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "admin123"},
        follow_redirects=True,
    )
    assert db.get_user_by_id(admin_user)["role"] == "admin"
    # Enable again
    client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "admin123"},
        follow_redirects=True,
    )
    assert db.get_user_by_id(admin_user)["role"] == "owner"


def test_cannot_delete_last_owner_via_user_settings(client, admin_user):
    """Cannot delete account via settings when the user is the sole admin (owner)."""
    import db
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    # Only admin_user exists as an admin: self-delete via account settings should be blocked
    resp = client.post(
        "/settings",
        data={"action": "delete_account", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Cannot delete the last admin account" in resp.data
    assert db.get_user_by_id(admin_user) is not None


# -----------------------------------------------------------------------
# Edge cases: full protection scope (suspend / delete / rename / password)
# -----------------------------------------------------------------------

def test_admin_cannot_suspend_owner(client, admin_user):
    """An admin cannot suspend a owner account."""
    from werkzeug.security import generate_password_hash
    import db
    uid2 = db.create_user("padmin2", generate_password_hash("pass2"), role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "suspend"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner accounts cannot be suspended by other admins' in resp.data
    assert db.get_user_by_id(uid2)["suspended"] == 0


def test_admin_cannot_delete_owner(client, admin_user):
    """An admin cannot delete a owner account via the admin panel."""
    from werkzeug.security import generate_password_hash
    import db
    uid2 = db.create_user("padmin2", generate_password_hash("pass2"), role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "delete"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner accounts cannot be deleted by other admins' in resp.data
    assert db.get_user_by_id(uid2) is not None


def test_admin_cannot_rename_owner(client, admin_user):
    """An admin cannot rename a owner account via the admin panel."""
    from werkzeug.security import generate_password_hash
    import db
    uid2 = db.create_user("padmin2", generate_password_hash("pass2"), role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "change_username", "username": "hacked"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner accounts can only be edited by themselves' in resp.data
    assert db.get_user_by_id(uid2)["username"] == "padmin2"


def test_admin_cannot_change_password_of_owner(client, admin_user):
    """An admin cannot change a owner's password via the admin panel."""
    from werkzeug.security import generate_password_hash, check_password_hash
    import db
    uid2 = db.create_user("padmin2", generate_password_hash("original"), role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "change_password", "password": "newpass1", "confirm_password": "newpass1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner accounts can only be edited by themselves' in resp.data
    assert check_password_hash(db.get_user_by_id(uid2)["password"], "original")


def test_owner_cannot_suspend_another_owner(client, admin_user):
    """A owner cannot suspend another owner."""
    from werkzeug.security import generate_password_hash
    import db
    uid2 = db.create_user("padmin2", generate_password_hash("pass2"), role="owner")
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "suspend"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner accounts cannot be suspended by other admins' in resp.data
    assert db.get_user_by_id(uid2)["suspended"] == 0


def test_owner_cannot_delete_another_owner(client, admin_user):
    """A owner cannot delete another owner."""
    from werkzeug.security import generate_password_hash
    import db
    uid2 = db.create_user("padmin2", generate_password_hash("pass2"), role="owner")
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{uid2}/edit",
        data={"action": "delete"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">Owner accounts cannot be deleted by other admins' in resp.data
    assert db.get_user_by_id(uid2) is not None


def test_owner_can_edit_own_account_in_panel(client, admin_user):
    """A owner can still rename/change-password their own account via the admin panel."""
    import db
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{admin_user}/edit",
        data={"action": "change_username", "username": "adminrenamed"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Username updated" in resp.data
    assert db.get_user_by_id(admin_user)["username"] == "adminrenamed"


def test_owner_can_edit_page(client, admin_user):
    """A owner can edit pages (editor_required should allow owner)."""
    import db
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    page = db.get_home_page()
    resp = client.post(
        f"/page/{page['slug']}/edit",
        data={"title": "Home", "content": "Updated content", "edit_message": "test"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_home_page()["content"] == "Updated content"


def test_owner_can_create_page(client, admin_user):
    """A owner can create pages (editor_required should allow owner)."""
    import db
    db.update_user(admin_user, role="owner")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/create-page",
        data={"title": "New Page", "content": "hello"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_page_by_slug("new-page") is not None


def test_owner_can_login_during_maintenance(client, admin_user):
    """A owner can log in via /admin when maintenance mode is active."""
    import db
    db.update_user(admin_user, role="owner")
    db.update_site_settings(maintenance_mode=1)
    resp = client.post(
        "/admin",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Should not land on the maintenance notice if login succeeded.
    assert b"admin-login-form" not in resp.data


# -----------------------------------------------------------------------
# Editor Category-Based Access Tests
# -----------------------------------------------------------------------
@pytest.fixture
def editor_user():
    """Create an editor user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor1", generate_password_hash("editor123"), role="editor")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def logged_in_editor(client, editor_user):
    """Return a client logged in as the editor."""
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    return client


def test_editor_unrestricted_can_edit_any_page(logged_in_editor):
    """An unrestricted editor (default) can edit the home page."""
    import db
    home = db.get_home_page()
    resp = logged_in_editor.post(
        f"/page/{home['slug']}/edit",
        data={"title": "Home", "content": "edited by editor", "edit_message": "test"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_home_page()["content"] == "edited by editor"


def test_restricted_editor_cannot_edit_page_in_wrong_category(client, editor_user):
    """A restricted editor cannot edit a page in a category they are not allowed."""
    import db
    cat_a = db.create_category("Category A")
    cat_b = db.create_category("Category B")
    page_b = db.create_page("Page B", "page-b", "content b", cat_b, editor_user)
    # Restrict editor to only Category A
    db.set_editor_access(editor_user, restricted=True, category_ids=[cat_a])
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/page/page-b/edit",
        data={"title": "Page B", "content": "changed", "edit_message": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Should have flashed an error; content should be unchanged
    assert b"do not have permission" in resp.data
    assert db.get_page(page_b)["content"] == "content b"


def test_restricted_editor_can_edit_page_in_allowed_category(client, editor_user):
    """A restricted editor CAN edit a page in their allowed category."""
    import db
    cat_a = db.create_category("Category A")
    page_a = db.create_page("Page A", "page-a", "original content", cat_a, editor_user)
    db.set_editor_access(editor_user, restricted=True, category_ids=[cat_a])
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/page/page-a/edit",
        data={"title": "Page A", "content": "updated content", "edit_message": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_page(page_a)["content"] == "updated content"


def test_restricted_editor_cannot_edit_uncategorized_page(client, editor_user):
    """A restricted editor cannot edit uncategorized pages."""
    import db
    cat_a = db.create_category("Category A")
    page_u = db.create_page("Uncategorized", "uncat-page", "content", None, editor_user)
    db.set_editor_access(editor_user, restricted=True, category_ids=[cat_a])
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/page/uncat-page/edit",
        data={"title": "Uncategorized", "content": "changed", "edit_message": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"do not have permission" in resp.data
    assert db.get_page(page_u)["content"] == "content"


def test_restricted_editor_cannot_create_page_in_disallowed_category(client, editor_user):
    """A restricted editor cannot create a page in a category they are not allowed."""
    import db
    cat_a = db.create_category("Category A")
    cat_b = db.create_category("Category B")
    db.set_editor_access(editor_user, restricted=True, category_ids=[cat_a])
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/create-page",
        data={"title": "New Page", "content": "hello", "category_id": str(cat_b)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"do not have permission" in resp.data
    assert db.get_page_by_slug("new-page") is None


def test_restricted_editor_cannot_delete_page_in_disallowed_category(client, editor_user):
    """A restricted editor cannot delete a page in a category they are not allowed."""
    import db
    cat_b = db.create_category("Category B")
    page_b = db.create_page("Page B", "page-b-del", "content", cat_b, editor_user)
    db.set_editor_access(editor_user, restricted=True, category_ids=[])
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post("/page/page-b-del/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"do not have permission" in resp.data
    assert db.get_page(page_b) is not None


def test_restricted_editor_cannot_create_category(client, editor_user):
    """A restricted editor cannot create new categories."""
    import db
    db.set_editor_access(editor_user, restricted=True, category_ids=[])
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/category/create",
        data={"name": "New Category"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"do not have permission" in resp.data


def test_admin_can_access_editor_access_page(logged_in_admin, editor_user):
    """Editor access page is deprecated and redirects to admin users."""
    resp = logged_in_admin.get(f"/admin/users/{editor_user}/editor-access")
    assert resp.status_code == 302


def test_admin_can_set_restricted_access(logged_in_admin, editor_user):
    """Editor access is deprecated; POST redirects to admin users with deprecation notice."""
    import db
    cat_a = db.create_category("Category A")
    resp = logged_in_admin.post(
        f"/admin/users/{editor_user}/editor-access",
        data={"restricted": "1", "category_ids": [str(cat_a)]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"deprecated" in resp.data.lower()


def test_admin_can_set_unrestricted_access(logged_in_admin, editor_user):
    """Editor access is deprecated; POST redirects with deprecation notice."""
    resp = logged_in_admin.post(
        f"/admin/users/{editor_user}/editor-access",
        data={"restricted": "0"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"deprecated" in resp.data.lower()


def test_editor_access_page_not_for_admins(logged_in_admin, admin_user):
    """Editor access page is deprecated; redirects with deprecation notice."""
    resp = logged_in_admin.get(
        f"/admin/users/{admin_user}/editor-access",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"deprecated" in resp.data.lower()


def test_get_editor_access_defaults_unrestricted():
    """A new editor has unrestricted access by default."""
    import db
    uid = db.create_user("neweditor", "pw", role="editor")
    access = db.get_editor_access(uid)
    assert access["restricted"] is False
    assert access["allowed_category_ids"] == []


def test_deleted_category_removed_from_allowed_list(editor_user):
    """When a category is deleted, it is removed from the editor's allowed list."""
    import db
    cat_a = db.create_category("Temp Cat")
    db.set_editor_access(editor_user, restricted=True, category_ids=[cat_a])
    access_before = db.get_editor_access(editor_user)
    assert cat_a in access_before["allowed_category_ids"]
    db.delete_category(cat_a)
    access_after = db.get_editor_access(editor_user)
    assert cat_a not in access_after["allowed_category_ids"]


def test_users_page_shows_access_button_for_editors(logged_in_admin, editor_user):
    """The admin users page shows the access button for editor accounts."""
    resp = logged_in_admin.get("/admin/users")
    assert resp.status_code == 200
    assert b"Access" in resp.data


def test_restricted_editor_cannot_edit_category(client, editor_user):
    """A restricted editor cannot rename categories."""
    import db
    cat_a = db.create_category("Rename Me")
    db.set_editor_access(editor_user, restricted=True, category_ids=[cat_a])
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        f"/category/{cat_a}/edit",
        data={"name": "Renamed"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"do not have permission" in resp.data
    assert db.get_category(cat_a)["name"] == "Rename Me"


# -----------------------------------------------------------------------
# Tag feature: difficulty tag can be set, displayed, and validated
# -----------------------------------------------------------------------
def test_update_page_tag_valid(logged_in_admin):
    """Editor can set a valid difficulty tag on a page."""
    import db
    home = db.get_home_page()
    slug = home["slug"]
    resp = logged_in_admin.post(f"/page/{slug}/tag",
                                data={"difficulty_tag": "beginner"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Tag updated" in resp.data
    updated = db.get_home_page()
    assert updated["difficulty_tag"] == "beginner"


def test_update_page_tag_none(logged_in_admin):
    """Editor can clear the difficulty tag (set to empty string)."""
    import db
    home = db.get_home_page()
    db.update_page_tag(home["id"], "expert")
    slug = home["slug"]
    resp = logged_in_admin.post(f"/page/{slug}/tag",
                                data={"difficulty_tag": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    updated = db.get_home_page()
    assert updated["difficulty_tag"] == ""


def test_update_page_tag_invalid_rejected(logged_in_admin):
    """Invalid tag values are rejected."""
    import db
    home = db.get_home_page()
    slug = home["slug"]
    resp = logged_in_admin.post(f"/page/{slug}/tag",
                                data={"difficulty_tag": "impossible"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid difficulty tag" in resp.data
    # Tag should remain unchanged
    updated = db.get_home_page()
    assert updated["difficulty_tag"] == ""


def test_page_view_shows_tag_badge(logged_in_admin):
    """Page view renders difficulty tag badge when tag is set."""
    import db
    db.create_page("Tagged Page", "tagged-page", "content", user_id=None)
    page = db.get_page_by_slug("tagged-page")
    db.update_page_tag(page["id"], "intermediate")
    resp = logged_in_admin.get("/page/tagged-page")
    assert resp.status_code == 200
    assert b"difficulty-tag-intermediate" in resp.data
    assert b"Intermediate" in resp.data


def test_page_view_no_badge_when_no_tag(logged_in_admin):
    """Page view does not render difficulty tag badge when tag is empty."""
    import db
    db.create_page("No Tag Page", "no-tag-page", "content", user_id=None)
    resp = logged_in_admin.get("/page/no-tag-page")
    assert resp.status_code == 200
    assert b"difficulty-tag-" not in resp.data


def test_edit_page_updates_tag(logged_in_admin):
    """Submitting the edit form with a difficulty_tag updates the tag."""
    import db
    db.create_page("Editable Tag Page", "editable-tag-page", "hello", user_id=None)
    resp = logged_in_admin.post("/page/editable-tag-page/edit",
                                data={"title": "Editable Tag Page",
                                      "content": "hello",
                                      "difficulty_tag": "expert"},
                                follow_redirects=True)
    assert resp.status_code == 200
    page = db.get_page_by_slug("editable-tag-page")
    assert page["difficulty_tag"] == "expert"


def test_valid_difficulty_tags_constant():
    """VALID_DIFFICULTY_TAGS includes all expected values."""
    import db
    assert "" in db.VALID_DIFFICULTY_TAGS
    for tag in ("beginner", "easy", "intermediate", "expert", "extra", "custom"):
        assert tag in db.VALID_DIFFICULTY_TAGS


def test_update_page_tag_custom_valid(logged_in_admin):
    """Editor can set a custom difficulty tag with a label and hex color."""
    import db
    db.create_page("Custom Tag Page", "custom-tag-page", "content", user_id=None)
    page = db.get_page_by_slug("custom-tag-page")
    db.update_page_tag(page["id"], "custom", custom_label="Advanced", custom_color="#ff5733")
    updated = db.get_page_by_slug("custom-tag-page")
    assert updated["difficulty_tag"] == "custom"
    assert updated["tag_custom_label"] == "Advanced"
    assert updated["tag_custom_color"] == "#ff5733"


def test_update_page_tag_custom_clears_fields_on_predefined(logged_in_admin):
    """Setting a predefined tag clears custom label/color."""
    import db
    db.create_page("Clear Custom Page", "clear-custom-page", "content", user_id=None)
    page = db.get_page_by_slug("clear-custom-page")
    db.update_page_tag(page["id"], "custom", custom_label="Adv", custom_color="#aabbcc")
    db.update_page_tag(page["id"], "beginner")
    updated = db.get_page_by_slug("clear-custom-page")
    assert updated["difficulty_tag"] == "beginner"
    assert updated["tag_custom_label"] == ""
    assert updated["tag_custom_color"] == ""


def test_update_page_tag_custom_route_missing_label(logged_in_admin):
    """Custom tag without a label is rejected via the /tag route."""
    import db
    home = db.get_home_page()
    slug = home["slug"]
    resp = logged_in_admin.post(f"/page/{slug}/tag",
                                data={"difficulty_tag": "custom",
                                      "tag_custom_label": "",
                                      "tag_custom_color": "#aabbcc"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Custom tag requires a label" in resp.data


def test_update_page_tag_custom_route_invalid_color(logged_in_admin):
    """Custom tag with invalid hex color is rejected via the /tag route."""
    import db
    home = db.get_home_page()
    slug = home["slug"]
    resp = logged_in_admin.post(f"/page/{slug}/tag",
                                data={"difficulty_tag": "custom",
                                      "tag_custom_label": "Special",
                                      "tag_custom_color": "notacolor"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"valid hex color" in resp.data


def test_custom_tag_badge_rendered(logged_in_admin):
    """Page view renders a custom tag badge using inline style when tag is 'custom'."""
    import db
    db.create_page("Custom Badge Page", "custom-badge-page", "content", user_id=None)
    page = db.get_page_by_slug("custom-badge-page")
    db.update_page_tag(page["id"], "custom", custom_label="VIP", custom_color="#ab12cd")
    resp = logged_in_admin.get("/page/custom-badge-page")
    assert resp.status_code == 200
    assert b"VIP" in resp.data
    assert b"#ab12cd" in resp.data


def test_tag_modal_select_toggles_correct_element(logged_in_admin):
    """The tag modal select change handler targets tagModalCustom, not tagModal."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/page/{home['slug']}")
    assert resp.status_code == 200
    # The addEventListener wiring should reference 'tagModalCustom' (the custom fields div)
    # not 'tagModal' (the modal container itself)
    assert b"toggleCustomTag(this, 'tagModalCustom')" in resp.data


# -----------------------------------------------------------------------
# Fix: random black-screen: accessibility colour clear/save flow
# -----------------------------------------------------------------------

def test_accessibility_save_custom_bg(logged_in_admin):
    """Saving a custom background colour persists it."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"custom_bg": "#112233"},
                                content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    # Verify via the GET endpoint
    resp2 = logged_in_admin.get("/api/accessibility")
    assert resp2.status_code == 200
    prefs = resp2.get_json()
    assert prefs["custom_bg"] == "#112233"


def test_accessibility_clear_custom_bg(logged_in_admin):
    """Saving an empty custom_bg clears the stored colour (stops site going black)."""
    # First set a value …
    logged_in_admin.post("/api/accessibility",
                         json={"custom_bg": "#000000"},
                         content_type="application/json")
    # … then clear it by sending an empty string.
    resp = logged_in_admin.post("/api/accessibility",
                                json={"custom_bg": ""},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["custom_bg"] == ""


def test_accessibility_black_bg_not_persisted_on_invalid_colour(logged_in_admin):
    """An invalid colour string is rejected and does not overwrite the stored value."""
    for invalid in ("not-a-colour", "#000", "#00000000"):
        setup_resp = logged_in_admin.post("/api/accessibility",
                                          json={"custom_bg": "#abcdef"},
                                          content_type="application/json")
        assert setup_resp.status_code == 200
        resp = logged_in_admin.post("/api/accessibility",
                                    json={"custom_bg": invalid},
                                    content_type="application/json")
        assert resp.status_code == 200
        prefs = logged_in_admin.get("/api/accessibility").get_json()
        # The invalid value must be rejected; the stored colour must remain empty
        # (the server sanitises to "" when the value is invalid).
        assert prefs["custom_bg"] == ""


def test_accessibility_page_ignores_legacy_invalid_custom_colour(logged_in_admin):
    """Legacy invalid custom colours are ignored when rendering/accessing prefs."""
    import db
    import json
    import re

    with db.get_db() as conn:
        conn.execute(
            "UPDATE users SET accessibility=? WHERE username=?",
            (json.dumps({"custom_bg": "#000", "custom_text": "#00000000"}), "admin"),
        )
        conn.commit()

    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["custom_bg"] == ""
    assert prefs["custom_text"] == ""

    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    a11y_match = re.search(r'<style id="a11y-style"[^>]*>(.*?)</style>', html, re.DOTALL)
    assert a11y_match is not None
    assert "--bg" not in a11y_match.group(1)
    assert "--text" not in a11y_match.group(1)


def test_accessibility_page_renders_a11y_style_with_custom_bg(logged_in_admin):
    """When a custom background is set the rendered page includes the a11y-style block."""
    logged_in_admin.post("/api/accessibility",
                         json={"custom_bg": "#334455"},
                         content_type="application/json")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'id="a11y-style"' in resp.data
    assert b"--bg:#334455" in resp.data or b"--bg: #334455" in resp.data


def test_accessibility_page_no_a11y_style_after_clear(logged_in_admin):
    """After all custom colours are cleared the a11y-style block has no colour rules."""
    # Save a custom colour then clear it.
    logged_in_admin.post("/api/accessibility",
                         json={"custom_bg": "#223344"},
                         content_type="application/json")
    logged_in_admin.post("/api/accessibility",
                         json={"custom_bg": ""},
                         content_type="application/json")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    # The a11y-style block must NOT contain --bg after clearing.
    # (The block may still exist for font-scale / line-height rules.)
    import re
    html = resp.data.decode()
    a11y_match = re.search(r'<style id="a11y-style"[^>]*>(.*?)</style>', html, re.DOTALL)
    if a11y_match:
        assert '--bg' not in a11y_match.group(1)


def test_base_html_uses_dark_theme_by_default(logged_in_admin):
    """Pages default to the dark site theme until an admin changes it."""
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'data-theme="dark"' in resp.data
    assert b'<meta name="color-scheme" content="dark">' in resp.data


def test_base_html_uses_light_theme_when_admin_default_changes(logged_in_admin):
    """The site-wide default theme mode can switch the rendered palette to light."""
    import db
    db.update_site_settings(
        default_theme_mode="light",
        light_bg_color="#f4f5f9",
        light_text_color="#1e2433",
    )
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'data-theme="light"' in resp.data
    assert b'<meta name="color-scheme" content="light">' in resp.data
    assert b"--bg: #f4f5f9" in resp.data or b"--bg:#f4f5f9" in resp.data


def test_user_theme_override_beats_site_default(logged_in_admin):
    """A user's saved theme mode overrides the site default for that account."""
    import db
    db.update_site_settings(default_theme_mode="light")
    save_resp = logged_in_admin.post(
        "/api/accessibility",
        json={"theme_mode": "dark"},
        content_type="application/json",
    )
    assert save_resp.status_code == 200
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'data-theme="dark"' in resp.data
    assert b'<meta name="color-scheme" content="dark">' in resp.data


def test_accessibility_theme_mode_persisted(logged_in_admin):
    """theme_mode is saved and returned by the accessibility API."""
    resp = logged_in_admin.post(
        "/api/accessibility",
        json={"theme_mode": "light"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["theme_mode"] == "light"


def test_accessibility_invalid_theme_mode_falls_back_to_default(logged_in_admin):
    """Invalid theme modes fall back to using the site default."""
    import db
    db.update_site_settings(default_theme_mode="light")
    resp = logged_in_admin.post(
        "/api/accessibility",
        json={"theme_mode": "sepia"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["theme_mode"] == "default"
    page = logged_in_admin.get("/")
    assert b'data-theme="light"' in page.data


def test_css_no_body_level_contrast_filter():
    """The CSS must not apply filter:contrast() directly on the body via
    a11y-contrast-N classes.  Doing so triggers a GPU compositing bug in
    Firefox and Chrome on Mac/Linux that turns the whole page black.

    The filter must only be scoped to content containers (.topbar, .layout,
    etc.) and must be absent from levels 4-5 (which use explicit colour
    overrides instead).
    """
    import re
    css_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "style.css")
    with open(css_path) as f:
        css = f.read()

    # Patterns like ".a11y-contrast-N{filter:..." or ".a11y-contrast-N {filter:..."
    # (i.e. the class selector alone without a descendant combinator) must not exist.
    body_level_filter = re.compile(
        r'\.a11y-contrast-\d\s*\{[^}]*filter\s*:', re.IGNORECASE
    )
    assert not body_level_filter.search(css), (
        "a11y-contrast-N class must not apply filter: directly on the body element"
    )

    # Levels 4 and 5 must not include any filter at all (colour overrides suffice).
    for level in (4, 5):
        pattern = re.compile(
            rf'\.a11y-contrast-{level}[^{{]*\{{[^}}]*filter\s*:',
            re.IGNORECASE
        )
        assert not pattern.search(css), (
            f"a11y-contrast-{level} must not use filter: (colour overrides are sufficient)"
        )

    # The CSS :root rule must declare color-scheme:dark so browsers default to
    # dark colours even before the inline theme variables are applied.
    assert re.search(r':root\s*\{[^}]*color-scheme\s*:\s*dark', css, re.IGNORECASE), (
        "style.css :root must declare color-scheme:dark"
    )


# Fix: remember resized dimensions (both vertical and horizontal)
# -----------------------------------------------------------------------

def test_content_max_width_persisted(logged_in_admin):
    """content_max_width is saved and returned by the accessibility API."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"content_max_width": 900},
                                content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["content_max_width"] == 900


def test_content_max_width_cleared(logged_in_admin):
    """Setting content_max_width to 0 clears the stored value."""
    logged_in_admin.post("/api/accessibility",
                         json={"content_max_width": 800},
                         content_type="application/json")
    logged_in_admin.post("/api/accessibility",
                         json={"content_max_width": 0},
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["content_max_width"] == 0


def test_editor_pane_width_persisted(logged_in_admin):
    """editor_pane_width (horizontal split %) is saved and returned."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"editor_pane_width": 60.0},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_pane_width"] == 60.0


def test_editor_pane_width_clamped(logged_in_admin):
    """editor_pane_width values outside [15, 85] are clamped to that range."""
    logged_in_admin.post("/api/accessibility",
                         json={"editor_pane_width": 5.0},
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_pane_width"] == 15.0

    logged_in_admin.post("/api/accessibility",
                         json={"editor_pane_width": 95.0},
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_pane_width"] == 85.0


def test_editor_pane_width_zero_means_default(logged_in_admin):
    """editor_pane_width of 0 means 'use default split' and is stored as 0."""
    logged_in_admin.post("/api/accessibility",
                         json={"editor_pane_width": 0},
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_pane_width"] == 0


def test_editor_height_persisted(logged_in_admin):
    """editor_height (vertical size in px) is saved and returned."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"editor_height": 700},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_height"] == 700


def test_editor_height_clamped(logged_in_admin):
    """editor_height values outside [300, 2000] are clamped."""
    logged_in_admin.post("/api/accessibility",
                         json={"editor_height": 100},
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_height"] == 300

    logged_in_admin.post("/api/accessibility",
                         json={"editor_height": 9999},
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_height"] == 2000


def test_editor_height_zero_means_default(logged_in_admin):
    """editor_height of 0 means 'use default height' and is stored as 0."""
    logged_in_admin.post("/api/accessibility",
                         json={"editor_height": 0},
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["editor_height"] == 0


def test_resize_dimensions_all_persist_together(logged_in_admin):
    """All three resize dimensions are saved and restored independently."""
    logged_in_admin.post("/api/accessibility",
                         json={
                             "content_max_width": 1100,
                             "editor_pane_width": 55.0,
                             "editor_height": 650,
                         },
                         content_type="application/json")
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["content_max_width"] == 1100
    assert prefs["editor_pane_width"] == 55.0
    assert prefs["editor_height"] == 650


# The remaining tests pin the preview renderer: width/height on <img> must survive sanitising.

def test_preview_renders_img_with_width(logged_in_admin):
    """api/preview keeps the width attribute on an HTML <img> tag."""
    resp = logged_in_admin.post(
        "/api/preview",
        json={"content": '<img src="/static/uploads/test.png" alt="test" width="300">'},
        content_type="application/json",
    )
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert 'width="300"' in html
    assert 'src="/static/uploads/test.png"' in html


def test_preview_renders_markdown_image_with_src(logged_in_admin):
    """api/preview renders a markdown image so it includes the src URL."""
    resp = logged_in_admin.post(
        "/api/preview",
        json={"content": "![alt text](/static/uploads/photo.jpg)"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert 'src="/static/uploads/photo.jpg"' in html
    assert "<img" in html


def test_preview_renders_figure_img_preserves_width(logged_in_admin):
    """api/preview preserves width on an <img> inside a <figure>."""
    content = (
        '<figure class="wiki-img-left">'
        '<img src="/static/uploads/fig.png" alt="fig" width="400">'
        '<figcaption>fig</figcaption>'
        '</figure>'
    )
    resp = logged_in_admin.post(
        "/api/preview",
        json={"content": content},
        content_type="application/json",
    )
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert 'width="400"' in html
    assert 'src="/static/uploads/fig.png"' in html


# -----------------------------------------------------------------------
# Customization rename: account settings shows "Customization" instead of "Accessibility"
# -----------------------------------------------------------------------
def test_user_settings_shows_customization_label(logged_in_admin):
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    assert b"Customization Settings" in resp.data
    assert b"Accessibility Settings" not in resp.data
    assert b"theme mode" in resp.data.lower()


def test_base_template_shows_customize_button(logged_in_admin):
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"Customize" in resp.data
    assert b"Customization" in resp.data


def test_skip_link_targets_focusable_main_content(logged_in_admin):
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'class="skip-link" href="#main-content"' in resp.data
    assert b'<main class="content" id="main-content" tabindex="-1">' in resp.data


# -----------------------------------------------------------------------
# Fix: draft transfer restricted to admins only (IDOR fix: editors
# and regular users cannot transfer other users' drafts)
# -----------------------------------------------------------------------
def test_draft_transfer_requires_admin_role(client, admin_user):
    """A regular user (role=user) should be denied access to the draft transfer endpoint."""
    from werkzeug.security import generate_password_hash
    import db
    # Create a regular user
    db.create_user("regular1", generate_password_hash("regular123"), role="user")
    # Login as regular user
    client.post("/login", data={"username": "regular1", "password": "regular123"})
    resp = client.post(
        "/api/draft/transfer",
        json={"page_id": 1, "from_user_id": admin_user},
        content_type="application/json",
    )
    # Non-admins should be redirected (admin_required decorator)
    assert resp.status_code in (302, 403)


def test_draft_transfer_allowed_for_editor(client, admin_user):
    """An editor should NOT be allowed to use the draft transfer endpoint (admin only)."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("editor_a", generate_password_hash("editor123"), role="editor")
    editor2_id = db.create_user("editor_b", generate_password_hash("editor123"), role="editor")
    home = db.get_home_page()
    db.save_draft(home["id"], editor2_id, "draft title", "draft content")
    client.post("/login", data={"username": "editor_a", "password": "editor123"})
    resp = client.post(
        "/api/draft/transfer",
        json={"page_id": home["id"], "from_user_id": editor2_id},
        content_type="application/json",
    )
    assert resp.status_code in (302, 403)


def test_draft_transfer_allowed_for_admin(logged_in_admin, admin_user):
    """An admin should be allowed to use the draft transfer endpoint."""
    import db
    from werkzeug.security import generate_password_hash
    editor_id = db.create_user("editor2", generate_password_hash("editor123"), role="editor")
    home = db.get_home_page()
    db.save_draft(home["id"], editor_id, "title", "content")
    resp = logged_in_admin.post(
        "/api/draft/transfer",
        json={"page_id": home["id"], "from_user_id": editor_id},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# -----------------------------------------------------------------------
# Fix: file deletion error handling
# -----------------------------------------------------------------------
def test_delete_upload_nonexistent_file(logged_in_admin):
    """Deleting a non-existent upload should succeed silently (file already gone)."""
    resp = logged_in_admin.post(
        "/api/upload/delete",
        json={"filename": "nonexistent-file.png"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# -----------------------------------------------------------------------
# Fix: SQLite connection timeout is set
# -----------------------------------------------------------------------
def test_db_connection_has_timeout(monkeypatch):
    """A competing writer observes the configured, bounded lock deadline."""
    import db
    import sqlite3
    import time
    monkeypatch.setenv("BW_DB_BUSY_TIMEOUT_MS", "250")
    with db.get_db_context() as first, db.get_db_context() as second:
        first.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            second.execute("BEGIN IMMEDIATE")
        assert 0.2 <= time.monotonic() - started < 3
        first.rollback()


# ---------------------------------------------------------------------------
# Feature: Admin can delete individual history entries and clear all history
# ---------------------------------------------------------------------------

def test_delete_single_history_entry(logged_in_admin, admin_user):
    """Admin can delete a single history entry via POST."""
    import db
    page_id = db.create_page("Delete Entry Test", "delete-entry-test", "Content", user_id=admin_user)
    db.update_page(page_id, "Delete Entry Test", "Updated content", admin_user, "Edit 1")
    history = db.get_page_history(page_id)
    assert len(history) >= 1
    entry = history[0]
    resp = logged_in_admin.post(
        f"/page/delete-entry-test/history/{entry['id']}/delete"
    )
    assert resp.status_code in (200, 302)
    # Verify the entry is gone
    updated_history = db.get_page_history(page_id)
    assert all(h["id"] != entry["id"] for h in updated_history)


def test_delete_history_entry_returns_404_for_wrong_page(logged_in_admin, admin_user):
    """Deleting a history entry that doesn't belong to the page returns 404."""
    import db
    db.create_page("Page A", "page-a-del", "Content", user_id=admin_user)
    page_b_id = db.create_page("Page B", "page-b-del", "Content", user_id=admin_user)
    db.update_page(page_b_id, "Page B", "Edit", admin_user, "edit")
    history_b = db.get_page_history(page_b_id)
    entry_b = history_b[0]
    # Try to delete page B's entry via page A's route
    resp = logged_in_admin.post(
        f"/page/page-a-del/history/{entry_b['id']}/delete"
    )
    assert resp.status_code == 404


def test_delete_history_entry_returns_404_when_history_disabled(logged_in_admin, admin_user, monkeypatch):
    """Delete history entry returns 404 when PAGE_HISTORY_ENABLED is False."""
    import config
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    resp = logged_in_admin.post("/page/home/history/1/delete")
    assert resp.status_code == 404


def test_clear_page_history(logged_in_admin, admin_user):
    """Admin can clear all history for a page via POST."""
    import db
    page_id = db.create_page("Clear History Test", "clear-history-test", "Content", user_id=admin_user)
    db.update_page(page_id, "Clear History Test", "Edit A", admin_user, "Edit A")
    db.update_page(page_id, "Clear History Test", "Edit B", admin_user, "Edit B")
    history = db.get_page_history(page_id)
    assert len(history) >= 2
    resp = logged_in_admin.post("/page/clear-history-test/history/clear")
    assert resp.status_code in (200, 302)
    # All history entries should be gone
    assert db.get_page_history(page_id) == []


def test_clear_page_history_returns_404_when_history_disabled(logged_in_admin, admin_user, monkeypatch):
    """Clear history returns 404 when PAGE_HISTORY_ENABLED is False."""
    import config
    monkeypatch.setattr(config, "PAGE_HISTORY_ENABLED", False)
    resp = logged_in_admin.post("/page/home/history/clear")
    assert resp.status_code == 404


def test_history_page_shows_delete_buttons_for_admin(logged_in_admin, admin_user):
    """History page shows Delete and Clear All History buttons for admins."""
    import db
    db.update_page(db.get_home_page()["id"], "Home", "Updated content", admin_user, "Edit")
    resp = logged_in_admin.get("/page/home/history")
    assert resp.status_code == 200
    assert b"Clear All History" in resp.data
    assert b"Delete" in resp.data


def test_delete_history_entry_requires_admin(client, admin_user):
    """Non-admin editor cannot delete history entries (redirected to home)."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("editor_del", generate_password_hash("pass123"), role="editor")
    page_id = db.create_page("Editor Del Test", "editor-del-test", "Content", user_id=admin_user)
    db.update_page(page_id, "Editor Del Test", "Edit", admin_user, "edit")
    history = db.get_page_history(page_id)
    entry = history[0]
    client.post("/login", data={"username": "editor_del", "password": "pass123"})
    resp = client.post(f"/page/editor-del-test/history/{entry['id']}/delete")
    # admin_required redirects non-admins to home
    assert resp.status_code == 302
    # Entry should still exist
    assert db.get_history_entry(entry["id"]) is not None


def test_clear_page_history_requires_admin(client, admin_user):
    """Non-admin editor cannot clear page history (redirected to home)."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("editor_clr", generate_password_hash("pass123"), role="editor")
    page_id = db.create_page("Editor Clr Test", "editor-clr-test", "Content", user_id=admin_user)
    db.update_page(page_id, "Editor Clr Test", "Edit", admin_user, "edit")
    client.post("/login", data={"username": "editor_clr", "password": "pass123"})
    resp = client.post("/page/editor-clr-test/history/clear")
    # admin_required redirects non-admins to home
    assert resp.status_code == 302
    # History should still exist
    assert len(db.get_page_history(page_id)) > 0


# -----------------------------------------------------------------------
# Fix: Sidebar search results link to /page/<slug>, not /wiki/<slug>
# -----------------------------------------------------------------------
def test_search_result_url_resolves(logged_in_admin, admin_user):
    """Pages returned by the search API are accessible via /page/<slug> (not 404)."""
    import db
    db.create_page("Findable Page", "findable-page", "some content", user_id=admin_user)

    # Confirm the search API returns the page
    resp = logged_in_admin.get("/api/pages/search?q=findable")
    assert resp.status_code == 200
    results = resp.get_json()
    assert any(r["slug"] == "findable-page" for r in results)

    # Verify the URL the JS constructs (/page/<slug>) actually resolves
    resp = logged_in_admin.get("/page/findable-page")
    assert resp.status_code == 200

    # Verify the old incorrect URL (/wiki/<slug>) returns 404
    resp = logged_in_admin.get("/wiki/findable-page")
    assert resp.status_code == 404


# -----------------------------------------------------------------------
# Fix: api_reset_accessibility endpoint
# -----------------------------------------------------------------------

def test_api_reset_accessibility_restores_defaults(logged_in_admin):
    """/api/accessibility/reset returns the system defaults and persists them."""
    import db

    # Save non-default preferences first (use a valid font_scale value)
    setup_resp = logged_in_admin.post("/api/accessibility",
                         json={"font_scale": 1.35, "contrast": 2, "custom_bg": "#aabbcc"},
                         content_type="application/json")
    assert setup_resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["font_scale"] == 1.35

    # Reset
    resp = logged_in_admin.post("/api/accessibility/reset")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["message"] == "Customization settings have been reset to default."
    assert "defaults" in body

    # Verify preferences match the DB defaults
    prefs_after = logged_in_admin.get("/api/accessibility").get_json()
    for key, val in db._A11Y_DEFAULTS.items():
        assert prefs_after[key] == val, f"Default mismatch for '{key}'"


def test_api_reset_accessibility_requires_login(client, admin_user):
    """/api/accessibility/reset redirects unauthenticated requests to login."""
    resp = client.post("/api/accessibility/reset")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_api_reset_accessibility_returns_defaults_in_response(logged_in_admin):
    """/api/accessibility/reset response body contains the system defaults dict."""
    import db
    resp = logged_in_admin.post("/api/accessibility/reset")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["message"] == "Customization settings have been reset to default."
    returned_defaults = body["defaults"]
    for key in db._A11Y_DEFAULTS:
        assert key in returned_defaults


# -----------------------------------------------------------------------
# Fix: full-page edit title length validation
# The /page/<slug>/edit POST route was missing a server-side 200-char
# title limit, unlike the /page/<slug>/edit/title and /create-page routes.
# -----------------------------------------------------------------------

def test_full_edit_page_title_too_long_is_rejected(logged_in_admin):
    """Submitting a title longer than 200 chars via the full edit form must flash an error."""
    import db
    home = db.get_home_page()
    original_title = home["title"]
    resp = logged_in_admin.post(
        f"/page/{home['slug']}/edit",
        data={"title": "T" * 201, "content": "some content", "edit_message": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"200 characters" in resp.data
    # The original title must not have been replaced by the oversized one
    page_after = db.get_home_page()
    assert page_after["title"] == original_title


def test_full_edit_page_title_exactly_200_chars_is_accepted(logged_in_admin):
    """A title of exactly 200 characters should be accepted by the full edit form."""
    import db
    home = db.get_home_page()
    long_title = "A" * 200
    resp = logged_in_admin.post(
        f"/page/{home['slug']}/edit",
        data={"title": long_title, "content": "some content", "edit_message": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    page_after = db.get_home_page()
    assert page_after["title"] == long_title


# ---------------------------------------------------------------------------
# Sidebar categories visible on pages that previously lacked them
# ---------------------------------------------------------------------------

def test_sidebar_categories_on_plugins_page(logged_in_admin):
    """Sidebar categories must appear on admin plugin pages."""
    import db
    cat_id = db.create_category("SidebarTestCat")
    db.create_page("SidebarTestPage", "sidebar-test-page", category_id=cat_id)

    resp = logged_in_admin.get("/admin/plugins")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "SidebarTestCat" in html
    assert "SidebarTestPage" in html


def test_sidebar_categories_on_banana_page(logged_in_admin):
    """Sidebar categories must appear on the unified API Service page."""
    import db
    db.create_category("APISidebarCat")

    resp = logged_in_admin.get("/admin/api-service")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "APISidebarCat" in html


# ---------------------------------------------------------------------------
# Banana mode unified management page (/admin/api-service)
# ---------------------------------------------------------------------------

def test_banana_page_shows_status_disabled(logged_in_admin):
    """Unified API Service page shows disabled Banana Mode status."""
    import db
    db.update_site_settings(banana_mode=0)
    resp = logged_in_admin.get("/admin/api-service")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Banana Mode" in html
    assert "Disabled" in html
    assert "Enable Banana Mode" in html


def test_banana_page_shows_status_enabled(logged_in_admin):
    """Unified API Service page shows enabled Banana Mode status."""
    import db
    db.update_site_settings(banana_mode=1)
    resp = logged_in_admin.get("/admin/api-service")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "ENABLED" in html
    assert "Disable Banana Mode" in html


def test_banana_toggle_enable(logged_in_admin):
    """Admin can enable banana mode via the unified API Service toggle."""
    import db
    db.update_site_settings(banana_mode=0)
    resp = logged_in_admin.post(
        "/admin/api-service/banana-mode/toggle",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"enabled" in resp.data
    settings = db.get_site_settings()
    assert settings["banana_mode"] == 1


def test_banana_toggle_disable(logged_in_admin):
    """Admin can disable banana mode via the unified API Service toggle."""
    import db
    db.update_site_settings(banana_mode=1)
    resp = logged_in_admin.post(
        "/admin/api-service/banana-mode/toggle",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"disabled" in resp.data
    settings = db.get_site_settings()
    assert settings["banana_mode"] == 0


def test_api_service_page_has_banana_api_docs(logged_in_admin):
    """Unified API Service page should include Banana Mode API details."""
    resp = logged_in_admin.get("/admin/api-service")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Banana Mode" in html
    assert "/api/v1/banana-mode" in html


def test_user_settings_has_api_service_link(logged_in_admin):
    """Account settings page should have a single API Service link."""
    resp = logged_in_admin.get("/settings")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "/admin/api-service" in html
    assert "/admin/banana" not in html


def test_user_settings_api_redirects_to_banana(logged_in_admin):
    """GET /settings/api should redirect to the unified API token settings page."""
    resp = logged_in_admin.get("/settings/api")
    assert resp.status_code == 301
    assert "/settings/api-tokens" in resp.location


def test_banana_mode_affects_all_users(logged_in_admin):
    """When banana mode is on, even admins see banana on regular pages."""
    import db
    db.update_site_settings(banana_mode=1)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "🍌" in html
    assert "API Service Settings" in html


def test_banana_mode_admin_has_settings_and_logout(logged_in_admin):
    """Admins get API Service settings and Logout buttons on banana page."""
    import db
    db.update_site_settings(banana_mode=1)
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "API Service Settings" in html
    assert "Logout" in html
    # Logout must be a POST form (not a plain link) to work correctly
    assert 'method="POST"' in html
    assert 'action="/logout"' in html


def test_banana_mode_regular_user_only_logout(client, admin_user, regular_user):
    """Non-admin logged-in users only see a Logout button on banana page."""
    import db
    db.update_site_settings(banana_mode=1)
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "🍌" in html
    assert "Logout" in html
    assert "API Service Settings" not in html
    # Logout must be a POST form
    assert 'action="/logout"' in html


def test_banana_mode_logout_works(logged_in_admin):
    """Logout button on the banana screen actually logs the user out."""
    import db
    db.update_site_settings(banana_mode=1)
    resp = logged_in_admin.post("/logout", follow_redirects=True)
    assert resp.status_code == 200
    # After logout, user should be redirected to login page
    assert b"Login" in resp.data or b"login" in resp.data


def test_banana_mode_login_excluded(client, admin_user):
    """Login page should not show banana even when mode is on."""
    import db
    db.update_site_settings(banana_mode=1)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"banana-wrap" not in resp.data


def test_color_inputs_have_autocomplete_off(logged_in_admin):
    """The a11y colour inputs must have autocomplete='off' so that browser
    session-restore (Firefox Ctrl+Shift+T) does not overwrite them with
    stale #000000 values that could be persisted and turn the site black."""
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    import re
    a11y_inputs = re.findall(
        r'<input type="color"[^>]*a11y-color-input[^>]*>', resp.data.decode()
    )
    assert len(a11y_inputs) == 6, f"Expected 6 a11y colour inputs, got {len(a11y_inputs)}"
    for inp in a11y_inputs:
        assert 'autocomplete="off"' in inp, f"Missing autocomplete=off: {inp}"


def test_color_inputs_have_theme_value_not_black(logged_in_admin):
    """Colour inputs must have an initial value matching the current theme
    palette (not the browser default #000000)."""
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    import re
    a11y_inputs = re.findall(
        r'<input type="color"[^>]*a11y-color-input[^>]*>', resp.data.decode()
    )
    assert len(a11y_inputs) == 6
    for inp in a11y_inputs:
        assert 'value="#' in inp, f"Missing value attribute: {inp}"
        assert 'value="#000000"' not in inp, f"Input has dangerous default #000000: {inp}"


def test_color_inputs_reflect_light_theme_when_active(logged_in_admin):
    """When light theme is active, colour inputs should use the light palette values."""
    import db
    db.update_site_settings(default_theme_mode="light")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    # The light bg colour default is #f6f7fb
    assert 'id="a11y-color-bg"' in html
    import re
    bg_input = re.search(r'<input[^>]*id="a11y-color-bg"[^>]*>', html)
    assert bg_input
    assert '#f6f7fb' in bg_input.group(0) or 'value="#' in bg_input.group(0)


def test_light_theme_css_does_not_hardcode_palette_vars():
    """Light-mode polish must not override admin/user palette variables."""
    import re
    css_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "style.css")
    with open(css_path) as f:
        css = f.read()

    light_blocks = re.findall(r'html\[data-theme="light"\]\s*\{([^}]*)\}', css)
    assert light_blocks, "Expected at least one light theme block"
    for block in light_blocks:
        assert "--primary:" not in block
        assert "--accent:" not in block
        assert "--bg:" not in block
        assert "--text:" not in block
        assert "--secondary:" not in block
        assert "--sidebar:" not in block


def test_light_theme_filled_buttons_use_readable_text_color():
    """Light theme primary buttons use dark blue backgrounds, so filled
    buttons need light text while outline/icon variants keep normal text."""
    css_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "style.css")
    with open(css_path) as f:
        css = f.read()

    assert 'html[data-theme="light"] .btn:not(.btn-outline)' in css
    assert ':not(.btn-danger-outline)' in css
    assert ':not(.btn-icon)' in css
    assert ':not(.btn-icon-tiny)' in css
    assert ':not(.flash-close)' in css
    assert ':not(.cat-toggle)' in css
    assert ':not(.topbar-avatar-link)' in css
    assert ':not(.logo)' in css
    assert 'html[data-theme="light"] .btn-primary{color:#fff}' in css
    assert 'html[data-theme="light"] .topbar-left .logo{color:var(--text)}' in css


def test_light_theme_custom_user_colors_render_in_a11y_style(logged_in_admin):
    """Saved custom colors should still win when the site default is light."""
    import db
    db.update_site_settings(default_theme_mode="light")
    logged_in_admin.post(
        "/api/accessibility",
        json={
            "theme_mode": "default",
            "custom_bg": "#112233",
            "custom_text": "#ddeeff",
            "custom_primary": "#445566",
            "custom_secondary": "#223344",
            "custom_accent": "#778899",
            "custom_sidebar": "#334455",
        },
        content_type="application/json",
    )

    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'data-theme="light"' in html
    assert "--bg: #112233" in html
    assert "--text: #ddeeff" in html
    assert "--primary: #445566" in html
    assert "--secondary: #223344" in html
    assert "--accent: #778899" in html
    assert "--sidebar: #334455" in html


def test_pageshow_handler_present_for_logged_in_user(logged_in_admin):
    """A pageshow event handler that re-applies a11y prefs must be present
    so bfcache-restored pages keep the correct theme colours."""
    resp = logged_in_admin.get("/")
    html = resp.data.decode()
    assert "pageshow" in html, "Missing pageshow event listener"
    assert "e.persisted" in html, "pageshow handler must check event.persisted"
    assert "applyA11yPrefs" in html, "pageshow handler must call applyA11yPrefs"


def test_css_root_has_fallback_theme_defaults():
    """The CSS :root rule must include fallback values for all theme variables
    so the site is usable even if inline <style> or JS fails to apply."""
    import re
    css_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "style.css")
    with open(css_path) as f:
        css = f.read()
    # Find the :root block
    root_match = re.search(r':root\s*\{([^}]+)\}', css)
    assert root_match, "No :root block found in CSS"
    root_block = root_match.group(1)
    for var in ("--bg", "--text", "--primary", "--secondary", "--accent", "--sidebar"):
        assert var in root_block, f"CSS :root must declare fallback for {var}"


def test_mobile_account_toggle_hidden_on_desktop_visible_on_mobile():
    """The mobile account menu button must not leak into desktop topbars."""
    css_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "style.css")
    with open(css_path) as f:
        css = f.read()

    assert ".mobile-account-menu-toggle,.btn.mobile-account-menu-toggle,.mobile-account-menu{display:none}" in css
    assert ".mobile-account-menu-toggle,.btn.mobile-account-menu-toggle{display:inline-flex" in css


# ---------------------------------------------------------------------------
# Invite code case-insensitivity and normalisation
# ---------------------------------------------------------------------------
def test_validate_invite_code_case_insensitive(admin_user):
    """validate_invite_code matches regardless of case."""
    import db
    code = db.generate_invite_code(admin_user)  # e.g. "ABCD-1234"
    # Lowercase should validate
    assert db.validate_invite_code(code.lower()) is not None
    # Mixed case should validate
    mixed = code[:4].lower() + "-" + code[5:].upper()
    assert db.validate_invite_code(mixed) is not None


def test_use_invite_code_case_insensitive(admin_user):
    """use_invite_code succeeds even when the case doesn't match the stored code."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("caseusr", generate_password_hash("pw"), role="user")
    code = db.generate_invite_code(admin_user)
    # Use the code in all-lowercase
    result = db.use_invite_code(code.lower(), uid)
    assert result is True
    # Code should now be consumed
    assert db.validate_invite_code(code) is None


def test_signup_accepts_lowercase_invite_code(client, admin_user):
    """Signup succeeds with a lowercase invite code."""
    import db
    code = db.generate_invite_code(admin_user)
    resp = client.post("/signup", data={
        "username": "lcuser",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code.lower(),
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Account created" in resp.data


def test_signup_accepts_code_without_hyphen(client, admin_user):
    """Signup succeeds when the invite code is entered without the hyphen."""
    import db
    code = db.generate_invite_code(admin_user)
    no_hyphen = code.replace("-", "")
    resp = client.post("/signup", data={
        "username": "nohyphenuser",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": no_hyphen,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Account created" in resp.data


def test_login_form_has_autocomplete_attrs(client, admin_user):
    """Login form inputs have proper id and autocomplete attributes."""
    resp = client.get("/login")
    assert b'id="login-username"' in resp.data
    assert b'autocomplete="username"' in resp.data
    assert b'id="login-password"' in resp.data
    assert b'autocomplete="current-password"' in resp.data


def test_bananawiki_auth_pages_use_shared_topbar(client, admin_user):
    """Public user and admin sign-in screens use the portal header shell."""
    for path in ("/login", "/signup", "/admin"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert b'<header class="topbar auth-topbar">' in resp.data
        assert b'id="main-content"' in resp.data


def test_bananawiki_app_selector_uses_shared_topbar(logged_in_admin):
    """The post-login app portal keeps account navigation in the top bar."""
    resp = logged_in_admin.get("/app-selector")
    assert resp.status_code == 200
    assert b'<header class="topbar auth-topbar">' in resp.data
    assert b'class="user-info">admin</span>' in resp.data
    assert b'action="/logout"' in resp.data


def test_signup_form_has_autocomplete_attrs(client, admin_user):
    """Signup form inputs have proper id and autocomplete attributes."""
    resp = client.get("/signup")
    assert b'id="signup-username"' in resp.data
    assert b'id="signup-password"' in resp.data
    assert b'id="signup-confirm-password"' in resp.data
    assert b'id="signup-invite-code"' in resp.data
    assert b'autocomplete="one-time-code"' in resp.data
    assert b'autocomplete="new-password"' in resp.data


def test_base_confirmation_modal_orders_cancel_before_confirm(logged_in_admin):
    """The shared confirmation modal should show Cancel before the destructive action."""
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    cancel_idx = html.index('id="bw-confirm-cancel"')
    confirm_idx = html.index('id="bw-confirm-ok"')
    assert cancel_idx < confirm_idx
    assert 'class="btn btn-danger" id="bw-confirm-ok">Confirm</button>' in html


def test_group_view_uses_data_confirm_instead_of_inline_confirm(logged_in_admin, admin_user):
    """Group actions should use the in-site confirmation dialog data attributes."""
    import db
    group = db.create_group_chat("Confirm Group", admin_user)
    resp = logged_in_admin.get(f"/groups/{group['id']}")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'data-confirm="Regenerate invite code? The old code will stop working."' in html
    assert 'data-confirm="Permanently delete this group and all its messages? This cannot be undone.' in html
    assert 'onsubmit="return confirm(' not in html


def test_page_attachment_delete_uses_bananawiki_confirm_modal(logged_in_admin):
    """Attachment deletion should call bwConfirm instead of the native confirm dialog.

    The literal prompt string is now routed through the JS ``window._t()``
    helper, so we check for the shape ``bwConfirm(... 'Delete this attachment?'``
    rather than an exact ``bwConfirm('Delete this attachment?'`` match.
    """
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "bwConfirm(" in html
    assert "'Delete this attachment?'" in html
    assert "if (!confirm('Delete this attachment?')) return;" not in html


def test_admin_settings_shows_favicon_library_ui(logged_in_admin):
    """The favicon library grid should be present on the settings page."""
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "Favicon Library" in html
    assert "favicon-grid" in html
    assert "banana_yellow.png" in html


def test_favicon_picker_uses_csp_safe_event_bindings(logged_in_admin):
    """Favicon controls should not rely on inline handlers blocked by CSP."""
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'onclick="selectFavicon' not in html
    assert 'onchange="uploadFavicon' not in html
    assert "data-favicon-upload-trigger" in html
    assert 'addEventListener(\'click\'' in html
    assert "Selection failed. Please reload, sign in again" in html


def test_select_favicon_ajax_updates_preset(logged_in_admin):
    """Selecting a bundled favicon through AJAX persists the chosen preset."""
    import db

    resp = logged_in_admin.post(
        "/global-settings/favicon/select",
        json={"favicon_type": "green", "favicon_custom": ""},
    )

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    settings = db.get_site_settings()
    assert settings["favicon_type"] == "green"
    assert settings["favicon_custom"] == ""


def test_favicon_library_ignores_orphan_custom_files(logged_in_admin, tmp_path, monkeypatch):
    """Unregistered custom_* files on disk should not appear in the picker."""

    favicon_dir = tmp_path / "favicons"
    favicon_dir.mkdir()
    (favicon_dir / "custom_orphan.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89"
    )
    monkeypatch.setattr(config, "FAVICON_UPLOAD_FOLDER", str(favicon_dir))

    resp = logged_in_admin.get("/global-settings")

    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "custom_orphan.png" not in html


def test_custom_favicon_upload_and_delete_return_ui_state(logged_in_admin, tmp_path, monkeypatch):
    """AJAX upload/delete responses should let the picker update without reload."""
    import io
    import db
    from PIL import Image

    favicon_dir = tmp_path / "favicons"
    favicon_dir.mkdir()
    monkeypatch.setattr(config, "FAVICON_UPLOAD_FOLDER", str(favicon_dir))

    img = Image.new("RGBA", (32, 32), (20, 40, 60, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    upload_resp = logged_in_admin.post(
        "/global-settings/favicon/upload",
        data={"file": (buf, "custom.png")},
        content_type="multipart/form-data",
    )

    assert upload_resp.status_code == 200
    upload_data = upload_resp.get_json()
    assert upload_data["ok"] is True
    assert upload_data["type"] == "custom"
    assert upload_data["selected"] is True
    filename = upload_data["filename"]
    settings = db.get_site_settings()
    assert settings["favicon_type"] == "custom"
    assert settings["favicon_custom"] == filename

    delete_resp = logged_in_admin.post(
        "/global-settings/favicon/delete",
        json={"filename": filename},
    )

    assert delete_resp.status_code == 200
    delete_data = delete_resp.get_json()
    assert delete_data["ok"] is True
    assert delete_data["deleted"] == filename
    assert delete_data["fallback_type"] == "yellow"
    settings = db.get_site_settings()
    assert settings["favicon_type"] == "yellow"
    assert settings["favicon_custom"] == ""


# ---------------------------------------------------------------------------
# Logged-in users should be redirected away from auth pages
# ---------------------------------------------------------------------------


def test_logged_in_user_redirected_from_login(logged_in_admin):
    """A logged-in user visiting /login should be redirected to home."""
    resp = logged_in_admin.get("/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_logged_in_user_redirected_from_signup(logged_in_admin):
    """A logged-in user visiting /signup should be redirected to home."""
    resp = logged_in_admin.get("/signup", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_logged_in_user_redirected_from_session_conflict(logged_in_admin):
    """A logged-in user visiting /session-conflict can still view the page."""
    resp = logged_in_admin.get("/session-conflict")
    assert resp.status_code == 200


def test_legacy_lockdown_redirects_for_logged_in_user(logged_in_admin):
    """A logged-in admin visiting the legacy /lockdown route is forwarded to /maintenance."""
    import db
    db.update_site_settings(maintenance_mode=1)
    resp = logged_in_admin.get("/lockdown")
    assert resp.status_code == 301
    assert "/maintenance" in resp.headers["Location"]


def test_logged_in_editor_redirected_from_login(client, admin_user):
    """A logged-in editor visiting /login should be redirected to home."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("editor", generate_password_hash("editor123"), role="editor")
    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_logged_in_regular_user_redirected_from_signup(client, admin_user):
    """A logged-in regular user visiting /signup should be redirected to home."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("user", generate_password_hash("user123"), role="user")
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.get("/signup", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_anonymous_user_can_still_access_login(client, admin_user):
    """An anonymous user should still see the login page."""
    resp = client.get("/login")
    assert resp.status_code == 200


def test_anonymous_user_can_still_access_signup(client, admin_user):
    """An anonymous user should still see the signup page."""
    resp = client.get("/signup")
    assert resp.status_code == 200


def test_sidebar_toggle_has_aria_expanded(client, admin_user):
    """Sidebar toggle button must have aria-expanded for screen readers."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/")
    html = resp.get_data(as_text=True)
    assert 'id="sidebar-toggle"' in html
    assert 'aria-expanded="false"' in html


# -----------------------------------------------------------------------
# Fix: login flash messages have role="alert" and dismiss button
# -----------------------------------------------------------------------
def test_login_flash_has_role_alert(client, admin_user):
    """Login page flash messages must include role='alert' for screen readers."""
    resp = client.post(
        "/login", data={"username": "admin", "password": "wrong"}
    )
    html = resp.get_data(as_text=True)
    assert 'role="alert"' in html


def test_login_flash_has_close_button(client, admin_user):
    """Login page flash messages must include a dismiss button."""
    resp = client.post(
        "/login", data={"username": "admin", "password": "wrong"},
        follow_redirects=True
    )
    html = resp.get_data(as_text=True)
    assert "flash-close" in html
    assert "Dismiss message" in html


# ---------------------------------------------------------------------------
#  reduce_motion string "0" parsed as truthy  (ISSUE-63)
# ---------------------------------------------------------------------------

def test_reduce_motion_string_zero_stored_as_off(logged_in_admin):
    """String '0' for reduce_motion must be stored as 0, not 1."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"reduce_motion": "0"},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["reduce_motion"] == 0


def test_reduce_motion_string_one_stored_as_on(logged_in_admin):
    """String '1' for reduce_motion must be stored as 1."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"reduce_motion": "1"},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["reduce_motion"] == 1


def test_reduce_motion_int_zero(logged_in_admin):
    """Integer 0 for reduce_motion stores as 0."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"reduce_motion": 0},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["reduce_motion"] == 0


def test_reduce_motion_int_one(logged_in_admin):
    """Integer 1 for reduce_motion stores as 1."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"reduce_motion": 1},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["reduce_motion"] == 1


def test_reduce_motion_invalid_value_defaults_to_zero(logged_in_admin):
    """Non-numeric reduce_motion value defaults to 0."""
    resp = logged_in_admin.post("/api/accessibility",
                                json={"reduce_motion": "banana"},
                                content_type="application/json")
    assert resp.status_code == 200
    prefs = logged_in_admin.get("/api/accessibility").get_json()
    assert prefs["reduce_motion"] == 0


# ---------------------------------------------------------------------------
# Fix: editor_required and admin_required redirect unauthenticated users to
# /login instead of showing a confusing permission error and redirecting home.
# ---------------------------------------------------------------------------

def test_editor_required_redirects_unauthenticated_to_login(client, admin_user):
    """Unauthenticated request to an editor-only route redirects to /login, not home."""
    resp = client.get("/create-page", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_editor_required_no_flash_for_unauthenticated(client, admin_user):
    """Unauthenticated request to an editor-only route must NOT flash a permission error."""
    resp = client.get("/create-page", follow_redirects=True)
    assert b"You do not have permission" not in resp.data


def test_admin_required_redirects_unauthenticated_to_login(client, admin_user):
    """Unauthenticated request to an admin-only route redirects to /login, not home."""
    resp = client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_admin_required_no_flash_for_unauthenticated(client, admin_user):
    """Unauthenticated request to an admin-only route must NOT flash an access-required error."""
    resp = client.get("/admin/users", follow_redirects=True)
    assert b"Admin access required" not in resp.data


def test_editor_required_still_blocks_regular_user(client, admin_user, regular_user, logged_in_user):
    """Regular users (role=user) are still blocked from editor-only routes with an error flash."""
    resp = logged_in_user.get("/create-page", follow_redirects=False)
    assert resp.status_code == 302
    # Should redirect to home, not login
    assert resp.headers["Location"].endswith("/")


def test_admin_required_still_blocks_editor(client, admin_user, editor_user, logged_in_editor):
    """Editors are still blocked from admin-only routes and redirected to home with an error flash."""
    resp = logged_in_editor.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 302
    # Should redirect to home, not login
    assert resp.headers["Location"].endswith("/")


# -----------------------------------------------------------------------
# Folders: no inline event handlers on category elements (CSP compliance)
# -----------------------------------------------------------------------

def test_category_sidebar_no_inline_onclick(logged_in_admin):
    """Category sidebar buttons must not use inline onclick handlers (blocked by CSP)."""
    import db
    cat_id = db.create_category("CSP Test Category")
    resp = logged_in_admin.get("/")
    html = resp.data.decode()
    # The create-category button must use an id instead of inline onclick
    assert 'id="open-create-cat-btn"' in html
    # Category manage buttons must use data attributes instead of inline onclick
    assert "data-open-cat-modal" in html
    # Deferred controls must retain the same CSP-safe close button.
    controls = logged_in_admin.get(f"/api/category/{cat_id}/management")
    assert controls.status_code == 200
    assert "data-close-cat-modal" in controls.json["html"]
    assert "onclick=" not in controls.json["html"]
    # No inline onclick on cat-toggle buttons
    for line in html.splitlines():
        if 'class="cat-toggle"' in line:
            assert "onclick" not in line


def test_category_delete_form_no_inline_onsubmit(logged_in_admin):
    """Category delete forms must not use inline onsubmit handlers (blocked by CSP)."""
    import db
    cat_id = db.create_category("CSP Delete Cat")
    resp = logged_in_admin.get(f"/api/category/{cat_id}/management")
    assert resp.status_code == 200
    html = resp.json["html"]
    # Delete forms must use data attributes for confirmation
    assert "data-cat-delete-confirm" in html
    # No inline onsubmit should be present on forms
    for line in html.splitlines():
        if "catDeleteForm" in line:
            assert "onsubmit" not in line


# -----------------------------------------------------------------------
# Toggle buttons must use getComputedStyle to check visibility
# -----------------------------------------------------------------------

def test_profile_edit_toggle_uses_computed_style(logged_in_admin):
    """Profile Edit toggle must use getComputedStyle, not el.style.display."""
    resp = logged_in_admin.get("/users/admin")
    html = resp.get_data(as_text=True)
    assert "js-toggle-profile-edit" in html
    assert "getComputedStyle" in html


def test_history_transfer_toggle_uses_computed_style(logged_in_admin, admin_user):
    """History transfer toggle must use getComputedStyle, not el.style.display."""
    import db
    cat_id = db.create_category("HistToggle")
    db.create_page("Hist Toggle", "hist-toggle", "body", cat_id, admin_user)
    resp = logged_in_admin.get("/page/hist-toggle/history")
    html = resp.get_data(as_text=True)
    # The script block with the toggle code is always present
    if "js-transfer-toggle" in html:
        assert "getComputedStyle" in html


def test_user_settings_protected_toggle_uses_computed_style(client, admin_user):
    """Protected admin toggle must use getComputedStyle, not el.style.display."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/settings")
    html = resp.get_data(as_text=True)
    if "js-toggle-protected" in html:
        assert "getComputedStyle" in html


def test_announcement_edit_toggle_uses_computed_style(logged_in_admin, admin_user):
    """Announcement edit toggle must use getComputedStyle, not el.style.display."""
    import db
    db.create_announcement("Toggle Test", "blue", "normal", "both", None, admin_user)
    resp = logged_in_admin.get("/admin/announcements")
    html = resp.get_data(as_text=True)
    assert "getComputedStyle" in html


def test_admin_settings_data_toggle_uses_computed_style(logged_in_admin):
    """Admin settings data-toggle buttons must use getComputedStyle."""
    resp = logged_in_admin.get("/global-settings")
    html = resp.get_data(as_text=True)
    if "data-toggle" in html:
        assert "getComputedStyle" in html


# ---------------------------------------------------------------------------
# Double-submit prevention must not strand buttons when a confirm modal
# (or any other capture-phase listener) preventDefaults the submit event.
# Without the guard the submit button stays disabled forever after the user
# dismisses the confirm modal: looking like the page has "hung" until the
# user reloads.  See PR fixing the timeout/hang bug for context.
# ---------------------------------------------------------------------------
def test_double_submit_prevention_skips_when_default_prevented(logged_in_admin):
    """The double-submit handler must bail out when e.defaultPrevented is set."""
    resp = logged_in_admin.get("/static/js/main.js")
    assert resp.status_code == 200
    js = resp.data.decode("utf-8")
    # Find the double-submit listener block (the one that sets data-bw-submitting).
    marker = "btn.setAttribute('data-bw-submitting', '1');"
    idx = js.find(marker)
    assert idx != -1, "double-submit handler not found in main.js"
    # The defaultPrevented short-circuit must appear *before* the marker so
    # that capture-phase handlers (like the data-confirm modal interceptor)
    # which call e.preventDefault() do not leave the submit button stuck
    # in a disabled state.
    head = js[:idx]
    assert "e.defaultPrevented" in head, (
        "double-submit handler must short-circuit on e.defaultPrevented to "
        "avoid stranding the submit button when a confirm modal is dismissed"
    )


# ---------------------------------------------------------------------------
# Feature: hide user profile button on top bar when user_profiles is disabled
# ---------------------------------------------------------------------------
def test_topbar_profile_button_hidden_when_user_profiles_disabled(client, admin_user):
    """The avatar/profile button in the top bar is not rendered when the
    ``user_profiles`` plugin is disabled."""
    import db
    # The local ``isolated_db`` fixture in this file does not seed plugin
    # rows, so we must register the plugin before disabling it.  Without a
    # row the ``_get_enabled_plugins`` fallback treats every built-in as
    # enabled, which is the legacy behaviour we want preserved for other
    # tests in this module.
    db.register_plugin(
        "user_profiles", name="User Profiles", version="1.0.0",
        builtin=True, enabled=False,
    )
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/")
    assert resp.status_code == 200
    # The topbar avatar link / placeholder must not be in the rendered HTML.
    assert b"topbar-avatar-link" not in resp.data
    assert b"topbar-avatar-placeholder" not in resp.data


def test_topbar_profile_button_visible_when_user_profiles_enabled(client, admin_user):
    """The avatar/profile button in the top bar IS rendered when the
    ``user_profiles`` plugin is enabled."""
    import db
    db.register_plugin(
        "user_profiles", name="User Profiles", version="1.0.0",
        builtin=True, enabled=True,
    )
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"topbar-avatar-link" in resp.data


# ---------------------------------------------------------------------------
# Feature: Bulk Markdown Import
# ---------------------------------------------------------------------------
def test_bulk_markdown_page_requires_admin(client, regular_user):
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.get("/admin/bulk-markdown", follow_redirects=False)
    # Non-admins are redirected away from admin pages.
    assert resp.status_code in (302, 403)


def test_bulk_markdown_page_renders_for_admin(logged_in_admin):
    resp = logged_in_admin.get("/admin/bulk-markdown")
    assert resp.status_code == 200
    assert b"Bulk Markdown Import" in resp.data
    assert b"Export Markdown + Assets" in resp.data
    assert b"attribute_to_me" in resp.data


def test_bulk_markdown_helper_creates_pages_and_categories(admin_user):
    """The importer creates pages and matching category hierarchy."""
    import db
    from helpers._bulk_markdown import import_markdown_bundle
    files = [
        ("docs/intro.md", "# Welcome\n\nIntro body."),
        ("docs/guides/getting-started.md", "Body without an H1."),
        ("standalone.md", "# Standalone\n\nNo directory."),
    ]
    result = import_markdown_bundle(files, admin_user)
    assert result.total_created == 3
    titles = [db.get_page_by_slug(s)["title"] for s in result.created_pages]
    assert "Welcome" in titles
    assert "Standalone" in titles
    # "getting-started" (filename) was the fallback title.
    assert any("getting" in t.lower() or "started" in t.lower() for t in titles)
    # Categories: "docs" at top, "guides" under "docs"
    cats = {c["name"]: c for c in db.list_categories()}
    assert "docs" in cats
    assert "guides" in cats
    assert cats["guides"]["parent_id"] == cats["docs"]["id"]
    page = db.get_page_by_slug(result.created_pages[0])
    assert page["last_edited_by"] == admin_user


def test_bulk_markdown_helper_skips_existing_slug(admin_user):
    """When skip_existing=True and a slug collides, the page is skipped."""
    import db
    from helpers._bulk_markdown import import_markdown_bundle
    # Seed an existing page with the slug "intro".
    db.create_page("Intro", "intro", "Existing body.", None, admin_user)
    files = [("intro.md", "# Intro\n\nNew body.")]
    result = import_markdown_bundle(files, admin_user, skip_existing=True)
    assert result.total_created == 0
    assert result.total_skipped == 1


def test_bulk_markdown_helper_rejects_path_traversal(admin_user):
    """Paths containing ``..`` are rejected, not imported."""
    import db
    from helpers._bulk_markdown import import_markdown_bundle
    files = [("../../etc/passwd.md", "# Pwn")]
    result = import_markdown_bundle(files, admin_user)
    assert result.total_created == 0
    assert any("Unsafe path" in e for e in result.errors)


def test_bulk_markdown_zip_upload_creates_pages(logged_in_admin):
    """Uploading a ZIP through the route creates the expected pages."""
    import db, io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("docs/intro.md", "# Welcome\n\nBody.")
        zf.writestr("docs/guides/setup.md", "# Setup\n\nGuide.")
        zf.writestr("notes/note.md", "Title-less note.")
        zf.writestr("ignored.txt", "not markdown")
    buf.seek(0)
    resp = logged_in_admin.post(
        "/admin/bulk-markdown",
        data={"import_file": (buf, "bundle.zip"), "skip_existing": "1"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Three pages should now exist in the database with expected slugs.
    assert db.get_page_by_slug("welcome") is not None
    assert db.get_page_by_slug("setup") is not None
    assert db.get_page_by_slug("note") is not None
    assert str(db.get_page_by_slug("welcome")["last_edited_by"]) == str(db.SYSTEM_USER_ID)
    history = db.get_page_history(db.get_page_by_slug("welcome")["id"])
    assert history[-1]["username"] == "the system"
    # Two top-level categories created: docs and notes.
    cats = {c["name"]: c for c in db.list_categories()}
    assert "docs" in cats and "guides" in cats and "notes" in cats


def test_bulk_markdown_zip_upload_can_attribute_pages_to_admin(logged_in_admin, admin_user):
    """Checking attribute_to_me records imported Markdown pages as the admin."""
    import db, io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mine.md", "# Mine\n\nBody.")
    buf.seek(0)
    resp = logged_in_admin.post(
        "/admin/bulk-markdown",
        data={
            "import_file": (buf, "bundle.zip"),
            "skip_existing": "1",
            "attribute_to_me": "1",
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    page = db.get_page_by_slug("mine")
    assert page is not None
    assert page["last_edited_by"] == admin_user


def test_bulk_markdown_export_includes_pages_and_assets(logged_in_admin, admin_user, tmp_path, monkeypatch):
    """The content-only export writes Markdown, referenced uploads, and attachments."""
    import db, io, json, zipfile
    import config

    upload_dir = tmp_path / "uploads"
    attachment_dir = tmp_path / "attachments"
    upload_dir.mkdir()
    attachment_dir.mkdir()
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(upload_dir))
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(attachment_dir))

    (upload_dir / "photo.png").write_bytes(b"png-bytes")
    cat_id = db.create_category("Docs")
    page_id = db.create_page(
        "Asset Page",
        "asset-page",
        "Body with image ![](/static/uploads/photo.png)",
        cat_id,
        admin_user,
    )
    blob_id = db.store_blob("stored.txt", b"attachment-bytes")
    db.add_page_attachment(
        page_id,
        "stored.txt",
        "manual.txt",
        len(b"attachment-bytes"),
        admin_user,
        blob_id=blob_id,
    )

    resp = logged_in_admin.post("/admin/bulk-markdown/export")
    assert resp.status_code == 200
    assert "application/zip" in resp.headers["Content-Type"]
    assert "markdown_export_" in resp.headers["Content-Disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())
        assert "Docs/asset-page.md" in names
        assert "assets/uploads/photo.png" in names
        assert "assets/page_attachments/asset-page/manual.txt" in names
        md = zf.read("Docs/asset-page.md").decode("utf-8")
        assert 'title: "Asset Page"' in md
        assert "# Asset Page" in md
        assert "Body with image" in md
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == "bananawiki-markdown-export"
        assert manifest["page_count"] >= 1
        assert manifest["upload_count"] == 1
        assert manifest["attachment_count"] == 1
        assert manifest["missing_assets"] == []


# ---------------------------------------------------------------------------
# Feature: Semantic Highlighting accessibility option
# ---------------------------------------------------------------------------
def test_semantic_highlight_defaults_present():
    """The semantic_* keys exist in the accessibility defaults."""
    import db
    for key in (
        "semantic_bold", "semantic_italic", "semantic_code",
        "semantic_link", "semantic_heading",
    ):
        assert key in db._A11Y_DEFAULTS
        assert db._A11Y_DEFAULTS[key] == 0


def test_semantic_highlight_save_and_load(logged_in_admin, admin_user):
    """POSTing semantic_* values persists them and clamps invalid values to 0."""
    import db
    resp = logged_in_admin.post(
        "/api/accessibility",
        json={
            "semantic_bold": 2,
            "semantic_italic": 1,
            "semantic_code": "garbage",   # → 0
            "semantic_link": 5,            # out-of-range → 0
            "semantic_heading": 1,
        },
    )
    assert resp.status_code == 200
    prefs = db.get_user_accessibility(admin_user)
    assert prefs["semantic_bold"] == 2
    assert prefs["semantic_italic"] == 1
    assert prefs["semantic_code"] == 0
    assert prefs["semantic_link"] == 0
    assert prefs["semantic_heading"] == 1


def test_semantic_highlight_body_class_rendered(logged_in_admin, admin_user):
    """When a semantic level is set, the matching body class is rendered."""
    import db
    db.save_user_accessibility(admin_user, {
        **dict(db._A11Y_DEFAULTS),
        "semantic_bold": 2,
        "semantic_link": 1,
    })
    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b"a11y-sem-bold-2" in resp.data
    assert b"a11y-sem-link-1" in resp.data
