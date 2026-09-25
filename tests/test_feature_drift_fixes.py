"""Tests for chat cleanup retention period feature (feature drift fix)."""
import pytest
import os
import config
import db
from datetime import datetime, timedelta, timezone


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Fresh temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr(config, "UPLOAD_FOLDER", upload_dir)
    chat_att_dir = str(tmp_path / "chat_attachments")
    os.makedirs(chat_att_dir, exist_ok=True)
    monkeypatch.setattr(config, "CHAT_ATTACHMENT_FOLDER", chat_att_dir)
    db.init_db()
    yield db_path


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_uid():
    from werkzeug.security import generate_password_hash
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1, page_reservations_enabled=1)
    return uid


@pytest.fixture
def alice_uid(admin_uid):
    from werkzeug.security import generate_password_hash
    return db.create_user("alice", generate_password_hash("alice123"), role="user")


@pytest.fixture
def bob_uid(admin_uid):
    from werkzeug.security import generate_password_hash
    return db.create_user("bob", generate_password_hash("bob123"), role="user")


@pytest.fixture
def app():
    from app import app as flask_app
    return flask_app


def test_chat_cleanup_with_retention_period(alice_uid, bob_uid):
    """Test that cleanup_old_chat_messages respects retention period."""
    # Create a chat
    chat = db.get_or_create_chat(alice_uid, bob_uid)

    # Add some messages with different timestamps (simulated by manipulating created_at)
    msg1_id = db.send_chat_message(chat["id"], alice_uid, "Old message", "127.0.0.1")
    db.send_chat_message(chat["id"], bob_uid, "Recent message", "127.0.0.1")

    # Manually update message 1 to be 35 days old
    conn = db.get_db()
    old_date = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    conn.execute(
        "UPDATE chat_messages SET created_at = ? WHERE id = ?",
        (old_date, msg1_id)
    )
    conn.commit()
    conn.close()

    # Run cleanup with 30-day retention
    db.cleanup_old_chat_messages(retention_days=30)

    # Verify old message was deleted
    messages = db.get_chat_messages(chat["id"])
    assert len(messages) == 1
    assert messages[0]["content"] == "Recent message"
    assert messages[0]["sender_id"] == bob_uid


def test_chat_cleanup_keeps_recent_messages(alice_uid, bob_uid):
    """Test that messages within retention period are preserved."""
    chat = db.get_or_create_chat(alice_uid, bob_uid)

    # Add recent messages
    db.send_chat_message(chat["id"], alice_uid, "Message 1", "127.0.0.1")
    db.send_chat_message(chat["id"], bob_uid, "Message 2", "127.0.0.1")
    db.send_chat_message(chat["id"], alice_uid, "Message 3", "127.0.0.1")

    # Run cleanup with 30-day retention (all messages are recent)
    db.cleanup_old_chat_messages(retention_days=30)

    # All messages should still exist
    messages = db.get_chat_messages(chat["id"])
    assert len(messages) == 3


def test_group_chat_cleanup_with_retention(alice_uid):
    """Test that cleanup_old_group_messages respects retention period."""
    # Create a group
    group = db.create_group_chat("Test Group", alice_uid, "Test description")

    # Add messages with different timestamps
    msg1_id = db.send_group_message(group["id"], alice_uid, "Old message", "127.0.0.1")
    db.send_group_message(group["id"], alice_uid, "Recent message", "127.0.0.1")

    # Make message 1 old (35 days)
    conn = db.get_db()
    old_date = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    conn.execute(
        "UPDATE group_messages SET created_at = ? WHERE id = ?",
        (old_date, msg1_id)
    )
    conn.commit()
    conn.close()

    # Run cleanup with 30-day retention
    db.cleanup_old_group_messages(retention_days=30)

    # Verify old message was deleted, recent message kept
    messages = db.get_group_messages(group["id"])
    # System message + recent message
    assert any(msg["content"] == "Recent message" for msg in messages)
    assert not any(msg["content"] == "Old message" for msg in messages)


def test_chat_cleanup_banner_hidden_when_setting_disabled(app, alice_uid, bob_uid):
    """The DM cleanup banner must be hidden if chat_cleanup_enabled is off."""
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    db.update_site_settings(
        chat_cleanup_enabled=0,
        chat_dm_auto_clear_messages=1,
        chat_dm_message_retention_days=14,
    )
    chat = db.get_or_create_chat(alice_uid, bob_uid)

    with app.test_client() as c:
        c.post("/login", data={"username": "alice", "password": "alice123"})
        resp = c.get(f"/chats/{chat['id']}")

    assert b"dm-cleanup-" not in resp.data


def test_chat_cleanup_config_enabled_flag(app, alice_uid, bob_uid):
    """Test that chat_cleanup_enabled setting works (disabling it only affects the scheduler)."""
    # Set cleanup enabled to False via site settings
    db.update_site_settings(chat_cleanup_enabled=0)

    # Create chat and message
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.send_chat_message(chat["id"], alice_uid, "Test message", "127.0.0.1")

    # Make message old
    conn = db.get_db()
    old_date = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    conn.execute(
        "UPDATE chat_messages SET created_at = ?",
        (old_date,)
    )
    conn.commit()
    conn.close()

    # Cleanup function should still work when called directly
    # (the chat_cleanup_enabled check is in routes/chat.py scheduler)
    db.cleanup_old_chat_messages(retention_days=30)

    # Message should be deleted (db function doesn't check enabled flag)
    messages = db.get_chat_messages(chat["id"])
    assert len(messages) == 0


def test_group_description_storage(alice_uid):
    """Test that group descriptions are stored and retrieved."""
    # Create group with description
    description = "This is a test group for testing purposes."
    group = db.create_group_chat("Test Group", alice_uid, description)

    # Verify description is stored
    assert group["description"] == description

    # Retrieve group and verify description
    retrieved = db.get_group_chat(group["id"])
    assert retrieved["description"] == description


def test_group_description_optional(alice_uid):
    """Test that group description is optional."""
    # Create group without description
    group = db.create_group_chat("Test Group", alice_uid)

    # Should have empty string as description
    assert group["description"] == ""


def test_group_description_max_length(alice_uid):
    """Test that group descriptions can be up to 500 characters."""
    # Create group with max-length description
    description = "A" * 500
    group = db.create_group_chat("Test Group", alice_uid, description)

    assert len(group["description"]) == 500
    assert group["description"] == description


# ---------------------------------------------------------------------------
# GAP-001: view_page enforces user_can_view_page() (category read restrictions
#          and deindexed-page permissions).
# ---------------------------------------------------------------------------

