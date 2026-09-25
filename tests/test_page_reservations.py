"""
Tests for the Page Reservation & Editing Lock System.

Tests reservation creation, expiry, cooldowns, race conditions,
permission guards, and integration with edit workflows.
"""
import os
import sys
import time
import pytest
from datetime import datetime, timedelta, timezone
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
    db.update_site_settings(setup_done=1, page_reservations_enabled=1)
    return uid


@pytest.fixture
def editor_user():
    """Create an editor user."""
    import db
    db.update_site_settings(setup_done=1, page_reservations_enabled=1)
    uid = db.create_user("editor", generate_password_hash("editor123"), role="editor")
    return uid


@pytest.fixture
def editor2_user():
    """Create a second editor user."""
    import db
    uid = db.create_user("editor2", generate_password_hash("editor123"), role="editor")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a client that is logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, editor_user):
    """Return a client that is logged in as editor."""
    client.post("/login", data={"username": "editor", "password": "editor123"})
    return client


@pytest.fixture
def test_page():
    """Create a test page."""
    import db
    db.update_site_settings(page_reservations_enabled=1)
    page_id = db.create_page("Test Page", "test-page", "Test content")
    return page_id


# ============================================================================
# UNIT TESTS: Database Functions
# ============================================================================

def test_reservations_disabled_by_default(isolated_db):
    """The reservation feature flag should default to off."""
    import db

    settings = db.get_site_settings()
    assert settings["page_reservations_enabled"] == 0
    assert settings["page_reservation_duration_hours"] == config.PAGE_RESERVATION_DURATION_HOURS
    assert settings["page_reservation_cooldown_hours"] == config.PAGE_RESERVATION_COOLDOWN_HOURS
    assert settings["default_reserved_pages_quota"] == 5

def test_reserve_page_success(editor_user, test_page):
    """Test successful page reservation."""
    import db

    reservation = db.reserve_page(test_page, editor_user)

    assert reservation is not None
    assert reservation["page_id"] == test_page
    assert reservation["user_id"] == editor_user
    assert reservation["reserved_at"] is not None
    assert reservation["expires_at"] is not None
    assert reservation["released_at"] is None

    # Check expiry is 48 hours in the future
    reserved_at = datetime.fromisoformat(reservation["reserved_at"]).replace(tzinfo=timezone.utc)
    expires_at = datetime.fromisoformat(reservation["expires_at"]).replace(tzinfo=timezone.utc)
    delta = expires_at - reserved_at
    assert delta.total_seconds() == config.PAGE_RESERVATION_DURATION_HOURS * 3600


def test_reserve_page_already_reserved(editor_user, editor2_user, test_page):
    """Test that reserving an already reserved page fails."""
    import db

    # Editor1 reserves the page
    db.reserve_page(test_page, editor_user)

    # Editor2 tries to reserve the same page
    with pytest.raises(ValueError, match="already reserved"):
        db.reserve_page(test_page, editor2_user)


def test_reserve_page_in_cooldown(editor_user, test_page):
    """Test that user cannot re-reserve during cooldown."""
    import db

    # Reserve and release
    db.reserve_page(test_page, editor_user)
    db.release_page_reservation(test_page, editor_user)

    # Try to reserve again immediately
    with pytest.raises(ValueError, match="cooldown"):
        db.reserve_page(test_page, editor_user)


def test_release_page_reservation(editor_user, test_page):
    """Test manual release of reservation."""
    import db

    # Reserve the page
    db.reserve_page(test_page, editor_user)

    # Release it
    released = db.release_page_reservation(test_page, editor_user)
    assert released is True

    # Verify page is no longer reserved
    status = db.get_page_reservation_status(test_page, editor_user)
    assert status["is_reserved"] is False


def test_release_creates_cooldown(editor_user, test_page):
    """Test that releasing a reservation creates a cooldown."""
    import db

    # Reserve and release
    db.reserve_page(test_page, editor_user)
    db.release_page_reservation(test_page, editor_user)

    # Check cooldown exists
    status = db.get_page_reservation_status(test_page, editor_user)
    assert status["user_in_cooldown"] is True
    assert status["cooldown_until"] is not None


def test_reserve_page_uses_custom_duration_from_settings(editor_user, test_page):
    """Reservation expiry uses the admin-configured duration."""
    import db

    db.update_site_settings(page_reservation_duration_hours=12)

    reservation = db.reserve_page(test_page, editor_user)
    reserved_at = datetime.fromisoformat(reservation["reserved_at"]).replace(tzinfo=timezone.utc)
    expires_at = datetime.fromisoformat(reservation["expires_at"]).replace(tzinfo=timezone.utc)

    assert (expires_at - reserved_at).total_seconds() == 12 * 3600


def test_release_page_reservation_uses_custom_cooldown(editor_user, test_page):
    """Cooldown length uses the admin-configured setting."""
    import db

    db.update_site_settings(page_reservation_cooldown_hours=6)

    db.reserve_page(test_page, editor_user)
    db.release_page_reservation(test_page, editor_user)

    conn = db.get_db()
    reservation = conn.execute(
        "SELECT released_at FROM page_reservations WHERE page_id=?",
        (test_page,),
    ).fetchone()
    cooldown = conn.execute(
        "SELECT cooldown_until FROM user_page_cooldowns WHERE page_id=? AND user_id=?",
        (test_page, editor_user),
    ).fetchone()
    conn.close()

    released_at = datetime.fromisoformat(reservation["released_at"]).replace(tzinfo=timezone.utc)
    cooldown_until = datetime.fromisoformat(cooldown["cooldown_until"]).replace(tzinfo=timezone.utc)

    assert (cooldown_until - released_at).total_seconds() == 6 * 3600


