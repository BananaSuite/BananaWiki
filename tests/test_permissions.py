"""
Tests for the custom permission system.

Tests the new granular permission system for editors and users,
including permission checking, category access, and role changes.
"""
import os
import sys
from html.parser import HTMLParser
import pytest
from werkzeug.security import generate_password_hash

# Ensure the project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
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


class _CheckedInputParser(HTMLParser):
    """Collect checked checkbox inputs from rendered HTML."""

    def __init__(self):
        super().__init__()
        self.checked_inputs = set()

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        attr_map = dict(attrs)
        if attr_map.get("type") != "checkbox" or "checked" not in attr_map:
            return
        self.checked_inputs.add((attr_map.get("name"), attr_map.get("value")))


@pytest.fixture
def client():
    """Create a test client."""
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_user():
    """Create an admin user and mark setup as done."""
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a client that is logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


# ============================================================================
# PERMISSION SYSTEM TESTS
# ============================================================================

def test_permission_constants():
    """Test that permission constants are properly defined."""
    from helpers._permissions import (
        PERMISSIONS,
        get_all_permission_keys,
        get_default_permissions,
    )

    # Check that permissions are defined
    assert len(PERMISSIONS) > 0

    # Check that all permission keys are unique
    all_keys = get_all_permission_keys()
    assert len(all_keys) == len(set(all_keys))

    # Check defaults exist for both roles
    editor_defaults = get_default_permissions('editor')
    user_defaults = get_default_permissions('user')

    assert len(editor_defaults) > 0
    assert len(user_defaults) > 0
    assert len(editor_defaults) > len(user_defaults)  # Editors should have more permissions


def test_create_user_with_permissions():
    """Test creating a user with default permissions."""
    import db
    from helpers._permissions import get_default_permissions

    # Create an editor
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")

    # Set default permissions
    defaults = get_default_permissions('editor')
    db.set_user_permissions(editor_id, defaults)

    # Verify permissions were set
    perms = db.get_user_permissions(editor_id)
    assert len(perms['enabled_permissions']) == len(defaults)
    assert perms['enabled_permissions'] == defaults


def test_permission_checking():
    """Test has_permission function."""
    import db
    from helpers._permissions import get_default_permissions

    # Create editor with default permissions
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    defaults = get_default_permissions('editor')
    db.set_user_permissions(editor_id, defaults)

    editor = {"id": editor_id, "role": "editor"}

    # Check permission that should be enabled
    assert db.has_permission(editor, "page.create")

    # Check permission that shouldn't be enabled by default
    assert not db.has_permission(editor, "history.revert")

    # Admins should have all permissions
    admin_id = db.create_user("admin1", generate_password_hash("pass123"), role="admin")
    admin = {"id": admin_id, "role": "admin"}
    assert db.has_permission(admin, "page.create")
    assert db.has_permission(admin, "category.delete")


