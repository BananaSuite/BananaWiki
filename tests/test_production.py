"""
Production-readiness tests for BananaWiki.

Covers routes and features not exercised by the existing test suite:
  - Setup success flow
  - Page delete (non-home page)
  - Admin invite-code generate and delete via HTTP routes
  - Admin create user (success)
  - Admin suspend / unsuspend user
  - Admin user list filters (role / status)
  - Draft API: save, load, and transfer endpoints
  - API preview (success path)
  - View history entry detail page
  - Announcement visibility for logged-out users
  - DB helpers: list_categories, get_category_tree, get_page, transfer_draft
  - Account password change (success + wrong current password)
  - Create page (success path)
  - Unauthenticated access redirects correctly
  - Setup page redirects once setup is complete
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


# ---------------------------------------------------------------------------
# Shared fixtures
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
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


# ---------------------------------------------------------------------------
# Setup flow
# ---------------------------------------------------------------------------

def test_setup_page_accessible_before_setup(client):
    """GET /setup should return 200 before setup is done."""
    resp = client.get("/setup")
    assert resp.status_code == 200
    assert b"setup" in resp.data.lower() or b"Setup" in resp.data


def test_setup_redirects_after_completion(client):
    """After setup is done, GET /setup should redirect to home."""
    import db
    db.update_site_settings(setup_done=1)
    db.create_user("someadmin", __import__("werkzeug.security",
                   fromlist=["generate_password_hash"]).generate_password_hash("x"), role="admin")
    resp = client.get("/setup")
    assert resp.status_code == 302


def test_before_request_hook_missing_site_settings_redirects_to_setup(client, monkeypatch):
    """When site_settings row is missing, non-setup requests redirect to /setup."""
    import db
    monkeypatch.setattr(db, "get_site_settings", lambda: None)
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/setup" in resp.headers["Location"]


def test_before_request_hook_missing_site_settings_allows_setup(client, monkeypatch):
    """When site_settings row is missing, /setup itself still renders."""
    import db
    monkeypatch.setattr(db, "get_site_settings", lambda: None)
    resp = client.get("/setup")
    assert resp.status_code == 200


def test_setup_success(client):
    """Valid POST to /setup creates the admin and marks setup done."""
    import db
    resp = client.post("/setup", data={
        "username": "newadmin",
        "password": "securepass",
        "confirm_password": "securepass",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Admin account created" in resp.data or b"logged in" in resp.data.lower()
    settings = db.get_site_settings()
    assert settings["setup_done"] == 1
    user = db.get_user_by_username("newadmin")
    assert user is not None
    assert user["role"] == "owner"


def test_setup_requires_min_password_length(client):
    """Password shorter than 8 characters should be rejected."""
    resp = client.post("/setup", data={
        "username": "admin",
        "password": "abc",
        "confirm_password": "abc",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"8 characters" in resp.data


def test_setup_rejects_password_mismatch(client):
    """Mismatching passwords should be rejected at setup."""
    resp = client.post("/setup", data={
        "username": "admin",
        "password": "password123",
        "confirm_password": "different123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"do not match" in resp.data.lower()


# ---------------------------------------------------------------------------
# Unauthenticated access redirects
# ---------------------------------------------------------------------------

def test_home_requires_login(client, admin_user):
    """/ redirects to /login when not authenticated."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "login" in resp.headers["Location"]


def test_create_page_requires_login(client, admin_user):
    """/create-page redirects to /login when not authenticated."""
    resp = client.get("/create-page", follow_redirects=False)
    assert resp.status_code == 302
    assert "login" in resp.headers["Location"]


def test_admin_settings_requires_login(client, admin_user):
    """/global-settings redirects when not authenticated."""
    resp = client.get("/global-settings", follow_redirects=False)
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Page delete route
# ---------------------------------------------------------------------------