def test_get_page_reservation_status_unreserved(test_page, editor_user):
    """Test status for unreserved page."""
    import db

    status = db.get_page_reservation_status(test_page, editor_user)

    assert status["is_reserved"] is False
    assert status["reserved_by"] is None
    assert status["reserved_by_username"] is None
    assert status["reserved_at"] is None
    assert status["expires_at"] is None
    assert status["time_remaining"] is None
    assert status["user_in_cooldown"] is False


def test_get_page_reservation_status_reserved(editor_user, test_page):
    """Test status for reserved page."""
    import db

    db.reserve_page(test_page, editor_user)
    status = db.get_page_reservation_status(test_page, editor_user)

    assert status["is_reserved"] is True
    assert status["reserved_by"] == editor_user
    assert status["reserved_by_username"] == "editor"
    assert status["reserved_at"] is not None
    assert status["expires_at"] is not None
    assert status["time_remaining"] is not None


def test_can_user_reserve_page(editor_user, test_page):
    """Test can_user_reserve_page function."""
    import db

    # Should be able to reserve initially
    can_reserve, reason = db.can_user_reserve_page(test_page, editor_user)
    assert can_reserve is True
    assert reason == ""

    # Reserve it
    db.reserve_page(test_page, editor_user)

    # Should not be able to reserve again
    can_reserve, reason = db.can_user_reserve_page(test_page, editor_user)
    assert can_reserve is False
    assert "already reserved" in reason.lower()


def test_can_user_edit_page_no_reservation(test_page, editor_user):
    """Test edit permission when page is not reserved."""
    import db

    can_edit, reason = db.can_user_edit_page(test_page, editor_user)
    assert can_edit is True
    assert reason == ""


def test_can_user_edit_page_own_reservation(test_page, editor_user):
    """Test edit permission when user holds the reservation."""
    import db

    db.reserve_page(test_page, editor_user)
    can_edit, reason = db.can_user_edit_page(test_page, editor_user)
    assert can_edit is True
    assert reason == ""


def test_can_user_edit_page_other_reservation(test_page, editor_user, editor2_user):
    """Test edit permission when another user holds the reservation."""
    import db

    db.reserve_page(test_page, editor_user)
    can_edit, reason = db.can_user_edit_page(test_page, editor2_user)
    assert can_edit is False
    assert "reserved by" in reason.lower()


def test_no_cross_page_cooldown(editor_user):
    """Test that cooldown only applies to the specific page."""
    import db

    page1 = db.create_page("Page 1", "page1", "Content 1")
    page2 = db.create_page("Page 2", "page2", "Content 2")

    # Reserve and release page1
    db.reserve_page(page1, editor_user)
    db.release_page_reservation(page1, editor_user)

    # User should still be able to reserve page2
    can_reserve, reason = db.can_user_reserve_page(page2, editor_user)
    assert can_reserve is True
    assert reason == ""

    # Verify can actually reserve page2
    reservation = db.reserve_page(page2, editor_user)
    assert reservation is not None


def test_cleanup_expired_reservations(editor_user, test_page, monkeypatch):
    """Test cleanup of expired reservations."""
    import db
    from datetime import datetime, timedelta, timezone

    # Create a reservation
    db.reserve_page(test_page, editor_user)

    # Get the reservation and manually set it to expired
    conn = db.get_db()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute(
        "UPDATE page_reservations SET expires_at=? WHERE page_id=?",
        (past, test_page)
    )
    conn.commit()
    conn.close()

    # Run cleanup
    result = db.cleanup_expired_reservations()
    assert result["reservations_cleaned"] == 1

    # Verify reservation is marked as released
    status = db.get_page_reservation_status(test_page, editor_user)
    assert status["is_reserved"] is False


def test_force_release_reservation(editor_user, test_page):
    """Test admin force release without cooldown."""
    import db

    # Reserve the page
    db.reserve_page(test_page, editor_user)

    # Force release (admin action)
    released = db.force_release_reservation(test_page)
    assert released is True

    # Verify page is no longer reserved
    status = db.get_page_reservation_status(test_page, editor_user)
    assert status["is_reserved"] is False

    # Verify NO cooldown was created (unlike regular release)
    assert status["user_in_cooldown"] is False


def test_get_user_reservations(editor_user):
    """Test getting all active reservations for a user."""
    import db

    page1 = db.create_page("Page 1", "page1", "Content 1")
    page2 = db.create_page("Page 2", "page2", "Content 2")
    page3 = db.create_page("Page 3", "page3", "Content 3")

    # Reserve pages 1 and 2
    db.reserve_page(page1, editor_user)
    db.reserve_page(page2, editor_user)

    # Get user's reservations
    reservations = db.get_user_reservations(editor_user)
    assert len(reservations) == 2

    # Verify page info is included
    page_ids = [r["page_id"] for r in reservations]
    assert page1 in page_ids
    assert page2 in page_ids
    assert page3 not in page_ids


# ============================================================================
# INTEGRATION TESTS: API Routes
# ============================================================================

def test_api_reserve_page(logged_in_editor, test_page):
    """Test POST /api/pages/<id>/reservation."""
    response = logged_in_editor.post(f"/api/pages/{test_page}/reservation")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["message"] == "Page has been successfully reserved for your editing."
    assert "reservation" in data


def test_api_reserve_page_already_reserved(logged_in_editor, logged_in_admin, test_page):
    """Test reserving an already reserved page returns 409."""
    # Editor reserves the page
    logged_in_editor.post(f"/api/pages/{test_page}/reservation")

    # Admin tries to reserve (different user)
    response = logged_in_admin.post(f"/api/pages/{test_page}/reservation")
    assert response.status_code == 409
    data = response.get_json()
    assert "error" in data


def test_api_release_reservation(logged_in_editor, test_page):
    """Test DELETE /api/pages/<id>/reservation."""
    # Reserve first
    logged_in_editor.post(f"/api/pages/{test_page}/reservation")

    # Release
    response = logged_in_editor.delete(f"/api/pages/{test_page}/reservation")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["message"] == "Page reservation has been successfully released."