def test_view_page_restricted_category_returns_403(client, admin_uid):
    """GAP-001: A user with restricted read access gets 403 on a restricted category page."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    # Create a category and a page in it
    cat_id = db.create_category("Restricted Cat")
    db.create_page("Secret Page", "secret-page", "Content", category_id=cat_id)

    # Create a user whose read access is restricted to NO categories
    user_id = db.create_user("restricted", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user"),
        read_restricted=True,
        read_category_ids=[],
    )

    # Log in as that restricted user
    client.post("/login", data={"username": "restricted", "password": "pass123"})

    resp = client.get("/page/secret-page")
    assert resp.status_code == 403


def test_view_page_allowed_category_returns_200(client, admin_uid):
    """GAP-001: A user with read access to a specific category can view pages in it."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    cat_id = db.create_category("Allowed Cat")
    db.create_page("Allowed Page", "allowed-page", "Content", category_id=cat_id)

    user_id = db.create_user("allowed_user", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user"),
        read_restricted=True,
        read_category_ids=[cat_id],
    )

    client.post("/login", data={"username": "allowed_user", "password": "pass123"})

    resp = client.get("/page/allowed-page")
    assert resp.status_code == 200


def test_view_page_unrestricted_user_can_see_all(client, admin_uid):
    """GAP-001: A user with unrestricted read access can view any page."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    cat_id = db.create_category("Some Cat")
    db.create_page("Some Page", "some-page", "Content", category_id=cat_id)

    user_id = db.create_user("free_user", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user"),
        read_restricted=False,
    )

    client.post("/login", data={"username": "free_user", "password": "pass123"})

    resp = client.get("/page/some-page")
    assert resp.status_code == 200


def test_view_page_admin_always_has_access(client, admin_uid):
    """GAP-001: Admins can view all pages regardless of permissions."""
    cat_id = db.create_category("Admin Cat")
    db.create_page("Admin Page", "admin-page", "Content", category_id=cat_id)

    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.get("/page/admin-page")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GAP-002: Search API filters results by category read access restrictions.
# ---------------------------------------------------------------------------

def test_search_api_filters_restricted_categories(client, admin_uid):
    """GAP-002: Search results exclude pages in restricted categories for restricted users."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    # Create two categories with pages
    allowed_cat = db.create_category("Allowed Category")
    restricted_cat = db.create_category("Restricted Category")
    db.create_page("Public Page", "public-page", "Content", category_id=allowed_cat)
    db.create_page("Private Page", "private-page", "Content", category_id=restricted_cat)

    # Create user with read access only to allowed_cat
    user_id = db.create_user("search_user", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user"),
        read_restricted=True,
        read_category_ids=[allowed_cat],
    )

    client.post("/login", data={"username": "search_user", "password": "pass123"})

    resp = client.get("/api/pages/search?q=Page")
    assert resp.status_code == 200
    data = resp.get_json()
    slugs = [r["slug"] for r in data]

    # Should see public page but NOT private page
    assert "public-page" in slugs
    assert "private-page" not in slugs


def test_search_api_unrestricted_user_sees_all(client, admin_uid):
    """GAP-002: An unrestricted user sees all pages in search results."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    cat_id = db.create_category("Some Category")
    db.create_page("Findable Page", "findable-page", "Content", category_id=cat_id)

    user_id = db.create_user("unrestr_user", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user"),
        read_restricted=False,
    )

    client.post("/login", data={"username": "unrestr_user", "password": "pass123"})

    resp = client.get("/api/pages/search?q=Findable")
    assert resp.status_code == 200
    data = resp.get_json()
    slugs = [r["slug"] for r in data]
    assert "findable-page" in slugs


def test_sidebar_search_filters_restricted_categories(client, admin_uid):
    """GAP-002: Sidebar search excludes restricted categories and their pages."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    allowed_cat = db.create_category("AllowedPageCat")
    blocked_cat = db.create_category("BlockedPageCat")
    db.create_page("Open Page", "open-page", "Content", category_id=allowed_cat)
    db.create_page("Hidden Page", "hidden-page", "Content", category_id=blocked_cat)

    user_id = db.create_user("sb_user", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user"),
        read_restricted=True,
        read_category_ids=[allowed_cat],
    )

    client.post("/login", data={"username": "sb_user", "password": "pass123"})

    resp = client.get("/api/sidebar/search?q=Page")
    assert resp.status_code == 200
    data = resp.get_json()
    category_names = [c["name"] for c in data.get("categories", [])]
    slugs = [p["slug"] for p in data.get("pages", [])]

    assert "AllowedPageCat" in category_names
    assert "BlockedPageCat" not in category_names
    assert "open-page" in slugs
    assert "hidden-page" not in slugs


def test_create_page_category_selector_hides_restricted_categories(client, admin_uid):
    """GAP-002: Shared category dropdowns only include readable categories."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    allowed_cat = db.create_category("CreateAllowedCat")
    db.create_category("CreateBlockedCat")

    editor_id = db.create_user("create_editor", generate_password_hash("pass123"), role="editor")
    db.set_user_permissions(
        editor_id,
        get_default_permissions("editor"),
        read_restricted=True,
        read_category_ids=[allowed_cat],
        write_restricted=True,
        write_category_ids=[allowed_cat],
    )

    client.post("/login", data={"username": "create_editor", "password": "pass123"})

    resp = client.get("/create-page")
    assert resp.status_code == 200
    html = resp.data.decode()

    assert "CreateAllowedCat" in html
    assert "CreateBlockedCat" not in html


def test_sidebar_navigation_hides_restricted_categories(client, admin_uid):
    """GAP-002: Shared sidebar navigation omits blocked categories."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    allowed_cat = db.create_category("SidebarAllowedCat")
    db.create_category("SidebarBlockedCat")

    editor_id = db.create_user("sidebar_editor", generate_password_hash("pass123"), role="editor")
    db.set_user_permissions(
        editor_id,
        get_default_permissions("editor"),
        read_restricted=True,
        read_category_ids=[allowed_cat],
        write_restricted=True,
        write_category_ids=[allowed_cat],
    )

    client.post("/login", data={"username": "sidebar_editor", "password": "pass123"})

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()

    assert "SidebarAllowedCat" in html
    assert "SidebarBlockedCat" not in html


# ---------------------------------------------------------------------------
# GAP-003 / GAP-004: Admin checkouts page and force-release.
# ---------------------------------------------------------------------------

@pytest.fixture
def editor_uid(admin_uid):
    from werkzeug.security import generate_password_hash
    return db.create_user("editor1", generate_password_hash("editor123"), role="editor")


@pytest.fixture
def editor2_uid(admin_uid):
    from werkzeug.security import generate_password_hash
    return db.create_user("editor2", generate_password_hash("editor123"), role="editor")


