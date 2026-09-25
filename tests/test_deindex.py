"""
Tests for the page deindex feature.
"""

import os
import sys
import pytest

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
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def editor_user():
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor", generate_password_hash("editor123"), role="editor")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def regular_user():
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("user1", generate_password_hash("user123"), role="user")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, editor_user):
    client.post("/login", data={"username": "editor", "password": "editor123"})
    return client


@pytest.fixture
def logged_in_user(client, regular_user):
    client.post("/login", data={"username": "user1", "password": "user123"})
    return client


# -----------------------------------------------------------------------
# DB layer: set_page_deindexed and is_deindexed flag
# -----------------------------------------------------------------------
def test_set_page_deindexed_sets_flag():
    import db
    page_id = db.create_page("Test Deindex", "test-deindex", "content")
    page = db.get_page(page_id)
    assert page["is_deindexed"] == 0

    db.set_page_deindexed(page_id, True)
    page = db.get_page(page_id)
    assert page["is_deindexed"] == 1

    db.set_page_deindexed(page_id, False)
    page = db.get_page(page_id)
    assert page["is_deindexed"] == 0


# -----------------------------------------------------------------------
# DB layer: search_pages excludes deindexed by default
# -----------------------------------------------------------------------
def test_search_pages_excludes_deindexed_by_default():
    import db
    page_id = db.create_page("Hidden Page", "hidden-page", "content")
    db.set_page_deindexed(page_id, True)

    results = db.search_pages("hidden")
    assert not any(r["slug"] == "hidden-page" for r in results)


def test_search_pages_includes_deindexed_when_requested():
    import db
    page_id = db.create_page("Hidden Page", "hidden-page", "content")
    db.set_page_deindexed(page_id, True)

    results = db.search_pages("hidden", include_deindexed=True)
    assert any(r["slug"] == "hidden-page" for r in results)


def test_search_pages_visible_page_always_included():
    import db
    db.create_page("Visible Page", "visible-page", "content")

    results = db.search_pages("visible")
    assert any(r["slug"] == "visible-page" for r in results)

    results = db.search_pages("visible", include_deindexed=True)
    assert any(r["slug"] == "visible-page" for r in results)


# -----------------------------------------------------------------------
# DB layer: get_adjacent_pages skips deindexed pages
# -----------------------------------------------------------------------
def test_adjacent_pages_skips_deindexed():
    import db
    cat_id = db.create_category("SeqCat")
    db.update_category_sequential_nav(cat_id, True)
    p1 = db.create_page("Page A", "page-a", "c", category_id=cat_id)
    p2 = db.create_page("Page B", "page-b", "c", category_id=cat_id)
    p3 = db.create_page("Page C", "page-c", "c", category_id=cat_id)

    # Deindex p2; prev/next for p1 should skip over p2 to p3 for p1 next
    db.set_page_deindexed(p2, True)

    prev_p, next_p = db.get_adjacent_pages(p1)
    assert prev_p is None
    # p2 is deindexed, so next should be p3
    assert next_p is not None
    assert next_p["id"] == p3

    prev_p, next_p = db.get_adjacent_pages(p3)
    assert next_p is None
    # p2 is deindexed, so prev should be p1
    assert prev_p is not None
    assert prev_p["id"] == p1


# -----------------------------------------------------------------------
# Route: toggle_page_deindex available to editors and admins
# -----------------------------------------------------------------------
def test_admin_can_deindex_page(logged_in_admin):
    import db
    db.create_page("Toggleable", "toggleable", "content")

    resp = logged_in_admin.post("/page/toggleable/deindex", follow_redirects=True)
    assert resp.status_code == 200
    assert b"deindexed" in resp.data.lower()

    page = db.get_page_by_slug("toggleable")
    assert page["is_deindexed"] == 1


def test_admin_can_reindex_page(logged_in_admin):
    import db
    page_id = db.create_page("Toggleable2", "toggleable2", "content")
    db.set_page_deindexed(page_id, True)

    resp = logged_in_admin.post("/page/toggleable2/deindex", follow_redirects=True)
    assert resp.status_code == 200
    assert b"reindexed" in resp.data.lower()

    page = db.get_page_by_slug("toggleable2")
    assert page["is_deindexed"] == 0