def test_api_release_reservation_not_holding(logged_in_admin, test_page, editor_user):
    """Test releasing when not holding reservation returns 404."""
    import db

    # Editor reserves the page
    db.reserve_page(test_page, editor_user)

    # Admin tries to release (but doesn't hold it)
    response = logged_in_admin.delete(f"/api/pages/{test_page}/reservation")
    assert response.status_code == 404


def test_api_get_reservation_status(logged_in_editor, test_page):
    """Test GET /api/pages/<id>/reservation/status."""
    response = logged_in_editor.get(f"/api/pages/{test_page}/reservation/status")
    assert response.status_code == 200
    data = response.get_json()
    assert "is_reserved" in data
    assert data["is_reserved"] is False


def test_api_get_reservation_status_reserved(logged_in_editor, test_page):
    """Test status endpoint returns correct info when reserved."""
    # Reserve the page
    logged_in_editor.post(f"/api/pages/{test_page}/reservation")

    # Get status
    response = logged_in_editor.get(f"/api/pages/{test_page}/reservation/status")
    data = response.get_json()
    assert data["is_reserved"] is True
    assert data["reserved_by"] is not None
    assert data["reserved_by_username"] == "editor"
    assert "time_remaining_text" in data


def test_sidebar_hides_lock_icon_for_unreserved_page(logged_in_editor, test_page):
    """Sidebar page links stay plain when a page has no active reservation."""
    response = logged_in_editor.get("/page/test-page")
    assert response.status_code == 200
    assert b"sidebar-reservation-indicator" not in response.data
    assert b"Reserved by another user" not in response.data
    assert b"Cooldown active for you" not in response.data


def test_sidebar_shows_blue_lock_for_own_reservation(logged_in_editor, editor_user):
    """Reserved category pages show the self-owned sidebar lock state."""
    import db

    category_id = db.create_category("Reserved Work")
    page_id = db.create_page("Reserved Page", "reserved-page", "Content", category_id=category_id)
    db.reserve_page(page_id, editor_user)

    response = logged_in_editor.get("/page/reserved-page")
    assert response.status_code == 200
    assert b"sidebar-reservation-indicator sidebar-reservation-indicator-self" in response.data
    assert b"<svg class=\"sidebar-reservation-indicator-icon\"" in response.data
    assert b"Reserved by you" in response.data


def test_sidebar_shows_red_lock_for_other_users_and_search_results(client, test_page, editor_user, editor2_user):
    """Reserved pages show the other-user lock state in the tree and sidebar search."""
    import db

    db.reserve_page(test_page, editor_user)
    client.post("/login", data={"username": "editor2", "password": "editor123"})

    response = client.get("/page/test-page")
    assert response.status_code == 200
    assert b"sidebar-reservation-indicator-self" not in response.data
    assert b"sidebar-reservation-indicator" in response.data
    assert b"<svg class=\"sidebar-reservation-indicator-icon\"" in response.data
    assert b"Reserved by another user" in response.data

    search_response = client.get("/api/sidebar/search?q=Test")
    assert search_response.status_code == 200
    data = search_response.get_json()
    match = next(page for page in data["pages"] if page["id"] == test_page)
    assert match["is_reserved"] is True
    assert match["reserved_by_current_user"] is False
    assert match["reservation_label"] == "Reserved by another user"


def test_sidebar_search_marks_own_reservation(logged_in_editor, test_page):
    """Sidebar search includes self-owned reservation metadata for blue lock rendering."""
    logged_in_editor.post(f"/api/pages/{test_page}/reservation")

    response = logged_in_editor.get("/api/sidebar/search?q=Test")
    assert response.status_code == 200
    data = response.get_json()
    match = next(page for page in data["pages"] if page["id"] == test_page)
    assert match["is_reserved"] is True
    assert match["reserved_by_current_user"] is True
    assert match["reservation_label"] == "Reserved by you"


def test_sidebar_shows_cooldown_icon_only_for_affected_viewer(client, test_page, editor_user, editor2_user):
    """Cooldown sidebar icon is visible only to the viewer who is in cooldown."""
    import db

    db.reserve_page(test_page, editor_user)
    db.release_page_reservation(test_page, editor_user)

    client.post("/login", data={"username": "editor", "password": "editor123"})
    response = client.get("/page/test-page")
    assert response.status_code == 200
    assert b"sidebar-reservation-indicator-cooldown" in response.data
    assert b"Cooldown active for you" in response.data

    search_response = client.get("/api/sidebar/search?q=Test")
    assert search_response.status_code == 200
    data = search_response.get_json()
    match = next(page for page in data["pages"] if page["id"] == test_page)
    assert match["is_reserved"] is False
    assert match["user_in_cooldown"] is True
    assert match["cooldown_label"] == "Cooldown active for you"

    client.post("/logout")
    client.post("/login", data={"username": "editor2", "password": "editor123"})
    other_response = client.get("/page/test-page")
    assert other_response.status_code == 200
    assert b"sidebar-reservation-indicator-cooldown" not in other_response.data
    assert b"Cooldown active for you" not in other_response.data

    other_search_response = client.get("/api/sidebar/search?q=Test")
    assert other_search_response.status_code == 200
    other_data = other_search_response.get_json()
    other_match = next(page for page in other_data["pages"] if page["id"] == test_page)
    assert other_match["user_in_cooldown"] is False
    assert other_match["cooldown_label"] is None


def test_api_reservation_requires_editor_role(client, test_page):
    """Test that reservation endpoints require editor role."""
    import db
    from werkzeug.security import generate_password_hash

    # Create regular user
    db.create_user("user", generate_password_hash("user123"), role="user")
    db.update_site_settings(setup_done=1)

    # Login as user
    client.post("/login", data={"username": "user", "password": "user123"})

    # Try to reserve
    response = client.post(f"/api/pages/{test_page}/reservation")
    assert response.status_code == 302  # Redirect due to @editor_required