def test_delete_page_success(logged_in_admin, admin_user):
    """Deleting a non-home page succeeds and removes it from the DB."""
    import db
    page_id = db.create_page("ToDelete", "to-delete", "content", None, admin_user)
    resp = logged_in_admin.post("/page/to-delete/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Page deleted" in resp.data
    assert db.get_page(page_id) is None


def test_delete_home_page_blocked(logged_in_admin):
    """The home page cannot be deleted."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post("/page/home/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot delete the home page" in resp.data
    assert db.get_page(home["id"]) is not None


def test_delete_nonexistent_page_returns_404(logged_in_admin):
    """Deleting a page that doesn't exist returns 404."""
    resp = logged_in_admin.post("/page/does-not-exist/delete")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Create page (success path)
# ---------------------------------------------------------------------------

def test_create_page_success(logged_in_admin):
    """Valid POST to /create-page creates the page and redirects to it."""
    import db
    resp = logged_in_admin.post("/create-page",
                                data={"title": "Brand New Page", "content": "hello",
                                      "category_id": ""},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Page created" in resp.data or b"Brand New Page" in resp.data
    page = db.get_page_by_slug("brand-new-page")
    assert page is not None
    assert page["title"] == "Brand New Page"


def test_create_page_generates_unique_slug(logged_in_admin):
    """Creating two pages with the same title generates unique slugs."""
    import db
    logged_in_admin.post("/create-page",
                         data={"title": "Duplicate Title", "content": "",
                               "category_id": ""})
    logged_in_admin.post("/create-page",
                         data={"title": "Duplicate Title", "content": "",
                               "category_id": ""})
    p1 = db.get_page_by_slug("duplicate-title")
    p2 = db.get_page_by_slug("duplicate-title-1")
    assert p1 is not None
    assert p2 is not None


# ---------------------------------------------------------------------------
# View history entry
# ---------------------------------------------------------------------------

def test_view_history_entry(logged_in_admin, admin_user):
    """GET /page/<slug>/history/<id> renders the historic version."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Version for history", admin_user, "history entry test")
    history = db.get_page_history(home["id"])
    entry = history[0]
    resp = logged_in_admin.get(f"/page/home/history/{entry['id']}")
    assert resp.status_code == 200
    assert b"Version for history" in resp.data or b"history" in resp.data.lower()


def test_view_history_entry_wrong_slug_returns_404(logged_in_admin, admin_user):
    """History entry belonging to a different page returns 404."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "some content", admin_user, "msg")
    history = db.get_page_history(home["id"])
    entry = history[0]
    # Create a different page and try to access home's history entry under its slug
    db.create_page("Other", "other-page", "content", None, admin_user)
    resp = logged_in_admin.get(f"/page/other-page/history/{entry['id']}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin invite codes (HTTP routes)
# ---------------------------------------------------------------------------

def test_admin_generate_invite_code(logged_in_admin):
    """POST /admin/codes/generate creates a new invite code and shows it."""
    import db
    resp = logged_in_admin.post("/admin/codes/generate", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invite code generated" in resp.data
    codes = db.list_invite_codes()
    assert len(codes) >= 1


def test_admin_delete_invite_code(logged_in_admin, admin_user):
    """POST /admin/codes/<id>/delete removes the code."""
    import db
    db.generate_invite_code(admin_user)
    codes = db.list_invite_codes()
    code_id = codes[0]["id"]
    resp = logged_in_admin.post(f"/admin/codes/{code_id}/delete",
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invite code deleted" in resp.data
    remaining = db.list_invite_codes()
    assert all(c["id"] != code_id for c in remaining)


def test_admin_codes_page_renders(logged_in_admin):
    """GET /admin/codes renders the codes page."""
    resp = logged_in_admin.get("/admin/codes")
    assert resp.status_code == 200
    assert b"Invite" in resp.data


def test_admin_codes_expired_page_renders(logged_in_admin, admin_user):
    """GET /admin/codes/expired renders the expired codes page."""
    import db
    code = db.generate_invite_code(admin_user)
    uid = db.create_user("someuser", __import__("werkzeug.security",
                          fromlist=["generate_password_hash"]).generate_password_hash("pw"))
    db.use_invite_code(code, uid)
    resp = logged_in_admin.get("/admin/codes/expired")
    assert resp.status_code == 200
    assert b"code" in resp.data.lower()


# ---------------------------------------------------------------------------
# Admin create user (success)
# ---------------------------------------------------------------------------

def test_admin_create_user_success(logged_in_admin):
    """Admin can create a new user via POST /admin/users/create."""
    import db
    resp = logged_in_admin.post("/admin/users/create", data={
        "username": "freshuser",
        "password": "password123",
        "confirm_password": "password123",
        "role": "editor",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"freshuser" in resp.data
    user = db.get_user_by_username("freshuser")
    assert user is not None
    assert user["role"] == "editor"


def test_admin_create_user_password_too_short(logged_in_admin):
    """Admin-created user with password shorter than 8 characters is rejected."""
    resp = logged_in_admin.post("/admin/users/create", data={
        "username": "shortpw",
        "password": "abcdefg",
        "confirm_password": "abcdefg",
        "role": "user",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"8 characters" in resp.data


# ---------------------------------------------------------------------------
# Admin suspend / unsuspend user
# ---------------------------------------------------------------------------

def test_admin_suspend_user(logged_in_admin, admin_user):
    """Admin can suspend another user."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("victim", generate_password_hash("pass123"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "suspend"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"User suspended" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1


def test_admin_unsuspend_user(logged_in_admin, admin_user):
    """Admin can unsuspend a suspended user."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("suspended_one", generate_password_hash("pass123"), role="user")
    db.update_user(uid, suspended=1)
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "unsuspend"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"User unsuspended" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_suspended_user_cannot_login(client, admin_user):
    """A suspended user should not be able to log in."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("banneduser", generate_password_hash("pass123"), role="user")
    db.update_user(uid, suspended=1)
    resp = client.post("/login", data={"username": "banneduser", "password": "pass123"},
                       follow_redirects=True)
    assert resp.status_code == 403
    assert b"issue with your account" in resp.data.lower()


def test_admin_cannot_suspend_last_admin(logged_in_admin, admin_user):
    """Suspending the last admin account should be blocked."""
    resp = logged_in_admin.post(f"/admin/users/{admin_user}/edit",
                                data={"action": "suspend"},
                                follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot suspend your own account" in resp.data


# ---------------------------------------------------------------------------
# Admin user list filters
# ---------------------------------------------------------------------------

def test_admin_users_filter_by_role(logged_in_admin, admin_user):
    """GET /admin/users?role=editor only shows editors in the user table."""
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("editorguy", generate_password_hash("pw"), role="editor")
    db.create_user("normaluser", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.get("/admin/users?role=editor")
    assert resp.status_code == 200
    # The editor should appear in the filtered list
    assert b"editorguy" in resp.data
    # The plain user should not appear when filtering by editor
    assert b"normaluser" not in resp.data


def test_admin_users_filter_by_status(logged_in_admin, admin_user):
    """GET /admin/users?status=suspended only shows suspended users."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("sususer", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1)
    resp = logged_in_admin.get("/admin/users?status=suspended")
    assert resp.status_code == 200
    assert b"sususer" in resp.data


# ---------------------------------------------------------------------------
# Draft API: save, load, transfer
# ---------------------------------------------------------------------------

def test_api_save_draft(logged_in_admin, admin_user):
    """POST /api/draft/save persists the draft to the DB."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post("/api/draft/save",
                                json={"page_id": home["id"],
                                      "title": "Saved Title",
                                      "content": "saved content"},
                                content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["message"] == "Draft saved successfully."
    draft = db.get_draft(home["id"], admin_user)
    assert draft is not None
    assert draft["title"] == "Saved Title"
    assert draft["content"] == "saved content"


def test_api_save_draft_missing_page_id(logged_in_admin):
    """POST /api/draft/save without page_id returns 400."""
    resp = logged_in_admin.post("/api/draft/save",
                                json={"title": "x", "content": "y"},
                                content_type="application/json")
    assert resp.status_code == 400


def test_api_save_draft_invalid_page_id(logged_in_admin):
    """POST /api/draft/save with non-numeric page_id returns 400."""
    resp = logged_in_admin.post("/api/draft/save",
                                json={"page_id": "abc", "title": "x", "content": "y"},
                                content_type="application/json")
    assert resp.status_code == 400


def test_api_save_draft_nonexistent_page(logged_in_admin):
    """POST /api/draft/save for a page that doesn't exist returns 404."""
    resp = logged_in_admin.post("/api/draft/save",
                                json={"page_id": 99999, "title": "x", "content": "y"},
                                content_type="application/json")
    assert resp.status_code == 404


def test_api_load_draft_no_draft(logged_in_admin):
    """GET /api/draft/load/<id> returns null fields when no draft exists."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.get(f"/api/draft/load/{home['id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["title"] is None
    assert data["content"] is None


def test_api_load_draft_existing_draft(logged_in_admin, admin_user):
    """GET /api/draft/load/<id> returns the saved draft."""
    import db
    home = db.get_home_page()
    db.save_draft(home["id"], admin_user, "My Draft", "My Content")
    resp = logged_in_admin.get(f"/api/draft/load/{home['id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["title"] == "My Draft"
    assert data["content"] == "My Content"
    assert data["updated_at"] is not None


def test_api_transfer_draft(logged_in_admin, admin_user):
    """POST /api/draft/transfer moves a draft from another user to the current user."""
    import db
    from werkzeug.security import generate_password_hash
    # Create a second user with a draft
    editor_id = db.create_user("editor_src", generate_password_hash("pw"), role="editor")
    home = db.get_home_page()
    db.save_draft(home["id"], editor_id, "Editor Draft", "Editor Content")
    resp = logged_in_admin.post("/api/draft/transfer",
                                json={"page_id": home["id"],
                                      "from_user_id": editor_id},
                                content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["message"] == "Draft has been successfully transferred to your account."
    # Draft should now be owned by admin
    transferred = db.get_draft(home["id"], admin_user)
    assert transferred is not None
    assert transferred["content"] == "Editor Content"


def test_api_transfer_draft_from_self_blocked(logged_in_admin, admin_user):
    """Transferring a draft from oneself should be rejected."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post("/api/draft/transfer",
                                json={"page_id": home["id"],
                                      "from_user_id": admin_user},
                                content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "yourself" in data["error"]


def test_api_transfer_draft_missing_source_returns_404(logged_in_admin):
    """Transferring a draft that doesn't exist returns 404."""
    import db
    home = db.get_home_page()
    resp = logged_in_admin.post("/api/draft/transfer",
                                json={"page_id": home["id"],
                                       "from_user_id": 99999},
                                content_type="application/json")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("method", "url_builder", "payload", "seed_draft"),
    [
        ("post", lambda page_id: "/api/draft/save",
         lambda page_id: {"page_id": page_id, "title": "Blocked", "content": "blocked"}, False),
        ("get", lambda page_id: f"/api/draft/load/{page_id}", None, False),
        ("get", lambda page_id: f"/api/draft/others/{page_id}", None, False),
        ("post", lambda page_id: "/api/draft/delete",
         lambda page_id: {"page_id": page_id}, True),
    ],
)
def test_restricted_editor_draft_endpoints_require_category_access(
    client, editor_user, admin_user, method, url_builder, payload, seed_draft
):
    """Restricted editors cannot use draft APIs for pages in disallowed categories."""
    import db

    allowed_cat = db.create_category("Allowed Drafts")
    blocked_cat = db.create_category("Blocked Drafts")
    blocked_page_id = db.create_page(
        "Blocked Draft Page", "blocked-draft-page", "content", blocked_cat, admin_user
    )
    db.set_editor_access(editor_user, restricted=True, category_ids=[allowed_cat])
    if seed_draft:
        db.save_draft(blocked_page_id, editor_user, "Restricted Draft", "protected draft")

    client.post("/login", data={"username": "editor", "password": "editor123"})
    kwargs = {"content_type": "application/json"}
    if payload is not None:
        kwargs["json"] = payload(blocked_page_id)
    resp = getattr(client, method)(url_builder(blocked_page_id), **kwargs)

    assert resp.status_code == 403
    assert "permission" in resp.get_json()["error"].lower()
    draft = db.get_draft(blocked_page_id, editor_user)
    if seed_draft:
        assert draft is not None
        assert draft["content"] == "protected draft"
    else:
        assert draft is None


def test_restricted_editor_cannot_transfer_draft_in_disallowed_category(client, editor_user, admin_user):
    """Restricted editors cannot take over drafts for pages outside their allowed categories."""
    import db
    from werkzeug.security import generate_password_hash

    allowed_cat = db.create_category("Allowed Transfer")
    blocked_cat = db.create_category("Blocked Transfer")
    blocked_page_id = db.create_page(
        "Blocked Transfer Page", "blocked-transfer-page", "content", blocked_cat, admin_user
    )
    source_user_id = db.create_user("editor_transfer_src", generate_password_hash("pw"), role="editor")
    db.save_draft(blocked_page_id, source_user_id, "Transfer Draft", "secret draft")
    db.set_editor_access(editor_user, restricted=True, category_ids=[allowed_cat])

    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.post(
        "/api/draft/transfer",
        json={"page_id": blocked_page_id, "from_user_id": source_user_id},
        content_type="application/json",
    )

    assert resp.status_code == 403
    assert db.get_draft(blocked_page_id, editor_user) is None
    assert db.get_draft(blocked_page_id, source_user_id)["content"] == "secret draft"


def test_restricted_editor_my_drafts_excludes_disallowed_categories(client, editor_user, admin_user):
    """Restricted editors only see drafts for categories they can still edit."""
    import db

    allowed_cat = db.create_category("Allowed My Drafts")
    blocked_cat = db.create_category("Blocked My Drafts")
    allowed_page_id = db.create_page(
        "Allowed Draft Page", "allowed-draft-page", "content", allowed_cat, admin_user
    )
    blocked_page_id = db.create_page(
        "Blocked Draft Page", "blocked-my-draft-page", "content", blocked_cat, admin_user
    )
    db.set_editor_access(editor_user, restricted=True, category_ids=[allowed_cat])
    db.save_draft(allowed_page_id, editor_user, "Allowed Draft", "allowed content")
    db.save_draft(blocked_page_id, editor_user, "Blocked Draft", "blocked content")

    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.get("/api/draft/mine")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["page_id"] == allowed_page_id
    assert data[0]["page_slug"] == "allowed-draft-page"
    assert data[0]["page_title"] == "Allowed Draft Page"
    assert data[0]["title"] == "Allowed Draft"
    assert data[0]["updated_at"]
    assert data[0]["updated_at_formatted"]


# ---------------------------------------------------------------------------
# API preview (success path)
# ---------------------------------------------------------------------------

def test_api_preview_returns_html(logged_in_admin):
    """POST /api/preview with markdown content returns rendered HTML."""
    resp = logged_in_admin.post("/api/preview",
                                json={"content": "# Hello\n\n**bold text**"},
                                content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "html" in data
    assert "<h1>" in data["html"] or "Hello" in data["html"]
    assert "bold" in data["html"] or "<strong>" in data["html"]


def test_api_preview_empty_content(logged_in_admin):
    """POST /api/preview with empty content returns empty HTML."""
    resp = logged_in_admin.post("/api/preview",
                                json={"content": ""},
                                content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "html" in data


def test_api_preview_requires_login(client, admin_user):
    """/api/preview should redirect unauthenticated requests."""
    resp = client.post("/api/preview",
                       json={"content": "test"},
                       content_type="application/json")
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Announcement visibility for logged-out users
# ---------------------------------------------------------------------------

def test_announcement_visible_to_logged_out_user(client, admin_user):
    """An announcement with visibility='logged_out' is shown to non-logged-in users."""
    import db
    ann_id = db.create_announcement("Public message", "blue", "normal",
                                    "logged_out", None, admin_user)
    resp = client.get(f"/announcements/{ann_id}")
    assert resp.status_code == 200
    assert b"Public message" in resp.data


def test_announcement_logged_in_only_hidden_from_guests(client, admin_user):
    """Announcement for logged-in users returns 404 for guests."""
    import db
    ann_id = db.create_announcement("Members only", "orange", "normal",
                                    "logged_in", None, admin_user)
    resp = client.get(f"/announcements/{ann_id}")
    assert resp.status_code == 404


def test_announcement_logged_out_only_hidden_from_logged_in(logged_in_admin, admin_user):
    """Announcement for logged-out users returns 404 for logged-in users."""
    import db
    ann_id = db.create_announcement("Guests only", "red", "normal",
                                    "logged_out", None, admin_user)
    resp = logged_in_admin.get(f"/announcements/{ann_id}")
    assert resp.status_code == 404


def test_inactive_announcement_returns_404(client, admin_user):
    """An inactive announcement should return 404."""
    import db
    ann_id = db.create_announcement("Inactive", "blue", "normal", "both", None, admin_user)
    db.update_announcement(ann_id, is_active=0)
    resp = client.get(f"/announcements/{ann_id}")
    assert resp.status_code == 404


def test_nonexistent_announcement_returns_404(client, admin_user):
    """A non-existent announcement ID returns 404."""
    resp = client.get("/announcements/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DB helpers: list_categories, get_category_tree, get_page, transfer_draft
# ---------------------------------------------------------------------------

def test_list_categories_empty():
    """list_categories() returns an empty list when no categories exist."""
    import db
    cats = db.list_categories()
    assert isinstance(cats, list)
    assert len(cats) == 0


def test_list_categories_returns_created():
    """list_categories() returns all created categories."""
    import db
    db.create_category("CatA")
    db.create_category("CatB")
    cats = db.list_categories()
    names = [c["name"] for c in cats]
    assert "CatA" in names
    assert "CatB" in names


def test_get_category_tree_empty():
    """get_category_tree() returns ([], []) when no categories and no pages."""
    import db
    cats, uncategorized = db.get_category_tree()
    assert isinstance(cats, list)
    assert isinstance(uncategorized, list)


def test_get_category_tree_with_categories():
    """get_category_tree() includes created categories."""
    import db
    db.create_category("TreeCat")
    cats, uncategorized = db.get_category_tree()
    assert any(c["name"] == "TreeCat" for c in cats)


def test_get_category_tree_excludes_home_page():
    """The sidebar category tree does not repeat the pinned home page."""
    import db
    cats, uncategorized = db.get_category_tree()
    assert all(not page["is_home"] for page in uncategorized)
    assert all(
        not page["is_home"]
        for category in cats
        for page in category.get("pages", [])
    )


def test_get_page_by_id():
    """get_page() retrieves a page by its integer ID."""
    import db
    page_id = db.create_page("By ID", "by-id", "content", None, None)
    page = db.get_page(page_id)
    assert page is not None
    assert page["title"] == "By ID"
    assert page["slug"] == "by-id"


def test_get_page_nonexistent_returns_none():
    """get_page() returns None for an ID that does not exist."""
    import db
    assert db.get_page(99999) is None


def test_transfer_draft_db_helper():
    """transfer_draft() moves a draft between users in the DB."""
    import db
    from werkzeug.security import generate_password_hash
    uid_a = db.create_user("usera", generate_password_hash("pw"), role="editor")
    uid_b = db.create_user("userb", generate_password_hash("pw"), role="editor")
    home = db.get_home_page()
    db.save_draft(home["id"], uid_a, "Draft from A", "content from A")
    db.transfer_draft(home["id"], uid_a, uid_b)
    assert db.get_draft(home["id"], uid_a) is None
    draft_b = db.get_draft(home["id"], uid_b)
    assert draft_b is not None
    assert draft_b["content"] == "content from A"


# ---------------------------------------------------------------------------
# Account password change
# ---------------------------------------------------------------------------

def test_account_change_password_success(client, admin_user):
    """User can change their own password with correct current password."""
    import db
    from werkzeug.security import generate_password_hash, check_password_hash
    db.create_user("pwchanger", generate_password_hash("oldpass"), role="user")
    client.post("/login", data={"username": "pwchanger", "password": "oldpass"})
    resp = client.post("/settings", data={
        "action": "change_password",
        "current_password": "oldpass",
        "new_password": "newpass123",
        "confirm_password": "newpass123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Password updated" in resp.data
    # Verify new password actually works
    user = db.get_user_by_username("pwchanger")
    assert check_password_hash(user["password"], "newpass123")


def test_account_change_password_wrong_current(client, admin_user):
    """Wrong current password should be rejected on account settings."""
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("pwwrong", generate_password_hash("oldpass"), role="user")
    client.post("/login", data={"username": "pwwrong", "password": "oldpass"})
    resp = client.post("/settings", data={
        "action": "change_password",
        "current_password": "notthepassword",
        "new_password": "newpass123",
        "confirm_password": "newpass123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Incorrect" in resp.data


def test_account_change_password_too_short(client, admin_user):
    """New password shorter than 8 characters should be rejected."""
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("pwshort", generate_password_hash("oldpass"), role="user")
    client.post("/login", data={"username": "pwshort", "password": "oldpass"})
    resp = client.post("/settings", data={
        "action": "change_password",
        "current_password": "oldpass",
        "new_password": "abc",
        "confirm_password": "abc",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"8 characters" in resp.data


# ---------------------------------------------------------------------------
# Additional DB helper: count_pages_in_category
# ---------------------------------------------------------------------------

def test_count_pages_in_category():
    """count_pages_in_category() returns correct page count."""
    import db
    cat_id = db.create_category("CountCat")
    assert db.count_pages_in_category(cat_id) == 0
    db.create_page("P1", "count-p1", "c", cat_id, None)
    db.create_page("P2", "count-p2", "c", cat_id, None)
    assert db.count_pages_in_category(cat_id) == 2


# ---------------------------------------------------------------------------
# Admin user edit: unknown action is silently ignored (no crash)
# ---------------------------------------------------------------------------

def test_admin_edit_user_unknown_action(logged_in_admin, admin_user):
    """POSTing an unknown action to admin edit should redirect without error."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("ignoreuser", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(f"/admin/users/{uid}/edit",
                                data={"action": "nonexistent_action"},
                                follow_redirects=True)
    assert resp.status_code == 200
    # Should not crash; the user should still exist
    assert db.get_user_by_id(uid) is not None


# ---------------------------------------------------------------------------
# Admin edit user: nonexistent user returns 404
# ---------------------------------------------------------------------------

def test_admin_edit_nonexistent_user_returns_404(logged_in_admin):
    """POSTing to /admin/users/<nonexistent>/edit returns 404."""
    resp = logged_in_admin.post("/admin/users/99999/edit",
                                data={"action": "suspend"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Render markdown helper sanity check
# ---------------------------------------------------------------------------

def test_render_markdown_via_preview(logged_in_admin):
    """Tables, code blocks, and links are rendered correctly."""
    resp = logged_in_admin.post("/api/preview",
                                json={"content": "| A | B |\n|---|---|\n| 1 | 2 |"},
                                content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    html = data["html"]
    assert "<table" in html or "<td" in html


# ---------------------------------------------------------------------------
# Invite code validation DB helper
# ---------------------------------------------------------------------------

def test_validate_invite_code_valid(admin_user):
    """validate_invite_code() returns the code row for a valid, unused code."""
    import db
    code = db.generate_invite_code(admin_user)
    row = db.validate_invite_code(code)
    assert row is not None
    assert row["code"] == code


def test_validate_invite_code_invalid():
    """validate_invite_code() returns None for a non-existent code."""
    import db
    assert db.validate_invite_code("XXXX-XXXX") is None


def test_validate_invite_code_used(admin_user):
    """validate_invite_code() returns None for an already-used code."""
    import db
    from werkzeug.security import generate_password_hash
    code = db.generate_invite_code(admin_user)
    uid = db.create_user("codeuser", generate_password_hash("pw"))
    db.use_invite_code(code, uid)
    assert db.validate_invite_code(code) is None


def test_validate_invite_code_rejects_suspended_creator(admin_user):
    """validate_invite_code() rejects codes created by suspended admins."""
    import db
    code = db.generate_invite_code(admin_user)
    db.update_user(admin_user, suspended=1)
    assert db.validate_invite_code(code) is None


def test_validate_invite_code_rejects_deleted_creator(admin_user):
    """validate_invite_code() rejects codes whose creator account was deleted."""
    import db
    code = db.generate_invite_code(admin_user)
    db.delete_user(admin_user)
    assert db.validate_invite_code(code) is None


def test_signup_rejects_invite_from_suspended_admin(client, admin_user):
    """Signup rejects legacy invite codes whose creator is now suspended."""
    import db
    code = db.generate_invite_code(admin_user)
    db.update_user(admin_user, suspended=1)

    resp = client.post("/signup", data={
        "username": "newuser",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"invalid or has expired" in resp.data
    assert db.get_user_by_username("newuser") is None


def test_signup_rejects_invite_from_deleted_admin(client, admin_user):
    """Signup rejects legacy invite codes whose creator account was deleted."""
    import db
    code = db.generate_invite_code(admin_user)
    db.delete_user(admin_user)

    resp = client.post("/signup", data={
        "username": "newuser",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": code,
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"invalid or has expired" in resp.data
    assert db.get_user_by_username("newuser") is None


# ---------------------------------------------------------------------------
# count_admins DB helper
# ---------------------------------------------------------------------------

def test_count_admins_with_one_admin(admin_user):
    """count_admins() returns 1 when only one admin exists."""
    import db
    assert db.count_admins() == 1


def test_count_admins_with_multiple(admin_user):
    """count_admins() reflects newly created admin accounts."""
    import db
    from werkzeug.security import generate_password_hash
    db.create_user("admin2", generate_password_hash("pw"), role="admin")
    assert db.count_admins() == 2


# ---------------------------------------------------------------------------
# User data export
# ---------------------------------------------------------------------------

def test_export_own_data_returns_zip(client, admin_user):
    """GET /settings/export returns a ZIP file for the logged-in user."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/settings/export")
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    assert b"PK" == resp.data[:2]  # ZIP magic bytes


def test_export_own_data_zip_contains_expected_files(client, admin_user):
    """The exported ZIP for own data contains all expected JSON files."""
    import io
    import zipfile
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/settings/export")
    assert resp.status_code == 200
    buf = io.BytesIO(resp.data)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
    assert "account.json" in names
    assert "contributions.json" in names
    assert "drafts.json" in names
    assert "username_history.json" in names
    assert "accessibility.json" in names


def test_export_own_data_zip_contains_expanded_activity_files(client, admin_user):
    """The profile export includes the richer profile/activity bundle."""
    import io
    import zipfile
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/settings/export")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "summary.json" in names
    assert "profile/profile.json" in names
    assert "profile/profile_fields.json" in names
    assert "contributions/page_edits.json" in names
    assert "contributions/pending_contributions.json" in names
    assert "contributions/contribution_quota_requests.json" in names
    assert "reservations.json" in names
    assert "activity.json" in names
    assert "assessments.json" in names
    assert "canvas/index.json" in names
    assert "kanban/index.json" in names
    assert "uploads/page_attachments.json" in names


def test_export_includes_user_created_canvas_and_kanban_artifacts(client, admin_user):
    """Mind maps/canvas layouts and Kanban boards are exported as downloadable JSON files."""
    import io
    import json as json_mod
    import zipfile
    import db

    db.canvas_create_layout(
        "Export Map",
        creator_id=admin_user,
        description="A map to export",
        data={"nodes": [{"id": "n1", "type": "text", "text": "Hello"}], "edges": []},
    )
    board_id = db.kanban_create_board("Export Board", "A board to export", admin_user)
    column_id = db.kanban_create_column(board_id, "Todo", 0)
    db.kanban_create_ticket(column_id, "Ship export", "Make the ZIP richer", admin_user)

    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/settings/export")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
        canvas_index = json_mod.loads(zf.read("canvas/index.json"))
        kanban_index = json_mod.loads(zf.read("kanban/index.json"))

    assert any(name.endswith("export-map.canvas.json") for name in names)
    assert any(name.endswith("export-board.kanban.json") for name in names)
    assert any(layout["title"] == "Export Map" for layout in canvas_index["layouts"])
    assert any(board["title"] == "Export Board" for board in kanban_index["boards"])


def test_export_own_data_account_json_no_password(client, admin_user):
    """The account.json in the export must not contain the password hash."""
    import io
    import json as json_mod
    import zipfile
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/settings/export")
    assert resp.status_code == 200
    buf = io.BytesIO(resp.data)
    with zipfile.ZipFile(buf) as zf:
        account = json_mod.loads(zf.read("account.json"))
    assert "password" not in account
    assert account["username"] == "admin"


def test_export_own_data_requires_login(client, admin_user):
    """GET /settings/export redirects unauthenticated users to login."""
    resp = client.get("/settings/export")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_admin_export_user_data(logged_in_admin, admin_user):
    """Admin can download any user's data as a ZIP."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("targetuser", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.get(f"/admin/users/{uid}/export")
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    assert b"PK" == resp.data[:2]


def test_admin_export_user_data_contains_account_json(logged_in_admin, admin_user):
    """Admin export ZIP contains account.json with the correct username."""
    import io
    import json as json_mod
    import zipfile
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("exportme", generate_password_hash("pw"), role="editor")
    resp = logged_in_admin.get(f"/admin/users/{uid}/export")
    assert resp.status_code == 200
    buf = io.BytesIO(resp.data)
    with zipfile.ZipFile(buf) as zf:
        account = json_mod.loads(zf.read("account.json"))
    assert account["username"] == "exportme"
    assert "password" not in account


def test_admin_export_nonexistent_user_returns_404(logged_in_admin):
    """Admin export for a non-existent user_id returns 404."""
    resp = logged_in_admin.get("/admin/users/nonexistent_id/export")
    assert resp.status_code == 404


def test_admin_export_requires_admin(client, admin_user):
    """Non-admin users cannot access the admin export route."""
    import db
    from werkzeug.security import generate_password_hash
    uid = db.create_user("regularuser", generate_password_hash("pw"), role="user")
    client.post("/login", data={"username": "regularuser", "password": "pw"})
    resp = client.get(f"/admin/users/{uid}/export")
    assert resp.status_code in (302, 403)


def test_export_includes_contributions(client, admin_user):
    """The contributions.json in the export contains page edits by the user."""
    import io
    import json as json_mod
    import zipfile
    import db
    # Create a page and have admin edit it (which creates a history entry)
    page_id = db.create_page("Export Test Page", "export-test", "v1", None, admin_user)
    db.update_page(page_id, "Export Test Page", "v2 content", admin_user, "update")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/settings/export")
    assert resp.status_code == 200
    buf = io.BytesIO(resp.data)
    with zipfile.ZipFile(buf) as zf:
        contributions = json_mod.loads(zf.read("contributions.json"))
    assert len(contributions) >= 1
    assert any(c["page_title"] == "Export Test Page" for c in contributions)


def test_profile_page_shows_user_data_export_links(client, admin_user):
    """Own and admin profile pages expose the user data export action."""
    import db
    from werkzeug.security import generate_password_hash

    uid = db.create_user("profileexport", generate_password_hash("pw"), role="user")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    own_resp = client.get("/users/admin")
    assert own_resp.status_code == 200
    assert b"/settings/export" in own_resp.data

    admin_resp = client.get("/users/profileexport")
    assert admin_resp.status_code == 200
    assert f"/admin/users/{uid}/export".encode() in admin_resp.data


# ---------------------------------------------------------------------------
# admin_hard_delete_code: permanently remove an expired/used invite code
# ---------------------------------------------------------------------------

def test_admin_hard_delete_expired_code(logged_in_admin, admin_user):
    """POST /admin/codes/expired/<id>/delete permanently removes an expired code."""
    import db
    # Generate and soft-delete an invite code so it appears in the expired list
    db.generate_invite_code(admin_user)
    codes = db.list_invite_codes()
    code_id = codes[0]["id"]
    db.delete_invite_code(code_id)   # soft-delete → appears in expired list

    expired_before = db.list_expired_codes()
    assert any(c["id"] == code_id for c in expired_before)

    resp = logged_in_admin.post(
        f"/admin/codes/expired/{code_id}/delete",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"permanently removed" in resp.data

    expired_after = db.list_expired_codes()
    assert all(c["id"] != code_id for c in expired_after)


def test_admin_hard_delete_code_requires_admin(client, admin_user):
    """A non-admin user cannot hard-delete an expired invite code."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("regularuser2", generate_password_hash("pw"), role="user")
    client.post("/login", data={"username": "regularuser2", "password": "pw"})

    db.generate_invite_code(admin_user)
    codes = db.list_invite_codes()
    code_id = codes[0]["id"]
    db.delete_invite_code(code_id)

    resp = client.post(
        f"/admin/codes/expired/{code_id}/delete",
        follow_redirects=True,
    )
    assert b"Admin access required" in resp.data


# ---------------------------------------------------------------------------
# 403 error handler
# ---------------------------------------------------------------------------

def test_403_error_template_exists():
    """The 403.html template must exist so the error handler can render it."""
    import os
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "templates", "wiki", "403.html"
    )
    assert os.path.isfile(template_path), "wiki/403.html template is missing"


def test_403_handler_renders_correct_template(logged_in_admin):
    """The 403 error handler renders the 403 template with status code 403."""
    from app import app
    from werkzeug.exceptions import Forbidden

    with app.test_request_context("/"):
        handler_dict = app.error_handler_spec.get(None, {}).get(403, {})
        handler = handler_dict.get(Forbidden)
        assert handler is not None, "No 403 error handler registered"
        result = handler(Forbidden())
        # Handler returns (html_string, status_code) tuple
        if isinstance(result, tuple):
            html, status_code = result
            assert status_code == 403
            assert "403" in html
        else:
            # Flask Response object
            assert result.status_code == 403
            assert b"403" in result.data


# ---------------------------------------------------------------------------
# admin_generate_code route logging and result
# ---------------------------------------------------------------------------

def test_admin_generate_code_creates_valid_code(logged_in_admin):
    """POST /admin/codes/generate creates a new invite code visible on the codes page."""
    import db
    before = len(db.list_invite_codes())

    resp = logged_in_admin.post("/admin/codes/generate", follow_redirects=True)
    assert resp.status_code == 200

    after = len(db.list_invite_codes())
    assert after == before + 1


def test_admin_generate_code_appears_on_page(logged_in_admin):
    """After generating a code, the actual generated code appears in the response HTML."""
    import db
    resp = logged_in_admin.post("/admin/codes/generate", follow_redirects=True)
    assert resp.status_code == 200

    # Get the code from the database and verify it appears in the response
    codes = db.list_invite_codes()
    assert codes, "No invite codes found after generate"
    code_str = codes[0]["code"]
    assert code_str.encode() in resp.data, f"Generated code '{code_str}' not found in response"


def test_footer_keeps_source_access_without_vendor_or_build_badges(logged_in_admin):
    """The source offer remains visible after removing old release branding."""
    import app
    response = logged_in_admin.get("/", follow_redirects=True)
    assert response.status_code == 200
    assert app.app.jinja_env.globals["source_code_url"].encode() in response.data
    assert b"source-notice" in response.data
    for obsolete in (b"Powered by", b"powered-by-bananawiki", b"bw-build-info", b"Codename", b"Compiled"):
        assert obsolete not in response.data


def test_asset_version_reveals_no_build_or_install_date(tmp_path, monkeypatch):
    """Stylesheet and script URLs carry a cache key on every page. It is a hash
    of the stylesheet, not its modification time, so it gives away neither
    when the release was built nor when it was unpacked, and copying the file
    without changing it does not bust every visitor's cache."""
    import os
    import re
    import app as app_module

    static = tmp_path / "static"
    (static / "css").mkdir(parents=True)
    css = static / "css" / "style.css"
    css.write_text("body { color: black; }")
    monkeypatch.setattr(app_module.app, "static_folder", str(static))
    monkeypatch.setattr(app_module, "_ASSET_VERSION", None)

    first = app_module._get_asset_version()
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert not re.fullmatch(r"\d{9,11}", first)

    os.utime(css, (1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(app_module, "_ASSET_VERSION", None)
    assert app_module._get_asset_version() == first

    css.write_text("body { color: navy; }")
    monkeypatch.setattr(app_module, "_ASSET_VERSION", None)
    assert app_module._get_asset_version() != first