def test_editor_can_deindex_page(logged_in_editor):
    import db
    db.create_page("EditorToggle", "editor-toggle", "content")

    resp = logged_in_editor.post("/page/editor-toggle/deindex", follow_redirects=True)
    assert resp.status_code == 200
    page = db.get_page_by_slug("editor-toggle")
    assert page["is_deindexed"] == 1


def test_regular_user_cannot_deindex_page(logged_in_user):
    import db
    db.create_page("UserToggle", "user-toggle", "content")

    resp = logged_in_user.post("/page/user-toggle/deindex", follow_redirects=True)
    assert resp.status_code == 200
    # Should be redirected with permission error
    page = db.get_page_by_slug("user-toggle")
    assert page["is_deindexed"] == 0


def test_cannot_deindex_home_page(logged_in_admin):
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post(f"/page/{home['slug']}/deindex", follow_redirects=True)
    assert resp.status_code == 200
    assert b"The home page cannot be deindexed" in resp.data
    page = db.get_page_by_slug(home["slug"])
    assert page["is_deindexed"] == 0


# -----------------------------------------------------------------------
# Route: deindexed page visibility follows the current permission model
# -----------------------------------------------------------------------
def test_deindexed_page_hidden_by_url_for_regular_user_without_permission(logged_in_user):
    import db
    page_id = db.create_page("Hidden Page2", "hidden-page2", "secret content")
    db.set_page_deindexed(page_id, True)

    resp = logged_in_user.get("/page/hidden-page2")
    assert resp.status_code == 403


def test_deindexed_page_accessible_by_url_with_explicit_permission(client, regular_user):
    import db
    from helpers._permissions import get_default_permissions

    page_id = db.create_page("Hidden Page3", "hidden-page3", "secret content")
    db.set_page_deindexed(page_id, True)
    db.set_user_permissions(
        regular_user,
        get_default_permissions("user") | {"page.view_deindexed"},
        read_restricted=False,
    )

    client.post("/login", data={"username": "user1", "password": "user123"})
    resp = client.get("/page/hidden-page3")

    assert resp.status_code == 200
    assert b"Hidden Page3" in resp.data


def test_deindexed_page_permission_still_respects_category_access(client):
    import db
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    db.update_site_settings(setup_done=1)
    blocked_cat = db.create_category("Blocked")
    page_id = db.create_page("Hidden Page4", "hidden-page4", "secret content", category_id=blocked_cat)
    db.set_page_deindexed(page_id, True)

    user_id = db.create_user("deindexed_reader", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user") | {"page.view_deindexed"},
        read_restricted=True,
        read_category_ids=[],
    )

    client.post("/login", data={"username": "deindexed_reader", "password": "pass123"})
    resp = client.get("/page/hidden-page4")
    assert resp.status_code == 403


# -----------------------------------------------------------------------
# API: search excludes deindexed for regular users, includes for editors
# -----------------------------------------------------------------------
def test_api_search_excludes_deindexed_for_regular_user(logged_in_user):
    import db
    page_id = db.create_page("Secret Wiki Page", "secret-wiki", "content")
    db.set_page_deindexed(page_id, True)

    resp = logged_in_user.get("/api/pages/search?q=Secret+Wiki")
    assert resp.status_code == 200
    data = resp.get_json()
    assert not any(r["slug"] == "secret-wiki" for r in data)


def test_api_search_includes_deindexed_for_editor_with_permission(client, editor_user):
    import db
    from helpers._permissions import get_default_permissions

    page_id = db.create_page("Secret Wiki Page", "secret-wiki2", "content")
    db.set_page_deindexed(page_id, True)
    db.set_user_permissions(
        editor_user,
        get_default_permissions("editor"),
        read_restricted=False,
    )

    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.get("/api/pages/search?q=Secret+Wiki")
    assert resp.status_code == 200
    data = resp.get_json()
    assert any(r["slug"] == "secret-wiki2" for r in data)