def test_api_reservation_returns_403_when_disabled(logged_in_editor, test_page):
    """Reservation API access is blocked when the feature is disabled."""
    import db

    db.update_site_settings(page_reservations_enabled=0)

    response = logged_in_editor.post(f"/api/pages/{test_page}/reservation")
    assert response.status_code == 403
    assert b"currently disabled" in response.data


def test_admin_can_enable_page_reservations_from_settings(logged_in_admin):
    """Admins can update reservation settings from the settings screen."""
    import db

    response = logged_in_admin.post("/global-settings", data={
        "site_name": "BananaWiki",
        "timezone": "UTC",
        "primary_color": "#7c8dc6",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#b8bcc8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
        "page_reservations_enabled": "1",
        "page_reservation_duration_hours": "12",
        "page_reservation_cooldown_hours": "6",
    })
    assert response.status_code in (200, 302)
    settings = db.get_site_settings()
    assert settings["page_reservations_enabled"] == 1
    assert settings["page_reservation_duration_hours"] == 12
    assert settings["page_reservation_cooldown_hours"] == 6


def test_settings_page_has_page_reservations_checkbox(logged_in_admin):
    """The settings page exposes reservation toggle and timing fields."""
    response = logged_in_admin.get("/global-settings")
    assert response.status_code == 200
    assert b"page_reservations_enabled" in response.data
    assert b"page_reservation_duration_hours" in response.data
    assert b"page_reservation_cooldown_hours" in response.data
    assert b"default_reserved_pages_quota" in response.data


def test_reserve_page_enforces_default_quota(editor_user):
    """Users cannot exceed the configured concurrent reservation quota."""
    import db

    db.update_site_settings(page_reservations_enabled=1, default_reserved_pages_quota=1)
    page_one = db.create_page("Quota Page One", "quota-page-one", "Test content")
    page_two = db.create_page("Quota Page Two", "quota-page-two", "Test content")

    db.reserve_page(page_one, editor_user)

    with pytest.raises(ValueError, match="quota"):
        db.reserve_page(page_two, editor_user)


def test_user_can_submit_quota_request_and_view_history(logged_in_editor):
    """Users can submit one pending quota request and review their own history."""
    response = logged_in_editor.get("/settings/reservation-quota")
    assert response.status_code == 200
    assert b"Quota Request History" in response.data

    response = logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "7",
            "reason": "I am editing multiple related pages.",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Quota request has been successfully submitted." in response.data
    assert b"Pending" in response.data
    assert b"I am editing multiple related pages." in response.data

    duplicate = logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "8",
            "reason": "Another request",
        },
        follow_redirects=True,
    )
    assert duplicate.status_code == 200
    assert b"already have a pending quota request" in duplicate.data


def test_denied_quota_request_allows_new_submission(logged_in_editor, admin_user):
    """Users may submit a new quota request after a denial."""
    import db

    editor = db.get_user_by_username("editor")
    logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "7",
            "reason": "Need a higher limit.",
        },
    )
    pending_request = db.get_pending_reservation_quota_request(editor["id"])

    logged_in_editor.get("/logout")
    logged_in_editor.post("/login", data={"username": "admin", "password": "admin123"})
    denied = logged_in_editor.post(
        f"/admin/users/{editor['id']}/reservation-quota",
        data={"action": "deny_request", "request_id": str(pending_request["id"])},
        follow_redirects=True,
    )
    assert denied.status_code == 200
    assert b"Quota request has been successfully denied." in denied.data
    assert b"Denied" in denied.data

    logged_in_editor.get("/logout")
    logged_in_editor.post("/login", data={"username": "editor", "password": "editor123"})
    response = logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "8",
            "reason": "Trying again after the denial.",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Quota request has been successfully submitted." in response.data
    assert b"Trying again after the denial." in response.data


def test_admin_can_review_quota_request_and_pending_indicator_appears(logged_in_editor, admin_user):
    """Admins can review quota requests from the dedicated page and see pending markers."""
    import db

    db.update_site_settings(default_reserved_pages_quota=1)
    editor = db.get_user_by_username("editor")
    first_page = db.create_page("Quota Review One", "quota-review-one", "Test content")
    db.create_page("Quota Review Two", "quota-review-two", "Test content")

    logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "2",
            "reason": "Need to reserve two pages during a rewrite.",
        },
    )

    logged_in_editor.get("/logout")
    logged_in_editor.post("/login", data={"username": "admin", "password": "admin123"})

    users_response = logged_in_editor.get("/admin/users")
    assert users_response.status_code == 200
    assert b"Pending quota request" in users_response.data

    review_page = logged_in_editor.get(f"/admin/users/{editor['id']}/reservation-quota")
    assert review_page.status_code == 200
    assert b"Need to reserve two pages during a rewrite." in review_page.data

    pending_request = db.get_pending_reservation_quota_request(editor["id"])
    approved = logged_in_editor.post(
        f"/admin/users/{editor['id']}/reservation-quota",
        data={"action": "approve_request", "request_id": str(pending_request["id"])},
        follow_redirects=True,
    )
    assert approved.status_code == 200
    assert b"Quota request has been successfully approved." in approved.data
    assert b"Approved" in approved.data

    logged_in_editor.get("/logout")
    logged_in_editor.post("/login", data={"username": "editor", "password": "editor123"})

    first_reservation = logged_in_editor.post("/page/quota-review-one/reserve", follow_redirects=True)
    second_reservation = logged_in_editor.post("/page/quota-review-two/reserve", follow_redirects=True)
    assert first_reservation.status_code == 200
    assert second_reservation.status_code == 200

    status = db.get_page_reservation_status(first_page, editor["id"])
    assert status["is_reserved"] is True
    assert db.get_effective_reserved_pages_quota(editor["id"]) == 2


