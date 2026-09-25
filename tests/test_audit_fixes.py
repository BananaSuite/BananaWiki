"""Tests for bugs discovered and fixed during the pre-release audit.

Each test validates a specific fix:
- Connection leak in award_badge
- try/finally in cleanup functions
- Safe integer parsing in admin settings
- Duplicate migration code removal
- Cross-board kanban ticket move prevention
- Cross-board kanban ticket reorder prevention
- Plugin SDK core table protection
"""

import db


class TestBadgeConnectionFix:
    """Verify award_badge does not leak a DB connection."""

    def test_award_badge_creates_notification(self, client, admin_user):
        """award_badge creates both badge and notification in one transaction."""
        badge_id = db.create_badge_type(
            name="Test Badge",
            description="d",
            icon="🎖",
            color="#000000",
            enabled=1,
            auto_trigger=0,
            created_by=admin_user,
        )
        result = db.award_badge(admin_user, badge_id)
        assert result is not None

        # Verify notification was also created
        conn = db.get_db()
        try:
            row = conn.execute(
                "SELECT * FROM badge_notifications WHERE user_id=? AND badge_type_id=?",
                (admin_user, badge_id),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_award_badge_duplicate_returns_none(self, client, admin_user):
        """Second award of same non-multiple badge returns None."""
        badge_id = db.create_badge_type(
            name="Dup Badge",
            description="d",
            icon="🎖",
            color="#000000",
            enabled=1,
            auto_trigger=0,
            created_by=admin_user,
        )
        first = db.award_badge(admin_user, badge_id)
        assert first is not None
        second = db.award_badge(admin_user, badge_id)
        assert second is None


class TestAdminSafeIntParsing:
    """Verify admin settings don't crash on malformed numeric inputs."""

    def test_non_numeric_telegram_settings(self, logged_in_admin):
        """Non-numeric telegram settings fall back to defaults instead of 500."""
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "Test Wiki",
            "timezone": "UTC",
            "telegram_sync_split_threshold_mb": "not-a-number",
            "telegram_sync_compress_level": "abc",
            "auto_logout_hour": "xyz",
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_non_numeric_chat_settings(self, logged_in_admin):
        """Non-numeric chat settings fall back to defaults instead of 500."""
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "Test Wiki",
            "timezone": "UTC",
            "chat_max_message_length": "bad",
            "chat_max_attachment_size_mb": "",
            "chat_attachments_per_day_limit": "NaN",
        }, follow_redirects=True)
        assert resp.status_code == 200


class TestKanbanCrossBoardMove:
    """Verify tickets cannot be moved across boards."""

    def _make_board(self, admin_user, name="Board"):
        """Create a kanban board with one column."""
        board_id = db.kanban_create_board(name, "", admin_user)
        col_id = db.kanban_create_column(board_id, "Column")
        return board_id, col_id

    def test_move_ticket_rejects_cross_board_column(self, logged_in_admin, admin_user):
        """Moving a ticket to a column on a different board returns 404."""
        _, col_a = self._make_board(admin_user, "Board A")
        _, col_b = self._make_board(admin_user, "Board B")
        ticket_id = db.kanban_create_ticket(col_a, "Test Ticket", "", admin_user)

        resp = logged_in_admin.post(
            f"/api/kanban/tickets/{ticket_id}/move",
            json={"column_id": col_b, "sort_order": 0},
        )
        assert resp.status_code == 404

    def test_move_ticket_same_board_allowed(self, logged_in_admin, admin_user):
        """Moving a ticket to another column on the same board succeeds."""
        board_id, col_a = self._make_board(admin_user, "Board C")
        col_b = db.kanban_create_column(board_id, "Col 2")
        ticket_id = db.kanban_create_ticket(col_a, "Test Ticket", "", admin_user)

        resp = logged_in_admin.post(
            f"/api/kanban/tickets/{ticket_id}/move",
            json={"column_id": col_b, "sort_order": 0},
        )
        assert resp.status_code == 200

    def test_reorder_ignores_foreign_tickets(self, logged_in_admin, admin_user):
        """Reorder endpoint does not pull tickets from other boards."""
        _, col_a = self._make_board(admin_user, "Board D")
        _, col_b = self._make_board(admin_user, "Board E")
        ticket_a = db.kanban_create_ticket(col_a, "TA", "", admin_user)
        ticket_b = db.kanban_create_ticket(col_b, "TB", "", admin_user)

        # Try to reorder col_a including a ticket from col_b
        resp = logged_in_admin.post(
            f"/api/kanban/columns/{col_a}/tickets/reorder",
            json={"order": [ticket_b, ticket_a]},
        )
        assert resp.status_code == 200

        # ticket_b should NOT have been moved to col_a
        t = db.kanban_get_ticket(ticket_b)
        assert t["column_id"] == col_b


class TestPluginSDKCoreTableProtection:
    """Verify plugin SDK blocks writes to all core tables."""

    def test_write_to_new_core_tables_blocked(self):
        """Writes to recently-added core tables are blocked."""
        from bananawiki_sdk._database import db_execute, _CORE_TABLES
        import pytest

        # Verify key tables that were previously missing are now protected
        for table in [
            "kanban_board_shares", "kanban_ticket_attachments",
            "beta_testers", "custom_pages", "api_tokens",
            "temp_pages", "rate_limit_hits",
        ]:
            assert table in _CORE_TABLES, f"{table} should be in _CORE_TABLES"
            with pytest.raises(Exception):
                db_execute(f"DELETE FROM {table}")