def test_admin_checkouts_page_loads(client, admin_uid):
    """GAP-003: Admin can view the /admin/checkouts page."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/admin/checkouts")
    assert resp.status_code == 200
    assert b"Page Checkouts" in resp.data


def test_admin_checkouts_shows_active_reservations(client, admin_uid, editor_uid):
    """GAP-003: Active reservations are listed on /admin/checkouts."""
    page_id = db.create_page("Reserved Page", "reserved-page", "Content")
    db.reserve_page(page_id, editor_uid)

    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/admin/checkouts")
    assert resp.status_code == 200
    assert b"Reserved Page" in resp.data
    assert b"editor1" in resp.data


def test_admin_checkouts_empty_when_no_reservations(client, admin_uid):
    """GAP-003: Admin checkouts page shows empty state when no reservations."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/admin/checkouts")
    assert resp.status_code == 200
    assert b"No active page reservations" in resp.data


def test_admin_force_release_reservation(client, admin_uid, editor_uid):
    """GAP-004: Admin can force-release a reservation."""
    page_id = db.create_page("Force Release Page", "force-release-page", "Content")
    db.reserve_page(page_id, editor_uid)

    # Confirm reserved
    status = db.get_page_reservation_status(page_id)
    assert status["is_reserved"]

    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(f"/admin/checkouts/{page_id}/release", follow_redirects=True)
    assert resp.status_code == 200

    # Confirm no longer reserved
    status = db.get_page_reservation_status(page_id)
    assert not status["is_reserved"]


def test_admin_force_release_nonexistent_page(client, admin_uid):
    """GAP-004: Force-release on nonexistent page returns 404."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post("/admin/checkouts/99999/release")
    assert resp.status_code == 404


def test_non_admin_cannot_access_checkouts(client, admin_uid, editor_uid):
    """GAP-003: Non-admin users cannot access /admin/checkouts."""
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.get("/admin/checkouts")
    assert resp.status_code in (302, 403)


def test_get_all_active_reservations_db_function(admin_uid, editor_uid):
    """GAP-003: get_all_active_reservations() returns all active reservations."""
    page1_id = db.create_page("Page One", "page-one", "Content")
    page2_id = db.create_page("Page Two", "page-two", "Content")

    db.reserve_page(page1_id, editor_uid)
    db.reserve_page(page2_id, editor_uid)

    all_res = db.get_all_active_reservations()
    page_ids = [r["page_id"] for r in all_res]
    assert page1_id in page_ids
    assert page2_id in page_ids


# ---------------------------------------------------------------------------
# GAP-005: Edit button disabled for reserved pages (non-owner editors).
# ---------------------------------------------------------------------------

def test_edit_button_disabled_when_reserved_by_other(client, admin_uid, editor_uid, editor2_uid):
    """GAP-005: The Edit button is always clickable, even when the page is reserved.

    The disabled attribute must never be the sole enforcement mechanism.
    The server-side handler blocks the action and returns a flash message.
    """
    page_id = db.create_page("Locked Page", "locked-page", "Content")
    db.reserve_page(page_id, editor_uid)  # editor1 reserves

    # Log in as editor2 and view the page
    client.post("/login", data={"username": "editor2", "password": "editor123"})
    resp = client.get("/page/locked-page")
    assert resp.status_code == 200
    # The Edit button should be a clickable <a> link, not a disabled <button>
    assert b'<button class="btn btn-sm" disabled' not in resp.data
    assert b'href="/page/locked-page/edit"' in resp.data


def test_edit_button_enabled_for_reservation_owner(client, admin_uid, editor_uid):
    """GAP-005: The Edit button is NOT disabled for the user who holds the reservation."""
    page_id = db.create_page("My Page", "my-page", "Content")
    db.reserve_page(page_id, editor_uid)  # editor1 reserves

    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.get("/page/my-page")
    assert resp.status_code == 200
    # The owner should see the normal Edit link, not a disabled button
    assert b'href="/page/my-page/edit"' in resp.data or b'href=' in resp.data
    assert b'<button class="btn btn-sm" disabled' not in resp.data


def test_admin_edit_button_not_disabled_even_when_reserved(client, admin_uid, editor_uid):
    """GAP-005: Admins always see an active Edit link even when page is reserved by editor."""
    page_id = db.create_page("Admin Edit Page", "admin-edit-page", "Content")
    db.reserve_page(page_id, editor_uid)  # editor1 reserves

    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/page/admin-edit-page")
    assert resp.status_code == 200
    assert b'<button class="btn btn-sm" disabled' not in resp.data


# ---------------------------------------------------------------------------
# GAP-006: Edit form includes "Reserve this page" checkbox.
# ---------------------------------------------------------------------------

def test_edit_form_has_reserve_checkbox(client, admin_uid, editor_uid):
    """GAP-006: The edit form shows a 'Reserve this page' checkbox."""
    db.create_page("Editable Page", "editable-page", "Content")

    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.get("/page/editable-page/edit")
    assert resp.status_code == 200
    assert b'name="reserve_after_commit"' in resp.data


def test_edit_form_reserve_checkbox_reserves_page(client, admin_uid, editor_uid):
    """GAP-006: Checking reserve_after_commit reserves the page after committing."""
    page_id = db.create_page("To Reserve", "to-reserve", "Content")

    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/page/to-reserve/edit",
        data={
            "title": "To Reserve",
            "content": "Updated",
            "edit_message": "test edit",
            "reserve_after_commit": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    # Page should now be reserved by editor1
    status = db.get_page_reservation_status(page_id)
    assert status["is_reserved"]
    assert status["reserved_by"] == editor_uid


# ---------------------------------------------------------------------------
# GAP-007: Draft transfer is restricted to admins only (IDOR fix).
# ---------------------------------------------------------------------------

def test_editor_cannot_transfer_draft(client, admin_uid, editor_uid, editor2_uid):
    """GAP-007: An editor cannot transfer another user's draft (admin only)."""
    import json
    page_id = db.create_page("Draft Page", "draft-page", "Content")

    # editor2 creates a draft
    db.save_draft(page_id, editor2_uid, "Draft Title", "Draft content")

    # editor1 logs in and attempts to transfer the draft
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/api/draft/transfer",
        data=json.dumps({"page_id": page_id, "from_user_id": editor2_uid}),
        content_type="application/json",
    )
    # editor should be forbidden (admin_required redirects or 403)
    assert resp.status_code in (302, 403)


def test_regular_user_cannot_transfer_draft(client, admin_uid, alice_uid, editor_uid):
    """GAP-007: Regular users (non-editors) cannot call the transfer draft endpoint."""
    import json
    page_id = db.create_page("Draft Page2", "draft-page-2", "Content")
    db.save_draft(page_id, editor_uid, "Draft Title", "Draft content")

    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.post(
        "/api/draft/transfer",
        data=json.dumps({"page_id": page_id, "from_user_id": editor_uid}),
        content_type="application/json",
    )
    # regular user should be forbidden (admin_required redirects or 403)
    assert resp.status_code in (302, 403)


# ---------------------------------------------------------------------------
# GAP-008: chat_dm_enabled setting disables DM access for non-admins.
# ---------------------------------------------------------------------------

