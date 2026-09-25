"""
Tests for:
1. Admin page reservation management (assign, clear cooldown)
"""

import db


# ============================================================================
# Admin page reservation management
# ============================================================================


class TestAdminReservationManagement:
    """Test admin reservation assignment, cooldown clearing, and force release."""

    def _create_test_page(self, admin_user):
        """Helper to create a test page and category, with reservations enabled."""
        db.update_site_settings(page_reservations_enabled=1)
        cat_id = db.create_category("Test Category")
        page_id = db.create_page("Test Page", "test-page", "Content", cat_id, admin_user)
        return page_id

    def test_admin_assign_reservation_bypasses_existing(self, admin_user, editor_user):
        """Admin can assign a reservation even if another user holds it."""
        page_id = self._create_test_page(admin_user)
        # Editor reserves the page
        db.reserve_page(page_id, editor_user)
        status = db.get_page_reservation_status(page_id, editor_user)
        assert status["is_reserved"]
        assert status["reserved_by"] == editor_user

        # Admin assigns the reservation to themselves, bypassing editor's hold
        result = db.admin_assign_reservation(page_id, admin_user)
        assert result is not None
        assert result["user_id"] == admin_user
        status = db.get_page_reservation_status(page_id, admin_user)
        assert status["is_reserved"]
        assert status["reserved_by"] == admin_user

    def test_admin_assign_reservation_bypasses_cooldown(self, admin_user, editor_user):
        """Admin can assign a reservation to a user who is in cooldown."""
        page_id = self._create_test_page(admin_user)
        # Editor reserves and then releases (creating a cooldown)
        db.reserve_page(page_id, editor_user)
        db.release_page_reservation(page_id, editor_user)
        # Verify cooldown exists
        can, reason = db.can_user_reserve_page(page_id, editor_user)
        assert not can
        assert "cooldown" in reason.lower()

        # Admin assigns back to editor, bypassing cooldown
        result = db.admin_assign_reservation(page_id, editor_user)
        assert result is not None
        assert result["user_id"] == editor_user

    def test_admin_clear_cooldown(self, admin_user, editor_user):
        """Admin can clear cooldowns for a page."""
        page_id = self._create_test_page(admin_user)
        db.reserve_page(page_id, editor_user)
        db.release_page_reservation(page_id, editor_user)
        # Verify cooldown
        can, reason = db.can_user_reserve_page(page_id, editor_user)
        assert not can

        # Clear cooldown
        count = db.admin_clear_cooldown(page_id, editor_user)
        assert count == 1

        # Now the user can reserve again
        can, reason = db.can_user_reserve_page(page_id, editor_user)
        assert can

    def test_admin_clear_cooldown_all_users(self, admin_user, editor_user):
        """Admin can clear all cooldowns for a page regardless of user."""
        page_id = self._create_test_page(admin_user)
        db.reserve_page(page_id, editor_user)
        db.release_page_reservation(page_id, editor_user)

        # Clear all cooldowns for the page (no user_id)
        count = db.admin_clear_cooldown(page_id)
        assert count >= 1

    def test_admin_assign_reservation_disabled_raises(self, admin_user):
        """Assigning when reservations are disabled raises ValueError."""
        db.update_site_settings(page_reservations_enabled=0)
        import pytest
        with pytest.raises(ValueError, match="disabled"):
            db.admin_assign_reservation(1, admin_user)

    def test_admin_checkouts_page_loads(self, logged_in_admin):
        """Admin checkouts page should load successfully."""
        resp = logged_in_admin.get("/admin/checkouts")
        assert resp.status_code == 200
        assert b"Page Checkouts" in resp.data
        assert b"Assign New Reservation" in resp.data

    def test_admin_assign_reservation_route(self, logged_in_admin, admin_user, editor_user):
        """Test the admin assign reservation route via POST."""
        page_id = self._create_test_page(admin_user)
        resp = logged_in_admin.post(
            f"/admin/checkouts/{page_id}/assign",
            data={"user_id": editor_user},
        )
        assert resp.status_code == 302
        status = db.get_page_reservation_status(page_id, editor_user)
        assert status["is_reserved"]
        assert status["reserved_by"] == editor_user

    def test_admin_assign_reservation_route_invalid_user(self, logged_in_admin, admin_user):
        """Assigning to a non-existent user shows error flash."""
        page_id = self._create_test_page(admin_user)
        resp = logged_in_admin.post(
            f"/admin/checkouts/{page_id}/assign",
            data={"user_id": "nonexistent"},
            follow_redirects=True,
        )
        assert b"does not exist" in resp.data

    def test_admin_assign_reservation_route_non_editor(self, logged_in_admin, admin_user, regular_user):
        """Admins can now assign reservations to any user role including regular users."""
        page_id = self._create_test_page(admin_user)
        resp = logged_in_admin.post(
            f"/admin/checkouts/{page_id}/assign",
            data={"user_id": regular_user},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"successfully assigned" in resp.data

    def test_admin_clear_cooldown_route(self, logged_in_admin, admin_user, editor_user):
        """Test the admin clear cooldown route via POST."""
        page_id = self._create_test_page(admin_user)
        db.reserve_page(page_id, editor_user)
        db.release_page_reservation(page_id, editor_user)

        resp = logged_in_admin.post(
            f"/admin/checkouts/{page_id}/clear-cooldown",
            data={},
        )
        assert resp.status_code == 302

        # Verify cooldown is cleared
        can, _ = db.can_user_reserve_page(page_id, editor_user)
        assert can

    def test_admin_assign_no_user_id(self, logged_in_admin, admin_user):
        """Assigning without a user_id shows error."""
        page_id = self._create_test_page(admin_user)
        resp = logged_in_admin.post(
            f"/admin/checkouts/{page_id}/assign",
            data={"user_id": ""},
            follow_redirects=True,
        )
        assert b"user must be selected" in resp.data

    def test_admin_assign_nonexistent_page(self, logged_in_admin, editor_user):
        """Assigning to a nonexistent page returns 404."""
        resp = logged_in_admin.post(
            "/admin/checkouts/99999/assign",
            data={"user_id": editor_user},
        )
        assert resp.status_code == 404

    def test_admin_clear_cooldown_nonexistent_page(self, logged_in_admin):
        """Clearing cooldown for a nonexistent page returns 404."""
        resp = logged_in_admin.post(
            "/admin/checkouts/99999/clear-cooldown",
            data={},
        )
        assert resp.status_code == 404

    def test_admin_clear_cooldown_no_active(self, logged_in_admin, admin_user):
        """Clearing cooldown when none exist shows info flash."""
        page_id = self._create_test_page(admin_user)
        resp = logged_in_admin.post(
            f"/admin/checkouts/{page_id}/clear-cooldown",
            data={},
            follow_redirects=True,
        )
        assert b"No active cooldowns" in resp.data

    def test_checkouts_page_shows_assign_form(self, logged_in_admin, admin_user, editor_user):
        """Checkouts page should show the reassign form when there are reservations."""
        page_id = self._create_test_page(admin_user)
        db.admin_assign_reservation(page_id, editor_user)
        resp = logged_in_admin.get("/admin/checkouts")
        assert resp.status_code == 200
        assert b"Reassign to:" in resp.data
        assert b"Clear Cooldowns" in resp.data