# ============================================================================
# INTEGRATION TESTS: Edit Workflow
# ============================================================================

def test_edit_page_with_no_reservation(logged_in_editor, test_page):
    """Test editing a page with no active reservation."""
    response = logged_in_editor.get("/page/test-page/edit")
    assert response.status_code == 200


def test_edit_page_with_own_reservation(logged_in_editor, test_page):
    """Test editing a page user has reserved."""
    import db as db_mod

    # Reserve as the logged in user
    editor = db_mod.get_user_by_username("editor")
    db_mod.reserve_page(test_page, editor["id"])

    response = logged_in_editor.get("/page/test-page/edit")
    assert response.status_code == 200


def test_edit_page_reserved_by_other(logged_in_editor, test_page, editor2_user):
    """Test that editing is blocked when page is reserved by another user."""
    import db

    # Another user reserves the page
    db.reserve_page(test_page, editor2_user)

    # Try to edit
    response = logged_in_editor.get("/page/test-page/edit", follow_redirects=True)
    assert b"Cannot edit" in response.data or b"reserved by" in response.data.lower()


def test_inline_title_edit_reserved_by_other_is_blocked(logged_in_editor, test_page, editor2_user):
    """Inline destructive edits are blocked for non-admins when reserved by another editor."""
    import db

    db.reserve_page(test_page, editor2_user)

    response = logged_in_editor.post(
        "/page/test-page/edit/title",
        data={"title": "Blocked Title"},
        follow_redirects=True,
    )
    assert b"currently reserved by" in response.data
    assert db.get_page(test_page)["title"] == "Test Page"


def test_deindex_reserved_by_other_is_blocked(logged_in_editor, test_page, editor2_user):
    """Deindexing respects the same reservation lock as other destructive page edits."""
    import db

    db.reserve_page(test_page, editor2_user)

    response = logged_in_editor.post(
        "/page/test-page/deindex",
        follow_redirects=True,
    )
    assert b"currently reserved by" in response.data
    assert db.get_page(test_page)["is_deindexed"] == 0


