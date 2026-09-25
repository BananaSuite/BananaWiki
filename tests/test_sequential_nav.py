"""
Tests for the toggle_category_sequential_nav route.

Covers the POST /category/<cat_id>/sequential-nav endpoint which enables
or disables Prev/Next sequential page navigation for a category.
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
    """Use a fresh temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate-limit store before and after each test."""
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
def admin_user():
    """Create an admin user and mark setup as done."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def editor_user(admin_user):
    """Create an unrestricted editor user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor1", generate_password_hash("editor123"), role="editor")
    return uid


@pytest.fixture
def restricted_editor(admin_user):
    """Create a restricted editor who cannot modify categories."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("restricted", generate_password_hash("restricted123"), role="editor")
    db.set_editor_access(uid, restricted=True, category_ids=[])
    return uid


@pytest.fixture
def regular_user(admin_user):
    """Create a regular (viewer) user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("user1", generate_password_hash("user123"), role="user")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a test client logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, editor_user):
    """Return a test client logged in as an unrestricted editor."""
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    return client


@pytest.fixture
def logged_in_restricted_editor(client, restricted_editor):
    """Return a test client logged in as a restricted editor."""
    client.post("/login", data={"username": "restricted", "password": "restricted123"})
    return client


@pytest.fixture
def logged_in_user(client, regular_user):
    """Return a test client logged in as a regular user."""
    client.post("/login", data={"username": "user1", "password": "user123"})
    return client


@pytest.fixture
def category(admin_user):
    """Create a test category and return its ID."""
    import db
    cat_id = db.create_category("Test Category", parent_id=None)
    return cat_id


# ---------------------------------------------------------------------------
# toggle_category_sequential_nav route tests
# ---------------------------------------------------------------------------

class TestToggleCategorySequentialNav:
    """HTTP-level tests for POST /category/<cat_id>/sequential-nav."""

    def test_admin_can_enable_sequential_nav(self, logged_in_admin, category):
        """Admin can enable sequential navigation on a category."""
        import db
        # Confirm it starts disabled
        cat = db.get_category(category)
        assert cat["sequential_nav"] == 0

        resp = logged_in_admin.post(
            f"/category/{category}/sequential-nav",
            data={"sequential_nav": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Sequential navigation setting updated" in resp.data
        cat = db.get_category(category)
        assert cat["sequential_nav"] == 1

    def test_admin_can_disable_sequential_nav(self, logged_in_admin, category):
        """Admin can disable sequential navigation on a category."""
        import db
        # First enable it
        db.update_category_sequential_nav(category, True)
        assert db.get_category(category)["sequential_nav"] == 1

        resp = logged_in_admin.post(
            f"/category/{category}/sequential-nav",
            data={"sequential_nav": "0"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Sequential navigation setting updated" in resp.data
        assert db.get_category(category)["sequential_nav"] == 0

    def test_editor_can_toggle_sequential_nav(self, logged_in_editor, category):
        """An editor with the current category permission can toggle sequential navigation."""
        import db
        from helpers._permissions import get_default_permissions

        editor = db.get_user_by_username("editor1")
        db.set_user_permissions(
            editor["id"],
            get_default_permissions("editor") | {"category.manage_sequential"},
        )
        resp = logged_in_editor.post(
            f"/category/{category}/sequential-nav",
            data={"sequential_nav": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Sequential navigation setting updated" in resp.data
        assert db.get_category(category)["sequential_nav"] == 1

    def test_restricted_editor_cannot_toggle_sequential_nav(
        self, logged_in_restricted_editor, category
    ):
        """A restricted editor is blocked from changing sequential navigation."""
        import db
        resp = logged_in_restricted_editor.post(
            f"/category/{category}/sequential-nav",
            data={"sequential_nav": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"do not have permission" in resp.data
        # Setting should be unchanged
        assert db.get_category(category)["sequential_nav"] == 0

    def test_regular_user_cannot_toggle_sequential_nav(
        self, logged_in_user, category
    ):
        """A regular (viewer) user is blocked from changing sequential navigation."""
        import db
        resp = logged_in_user.post(
            f"/category/{category}/sequential-nav",
            data={"sequential_nav": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"do not have permission" in resp.data
        assert db.get_category(category)["sequential_nav"] == 0

    def test_nonexistent_category_returns_404(self, logged_in_admin):
        """A request for a non-existent category ID returns 404."""
        resp = logged_in_admin.post(
            "/category/99999/sequential-nav",
            data={"sequential_nav": "1"},
        )
        assert resp.status_code == 404

    def test_requires_login(self, client, category):
        """Unauthenticated POST is redirected to the login page."""
        resp = client.post(
            f"/category/{category}/sequential-nav",
            data={"sequential_nav": "1"},
        )
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_missing_form_value_defaults_to_disabled(self, logged_in_admin, category):
        """When sequential_nav form field is absent the setting is set to disabled (0)."""
        import db
        db.update_category_sequential_nav(category, True)

        resp = logged_in_admin.post(
            f"/category/{category}/sequential-nav",
            data={},           # no sequential_nav key
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Sequential navigation setting updated" in resp.data
        assert db.get_category(category)["sequential_nav"] == 0

    def test_sequential_nav_enables_adjacent_page_lookup(
        self, logged_in_admin, category
    ):
        """Enabling sequential nav causes get_adjacent_pages to return neighbours."""
        import db
        page_a_id = db.create_page("Page A", "page-a-seq", "content a", category_id=category)
        page_b_id = db.create_page("Page B", "page-b-seq", "content b", category_id=category)

        # Before enabling: no adjacent pages
        prev_p, next_p = db.get_adjacent_pages(page_a_id)
        assert prev_p is None
        assert next_p is None

        # Enable sequential nav via the HTTP route
        resp = logged_in_admin.post(
            f"/category/{category}/sequential-nav",
            data={"sequential_nav": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # After enabling: page A should have page B as next
        prev_p, next_p = db.get_adjacent_pages(page_a_id)
        assert next_p is not None
        assert next_p["id"] == page_b_id


# ---------------------------------------------------------------------------
# DB layer: get_adjacent_pages skips pending-deletion pages
# ---------------------------------------------------------------------------
def test_adjacent_pages_skips_pending_deletion(isolated_db):
    """get_adjacent_pages must not return pages that are pending deletion."""
    import db
    cat_id = db.create_category("SeqCatPD")
    db.update_category_sequential_nav(cat_id, True)
    p1 = db.create_page("Page A", "page-a-pd", "c", category_id=cat_id)
    p2 = db.create_page("Page B", "page-b-pd", "c", category_id=cat_id)
    p3 = db.create_page("Page C", "page-c-pd", "c", category_id=cat_id)

    # Create a dummy user to satisfy the foreign-key reference
    user_id = db.create_user("pduser", "pass", "user")
    db.mark_page_pending_deletion(p2, user_id)

    # p2 is pending deletion, so p1's next should skip to p3
    prev_p, next_p = db.get_adjacent_pages(p1)
    assert prev_p is None
    assert next_p is not None
    assert next_p["id"] == p3

    # p2 is pending deletion, so p3's prev should skip back to p1
    prev_p, next_p = db.get_adjacent_pages(p3)
    assert next_p is None
    assert prev_p is not None
    assert prev_p["id"] == p1