def test_user_cannot_receive_editor_only_permissions():
    """Legacy per-user overrides no longer replace role-derived defaults."""
    import db

    user_id = db.create_user("user_editorish", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(user_id, {"page.create", "page.edit_all", "page.view_all"})

    user = {"id": user_id, "role": "user"}
    perms = db.get_user_permissions(user_id)

    assert "page.view_all" in perms["enabled_permissions"]
    assert db.has_permission(user, "page.view_all")
    assert not db.has_permission(user, "page.create")
    assert not db.has_permission(user, "page.edit_all")


def test_category_read_access():
    """Custom-role read restrictions are enforced."""
    import db

    # Create categories
    cat1 = db.create_category("Category 1")
    cat2 = db.create_category("Category 2")

    # Create user with custom-role restricted read access to cat1 only
    user_id = db.create_user("user1", generate_password_hash("pass123"), role="user")
    role_id = db.create_custom_role(
        name="Restricted Reader",
        base_role="user",
        permission_keys={"page.view_all"},
        read_restricted=True,
        read_category_ids=[cat1],
    )
    db.assign_custom_role(user_id, role_id)

    user = {"id": user_id, "role": "user"}

    # Should have access to cat1
    assert db.has_category_read_access(user, cat1)

    # Should NOT have access to cat2
    assert not db.has_category_read_access(user, cat2)

    # Should NOT have access to uncategorized (None)
    assert not db.has_category_read_access(user, None)


def test_category_write_access():
    """Custom-role write restrictions are enforced for editors."""
    import db

    # Create categories
    cat1 = db.create_category("Category 1")
    cat2 = db.create_category("Category 2")

    # Create editor with custom-role restricted write access to cat1 only
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    role_id = db.create_custom_role(
        name="Restricted Editor",
        base_role="editor",
        permission_keys={"page.view_all", "page.create"},
        write_restricted=True,
        write_category_ids=[cat1],
    )
    db.assign_custom_role(editor_id, role_id)

    editor = {"id": editor_id, "role": "editor"}

    # Should have write access to cat1
    assert db.has_category_write_access(editor, cat1)

    # Should NOT have write access to cat2
    assert not db.has_category_write_access(editor, cat2)

    # Should NOT have write access to uncategorized (None)
    assert not db.has_category_write_access(editor, None)


def test_unrestricted_category_access():
    """Test unrestricted category access."""
    import db
    from helpers._permissions import get_default_permissions

    # Create categories
    cat1 = db.create_category("Category 1")
    cat2 = db.create_category("Category 2")

    # Create editor with unrestricted access
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    defaults = get_default_permissions('editor')
    db.set_user_permissions(
        editor_id, defaults,
        read_restricted=False,
        write_restricted=False,
    )

    editor = {"id": editor_id, "role": "editor"}

    # Should have access to all categories
    assert db.has_category_read_access(editor, cat1)
    assert db.has_category_read_access(editor, cat2)
    assert db.has_category_read_access(editor, None)
    assert db.has_category_write_access(editor, cat1)
    assert db.has_category_write_access(editor, cat2)
    assert db.has_category_write_access(editor, None)


def test_clear_user_permissions():
    """Clearing legacy rows does not affect role-derived defaults."""
    import db
    from helpers._permissions import get_default_permissions

    # Create user with permissions
    user_id = db.create_user("user1", generate_password_hash("pass123"), role="user")
    defaults = get_default_permissions('user')
    db.set_user_permissions(user_id, defaults)

    # Verify permissions exist
    perms = db.get_user_permissions(user_id)
    assert len(perms['enabled_permissions']) > 0

    # Clear permissions
    db.clear_user_permissions(user_id)

    # Verify role-derived defaults are still returned
    perms = db.get_user_permissions(user_id)
    assert len(perms['enabled_permissions']) > 0


def test_admin_permission_page(logged_in_admin):
    """Per-user permissions page redirects after deprecation."""
    import db
    from helpers._permissions import get_default_permissions

    # Create a user
    user_id = db.create_user("testuser", generate_password_hash("pass123"), role="user")
    defaults = get_default_permissions('user')
    db.set_user_permissions(user_id, defaults)

    # Access deprecated permissions page
    resp = logged_in_admin.get(f"/admin/users/{user_id}/permissions", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Per-user permission customization has been deprecated" in resp.data


def test_update_permissions_via_admin(logged_in_admin):
    """Posting deprecated per-user permissions route has no effect."""
    import db
    from helpers._permissions import get_default_permissions

    # Create an editor
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    defaults = get_default_permissions('editor')
    db.set_user_permissions(editor_id, defaults)

    # Attempt update via deprecated POST route
    resp = logged_in_admin.post(
        f"/admin/users/{editor_id}/permissions",
        data={
            "permissions": ["page.view_all", "page.create"],
            "read_restricted": "1",
            "read_category_ids": [],
            "write_restricted": "1",
            "write_category_ids": [],
        }
    )
    assert resp.status_code == 302  # Redirect

    # Verify defaults remain role-derived
    perms = db.get_user_permissions(editor_id)
    assert "page.view_all" in perms['enabled_permissions']
    assert "page.create" in perms['enabled_permissions']
    assert not perms['category_access']['restricted']


def test_role_change_initializes_permissions(logged_in_admin):
    """Test that changing role initializes default permissions."""
    import db

    # Create a user
    user_id = db.create_user("testuser", generate_password_hash("pass123"), role="user")

    # Change role to editor
    resp = logged_in_admin.post(
        f"/admin/users/{user_id}/edit",
        data={
            "action": "change_role",
            "role": "editor",
        }
    )
    assert resp.status_code == 302

    # Verify permissions were initialized
    perms = db.get_user_permissions(user_id)
    assert len(perms['enabled_permissions']) > 0


def test_create_user_initializes_permissions(logged_in_admin):
    """Test that creating a user/editor initializes permissions."""
    import db

    # Create an editor through admin interface
    resp = logged_in_admin.post(
        "/admin/users/create",
        data={
            "username": "neweditor",
            "password": "password123",
            "confirm_password": "password123",
            "role": "editor",
        }
    )
    assert resp.status_code == 302

    # Get the created user
    user = db.get_user_by_username("neweditor")
    assert user is not None

    # Verify permissions were initialized
    perms = db.get_user_permissions(user["id"])
    assert len(perms['enabled_permissions']) > 0


def test_user_can_view_page_helper():
    """Test the user_can_view_page helper function."""
    import db
    from helpers._auth import user_can_view_page

    # Create a category and pages
    cat_id = db.create_category("Test Category")
    db.create_page("Test Page", "test-page", "Content", category_id=cat_id)
    deindexed_page_id = db.create_page("Hidden Page", "hidden", "Secret", category_id=cat_id)
    db.set_page_deindexed(deindexed_page_id, True)

    # Get page objects
    page = db.get_page_by_slug("test-page")
    deindexed_page = db.get_page_by_slug("hidden")

    # Create user with restricted read access (no access to this category)
    user_id = db.create_user("user1", generate_password_hash("pass123"), role="user")
    role_id = db.create_custom_role(
        name="No Category Reader",
        base_role="user",
        permission_keys={"page.view_all"},
        read_restricted=True,
        read_category_ids=[],  # No categories allowed
    )
    db.assign_custom_role(user_id, role_id)
    user = {"id": user_id, "role": "user"}

    # User should NOT be able to view the page
    assert not user_can_view_page(user, page)

    # User should NOT be able to view deindexed page
    assert not user_can_view_page(user, deindexed_page)

    # Create editor with view_deindexed permission and unrestricted access
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    editor_role_id = db.create_custom_role(
        name="Deindexed Viewer",
        base_role="editor",
        permission_keys={"page.view_all", "page.view_deindexed"},
    )
    db.assign_custom_role(editor_id, editor_role_id)
    editor = {"id": editor_id, "role": "editor"}

    # Editor should be able to view regular page
    assert user_can_view_page(editor, page)

    # Editor should be able to view deindexed page (if has permission)
    assert user_can_view_page(editor, deindexed_page)


def test_write_access_also_grants_read_access():
    """Custom-role editor write access is reflected in read checks."""
    import db

    # Create categories
    cat1 = db.create_category("Category 1")
    cat2 = db.create_category("Category 2")
    cat3 = db.create_category("Category 3")

    # Create editor role with:
    # - Read access to cat1 and cat2
    # - Write access to cat2 and cat3
    editor_id = db.create_user("editor1", generate_password_hash("pass123"), role="editor")
    role_id = db.create_custom_role(
        name="Split Access Editor",
        base_role="editor",
        permission_keys={"page.view_all", "page.create"},
        read_restricted=True,
        read_category_ids=[cat1, cat2],
        write_restricted=True,
        write_category_ids=[cat2, cat3],
    )
    db.assign_custom_role(editor_id, role_id)

    editor = {"id": editor_id, "role": "editor"}

    perms = db.get_user_permissions(editor_id)

    # cat1: read yes, write no
    assert db.has_category_read_access(editor, cat1)
    assert not db.has_category_write_access(editor, cat1)

    # cat2: read yes, write yes
    assert db.has_category_read_access(editor, cat2)
    assert db.has_category_write_access(editor, cat2)

    # cat3: write yes, so read is also granted
    assert db.has_category_read_access(editor, cat3)
    assert db.has_category_write_access(editor, cat3)
    assert cat3 in perms["category_write_access"]["allowed_category_ids"]


def test_editor_write_unrestricted_clears_restricted_read_access():
    """Test that unrestricted editor write access also yields unrestricted read access."""
    import db
    from helpers._permissions import get_default_permissions

    cat1 = db.create_category("Category 1")
    cat2 = db.create_category("Category 2")

    editor_id = db.create_user("editor_unrestricted", generate_password_hash("pass123"), role="editor")
    db.set_user_permissions(
        editor_id,
        get_default_permissions("editor"),
        read_restricted=True,
        read_category_ids=[cat1],
        write_restricted=False,
    )

    editor = {"id": editor_id, "role": "editor"}
    perms = db.get_user_permissions(editor_id)

    assert not perms["category_access"]["restricted"]
    assert db.has_category_read_access(editor, cat1)
    assert db.has_category_read_access(editor, cat2)


def test_admin_permission_page_shows_write_categories_as_read_access(logged_in_admin):
    """Deprecated per-user permissions page redirects with an info message."""
    import db

    read_cat = db.create_category("Read Category")
    write_cat = db.create_category("Write Category")
    editor_id = db.create_user("editor_ui", generate_password_hash("pass123"), role="editor")

    conn = db.get_db()
    conn.execute(
        "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 1)",
        (editor_id,),
    )
    conn.execute(
        "INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'read')",
        (editor_id, read_cat),
    )
    conn.execute(
        "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', 1)",
        (editor_id,),
    )
    conn.execute(
        "INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'write')",
        (editor_id, write_cat),
    )
    conn.commit()
    conn.close()

    resp = logged_in_admin.get(f"/admin/users/{editor_id}/permissions", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Per-user permission customization has been deprecated" in resp.data


def test_user_permission_page_hides_editor_only_permissions(logged_in_admin):
    """Deprecated per-user permissions UI no longer renders."""
    import db

    user_id = db.create_user("plainuser", generate_password_hash("pass123"), role="user")

    resp = logged_in_admin.get(f"/admin/users/{user_id}/permissions", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Per-user permission customization has been deprecated" in resp.data


def test_user_permission_post_ignores_editor_only_permissions_and_write_access(logged_in_admin):
    """Deprecated per-user permissions POST leaves role defaults intact."""
    import db

    cat_id = db.create_category("Restricted Category")
    user_id = db.create_user("backenduser", generate_password_hash("pass123"), role="user")

    resp = logged_in_admin.post(
        f"/admin/users/{user_id}/permissions",
        data={
            "permissions": ["page.view_all", "page.edit_all", "page.create"],
            "read_restricted": "1",
            "read_category_ids": [str(cat_id)],
            "write_restricted": "1",
            "write_category_ids": [str(cat_id)],
        }
    )

    assert resp.status_code == 302

    perms = db.get_user_permissions(user_id)
    user = {"id": user_id, "role": "user"}

    assert "page.view_all" in perms["enabled_permissions"]
    assert not perms["category_access"]["restricted"]
    assert perms["category_access"]["allowed_category_ids"] == []
    assert not perms["category_write_access"]["restricted"]
    assert perms["category_write_access"]["allowed_category_ids"] == []
    assert not db.has_category_write_access(user, cat_id)


def _make_tracked_perm_context(module, trigger_sql, error_msg="simulated failure", use_cursor=False):
    """Create a tracked get_db_context that records cleanup and raises on trigger_sql."""
    from contextlib import contextmanager

    cleaned = []
    original = module.get_db_context

    @contextmanager
    def _ctx():
        with original() as real_conn:

            class _FailCur:
                def __init__(self, real_cur):
                    self._real = real_cur

                def execute(self, sql, *a, **kw):
                    if trigger_sql in sql:
                        raise RuntimeError(error_msg)
                    return self._real.execute(sql, *a, **kw)

                def __getattr__(self, name):
                    return getattr(self._real, name)

            class _W:
                def execute(self, sql, *a, **kw):
                    if trigger_sql in sql:
                        raise RuntimeError(error_msg)
                    return real_conn.execute(sql, *a, **kw)

                def cursor(self):
                    return _FailCur(real_conn.cursor())

                def __getattr__(self, name):
                    return getattr(real_conn, name)

            try:
                yield _W()
            finally:
                cleaned.append(True)

    return _ctx, cleaned


def test_get_user_permissions_closes_conn_on_error(monkeypatch):
    """get_user_permissions() cleans up DB connection even when a query raises."""
    import db
    import db._permissions as pmod

    uid = db.create_user("perm_conn_test", generate_password_hash("pw"), role="editor")
    ctx, cleaned = _make_tracked_perm_context(
        pmod, "SELECT role, custom_role_id FROM users", "simulated query failure"
    )
    monkeypatch.setattr(pmod, "get_db_context", ctx)

    with pytest.raises(RuntimeError, match="simulated query failure"):
        db.get_user_permissions(uid)

    assert cleaned, "connection cleanup was never called after the exception"


def test_set_user_permissions_closes_conn_on_error(monkeypatch):
    """set_user_permissions() cleans up DB connection even when a query raises."""
    import db
    import db._permissions as pmod

    uid = db.create_user("perm_conn_set", generate_password_hash("pw"), role="editor")
    ctx, cleaned = _make_tracked_perm_context(pmod, "DELETE", "simulated write failure")
    monkeypatch.setattr(pmod, "get_db_context", ctx)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        db.set_user_permissions(uid, {"page.create"})

    assert cleaned, "connection cleanup was never called after the exception"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