def test_move_page_is_still_allowed_when_reserved(logged_in_editor, test_page, editor2_user):
    """Moves remain available because they do not change page content."""
    import db

    destination = db.create_category("Moved")
    db.reserve_page(test_page, editor2_user)

    response = logged_in_editor.post(
        "/page/test-page/move",
        data={"category_id": str(destination)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Page moved." in response.data
    assert db.get_page(test_page)["category_id"] == destination


def test_admin_reserved_edit_page_shows_override_warning(logged_in_admin, editor_user, test_page):
    """Admins are warned when they open a reserved page for editing."""
    import db

    db.reserve_page(test_page, editor_user)

    response = logged_in_admin.get("/page/test-page/edit")
    assert response.status_code == 200
    assert b"You are editing as an administrator" in response.data


def test_reservation_directory_lists_pages_and_actions(logged_in_editor, test_page):
    """Editors can discover reservable pages from the reservation directory."""
    response = logged_in_editor.get("/reservations")
    assert response.status_code == 200
    assert b"Page Reservations" in response.data
    assert b"Test Page" in response.data
    assert b"Reserve" in response.data


def test_reservation_directory_redirects_when_disabled(logged_in_editor, test_page):
    """The directory should redirect with guidance when reservations are disabled."""
    import db

    db.update_site_settings(page_reservations_enabled=0)

    response = logged_in_editor.get("/reservations", follow_redirects=True)
    assert response.status_code == 200
    assert b"currently disabled" in response.data


def test_reserved_page_is_editable_when_feature_disabled(logged_in_editor, test_page, editor2_user):
    """Disabling reservations should stop them from blocking edits."""
    import db

    db.reserve_page(test_page, editor2_user)
    db.update_site_settings(page_reservations_enabled=0)

    response = logged_in_editor.get("/page/test-page/edit")
    assert response.status_code == 200


def test_save_page_verifies_reservation(logged_in_editor, test_page, editor2_user):
    """Test that saving checks reservation status."""
    import db

    # Another user reserves the page
    db.reserve_page(test_page, editor2_user)

    # Try to save
    response = logged_in_editor.post(
        "/page/test-page/edit",
        data={"title": "Updated", "content": "Updated content"},
        follow_redirects=True
    )
    # Should be redirected with error
    assert b"Cannot save" in response.data or b"reserved" in response.data.lower()


def test_permission_guard_category_access(logged_in_editor, test_page):
    """Test that category permissions are checked for reservations."""
    import db

    # Create a category and set the page to that category
    cat_id = db.create_category("Restricted")
    db.update_page_category(test_page, cat_id)

    # Set editor to be restricted to a different category (not cat_id), so
    # they have no write access to the "Restricted" category.
    editor = db.get_user_by_username("editor")
    other_cat_id = db.create_category("Allowed")
    db.set_user_permissions(
        editor["id"],
        db.get_user_permissions(editor["id"])["enabled_permissions"],
        write_restricted=True,
        write_category_ids=[other_cat_id],
    )

    # Try to reserve - should fail due to category permissions
    response = logged_in_editor.post(f"/api/pages/{test_page}/reservation")
    assert response.status_code == 403


# ============================================================================
# RACE CONDITION TESTS
# ============================================================================

def test_concurrent_reservation_attempts(test_page):
    """Test that only one user can reserve when attempting simultaneously."""
    import db
    import threading
    from werkzeug.security import generate_password_hash

    # Create two users
    user1 = db.create_user("user1", generate_password_hash("pass"), role="editor")
    user2 = db.create_user("user2", generate_password_hash("pass"), role="editor")

    results = {"user1": None, "user2": None}

    def try_reserve(user_id, key):
        try:
            results[key] = db.reserve_page(test_page, user_id)
        except ValueError as e:
            results[key] = str(e)

    # Start two threads trying to reserve simultaneously
    t1 = threading.Thread(target=try_reserve, args=(user1, "user1"))
    t2 = threading.Thread(target=try_reserve, args=(user2, "user2"))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one should succeed, one should fail
    success_count = sum(1 for r in results.values() if isinstance(r, dict))
    error_count = sum(1 for r in results.values() if isinstance(r, str))

    assert success_count == 1
    assert error_count == 1


# ============================================================================
# EDGE CASE TESTS
# ============================================================================

def test_user_deleted_releases_reservation(test_page, editor_user):
    """Test that deleting a user releases their reservations."""
    import db

    # Reserve the page
    db.reserve_page(test_page, editor_user)

    # Verify it's reserved
    status = db.get_page_reservation_status(test_page, editor_user)
    assert status["is_reserved"] is True

    # Delete the user (ON DELETE CASCADE should handle this)
    db.delete_user(editor_user)

    # Page should have no reservation now
    # (Note: reservation record will be deleted due to CASCADE, so status check should show unreserved)
    status = db.get_page_reservation_status(test_page, None)
    assert status["is_reserved"] is False


def test_page_deleted_removes_reservation(editor_user):
    """Test that deleting a page removes its reservation."""
    import db

    page_id = db.create_page("Temp Page", "temp", "Content")
    db.reserve_page(page_id, editor_user)

    # Delete the page
    db.delete_page(page_id)

    # Reservation should be gone (CASCADE delete)
    # Verify by checking the database directly
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM page_reservations WHERE page_id=?",
        (page_id,)
    ).fetchone()
    conn.close()
    assert row is None


# ============================================================================
# PERMISSION LOSS & CLEANUP TESTS
# ============================================================================

def test_reservation_auto_released_on_role_downgrade(editor_user, editor2_user, test_page):
    """Test that a reservation is auto-released when the reserving user's role
    is downgraded from editor to user (losing edit permissions)."""
    import db

    # Editor reserves the page
    db.reserve_page(test_page, editor_user)

    status = db.get_page_reservation_status(test_page, editor2_user)
    assert status["is_reserved"] is True

    # Admin downgrades editor to user
    db.update_user(editor_user, role="user")

    # Reservation should be auto-released when checked
    status = db.get_page_reservation_status(test_page, editor2_user)
    assert status["is_reserved"] is False


def test_reservation_auto_released_allows_new_reservation(editor_user, editor2_user, test_page):
    """Test that after auto-release due to role downgrade, another user can
    reserve the same page."""
    import db

    # Editor reserves the page
    db.reserve_page(test_page, editor_user)

    # Admin downgrades editor to user
    db.update_user(editor_user, role="user")

    # Verify auto-release happened
    status = db.get_page_reservation_status(test_page, editor2_user)
    assert status["is_reserved"] is False

    # Editor2 should be able to reserve now
    reservation = db.reserve_page(test_page, editor2_user)
    assert reservation is not None
    assert reservation["user_id"] == editor2_user


def test_can_user_edit_page_after_role_downgrade(editor_user, editor2_user, test_page):
    """Test that can_user_edit_page returns True for other editors after the
    reserving user is downgraded."""
    import db

    db.reserve_page(test_page, editor_user)

    # Initially, editor2 cannot edit
    can_edit, reason = db.can_user_edit_page(test_page, editor2_user)
    assert can_edit is False

    # Downgrade editor to user
    db.update_user(editor_user, role="user")

    # Now editor2 can edit (reservation is auto-released)
    can_edit, reason = db.can_user_edit_page(test_page, editor2_user)
    assert can_edit is True


def test_cleanup_called_in_get_all_active_reservations(editor_user, test_page, monkeypatch):
    """Test that get_all_active_reservations cleans up expired entries."""
    import db

    # Create a reservation and manually expire it
    db.reserve_page(test_page, editor_user)

    # Monkey-patch time so the reservation is expired
    fake_now = datetime.now(timezone.utc) + timedelta(hours=49)
    monkeypatch.setattr(
        "db._reservations.datetime",
        type("FakeDatetime", (), {
            "now": staticmethod(lambda tz=None: fake_now),
            "fromisoformat": datetime.fromisoformat,
        })
    )

    # get_all_active_reservations should call cleanup and show no active reservations
    active = db.get_all_active_reservations()
    assert len(active) == 0

    # Verify the expired reservation was marked as released
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM page_reservations WHERE page_id=?",
        (test_page,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["released_at"] is not None


def test_admin_can_edit_page_reserved_by_editor(admin_user, editor_user, test_page):
    """Test that admins can bypass reservation locks when editing a page."""
    import db

    db.reserve_page(test_page, editor_user)

    # Admin should be able to edit even though the page is reserved by the editor
    can_edit, reason = db.can_user_edit_page(test_page, admin_user)
    assert can_edit is True
    assert reason == ""


def test_admin_can_edit_reserved_page_via_route(logged_in_admin, editor_user, test_page):
    """Test that admins can access the edit page route for a reserved page."""
    import db

    db.reserve_page(test_page, editor_user)

    # Admin should be able to GET the edit page
    resp = logged_in_admin.get("/page/test-page/edit", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Cannot edit" not in resp.data


def test_regular_user_cannot_access_reservation_quota_page(client, admin_user):
    """Regular users (role='user') should be denied access to the quota request page."""
    import db

    db.create_user("regular", generate_password_hash("regular123"), role="user")
    client.post("/login", data={"username": "regular", "password": "regular123"})

    get_resp = client.get("/settings/reservation-quota", follow_redirects=True)
    assert b"You do not have permission" in get_resp.data

    post_resp = client.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "7",
            "reason": "I want more quota.",
        },
        follow_redirects=True,
    )
    assert b"You do not have permission" in post_resp.data


# ============================================================================
# QUOTA SYSTEM OPTIMIZATION TESTS
# ============================================================================

def test_admin_can_set_own_quota_directly(logged_in_admin):
    """Admins can change their own quota directly without sending a request."""
    import db

    response = logged_in_admin.post(
        "/settings/reservation-quota",
        data={"action": "set_quota", "new_quota": "15"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully updated to 15" in response.data

    admin = db.get_user_by_username("admin")
    assert db.get_effective_reserved_pages_quota(admin["id"]) == 15


def test_admin_can_set_own_quota_to_unlimited(logged_in_admin):
    """Admins can set their own quota to unlimited."""
    import db

    response = logged_in_admin.post(
        "/settings/reservation-quota",
        data={"action": "set_quota", "unlimited": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully updated to Unlimited" in response.data

    admin = db.get_user_by_username("admin")
    assert db.get_effective_reserved_pages_quota(admin["id"]) == -1


def test_admin_can_decrease_own_quota(logged_in_admin):
    """Admins can decrease their own quota."""
    import db

    admin = db.get_user_by_username("admin")
    db.set_user_reserved_pages_quota(admin["id"], 10)
    response = logged_in_admin.post(
        "/settings/reservation-quota",
        data={"action": "set_quota", "new_quota": "3"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully updated to 3" in response.data
    assert db.get_effective_reserved_pages_quota(admin["id"]) == 3


def test_admin_sees_set_quota_form_not_request_form(logged_in_admin):
    """Admin's reservation quota page shows 'Set Your Quota' instead of request form."""
    response = logged_in_admin.get("/settings/reservation-quota")
    assert response.status_code == 200
    assert b'id="admin-self-quota"' in response.data
    assert b'value="submit_quota_request"' not in response.data


def test_editor_sees_request_form_not_set_form(logged_in_editor, admin_user):
    """Editors see the request form, not the direct set form."""
    response = logged_in_editor.get("/settings/reservation-quota")
    assert response.status_code == 200
    assert b'value="submit_quota_request"' in response.data
    assert b'id="admin-self-quota"' not in response.data


def test_editor_can_cancel_pending_quota_request(logged_in_editor, admin_user):
    """Editors can cancel a pending quota request to submit a new one."""
    import db

    logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "10",
            "reason": "Need more quota.",
        },
    )
    editor = db.get_user_by_username("editor")
    pending = db.get_pending_reservation_quota_request(editor["id"])
    assert pending is not None

    cancel_response = logged_in_editor.post(
        "/settings/reservation-quota",
        data={"action": "cancel_request", "request_id": str(pending["id"])},
        follow_redirects=True,
    )
    assert cancel_response.status_code == 200
    assert b"successfully cancelled" in cancel_response.data

    assert db.get_pending_reservation_quota_request(editor["id"]) is None

    new_response = logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "8",
            "reason": "Trying again after cancel.",
        },
        follow_redirects=True,
    )
    assert new_response.status_code == 200
    assert b"successfully submitted" in new_response.data


def test_editor_can_request_unlimited_quota(logged_in_editor, admin_user):
    """Editors can request unlimited quota."""
    import db

    response = logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "unlimited": "1",
            "reason": "I need unlimited reservations.",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully submitted" in response.data

    editor = db.get_user_by_username("editor")
    pending = db.get_pending_reservation_quota_request(editor["id"])
    assert pending is not None
    assert pending["requested_quota"] == -1


def test_admin_can_set_user_quota_directly(logged_in_admin, editor_user):
    """Admins can directly set any user's quota."""
    import db

    editor = db.get_user_by_username("editor")
    response = logged_in_admin.post(
        f"/admin/users/{editor['id']}/reservation-quota",
        data={"action": "set_quota", "new_quota": "20"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully updated to 20" in response.data
    assert db.get_effective_reserved_pages_quota(editor["id"]) == 20


def test_admin_can_set_user_quota_to_unlimited(logged_in_admin, editor_user):
    """Admins can set a user's quota to unlimited."""
    import db

    editor = db.get_user_by_username("editor")
    response = logged_in_admin.post(
        f"/admin/users/{editor['id']}/reservation-quota",
        data={"action": "set_quota", "unlimited": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully updated to Unlimited" in response.data
    assert db.get_effective_reserved_pages_quota(editor["id"]) == -1


def test_admin_can_decrease_user_quota(logged_in_admin, editor_user):
    """Admins can decrease a user's quota."""
    import db

    editor = db.get_user_by_username("editor")
    db.set_user_reserved_pages_quota(editor["id"], 10)
    response = logged_in_admin.post(
        f"/admin/users/{editor['id']}/reservation-quota",
        data={"action": "set_quota", "new_quota": "2"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully updated to 2" in response.data
    assert db.get_effective_reserved_pages_quota(editor["id"]) == 2


def test_admin_view_shows_set_quota_form(logged_in_admin, editor_user):
    """Admin view of a user's quota page shows 'Set Quota' form."""
    import db

    editor = db.get_user_by_username("editor")
    response = logged_in_admin.get(f"/admin/users/{editor['id']}/reservation-quota")
    assert response.status_code == 200
    assert b"Set Quota for" in response.data


def test_unlimited_quota_bypasses_reservation_limit(editor_user):
    """Users with unlimited quota can reserve beyond the default limit."""
    import db

    db.update_site_settings(page_reservations_enabled=1, default_reserved_pages_quota=1)
    db.set_user_reserved_pages_quota(editor_user, -1)

    page1 = db.create_page("Unlimited Page 1", "unlimited-page-1", "Content")
    page2 = db.create_page("Unlimited Page 2", "unlimited-page-2", "Content")
    page3 = db.create_page("Unlimited Page 3", "unlimited-page-3", "Content")

    db.reserve_page(page1, editor_user)
    db.reserve_page(page2, editor_user)
    db.reserve_page(page3, editor_user)

    assert db.get_user_active_reservation_count(editor_user) == 3


def test_cancel_only_own_pending_request():
    """Users cannot cancel another user's pending request."""
    import db

    db.update_site_settings(setup_done=1, page_reservations_enabled=1)
    user1 = db.create_user("user1", generate_password_hash("user1pass"), role="editor")
    user2 = db.create_user("user2", generate_password_hash("user2pass"), role="editor")
    db.create_reservation_quota_request(user1, 10, "Need more")

    pending = db.get_pending_reservation_quota_request(user1)
    with pytest.raises(ValueError, match="permission"):
        db.cancel_reservation_quota_request(pending["id"], user2)


def test_cancel_non_pending_request_raises():
    """Cancelling a request that is not pending raises ValueError."""
    import db

    db.update_site_settings(setup_done=1, page_reservations_enabled=1)
    admin = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    editor = db.create_user("editor", generate_password_hash("editor123"), role="editor")
    db.create_reservation_quota_request(editor, 10, "Need more")

    pending = db.get_pending_reservation_quota_request(editor)
    db.review_reservation_quota_request(pending["id"], admin, approved=True)

    with pytest.raises(ValueError, match="pending"):
        db.cancel_reservation_quota_request(pending["id"], editor)


def test_editor_can_request_lower_quota(logged_in_editor, admin_user):
    """Editors can request a quota lower than their current quota."""
    import db

    editor = db.get_user_by_username("editor")
    db.set_user_reserved_pages_quota(editor["id"], 10)

    response = logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "3",
            "reason": "I don't need so many anymore.",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"successfully submitted" in response.data


def test_unlimited_displayed_in_quota_page(logged_in_admin):
    """The quota page shows 'unlimited' when the user has unlimited quota."""
    import db

    admin = db.get_user_by_username("admin")
    db.set_user_reserved_pages_quota(admin["id"], -1)

    response = logged_in_admin.get("/settings/reservation-quota")
    assert response.status_code == 200
    assert b"unlimited" in response.data


def test_cancelled_status_shows_in_history(logged_in_editor, admin_user):
    """Cancelled requests show with 'Cancelled' status in the history."""
    import db

    logged_in_editor.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "8",
            "reason": "Testing cancel display.",
        },
    )
    editor = db.get_user_by_username("editor")
    pending = db.get_pending_reservation_quota_request(editor["id"])
    logged_in_editor.post(
        "/settings/reservation-quota",
        data={"action": "cancel_request", "request_id": str(pending["id"])},
    )

    response = logged_in_editor.get("/settings/reservation-quota")
    assert response.status_code == 200
    assert b"Cancelled" in response.data


def test_admin_cannot_submit_quota_request(logged_in_admin):
    """Admins cannot submit quota requests. They set directly."""
    response = logged_in_admin.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "10",
            "reason": "Should not work for admin.",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    # No request should have been created; no success flash
    assert b'class="flash-message">Quota request has been successfully submitted' not in response.data


# Issue-64 regression group: every failing query below must still hand its connection back.

def _make_tracked_context(module, trigger_sql, error_msg="simulated failure"):
    """Create a tracked get_db_context that records cleanup and raises on trigger_sql."""
    from contextlib import contextmanager

    cleaned = []
    original = module.get_db_context

    @contextmanager
    def _ctx():
        with original() as real_conn:

            class _W:
                def execute(self, sql, *a, **kw):
                    if sql.startswith(trigger_sql):
                        raise RuntimeError(error_msg)
                    return real_conn.execute(sql, *a, **kw)

                def __getattr__(self, name):
                    return getattr(real_conn, name)

            try:
                yield _W()
            finally:
                cleaned.append(True)

    return _ctx, cleaned


def test_review_reservation_quota_request_closes_conn_on_error(monkeypatch):
    """review_reservation_quota_request() cleans up DB connection even when an exception occurs."""
    import db
    import db._reservations as rmod

    ctx, cleaned = _make_tracked_context(rmod, "SELECT * FROM reservation_quota_requests", "simulated read failure")
    monkeypatch.setattr(rmod, "get_db_context", ctx)

    with pytest.raises(RuntimeError, match="simulated read failure"):
        db.review_reservation_quota_request(999, "fake_admin", True)

    assert cleaned, "connection cleanup was never called after the exception"


def test_cancel_reservation_quota_request_closes_conn_on_error(monkeypatch):
    """cancel_reservation_quota_request() cleans up DB connection even when an exception occurs."""
    import db
    import db._reservations as rmod

    ctx, cleaned = _make_tracked_context(rmod, "SELECT * FROM reservation_quota_requests", "simulated read failure")
    monkeypatch.setattr(rmod, "get_db_context", ctx)

    with pytest.raises(RuntimeError, match="simulated read failure"):
        db.cancel_reservation_quota_request(999, "fake_user")

    assert cleaned, "connection cleanup was never called after the exception"


def test_release_page_reservation_closes_conn_on_error(monkeypatch):
    """release_page_reservation() cleans up DB connection even when an exception occurs."""
    import db
    import db._reservations as rmod

    db.update_site_settings(setup_done=1, page_reservations_enabled=1)

    ctx, cleaned = _make_tracked_context(rmod, "SELECT * FROM page_reservations", "simulated read failure")
    monkeypatch.setattr(rmod, "get_db_context", ctx)

    with pytest.raises(RuntimeError, match="simulated read failure"):
        db.release_page_reservation(1, user_id="someone")

    assert cleaned, "connection cleanup was never called after the exception"


def test_cleanup_expired_reservations_closes_conn_on_error(monkeypatch):
    """cleanup_expired_reservations() cleans up DB connection even when an exception occurs."""
    import db
    import db._reservations as rmod

    ctx, cleaned = _make_tracked_context(rmod, "UPDATE page_reservations SET released_at", "simulated write failure")
    monkeypatch.setattr(rmod, "get_db_context", ctx)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        db.cleanup_expired_reservations()

    assert cleaned, "connection cleanup was never called after the exception"