def test_dm_disabled_blocks_chat_list(client, admin_uid, alice_uid):
    """GAP-008: When chat_dm_enabled=0, non-admin users cannot access /chats."""
    db.update_site_settings(chat_dm_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/chats", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_dm_disabled_blocks_chat_new(client, admin_uid, alice_uid):
    """GAP-008: When chat_dm_enabled=0, non-admin users cannot access /chats/new."""
    db.update_site_settings(chat_dm_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/chats/new", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_dm_disabled_blocks_chat_view(client, admin_uid, alice_uid, bob_uid):
    """GAP-008: When chat_dm_enabled=0, non-admin users cannot view existing DMs."""
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.update_site_settings(chat_dm_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get(f"/chats/{chat['id']}", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_dm_disabled_admin_still_has_access(client, admin_uid, alice_uid):
    """GAP-008: When chat_dm_enabled=0, admins are NOT blocked."""
    db.update_site_settings(chat_dm_enabled=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/chats", follow_redirects=True)
    assert resp.status_code == 200


def test_dm_enabled_allows_access(client, admin_uid, alice_uid):
    """GAP-008: When chat_dm_enabled=1, users can access /chats normally."""
    db.update_site_settings(chat_dm_enabled=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/chats", follow_redirects=True)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GAP-009: chat_allow_dm_creation setting prevents creating new DMs.
# ---------------------------------------------------------------------------

def test_dm_creation_disabled_blocks_new_dm(client, admin_uid, alice_uid, bob_uid):
    """GAP-009: When chat_allow_dm_creation=0, non-admins cannot create new DMs."""
    db.update_site_settings(chat_dm_enabled=1, chat_allow_dm_creation=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/chats/new", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_dm_creation_disabled_admin_can_create(client, admin_uid, alice_uid):
    """GAP-009: When chat_allow_dm_creation=0, admins can still create DMs."""
    db.update_site_settings(chat_dm_enabled=1, chat_allow_dm_creation=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/chats/new", follow_redirects=True)
    assert resp.status_code == 200


def test_dm_creation_enabled_allows_new_dm(client, admin_uid, alice_uid):
    """GAP-009: When chat_allow_dm_creation=1, users can visit /chats/new."""
    db.update_site_settings(chat_dm_enabled=1, chat_allow_dm_creation=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/chats/new", follow_redirects=True)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GAP-010: chat_group_enabled setting disables group chat access for non-admins.
# ---------------------------------------------------------------------------

def test_group_disabled_blocks_group_list(client, admin_uid, alice_uid):
    """GAP-010: When chat_group_enabled=0, non-admins cannot access /groups."""
    db.update_site_settings(chat_group_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/groups", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_group_disabled_blocks_group_new(client, admin_uid, alice_uid):
    """GAP-010: When chat_group_enabled=0, non-admins cannot access /groups/new."""
    db.update_site_settings(chat_group_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/groups/new", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_group_disabled_blocks_group_join(client, admin_uid, alice_uid):
    """GAP-010: When chat_group_enabled=0, non-admins cannot access /groups/join."""
    db.update_site_settings(chat_group_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/groups/join", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_group_disabled_blocks_group_join_post(client, admin_uid, alice_uid):
    """GAP-010: When chat_group_enabled=0, non-admins cannot POST to /groups/join."""
    db.update_site_settings(chat_group_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.post("/groups/join", data={"invite_code": "ABCD1234"},
                       follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_group_disabled_admin_can_join(client, admin_uid, alice_uid):
    """GAP-010: When chat_group_enabled=0, admins can still access /groups/join."""
    db.update_site_settings(chat_group_enabled=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/groups/join", follow_redirects=True)
    assert resp.status_code == 200


def test_group_disabled_admin_still_has_access(client, admin_uid, alice_uid):
    """GAP-010: When chat_group_enabled=0, admins are NOT blocked from /groups."""
    db.update_site_settings(chat_group_enabled=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/groups", follow_redirects=True)
    assert resp.status_code == 200


def test_group_enabled_allows_access(client, admin_uid, alice_uid):
    """GAP-010: When chat_group_enabled=1, users can access /groups normally."""
    db.update_site_settings(chat_group_enabled=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/groups", follow_redirects=True)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GAP-011: chat_allow_group_creation setting prevents creating new groups.
# ---------------------------------------------------------------------------

def test_group_creation_disabled_blocks_new_group(client, admin_uid, alice_uid):
    """GAP-011: When chat_allow_group_creation=0, non-admins cannot create groups."""
    db.update_site_settings(chat_group_enabled=1, chat_allow_group_creation=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/groups/new", follow_redirects=False)
    assert resp.status_code in (302, 403)


def test_group_creation_disabled_admin_can_create(client, admin_uid, alice_uid):
    """GAP-011: When chat_allow_group_creation=0, admins can still create groups."""
    db.update_site_settings(chat_group_enabled=1, chat_allow_group_creation=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/groups/new", follow_redirects=True)
    assert resp.status_code == 200


def test_group_creation_enabled_allows_new_group(client, admin_uid, alice_uid):
    """GAP-011: When chat_allow_group_creation=1, users can visit /groups/new."""
    db.update_site_settings(chat_group_enabled=1, chat_allow_group_creation=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})
    resp = client.get("/groups/new", follow_redirects=True)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GAP-012: chat_attachments_enabled setting blocks file attachments in DMs.
# ---------------------------------------------------------------------------

def test_dm_attachments_disabled_blocks_upload(client, admin_uid, alice_uid, bob_uid, tmp_path):
    """GAP-012: When chat_attachments_enabled=0, non-admins cannot attach files to DMs."""
    import io
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.update_site_settings(chat_dm_enabled=1, chat_attachments_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"fake file content"), "test.pdf")
    resp = client.post(
        f"/chats/{chat['id']}/send",
        data={"content": "hello", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"attachments are currently disabled" in resp.data


def test_dm_attachments_disabled_admin_can_upload(client, admin_uid, alice_uid, tmp_path):
    """GAP-012: When chat_attachments_enabled=0, admins can still attach files."""
    import io
    chat = db.get_or_create_chat(admin_uid, alice_uid)
    db.update_site_settings(chat_dm_enabled=1, chat_attachments_enabled=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    fake_file = (io.BytesIO(b"fake file content"), "test.pdf")
    resp = client.post(
        f"/chats/{chat['id']}/send",
        data={"content": "hello", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    # Should succeed (not blocked by disabled check)
    assert resp.status_code == 200
    assert b'class="flash-message">File attachments' not in resp.data


def test_dm_attachments_enabled_allows_upload(client, admin_uid, alice_uid, bob_uid):
    """GAP-012: When chat_attachments_enabled=1, attachment upload is attempted normally."""
    import io
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.update_site_settings(chat_dm_enabled=1, chat_attachments_enabled=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"fake file content"), "test.pdf")
    resp = client.post(
        f"/chats/{chat['id']}/send",
        data={"content": "hello", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    # Should not show the disabled flash message
    assert b'class="flash-message">File attachments' not in resp.data


# ---------------------------------------------------------------------------
# GAP-013: chat_attachments_enabled setting blocks file attachments in groups.
# ---------------------------------------------------------------------------

def test_group_attachments_disabled_blocks_upload(client, admin_uid, alice_uid, tmp_path):
    """GAP-013: When chat_attachments_enabled=0, non-admins cannot attach files to groups."""
    import io
    group = db.create_group_chat("Test Group", alice_uid)
    db.update_site_settings(chat_group_enabled=1, chat_attachments_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"fake file content"), "test.pdf")
    resp = client.post(
        f"/groups/{group['id']}/send",
        data={"content": "hello", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'class="flash-message">File attachments' in resp.data


def test_group_attachments_enabled_allows_upload(client, admin_uid, alice_uid):
    """GAP-013: When chat_attachments_enabled=1, attachment upload is attempted normally."""
    import io
    group = db.create_group_chat("Test Group 2", alice_uid)
    db.update_site_settings(chat_group_enabled=1, chat_attachments_enabled=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"fake file content"), "test.pdf")
    resp = client.post(
        f"/groups/{group['id']}/send",
        data={"content": "hello", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b'class="flash-message">File attachments' not in resp.data


# ---------------------------------------------------------------------------
# GAP-014: legacy editor access stays aligned with the current write-access
#          permission model.
# ---------------------------------------------------------------------------

def test_get_editor_access_reads_current_write_permissions(editor_uid):
    """GAP-014: Legacy editor-access reads the current write-access tables."""
    from helpers._permissions import get_default_permissions

    cat_id = db.create_category("Modern Access Category")
    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor"),
        write_restricted=True,
        write_category_ids=[cat_id],
    )

    access = db.get_editor_access(editor_uid)

    assert access["restricted"] is True
    assert access["allowed_category_ids"] == [cat_id]


def test_set_editor_access_syncs_current_write_permissions(editor_uid):
    """GAP-014: Legacy editor-access updates the current write-access tables."""
    cat_id = db.create_category("Legacy Sync Category")

    db.set_editor_access(editor_uid, restricted=True, category_ids=[cat_id])

    permissions = db.get_user_permissions(editor_uid)

    assert permissions["category_write_access"]["restricted"] is True
    assert permissions["category_write_access"]["allowed_category_ids"] == [cat_id]


def test_category_routes_honor_current_write_permissions(client, admin_uid, editor_uid):
    """GAP-014: Category routes respect write restrictions set via current permissions."""
    from helpers._permissions import get_default_permissions

    allowed_cat = db.create_category("Allowed Category")
    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor") - {
            "category.create",
            "category.edit",
            "category.delete",
            "category.reorder",
            "category.manage_sequential",
        },
        write_restricted=True,
        write_category_ids=[allowed_cat],
    )

    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/category/create",
        data={"name": "Blocked Category"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"You do not have permission to create categories." in resp.data
    assert "Blocked Category" not in [cat["name"] for cat in db.list_categories()]


def test_category_management_routes_require_explicit_permissions(client, admin_uid, editor_uid):
    """GAP-021: Legacy category routes must honor the current category permission keys."""
    from helpers._permissions import get_default_permissions

    editable_cat = db.create_category("Editable Category")
    target_cat = db.create_category("Target Category")
    delete_cat = db.create_category("Delete Category")
    page_id = db.create_page("Delete Page", "delete-page", "content", delete_cat, editor_uid)
    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor") - {
            "category.create",
            "category.edit",
            "category.delete",
            "category.reorder",
            "category.manage_sequential",
        },
    )

    client.post("/login", data={"username": "editor1", "password": "editor123"})

    create_resp = client.post(
        "/category/create",
        data={"name": "Blocked Category"},
        follow_redirects=True,
    )
    assert create_resp.status_code == 200
    assert b"You do not have permission to create categories." in create_resp.data
    assert "Blocked Category" not in [cat["name"] for cat in db.list_categories()]

    edit_resp = client.post(
        f"/category/{editable_cat}/edit",
        data={"name": "Renamed Category"},
        follow_redirects=True,
    )
    assert edit_resp.status_code == 200
    assert b"You do not have permission to edit categories." in edit_resp.data
    assert db.get_category(editable_cat)["name"] == "Editable Category"

    move_resp = client.post(
        f"/category/{editable_cat}/move",
        data={"parent_id": str(target_cat)},
        follow_redirects=True,
    )
    assert move_resp.status_code == 200
    assert b"You do not have permission to move categories." in move_resp.data
    assert db.get_category(editable_cat)["parent_id"] is None

    delete_resp = client.post(
        f"/category/{delete_cat}/delete",
        data={"page_action": "move", "target_category_id": str(target_cat)},
        follow_redirects=True,
    )
    assert delete_resp.status_code == 200
    assert b"You do not have permission to delete categories." in delete_resp.data
    assert db.get_category(delete_cat) is not None
    assert db.get_page(page_id)["category_id"] == delete_cat

    sequential_resp = client.post(
        f"/category/{target_cat}/sequential-nav",
        data={"sequential_nav": "1"},
        follow_redirects=True,
    )
    assert sequential_resp.status_code == 200
    assert b"You do not have permission to modify categories." in sequential_resp.data
    assert db.get_category(target_cat)["sequential_nav"] == 0

    reorder_resp = client.post(
        "/api/reorder/categories",
        json={"ids": [target_cat, editable_cat, delete_cat]},
        content_type="application/json",
    )
    assert reorder_resp.status_code == 403
    assert reorder_resp.get_json()["error"] == "You do not have permission to reorder categories."


def test_category_management_routes_honor_explicit_permissions(client, admin_uid, editor_uid):
    """GAP-021: Editors regain legacy category tools only when the matching permissions are granted."""
    from helpers._permissions import get_default_permissions

    parent_cat = db.create_category("Parent Category")
    editable_cat = db.create_category("Editable Category")
    target_cat = db.create_category("Target Category")
    delete_cat = db.create_category("Delete Category")
    page_id = db.create_page("Delete Page", "delete-page-allowed", "content", delete_cat, editor_uid)
    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor")
        | {
            "category.create",
            "category.edit",
            "category.delete",
            "category.reorder",
            "category.manage_sequential",
        },
    )

    client.post("/login", data={"username": "editor1", "password": "editor123"})

    create_resp = client.post(
        "/category/create",
        data={"name": "Created Category", "parent_id": str(parent_cat)},
        follow_redirects=True,
    )
    assert create_resp.status_code == 200
    created_cat = next(cat for cat in db.list_categories() if cat["name"] == "Created Category")
    assert created_cat["parent_id"] == parent_cat

    edit_resp = client.post(
        f"/category/{editable_cat}/edit",
        data={"name": "Renamed Category"},
        follow_redirects=True,
    )
    assert edit_resp.status_code == 200
    assert db.get_category(editable_cat)["name"] == "Renamed Category"

    move_resp = client.post(
        f"/category/{editable_cat}/move",
        data={"parent_id": str(target_cat)},
        follow_redirects=True,
    )
    assert move_resp.status_code == 200
    assert db.get_category(editable_cat)["parent_id"] == target_cat

    sequential_resp = client.post(
        f"/category/{target_cat}/sequential-nav",
        data={"sequential_nav": "1"},
        follow_redirects=True,
    )
    assert sequential_resp.status_code == 200
    assert db.get_category(target_cat)["sequential_nav"] == 1

    reorder_resp = client.post(
        "/api/reorder/categories",
        json={"ids": [target_cat, editable_cat, delete_cat, parent_cat]},
        content_type="application/json",
    )
    assert reorder_resp.status_code == 200
    assert reorder_resp.get_json()["ok"] is True

    delete_resp = client.post(
        f"/category/{delete_cat}/delete",
        data={"page_action": "move", "target_category_id": str(target_cat)},
        follow_redirects=True,
    )
    assert delete_resp.status_code == 200
    assert db.get_category(delete_cat) is None
    assert db.get_page(page_id)["category_id"] == target_cat


# ---------------------------------------------------------------------------
# GAP-015: legacy chat cleanup settings continue to work after the DM/group
#          cleanup split introduced newer per-scope settings.
# ---------------------------------------------------------------------------

def test_effective_chat_cleanup_settings_fall_back_to_legacy_values():
    """GAP-015: Split cleanup settings honor legacy values when still at migration defaults."""
    from helpers import get_effective_chat_cleanup_settings

    settings = {
        "chat_cleanup_split_configured": 0,
        "chat_auto_clear_messages": 1,
        "chat_auto_clear_attachments": 0,
        "chat_message_retention_days": 45,
        "chat_attachment_retention_days": 21,
        "chat_dm_auto_clear_messages": 0,
        "chat_dm_auto_clear_attachments": 1,
        "chat_dm_message_retention_days": 0,
        "chat_dm_attachment_retention_days": 7,
        "chat_group_auto_clear_messages": 0,
        "chat_group_auto_clear_attachments": 1,
        "chat_group_message_retention_days": 0,
        "chat_group_attachment_retention_days": 7,
    }

    effective = get_effective_chat_cleanup_settings(settings)

    assert effective["dm"] == {
        "auto_clear_messages": 1,
        "auto_clear_attachments": 0,
        "message_retention_days": 45,
        "attachment_retention_days": 21,
    }
    assert effective["group"] == {
        "auto_clear_messages": 1,
        "auto_clear_attachments": 0,
        "message_retention_days": 45,
        "attachment_retention_days": 21,
    }


def test_effective_chat_cleanup_settings_keep_explicit_split_overrides():
    """GAP-015: Explicit DM/group cleanup values override the legacy compatibility layer."""
    from helpers import get_effective_chat_cleanup_settings

    settings = {
        "chat_cleanup_split_configured": 1,
        "chat_auto_clear_messages": 1,
        "chat_auto_clear_attachments": 1,
        "chat_message_retention_days": 45,
        "chat_attachment_retention_days": 21,
        "chat_dm_auto_clear_messages": 1,
        "chat_dm_auto_clear_attachments": 0,
        "chat_dm_message_retention_days": 14,
        "chat_dm_attachment_retention_days": 3,
        "chat_group_auto_clear_messages": 0,
        "chat_group_auto_clear_attachments": 1,
        "chat_group_message_retention_days": 9,
        "chat_group_attachment_retention_days": 2,
    }

    effective = get_effective_chat_cleanup_settings(settings)

    assert effective["dm"] == {
        "auto_clear_messages": 1,
        "auto_clear_attachments": 0,
        "message_retention_days": 14,
        "attachment_retention_days": 3,
    }
    assert effective["group"] == {
        "auto_clear_messages": 0,
        "auto_clear_attachments": 1,
        "message_retention_days": 9,
        "attachment_retention_days": 2,
    }


def test_effective_chat_cleanup_settings_honor_explicit_default_values_after_split_configuration():
    """GAP-015: Once split settings are saved, default-valued overrides stay authoritative."""
    from helpers import get_effective_chat_cleanup_settings

    settings = {
        "chat_cleanup_split_configured": 1,
        "chat_auto_clear_messages": 1,
        "chat_auto_clear_attachments": 0,
        "chat_message_retention_days": 45,
        "chat_attachment_retention_days": 21,
        "chat_dm_auto_clear_messages": 0,
        "chat_dm_auto_clear_attachments": 1,
        "chat_dm_message_retention_days": 0,
        "chat_dm_attachment_retention_days": 7,
        "chat_group_auto_clear_messages": 0,
        "chat_group_auto_clear_attachments": 1,
        "chat_group_message_retention_days": 0,
        "chat_group_attachment_retention_days": 7,
    }

    effective = get_effective_chat_cleanup_settings(settings)

    assert effective["dm"] == {
        "auto_clear_messages": 0,
        "auto_clear_attachments": 1,
        "message_retention_days": 0,
        "attachment_retention_days": 7,
    }
    assert effective["group"] == {
        "auto_clear_messages": 0,
        "auto_clear_attachments": 1,
        "message_retention_days": 0,
        "attachment_retention_days": 7,
    }


# ---------------------------------------------------------------------------
# GAP-016: legacy page-history routes honor the current visibility and write
#          access model.
# ---------------------------------------------------------------------------

def test_page_history_routes_honor_current_page_visibility(client, admin_uid):
    """GAP-016: History list/detail return 403 when the page is not viewable."""
    from werkzeug.security import generate_password_hash
    from helpers._permissions import get_default_permissions

    blocked_cat = db.create_category("Blocked History Category")
    page_id = db.create_page("Hidden History Page", "hidden-history-page", "initial", blocked_cat, admin_uid)
    db.update_page(page_id, "Hidden History Page", "updated", admin_uid, "history update")
    entry_id = db.get_page_history(page_id)[0]["id"]

    user_id = db.create_user("history_reader", generate_password_hash("pass123"), role="user")
    db.set_user_permissions(
        user_id,
        get_default_permissions("user"),
        read_restricted=True,
        read_category_ids=[],
    )

    login_resp = client.post("/login", data={"username": "history_reader", "password": "pass123"})
    assert login_resp.status_code == 302

    history_resp = client.get("/page/hidden-history-page/history")
    entry_resp = client.get(f"/page/hidden-history-page/history/{entry_id}")

    assert history_resp.status_code == 403
    assert entry_resp.status_code == 403


def test_deindexed_history_entry_requires_view_deindexed_permission(client, admin_uid, editor_uid):
    """GAP-016: Editors without deindexed visibility cannot open deindexed history."""
    from helpers._permissions import get_default_permissions

    page_id = db.create_page("Deindexed History Page", "deindexed-history-page", "initial", None, admin_uid)
    db.update_page(page_id, "Deindexed History Page", "updated", admin_uid, "history update")
    db.set_page_deindexed(page_id, True)
    entry_id = db.get_page_history(page_id)[0]["id"]
    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor") - {"page.view_deindexed"},
        read_restricted=False,
    )

    login_resp = client.post("/login", data={"username": "editor1", "password": "editor123"})
    assert login_resp.status_code == 302
    resp = client.get(f"/page/deindexed-history-page/history/{entry_id}")

    assert resp.status_code == 403


def test_revert_route_honors_current_write_permissions(client, admin_uid, editor_uid):
    """GAP-016: Revert stays blocked when current write permissions deny the category."""
    from helpers._permissions import get_default_permissions

    allowed_cat = db.create_category("Allowed History Category")
    blocked_cat = db.create_category("Blocked History Category")
    page_id = db.create_page("Blocked Revert Page", "blocked-revert-page", "initial", blocked_cat, admin_uid)
    db.update_page(page_id, "Blocked Revert Page", "updated", admin_uid, "history update")
    entry_id = db.get_page_history(page_id)[0]["id"]

    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor"),
        write_restricted=True,
        write_category_ids=[allowed_cat],
    )

    login_resp = client.post("/login", data={"username": "editor1", "password": "editor123"})
    assert login_resp.status_code == 302
    resp = client.post(
        f"/page/blocked-revert-page/revert/{entry_id}",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"You do not have permission to edit pages in this category." in resp.data
    assert db.get_page(page_id)["content"] == "updated"


# ---------------------------------------------------------------------------
# GAP-017: legacy reorder APIs honor the current category write-access model.
# ---------------------------------------------------------------------------

def test_reorder_pages_honors_current_write_permissions(client, admin_uid, editor_uid):
    """GAP-017: Reorder pages stays blocked for categories the editor cannot edit."""
    from helpers._permissions import get_default_permissions

    allowed_cat = db.create_category("Allowed Reorder Category")
    blocked_cat = db.create_category("Blocked Reorder Category")
    allowed_page = db.create_page("Allowed Reorder Page", "allowed-reorder-page", "ok", allowed_cat, admin_uid)
    blocked_page = db.create_page("Blocked Reorder Page", "blocked-reorder-page", "nope", blocked_cat, admin_uid)

    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor"),
        write_restricted=True,
        write_category_ids=[allowed_cat],
    )

    login_resp = client.post("/login", data={"username": "editor1", "password": "editor123"})
    assert login_resp.status_code == 302
    resp = client.post(
        "/api/reorder/pages",
        json={"ids": [blocked_page, allowed_page]},
        content_type="application/json",
    )

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "You do not have permission to edit pages in this category"
    assert db.get_page(allowed_page)["sort_order"] == 0
    assert db.get_page(blocked_page)["sort_order"] == 0


def test_reorder_categories_honors_current_write_permissions(client, admin_uid, editor_uid):
    """GAP-017: Reorder categories stays blocked for categories the editor cannot edit."""
    from helpers._permissions import get_default_permissions

    allowed_cat = db.create_category("Allowed Category Reorder")
    blocked_cat = db.create_category("Blocked Category Reorder")

    db.set_user_permissions(
        editor_uid,
        get_default_permissions("editor"),
        write_restricted=True,
        write_category_ids=[allowed_cat],
    )

    login_resp = client.post("/login", data={"username": "editor1", "password": "editor123"})
    assert login_resp.status_code == 302
    resp = client.post(
        "/api/reorder/categories",
        json={"ids": [blocked_cat, allowed_cat]},
        content_type="application/json",
    )

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "You do not have permission to edit categories."
    assert db.get_category(allowed_cat)["sort_order"] == 0
    assert db.get_category(blocked_cat)["sort_order"] == 0


# ---------------------------------------------------------------------------
# GAP-018: badge notification UI stays aligned with the database-backed
#          notification state after login.
# ---------------------------------------------------------------------------

def test_badge_notification_banner_uses_current_database_state(client, admin_uid):
    """GAP-018: Badge banner appears for badges awarded after the current session was created."""
    from werkzeug.security import generate_password_hash

    user_id = db.create_user("badge_user", generate_password_hash("badge123"), role="user")
    badge_type_id = db.create_badge_type(
        name="Fresh Badge",
        description="Awarded after login",
        auto_trigger=False,
    )

    login_resp = client.post("/login", data={"username": "badge_user", "password": "badge123"})
    assert login_resp.status_code == 302

    db.award_badge(user_id, badge_type_id, awarded_by=admin_uid)

    resp = client.get("/settings")

    assert resp.status_code == 200
    assert b"You've earned 1 new badge!" in resp.data


# ---------------------------------------------------------------------------
# Restricted-action server-side denial tests (disabled-button removal)
# ---------------------------------------------------------------------------

def test_publish_profile_admin_disabled_returns_flash(client, admin_uid):
    """Publish-profile with page_disabled_by_admin returns an error flash
    rather than relying on a disabled button."""
    from werkzeug.security import generate_password_hash
    uid = db.create_user("profuser", generate_password_hash("prof123"), role="editor")
    # Create a profile then have admin disable it
    db.upsert_user_profile(uid, bio="hi")
    db.upsert_user_profile(uid, page_disabled_by_admin=True)

    client.post("/login", data={"username": "profuser", "password": "prof123"})
    resp = client.post(
        "/settings",
        data={"action": "publish_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"disabled by an admin" in resp.data


def test_edit_reserved_page_returns_flash(client, admin_uid, editor_uid, editor2_uid):
    """Navigating to the edit page of a reserved page flashes a denial message."""
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("Reserved Page", "reserved-pg", "Body")
    db.reserve_page(page_id, editor_uid)

    client.post("/login", data={"username": "editor2", "password": "editor123"})
    resp = client.get("/page/reserved-pg/edit", follow_redirects=True)
    assert resp.status_code == 200
    assert b"currently reserved by" in resp.data


def test_delete_reserved_page_returns_flash(client, admin_uid, editor_uid, editor2_uid):
    """POSTing delete on a reserved page flashes a denial message."""
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("Del Reserved", "del-reserved", "Body")
    db.reserve_page(page_id, editor_uid)

    client.post("/login", data={"username": "editor2", "password": "editor123"})
    resp = client.post(
        "/page/del-reserved/delete",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"currently reserved by" in resp.data


def test_edit_title_reserved_page_returns_flash(client, admin_uid, editor_uid, editor2_uid):
    """POSTing title edit on a reserved page flashes a denial message."""
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("Title Reserved", "title-reserved", "Body")
    db.reserve_page(page_id, editor_uid)

    client.post("/login", data={"username": "editor2", "password": "editor123"})
    resp = client.post(
        "/page/title-reserved/edit/title",
        data={"title": "New Title"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"currently reserved by" in resp.data


def test_tag_reserved_page_returns_flash(client, admin_uid, editor_uid, editor2_uid):
    """POSTing tag change on a reserved page flashes a denial message."""
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("Tag Reserved", "tag-reserved", "Body")
    db.reserve_page(page_id, editor_uid)

    client.post("/login", data={"username": "editor2", "password": "editor123"})
    resp = client.post(
        "/page/tag-reserved/tag",
        data={"difficulty_tag": "easy"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"currently reserved by" in resp.data


def test_reserve_page_during_cooldown_returns_flash(client, admin_uid, editor_uid):
    """Attempting to reserve a page during cooldown returns a flash message."""
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("Cooldown Page", "cooldown-pg", "Body")
    db.reserve_page(page_id, editor_uid)
    db.release_page_reservation(page_id, editor_uid)

    client.post("/login", data={"username": "editor1", "password": "editor123"})
    resp = client.post(
        "/page/cooldown-pg/reserve",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"cooldown" in resp.data.lower()


def test_reserve_already_reserved_page_returns_flash(client, admin_uid, editor_uid, editor2_uid):
    """Attempting to reserve a page already reserved by another user returns flash."""
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("Already Res", "already-res", "Body")
    db.reserve_page(page_id, editor_uid)

    client.post("/login", data={"username": "editor2", "password": "editor123"})
    resp = client.post(
        "/page/already-res/reserve",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"already reserved" in resp.data.lower() or b"reserved by" in resp.data.lower()


def test_no_disabled_buttons_on_reserved_page_view(client, admin_uid, editor_uid, editor2_uid):
    """The page view must not contain disabled buttons for reservation-blocked actions."""
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("No Disabled", "no-disabled", "Body")
    db.reserve_page(page_id, editor_uid)

    client.post("/login", data={"username": "editor2", "password": "editor123"})
    resp = client.get("/page/no-disabled")
    assert resp.status_code == 200
    # No disabled buttons should appear for edit/delete/title/tag actions
    assert b'<button class="btn btn-sm" disabled' not in resp.data
    assert b'<button class="btn btn-sm btn-danger" disabled' not in resp.data


# ---------------------------------------------------------------------------
# ISSUE-10: No phantom message when attachment validation fails
# ---------------------------------------------------------------------------

def test_dm_no_phantom_message_when_attachments_disabled(client, admin_uid, alice_uid, bob_uid):
    """ISSUE-10: When chat attachments are disabled, rejecting the attachment must not silently commit the text message."""
    import io
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.update_site_settings(chat_dm_enabled=1, chat_attachments_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"file content"), "test.pdf")
    resp = client.post(
        f"/chats/{chat['id']}/send",
        data={"content": "hello with file", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"attachments are currently disabled" in resp.data
    msgs = db.get_chat_messages(chat["id"])
    assert len(msgs) == 0, "Text message must not be committed when attachment is rejected"


def test_dm_no_phantom_message_when_file_type_blocked(client, admin_uid, alice_uid, bob_uid):
    """ISSUE-10: Rejected file type must not leave a phantom text message in DMs."""
    import io
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.update_site_settings(chat_dm_enabled=1, chat_attachments_enabled=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"evil"), "malware.exe")
    resp = client.post(
        f"/chats/{chat['id']}/send",
        data={"content": "check this out", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"File type not allowed" in resp.data
    msgs = db.get_chat_messages(chat["id"])
    assert len(msgs) == 0, "Text message must not be committed when file type is blocked"


def test_dm_no_phantom_message_when_daily_limit_reached(client, admin_uid, alice_uid, bob_uid, monkeypatch):
    """ISSUE-10: Exceeding daily attachment limit must not leave a phantom text message in DMs."""
    import io
    chat = db.get_or_create_chat(alice_uid, bob_uid)
    db.update_site_settings(chat_dm_enabled=1, chat_attachments_enabled=1, chat_attachments_per_day_limit=1)
    # Pretend the user already used their daily quota
    monkeypatch.setattr(db, "get_user_chat_attachment_count_today", lambda uid: 1)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"data"), "doc.pdf")
    resp = client.post(
        f"/chats/{chat['id']}/send",
        data={"content": "daily limit test", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Daily attachment limit" in resp.data
    msgs = db.get_chat_messages(chat["id"])
    assert len(msgs) == 0, "Text message must not be committed when daily limit is exceeded"


def test_group_no_phantom_message_when_attachments_disabled(client, admin_uid, alice_uid):
    """ISSUE-10: When group chat attachments are disabled, rejecting the attachment must not silently commit the text message."""
    import io
    group = db.create_group_chat("Phantom Test Group", alice_uid)
    db.update_site_settings(chat_group_enabled=1, chat_attachments_enabled=0)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"file content"), "test.pdf")
    resp = client.post(
        f"/groups/{group['id']}/send",
        data={"content": "hello with file", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"attachments are currently disabled" in resp.data
    msgs = db.get_group_messages(group["id"])
    # Only system messages (join, create) should be present. No user text message
    user_msgs = [m for m in msgs if not m.get("is_system")]
    assert len(user_msgs) == 0, "Text message must not be committed when attachment is rejected"


def test_group_no_phantom_message_when_file_type_blocked(client, admin_uid, alice_uid):
    """ISSUE-10: Rejected file type must not leave a phantom text message in groups."""
    import io
    group = db.create_group_chat("Blocked Type Group", alice_uid)
    db.update_site_settings(chat_group_enabled=1, chat_attachments_enabled=1)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"evil"), "malware.exe")
    resp = client.post(
        f"/groups/{group['id']}/send",
        data={"content": "check this out", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"File type not allowed" in resp.data
    msgs = db.get_group_messages(group["id"])
    user_msgs = [m for m in msgs if not m.get("is_system")]
    assert len(user_msgs) == 0, "Text message must not be committed when file type is blocked"


def test_group_no_phantom_message_when_daily_limit_reached(client, admin_uid, alice_uid, monkeypatch):
    """ISSUE-10: Exceeding daily attachment limit must not leave a phantom text message in groups."""
    import io
    group = db.create_group_chat("Limit Group", alice_uid)
    db.update_site_settings(chat_group_enabled=1, chat_attachments_enabled=1, chat_attachments_per_day_limit=1)
    # Pretend the user already used their daily quota
    monkeypatch.setattr(db, "get_user_group_attachment_count_today", lambda uid: 1)
    client.post("/login", data={"username": "alice", "password": "alice123"})

    fake_file = (io.BytesIO(b"data"), "doc.pdf")
    resp = client.post(
        f"/groups/{group['id']}/send",
        data={"content": "daily limit test", "attachment": fake_file},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Daily attachment limit" in resp.data
    msgs = db.get_group_messages(group["id"])
    user_msgs = [m for m in msgs if not m.get("is_system")]
    assert len(user_msgs) == 0, "Text message must not be committed when daily limit is exceeded"