def test_api_search_includes_deindexed_for_regular_user_with_permission(client, regular_user):
    import db
    from helpers._permissions import get_default_permissions

    page_id = db.create_page("Secret Wiki Page", "secret-wiki3", "content")
    db.set_page_deindexed(page_id, True)
    db.set_user_permissions(
        regular_user,
        get_default_permissions("user") | {"page.view_deindexed"},
        read_restricted=False,
    )

    client.post("/login", data={"username": "user1", "password": "user123"})
    resp = client.get("/api/pages/search?q=Secret+Wiki")
    assert resp.status_code == 200
    data = resp.get_json()
    assert any(r["slug"] == "secret-wiki3" for r in data)


def test_api_search_excludes_deindexed_for_editor_without_permission(client, editor_user):
    import db
    from helpers._permissions import get_default_permissions

    page_id = db.create_page("Secret Wiki Page", "secret-wiki4", "content")
    db.set_page_deindexed(page_id, True)
    db.set_user_permissions(
        editor_user,
        get_default_permissions("editor") - {"page.view_deindexed"},
        read_restricted=False,
    )

    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.get("/api/pages/search?q=Secret+Wiki")
    assert resp.status_code == 200
    data = resp.get_json()
    assert not any(r["slug"] == "secret-wiki4" for r in data)


def test_sidebar_search_includes_deindexed_only_with_permission(client, regular_user):
    import db
    from helpers._permissions import get_default_permissions

    page_id = db.create_page("Secret Sidebar Page", "secret-sidebar", "content")
    db.set_page_deindexed(page_id, True)

    client.post("/login", data={"username": "user1", "password": "user123"})
    resp = client.get("/api/sidebar/search?q=Secret+Sidebar")
    assert resp.status_code == 200
    data = resp.get_json()
    assert not any(r["slug"] == "secret-sidebar" for r in data["pages"])

    db.set_user_permissions(
        regular_user,
        get_default_permissions("user") | {"page.view_deindexed"},
        read_restricted=False,
    )

    resp = client.get("/api/sidebar/search?q=Secret+Sidebar")
    assert resp.status_code == 200
    data = resp.get_json()
    assert any(r["slug"] == "secret-sidebar" for r in data["pages"])


def test_api_search_includes_deindexed_for_admin(logged_in_admin):
    import db
    page_id = db.create_page("Secret Wiki Page", "secret-wiki3", "content")
    db.set_page_deindexed(page_id, True)

    resp = logged_in_admin.get("/api/pages/search?q=Secret+Wiki")
    assert resp.status_code == 200
    data = resp.get_json()
    assert any(r["slug"] == "secret-wiki3" for r in data)


# -----------------------------------------------------------------------
# Template: deindexed badge visible on page for editors/admins
# -----------------------------------------------------------------------
def test_deindexed_badge_shown_to_admin(logged_in_admin):
    import db
    page_id = db.create_page("Badge Test", "badge-test", "content")
    db.set_page_deindexed(page_id, True)

    resp = logged_in_admin.get("/page/badge-test")
    assert resp.status_code == 200
    assert b"Deindexed" in resp.data
    assert b"Reindex" in resp.data


def test_deindexed_page_hidden_from_regular_user_without_permission(logged_in_user):
    import db
    page_id = db.create_page("Badge Test2", "badge-test2", "content")
    db.set_page_deindexed(page_id, True)

    resp = logged_in_user.get("/page/badge-test2")
    assert resp.status_code == 403


# -----------------------------------------------------------------------
# Template: category tree includes deindexed indicator in sidebar
# -----------------------------------------------------------------------
def test_category_tree_includes_deindexed_for_admin(logged_in_admin):
    import db
    cat_id = db.create_category("TestCat")
    page_id = db.create_page("Deindexed In Cat", "deindexed-in-cat", "c", category_id=cat_id)
    db.set_page_deindexed(page_id, True)

    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    # Admin should see the page (crossed out) in sidebar
    assert b"deindexed-in-cat" in resp.data


def test_category_tree_hides_deindexed_from_regular_user(logged_in_user):
    import db
    cat_id = db.create_category("TestCat2")
    page_id = db.create_page("Deindexed In Cat2", "deindexed-in-cat2", "c", category_id=cat_id)
    db.set_page_deindexed(page_id, True)

    resp = logged_in_user.get("/")
    assert resp.status_code == 200
    # Regular user should NOT see the page in sidebar
    assert b"deindexed-in-cat2" not in resp.data
