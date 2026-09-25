"""Tests for the kanban board feature."""

import io
import json
from datetime import datetime, timedelta, timezone
import pytest


def _login(client, username, password):
    """Log in via the test client."""
    return client.post("/login", data={"username": username, "password": password})


def _enable_kanban_plugin():
    """Enable the kanban plugin in the database."""
    import db
    db.enable_plugin("kanban")


def _set_kanban_access(access="admin", write_access="admin"):
    """Set kanban access level in site settings."""
    import db
    db.update_site_settings(kanban_access=access, kanban_write_access=write_access)


def _make_kanban_file(title="Imported Board"):
    """Build a minimal kanban export JSON file-like payload."""
    payload = {
        "title": title,
        "description": "Imported by test",
        "columns": [{"title": "Todo", "tickets": []}],
    }
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class TestKanbanPluginGating:
    """Kanban routes return 404 when the plugin is disabled."""

    def test_kanban_list_404_when_disabled(self, client, admin_user):
        """Kanban list returns 404 when the plugin is disabled."""
        import db
        db.disable_plugin("kanban")
        _login(client, "admin", "admin123")
        resp = client.get("/kanban")
        assert resp.status_code == 404

    def test_kanban_api_404_when_disabled(self, client, admin_user):
        """Kanban API returns 404 when the plugin is disabled."""
        import db
        db.disable_plugin("kanban")
        _login(client, "admin", "admin123")
        resp = client.get("/api/kanban/tickets/1")
        assert resp.status_code == 404


class TestKanbanAccessControl:
    """Kanban access respects the kanban_access site setting."""

    def test_unauthenticated_user_redirected(self, client, admin_user):
        """Unauthenticated users are redirected to login."""
        _enable_kanban_plugin()
        resp = client.get("/kanban")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_admin_can_access_by_default(self, client, admin_user):
        """Admins can access the kanban board by default."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/kanban")
        assert resp.status_code == 200
        assert b"Kanban Boards" in resp.data

    def test_editor_blocked_by_default(self, client, admin_user, editor_user):
        """Editors cannot access the kanban board when access=admin."""
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "editor", "editor123")
        resp = client.get("/kanban")
        assert resp.status_code == 403

    def test_editor_can_access_when_allowed(self, client, admin_user, editor_user):
        """Editors can access the kanban board when access=editor."""
        _enable_kanban_plugin()
        _set_kanban_access("editor")
        _login(client, "editor", "editor123")
        resp = client.get("/kanban")
        assert resp.status_code == 200

    def test_user_blocked_by_default(self, client, admin_user, regular_user):
        """Regular users cannot access the kanban board when access=admin."""
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "user", "user123")
        resp = client.get("/kanban")
        assert resp.status_code == 403

    def test_user_can_access_when_all(self, client, admin_user, regular_user):
        """Regular users can access the kanban board when access=all."""
        _enable_kanban_plugin()
        _set_kanban_access("all")
        _login(client, "user", "user123")
        resp = client.get("/kanban")
        assert resp.status_code == 200


class TestKanbanBoardCRUD:
    """Tests for creating, reading, editing, and deleting boards."""

    def test_create_board(self, client, admin_user):
        """Admin can create a board."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/kanban/create", data={
            "title": "Sprint Board",
            "description": "Main sprint board",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Sprint Board" in resp.data
        assert b"Board has been successfully created" in resp.data

    def test_create_board_has_default_columns(self, client, admin_user):
        """New board gets three default columns: To Do, In Progress, Done."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/kanban/create", data={
            "title": "Test Board",
        }, follow_redirects=True)
        assert b"To Do" in resp.data
        assert b"In Progress" in resp.data
        assert b"Done" in resp.data

    def test_create_board_empty_title(self, client, admin_user):
        """Board creation with empty title is rejected."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/kanban/create", data={
            "title": "",
        }, follow_redirects=True)
        assert b"Board title is required" in resp.data

    def test_view_board(self, client, admin_user):
        """Board view page loads with columns."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Test", "", admin_user)
        db.kanban_create_column(board_id, "Backlog", 0)
        resp = client.get(f"/kanban/{board_id}")
        assert resp.status_code == 200
        assert b"Test" in resp.data
        assert b"Backlog" in resp.data

    def test_view_nonexistent_board(self, client, admin_user):
        """Viewing a non-existent board returns 404."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/kanban/99999")
        assert resp.status_code == 404

    def test_edit_board(self, client, admin_user):
        """Admin can edit a board's title and description."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Old Title", "", admin_user)
        resp = client.post(f"/kanban/{board_id}/edit", data={
            "title": "New Title",
            "description": "Updated description",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Board has been successfully updated" in resp.data

    def test_delete_board(self, client, admin_user):
        """Admin can delete a board."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("To Delete", "", admin_user)
        resp = client.post(f"/kanban/{board_id}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Board has been successfully deleted" in resp.data

    def test_board_list_page(self, client, admin_user):
        """Board list shows all created boards."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        db.kanban_create_board("Board Alpha", "", admin_user)
        db.kanban_create_board("Board Beta", "", admin_user)
        resp = client.get("/kanban")
        assert b"Board Alpha" in resp.data
        assert b"Board Beta" in resp.data


class TestKanbanWriteAccess:
    """Write operations respect the kanban_write_access setting."""

    def test_read_only_user_cannot_create_board(self, client, admin_user, regular_user):
        """Users with read access but no write access cannot create boards."""
        _enable_kanban_plugin()
        _set_kanban_access("all", "admin")
        _login(client, "user", "user123")
        resp = client.post("/kanban/create", data={
            "title": "Should Fail",
        }, follow_redirects=True)
        assert b"do not have the required permissions" in resp.data

    def test_editor_with_write_access_can_create(self, client, admin_user, editor_user):
        """Editors with write access can create boards."""
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        _login(client, "editor", "editor123")
        resp = client.post("/kanban/create", data={
            "title": "Editor Board",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Board has been successfully created" in resp.data

    def test_user_with_all_write_access_can_create_board(self, client, admin_user, regular_user):
        """Regular users can create boards when global write access is all."""
        _enable_kanban_plugin()
        _set_kanban_access("all", "all")
        _login(client, "user", "user123")
        resp = client.post("/kanban/create", data={
            "title": "User Board",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Board has been successfully created" in resp.data
        assert b"User Board" in resp.data

    def test_editor_with_write_access_can_import_board(self, client, admin_user, editor_user):
        """Editors can import boards when global write access is editor."""
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        _login(client, "editor", "editor123")
        resp = client.post(
            "/kanban/import",
            data={"import_file": (_make_kanban_file("Editor Import"), "editor.kanban.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"successfully imported" in resp.data
        assert b"Editor Import" in resp.data

    def test_user_with_all_write_access_can_import_board(self, client, admin_user, regular_user):
        """Regular users can import boards when global write access is all."""
        _enable_kanban_plugin()
        _set_kanban_access("all", "all")
        _login(client, "user", "user123")
        resp = client.post(
            "/kanban/import",
            data={"import_file": (_make_kanban_file("User Import"), "user.kanban.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"successfully imported" in resp.data
        assert b"User Import" in resp.data

    def test_read_only_user_cannot_import_board(self, client, admin_user, regular_user):
        """Users with read access but no write access cannot import boards."""
        _enable_kanban_plugin()
        _set_kanban_access("all", "admin")
        _login(client, "user", "user123")
        resp = client.post(
            "/kanban/import",
            data={"import_file": (_make_kanban_file("Should Fail"), "blocked.kanban.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"do not have the required permissions" in resp.data

    def test_read_only_no_add_ticket_button(self, client, admin_user, regular_user):
        """Read-only users don't see the add ticket button."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("all", "admin")
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("RO Board", "", admin_user)
        db.kanban_create_column(board_id, "Col", 0)
        client.get("/logout")
        _login(client, "user", "user123")
        resp = client.get(f"/kanban/{board_id}")
        assert resp.status_code == 200
        assert b"kanban-add-ticket-btn" not in resp.data


class TestKanbanColumnAPI:
    """Tests for column CRUD via JSON API."""

    def test_create_column(self, client, admin_user):
        """Admin can create a column via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("API Board", "", admin_user)
        resp = client.post(f"/api/kanban/{board_id}/columns",
                           data=json.dumps({"title": "Backlog"}),
                           content_type="application/json")
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["title"] == "Backlog"
        assert "id" in data

    def test_create_column_empty_title(self, client, admin_user):
        """Column creation with empty title is rejected."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        resp = client.post(f"/api/kanban/{board_id}/columns",
                           data=json.dumps({"title": ""}),
                           content_type="application/json")
        assert resp.status_code == 400

    def test_update_column(self, client, admin_user):
        """Admin can rename a column via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Old Name", 0)
        resp = client.put(f"/api/kanban/columns/{col_id}",
                          data=json.dumps({"title": "New Name"}),
                          content_type="application/json")
        assert resp.status_code == 200

    def test_delete_column(self, client, admin_user):
        """Admin can delete a column via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "To Delete", 0)
        resp = client.delete(f"/api/kanban/columns/{col_id}")
        assert resp.status_code == 200
        assert db.kanban_get_column(col_id) is None

    def test_reorder_columns(self, client, admin_user):
        """Columns can be reordered via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        c1 = db.kanban_create_column(board_id, "A", 0)
        c2 = db.kanban_create_column(board_id, "B", 1)
        resp = client.post(f"/api/kanban/{board_id}/columns/reorder",
                           data=json.dumps({"order": [c2, c1]}),
                           content_type="application/json")
        assert resp.status_code == 200
        cols = db.kanban_list_columns(board_id)
        assert cols[0]["id"] == c2
        assert cols[1]["id"] == c1


class TestKanbanTicketAPI:
    """Tests for ticket CRUD via JSON API."""

    def test_create_ticket(self, client, admin_user):
        """Admin can create a ticket via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        resp = client.post(f"/api/kanban/columns/{col_id}/tickets",
                           data=json.dumps({"title": "Fix bug", "priority": "high"}),
                           content_type="application/json")
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["title"] == "Fix bug"
        assert data["priority"] == "high"

    def test_create_ticket_empty_title(self, client, admin_user):
        """Ticket creation with empty title is rejected."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        resp = client.post(f"/api/kanban/columns/{col_id}/tickets",
                           data=json.dumps({"title": ""}),
                           content_type="application/json")
        assert resp.status_code == 400

    def test_get_ticket(self, client, admin_user):
        """Admin can get ticket details via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "My Ticket", "Desc", admin_user)
        resp = client.get(f"/api/kanban/tickets/{tid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "My Ticket"
        assert data["description"] == "Desc"
        assert "description_html" in data

    def test_get_nonexistent_ticket(self, client, admin_user):
        """Getting a non-existent ticket returns 404."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/api/kanban/tickets/99999")
        assert resp.status_code == 404

    def test_update_ticket(self, client, admin_user):
        """Admin can update a ticket via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "Old", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"title": "New", "priority": "critical"}),
                          content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["title"] == "New"
        assert ticket["priority"] == "critical"

    def test_update_ticket_invalid_priority(self, client, admin_user):
        """Invalid priority value is rejected."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "T", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"priority": "super-urgent"}),
                          content_type="application/json")
        assert resp.status_code == 400

    def test_delete_ticket(self, client, admin_user):
        """Admin can delete a ticket via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "To Delete", "", admin_user)
        resp = client.delete(f"/api/kanban/tickets/{tid}")
        assert resp.status_code == 200
        assert db.kanban_get_ticket(tid) is None

    def test_move_ticket(self, client, admin_user):
        """Admin can move a ticket between columns via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col1 = db.kanban_create_column(board_id, "Todo", 0)
        col2 = db.kanban_create_column(board_id, "Done", 1)
        tid = db.kanban_create_ticket(col1, "Move Me", "", admin_user)
        resp = client.post(f"/api/kanban/tickets/{tid}/move",
                           data=json.dumps({"column_id": col2, "sort_order": 0}),
                           content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["column_id"] == col2

    def test_reorder_tickets(self, client, admin_user):
        """Tickets can be reordered within a column via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        t1 = db.kanban_create_ticket(col_id, "A", "", admin_user, sort_order=0)
        t2 = db.kanban_create_ticket(col_id, "B", "", admin_user, sort_order=1)
        resp = client.post(f"/api/kanban/columns/{col_id}/tickets/reorder",
                           data=json.dumps({"order": [t2, t1]}),
                           content_type="application/json")
        assert resp.status_code == 200
        tickets = db.kanban_list_tickets(col_id)
        assert tickets[0]["id"] == t2
        assert tickets[1]["id"] == t1

    def test_reorder_tickets_does_not_move_across_columns(self, client, admin_user):
        """Reorder API must not silently move tickets from another column."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_a = db.kanban_create_column(board_id, "Todo", 0)
        col_b = db.kanban_create_column(board_id, "Done", 1)
        t1 = db.kanban_create_ticket(col_a, "A", "", admin_user, sort_order=0)
        t2 = db.kanban_create_ticket(col_b, "B", "", admin_user, sort_order=0)
        # Try to reorder col_a including a ticket from col_b
        resp = client.post(f"/api/kanban/columns/{col_a}/tickets/reorder",
                           data=json.dumps({"order": [t2, t1]}),
                           content_type="application/json")
        assert resp.status_code == 200
        # t2 must remain in col_b. It should NOT have been moved to col_a
        tickets_a = db.kanban_list_tickets(col_a)
        tickets_b = db.kanban_list_tickets(col_b)
        assert len(tickets_a) == 1
        assert tickets_a[0]["id"] == t1
        assert len(tickets_b) == 1
        assert tickets_b[0]["id"] == t2

    def test_ticket_assign_user(self, client, admin_user, editor_user):
        """Tickets can be assigned to users who have board access."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "admin")
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "Assign", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": editor_user}),
                          content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["assigned_to"] == editor_user

    def test_ticket_assign_invalid_user(self, client, admin_user):
        """Assigning a ticket to a non-existent user is rejected."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "T", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": "nonexistent"}),
                          content_type="application/json")
        assert resp.status_code == 400


class TestKanbanAdminSettings:
    """Tests for kanban settings in the admin panel."""

    def test_settings_page_shows_kanban_section(self, client, admin_user):
        """Admin settings page shows the kanban settings section."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/global-settings")
        assert resp.status_code == 200
        assert b"Kanban Board" in resp.data
        assert b"kanban_access" in resp.data

    def test_settings_save_kanban_access(self, client, admin_user):
        """Admin can change kanban access level."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        settings = db.get_site_settings()
        # Submit the full settings form with kanban fields
        resp = client.post("/global-settings", data={
            "site_name": settings["site_name"],
            "timezone": "UTC",
            "favicon_type": "yellow",
            "default_theme_mode": "dark",
            "kanban_access": "all",
            "kanban_write_access": "editor",
            "primary_color": settings["primary_color"],
            "secondary_color": settings["secondary_color"],
            "accent_color": settings["accent_color"],
            "text_color": settings["text_color"],
            "sidebar_color": settings["sidebar_color"],
            "bg_color": settings["bg_color"],
            "light_primary_color": settings["light_primary_color"],
            "light_secondary_color": settings["light_secondary_color"],
            "light_accent_color": settings["light_accent_color"],
            "light_text_color": settings["light_text_color"],
            "light_sidebar_color": settings["light_sidebar_color"],
            "light_bg_color": settings["light_bg_color"],
        }, follow_redirects=True)
        assert resp.status_code == 200
        updated = db.get_site_settings()
        assert updated["kanban_access"] == "all"
        assert updated["kanban_write_access"] == "editor"


class TestKanbanSidebarLink:
    """Tests for the kanban sidebar link visibility."""

    def test_sidebar_shows_kanban_for_admin(self, client, admin_user):
        """Sidebar shows kanban link for admins when plugin is enabled."""
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/")
        assert b"Kanban" in resp.data

    def test_sidebar_hidden_when_plugin_disabled(self, client, admin_user):
        """Sidebar hides kanban link when plugin is disabled."""
        import db
        db.disable_plugin("kanban")
        _login(client, "admin", "admin123")
        resp = client.get("/")
        # The word "Kanban" should not appear as a sidebar link
        assert b'Kanban</a>' not in resp.data

    def test_sidebar_hidden_for_user_without_access(self, client, admin_user, regular_user):
        """Sidebar hides kanban link for users without access."""
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "user", "user123")
        resp = client.get("/")
        assert b'Kanban</a>' not in resp.data


class TestKanbanDB:
    """Tests for the db._kanban module functions."""

    def test_board_crud(self, admin_user):
        """Basic board CRUD operations work."""
        import db
        bid = db.kanban_create_board("Test Board", "Desc", admin_user)
        board = db.kanban_get_board(bid)
        assert board is not None
        assert board["title"] == "Test Board"
        db.kanban_update_board(bid, title="Updated")
        board = db.kanban_get_board(bid)
        assert board["title"] == "Updated"
        db.kanban_delete_board(bid)
        assert db.kanban_get_board(bid) is None

    def test_column_crud(self, admin_user):
        """Basic column CRUD operations work."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col1", 0)
        col = db.kanban_get_column(cid)
        assert col["title"] == "Col1"
        db.kanban_update_column(cid, title="Renamed")
        assert db.kanban_get_column(cid)["title"] == "Renamed"
        db.kanban_delete_column(cid)
        assert db.kanban_get_column(cid) is None

    def test_ticket_crud(self, admin_user):
        """Basic ticket CRUD operations work."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "Ticket", "Desc", admin_user, "high")
        ticket = db.kanban_get_ticket(tid)
        assert ticket["title"] == "Ticket"
        assert ticket["priority"] == "high"
        db.kanban_update_ticket(tid, title="Updated")
        assert db.kanban_get_ticket(tid)["title"] == "Updated"
        db.kanban_delete_ticket(tid)
        assert db.kanban_get_ticket(tid) is None

    def test_cascade_delete(self, admin_user):
        """Deleting a board cascades to columns and tickets."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "Ticket", "", admin_user)
        db.kanban_delete_board(bid)
        assert db.kanban_get_column(cid) is None
        assert db.kanban_get_ticket(tid) is None

    def test_move_ticket(self, admin_user):
        """Moving a ticket to a different column works."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        c1 = db.kanban_create_column(bid, "A", 0)
        c2 = db.kanban_create_column(bid, "B", 1)
        tid = db.kanban_create_ticket(c1, "T", "", admin_user)
        db.kanban_move_ticket(tid, c2, 0)
        assert db.kanban_get_ticket(tid)["column_id"] == c2

    def test_count_board_tickets(self, admin_user):
        """Ticket counting across columns works."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        c1 = db.kanban_create_column(bid, "A", 0)
        c2 = db.kanban_create_column(bid, "B", 1)
        db.kanban_create_ticket(c1, "T1", "", admin_user)
        db.kanban_create_ticket(c1, "T2", "", admin_user)
        db.kanban_create_ticket(c2, "T3", "", admin_user)
        assert db.kanban_count_board_tickets(bid) == 3

    def test_list_boards(self, admin_user):
        """Listing boards returns all boards."""
        import db
        db.kanban_create_board("A", "", admin_user)
        db.kanban_create_board("B", "", admin_user)
        boards = db.kanban_list_boards()
        assert len(boards) >= 2

    def test_list_board_tickets(self, admin_user):
        """Listing all tickets for a board works."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        c1 = db.kanban_create_column(bid, "A", 0)
        c2 = db.kanban_create_column(bid, "B", 1)
        db.kanban_create_ticket(c1, "T1", "", admin_user)
        db.kanban_create_ticket(c2, "T2", "", admin_user)
        tickets = db.kanban_list_board_tickets(bid)
        assert len(tickets) == 2

    def test_update_board_invalid_column(self, admin_user):
        """Updating a board with an invalid column raises ValueError."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        with pytest.raises(ValueError):
            db.kanban_update_board(bid, invalid_col="x")


class TestKanbanSharingDB:
    """Tests for the kanban board sharing database functions."""

    def test_set_board_visibility(self, admin_user):
        """Board visibility can be changed."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "private")
        board = db.kanban_get_board(bid)
        assert board["visibility"] == "private"
        db.kanban_set_board_visibility(bid, "shared")
        board = db.kanban_get_board(bid)
        assert board["visibility"] == "shared"

    def test_set_board_visibility_invalid(self, admin_user):
        """Invalid visibility value raises ValueError."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        with pytest.raises(ValueError):
            db.kanban_set_board_visibility(bid, "invalid")

    def test_set_and_get_board_share(self, admin_user):
        """Can set and retrieve board shares."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "view")
        db.kanban_set_board_share(bid, "role", "user", "write")
        shares = db.kanban_get_board_shares(bid)
        assert len(shares) == 2
        targets = {s["target"]: s["access_level"] for s in shares}
        assert targets["editor"] == "view"
        assert targets["user"] == "write"

    def test_set_board_share_upsert(self, admin_user):
        """Setting a share twice updates the access level."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "view")
        db.kanban_set_board_share(bid, "role", "editor", "write")
        shares = db.kanban_get_board_shares(bid)
        assert len(shares) == 1
        assert shares[0]["access_level"] == "write"

    def test_remove_board_share(self, admin_user):
        """Can remove a specific board share."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "view")
        db.kanban_remove_board_share(bid, "role", "editor")
        assert len(db.kanban_get_board_shares(bid)) == 0

    def test_clear_board_shares(self, admin_user):
        """Can clear all shares for a board."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "view")
        db.kanban_set_board_share(bid, "role", "user", "view")
        db.kanban_clear_board_shares(bid)
        assert len(db.kanban_get_board_shares(bid)) == 0

    def test_user_can_view_board_owner(self, admin_user, editor_user):
        """Board creator can always view their board."""
        import db
        bid = db.kanban_create_board("Board", "", editor_user)
        db.kanban_set_board_visibility(bid, "private")
        board = db.kanban_get_board(bid)
        editor = db.get_user_by_id(editor_user)
        assert db.kanban_user_can_view_board(editor, board) is True

    def test_user_can_view_board_public(self, admin_user, regular_user):
        """Anyone can view a public board."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        board = db.kanban_get_board(bid)
        user = db.get_user_by_id(regular_user)
        assert db.kanban_user_can_view_board(user, board) is True

    def test_user_cannot_view_private_board(self, admin_user, regular_user):
        """Non-owner cannot view a private board without shares."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "private")
        board = db.kanban_get_board(bid)
        user = db.get_user_by_id(regular_user)
        assert db.kanban_user_can_view_board(user, board) is False

    def test_user_can_view_shared_board_by_role(self, admin_user, editor_user):
        """User with role share can view a shared board."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "role", "editor", "view")
        board = db.kanban_get_board(bid)
        editor = db.get_user_by_id(editor_user)
        assert db.kanban_user_can_view_board(editor, board) is True

    def test_user_can_view_shared_board_by_user(self, admin_user, regular_user):
        """User with user-specific share can view a shared board."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "private")
        db.kanban_set_board_share(bid, "user", regular_user, "view")
        board = db.kanban_get_board(bid)
        user = db.get_user_by_id(regular_user)
        assert db.kanban_user_can_view_board(user, board) is True

    def test_user_can_write_board_owner(self, editor_user):
        """Board creator can always write their board."""
        import db
        bid = db.kanban_create_board("Board", "", editor_user)
        board = db.kanban_get_board(bid)
        editor = db.get_user_by_id(editor_user)
        assert db.kanban_user_can_write_board(editor, board) is True

    def test_user_cannot_write_view_only_share(self, admin_user, editor_user):
        """User with view-only share cannot write."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "view")
        board = db.kanban_get_board(bid)
        editor = db.get_user_by_id(editor_user)
        assert db.kanban_user_can_write_board(editor, board) is False

    def test_user_can_write_with_write_share(self, admin_user, editor_user):
        """User with write share can write."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "write")
        board = db.kanban_get_board(bid)
        editor = db.get_user_by_id(editor_user)
        assert db.kanban_user_can_write_board(editor, board) is True

    def test_list_boards_for_user_filters_private(self, admin_user, editor_user):
        """list_boards_for_user excludes private boards not shared with user."""
        import db
        bid1 = db.kanban_create_board("Public", "", admin_user)
        bid2 = db.kanban_create_board("Private", "", admin_user)
        db.kanban_set_board_visibility(bid2, "private")
        editor = db.get_user_by_id(editor_user)
        boards = db.kanban_list_boards_for_user(editor)
        board_ids = [b["id"] for b in boards]
        assert bid1 in board_ids
        assert bid2 not in board_ids

    def test_list_boards_for_user_includes_shared(self, admin_user, editor_user):
        """list_boards_for_user includes boards shared with user's role."""
        import db
        bid = db.kanban_create_board("Shared", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "role", "editor", "view")
        editor = db.get_user_by_id(editor_user)
        boards = db.kanban_list_boards_for_user(editor)
        board_ids = [b["id"] for b in boards]
        assert bid in board_ids

    def test_list_boards_for_user_includes_own(self, editor_user):
        """list_boards_for_user always includes boards the user created."""
        import db
        bid = db.kanban_create_board("My Board", "", editor_user)
        db.kanban_set_board_visibility(bid, "private")
        editor = db.get_user_by_id(editor_user)
        boards = db.kanban_list_boards_for_user(editor)
        board_ids = [b["id"] for b in boards]
        assert bid in board_ids

    def test_cascade_delete_clears_shares(self, admin_user):
        """Deleting a board cascades to its shares."""
        import db
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "view")
        db.kanban_delete_board(bid)
        assert len(db.kanban_get_board_shares(bid)) == 0


class TestKanbanSharingAPI:
    """Tests for board settings API endpoints."""

    def test_get_settings_as_owner(self, client, admin_user):
        """Owner can get board settings via API."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        resp = client.get(f"/api/kanban/{bid}/settings")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "visibility" in data
        assert "shares" in data
        assert data["visibility"] == "public"

    def test_get_settings_non_owner_denied(self, client, admin_user, editor_user):
        """Non-owner cannot get board settings."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        _login(client, "editor", "editor123")
        bid = db.kanban_create_board("Board", "", admin_user)
        resp = client.get(f"/api/kanban/{bid}/settings")
        assert resp.status_code == 403

    def test_update_settings_visibility(self, client, admin_user):
        """Owner can update board visibility."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        resp = client.put(f"/api/kanban/{bid}/settings",
                          data=json.dumps({"visibility": "private"}),
                          content_type="application/json")
        assert resp.status_code == 200
        board = db.kanban_get_board(bid)
        assert board["visibility"] == "private"

    def test_update_settings_shares(self, client, admin_user, editor_user):
        """Owner can set sharing rules via API."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        resp = client.put(f"/api/kanban/{bid}/settings",
                          data=json.dumps({
                              "visibility": "shared",
                              "shares": [
                                  {"share_type": "role", "target": "editor", "access_level": "write"},
                                  {"share_type": "user", "target": editor_user, "access_level": "view"},
                              ]
                          }),
                          content_type="application/json")
        assert resp.status_code == 200
        shares = db.kanban_get_board_shares(bid)
        assert len(shares) == 2

    def test_update_settings_replaces_shares(self, client, admin_user):
        """Updating shares replaces all existing shares."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("all")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "view")
        db.kanban_set_board_share(bid, "role", "user", "view")
        resp = client.put(f"/api/kanban/{bid}/settings",
                          data=json.dumps({
                              "shares": [
                                  {"share_type": "role", "target": "editor", "access_level": "write"},
                              ]
                          }),
                          content_type="application/json")
        assert resp.status_code == 200
        shares = db.kanban_get_board_shares(bid)
        assert len(shares) == 1
        assert shares[0]["target"] == "editor"
        assert shares[0]["access_level"] == "write"

    def test_private_board_not_visible_to_others(self, client, admin_user, editor_user):
        """Private board is not visible on the board list for non-owner."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Secret Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "private")
        client.get("/logout")
        _login(client, "editor", "editor123")
        resp = client.get("/kanban")
        assert resp.status_code == 200
        assert b"Secret Board" not in resp.data

    def test_admin_can_view_private_board_owned_by_editor(self, client, admin_user, editor_user):
        """Admins can open a private board created by another user."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Editor Secret Board", "", editor_user)
        db.kanban_set_board_visibility(bid, "private")
        _login(client, "admin", "admin123")
        resp = client.get(f"/kanban/{bid}")
        assert resp.status_code == 200
        assert b"Editor Secret Board" in resp.data
        assert b"\xe2\x9a\x99 Settings" in resp.data
        assert b"Delete Board" in resp.data

    def test_admin_can_invite_users_to_private_board_they_do_not_own(self, client, admin_user, editor_user, regular_user):
        """Admins can invite other users from a private board's settings UI."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Editor Secret Board", "", editor_user)
        db.kanban_set_board_visibility(bid, "private")
        _login(client, "admin", "admin123")
        resp = client.get(f"/kanban/{bid}")
        assert resp.status_code == 200
        regular = db.get_user_by_id(regular_user)
        invite_option = f'<option value="{regular_user}">{regular["username"]} ({regular["role"]})</option>'
        assert invite_option.encode() in resp.data

    def test_shared_board_visible_to_shared_role(self, client, admin_user, editor_user):
        """Board shared with editor role is visible to editors."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Shared Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "role", "editor", "view")
        client.get("/logout")
        _login(client, "editor", "editor123")
        resp = client.get("/kanban")
        assert b"Shared Board" in resp.data

    def test_view_only_user_cannot_write_to_board(self, client, admin_user, editor_user):
        """Editor with view-only share cannot create tickets."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "role", "editor", "view")
        client.get("/logout")
        _login(client, "editor", "editor123")
        resp = client.post(f"/api/kanban/columns/{cid}/tickets",
                           data=json.dumps({"title": "Should Fail"}),
                           content_type="application/json")
        assert resp.status_code == 403

    def test_write_share_user_can_create_tickets(self, client, admin_user, editor_user):
        """Editor with write share can create tickets."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        db.kanban_set_board_share(bid, "role", "editor", "write")
        client.get("/logout")
        _login(client, "editor", "editor123")
        resp = client.post(f"/api/kanban/columns/{cid}/tickets",
                           data=json.dumps({"title": "Should Work"}),
                           content_type="application/json")
        assert resp.status_code == 201

    def test_only_owner_can_delete_board(self, client, admin_user, editor_user):
        """Only the owner (or admin) can delete a board."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        _login(client, "editor", "editor123")
        bid = db.kanban_create_board("Editor Board", "", editor_user)
        # Editor is the owner, so they can delete
        resp = client.post(f"/kanban/{bid}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Board has been successfully deleted" in resp.data

    def test_non_owner_cannot_delete_board(self, client, admin_user, editor_user):
        """Non-owner editor cannot delete a board even with write share."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Admin Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "write")
        _login(client, "editor", "editor123")
        resp = client.post(f"/kanban/{bid}/delete", follow_redirects=True)
        assert b"do not have the required permissions" in resp.data
        assert db.kanban_get_board(bid) is not None


class TestKanbanTicketMetadata:
    """Tests for ticket due_date and color label fields."""

    def test_create_ticket_with_due_date(self, client, admin_user):
        """Ticket can be created with a due date."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Todo", 0)
        resp = client.post(f"/api/kanban/columns/{cid}/tickets",
                           data=json.dumps({"title": "Task", "due_date": "2026-04-01"}),
                           content_type="application/json")
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["due_date"] == "2026-04-01"

    def test_update_ticket_due_date(self, client, admin_user):
        """Ticket due date can be updated."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Todo", 0)
        tid = db.kanban_create_ticket(cid, "Task", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"due_date": "2026-05-15"}),
                          content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["due_date"] == "2026-05-15"

    def test_clear_ticket_due_date(self, client, admin_user):
        """Ticket due date can be cleared."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Todo", 0)
        tid = db.kanban_create_ticket(cid, "Task", "", admin_user)
        db.kanban_update_ticket(tid, due_date="2026-05-15")
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"due_date": ""}),
                          content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["due_date"] is None

    def test_update_ticket_color(self, client, admin_user):
        """Ticket color label can be updated."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Todo", 0)
        tid = db.kanban_create_ticket(cid, "Task", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"color": "#e91e63"}),
                          content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["color"] == "#e91e63"

    def test_get_ticket_includes_new_fields(self, client, admin_user):
        """GET ticket API includes due_date and color fields."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Todo", 0)
        tid = db.kanban_create_ticket(cid, "Task", "", admin_user)
        db.kanban_update_ticket(tid, due_date="2026-06-01", color="#4caf50")
        resp = client.get(f"/api/kanban/tickets/{tid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["due_date"] == "2026-06-01"
        assert data["color"] == "#4caf50"

    def test_create_ticket_shorthand_sets_metadata_and_labels(self, client, admin_user, editor_user):
        """Title shorthand can populate assignees, labels, priority, color, and due date."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Todo", 0)
        resp = client.post(
            f"/api/kanban/columns/{cid}/tickets",
            data=json.dumps({
                "title": "Ship docs @editor +release +docs !high color:red due:tomorrow"
            }),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["title"] == "Ship docs"
        assert data["priority"] == "high"
        assert data["color"] == "#e91e63"
        assert data["labels"] == ["release", "docs"]
        assert data["due_date"] == (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        assert any(a["username"] == "editor" for a in data["assignees"])

    def test_update_ticket_title_shorthand_sets_labels(self, client, admin_user):
        """Updating the title with shorthand persists parsed labels and metadata."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Todo", 0)
        tid = db.kanban_create_ticket(cid, "Task", "", admin_user)
        resp = client.put(
            f"/api/kanban/tickets/{tid}",
            data=json.dumps({"title": "Task +backend +api !critical color:blue due:2026-07-01"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["title"] == "Task"
        assert ticket["priority"] == "critical"
        assert ticket["color"] == "#2196f3"
        assert ticket["due_date"] == "2026-07-01"
        resp = client.get(f"/api/kanban/tickets/{tid}")
        data = resp.get_json()
        assert data["labels"] == ["backend", "api"]


class TestKanbanTicketDescriptionAndOptions:
    """Tests for ticket description rendering and option save/load cycle."""

    def test_description_html_rendered(self, client, admin_user):
        """GET ticket API returns rendered Markdown HTML in description_html."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "**bold** text", admin_user)
        resp = client.get(f"/api/kanban/tickets/{tid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "<strong>bold</strong>" in data["description_html"]

    def test_empty_description_returns_empty_html(self, client, admin_user):
        """GET ticket API returns empty description_html for empty description."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        resp = client.get(f"/api/kanban/tickets/{tid}")
        data = resp.get_json()
        assert "description_html" in data
        assert data["description_html"] is not None
        # Empty description renders to empty or whitespace-only HTML
        assert data["description_html"].strip() == ""

    def test_description_with_markdown_image(self, client, admin_user):
        """Markdown image syntax in description is rendered as img tag."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "![alt](/img.png)", admin_user)
        resp = client.get(f"/api/kanban/tickets/{tid}")
        data = resp.get_json()
        assert "<img" in data["description_html"]
        assert 'alt="alt"' in data["description_html"]

    def test_color_persists_across_reads(self, client, admin_user):
        """Color set via PUT is returned on subsequent GET."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        client.put(f"/api/kanban/tickets/{tid}",
                   data=json.dumps({"color": "#ff9800"}),
                   content_type="application/json")
        resp = client.get(f"/api/kanban/tickets/{tid}")
        assert resp.get_json()["color"] == "#ff9800"

    def test_assignee_persists_across_reads(self, client, admin_user):
        """Assigned user set via PUT is returned on subsequent GET."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        client.put(f"/api/kanban/tickets/{tid}",
                   data=json.dumps({"assigned_to": admin_user}),
                   content_type="application/json")
        resp = client.get(f"/api/kanban/tickets/{tid}")
        data = resp.get_json()
        assert data["assigned_to"] == admin_user
        assert data["assigned_to_username"] == "admin"

    def test_clear_color_persists(self, client, admin_user):
        """Setting color to empty string clears the color."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        # Set color
        client.put(f"/api/kanban/tickets/{tid}",
                   data=json.dumps({"color": "#e91e63"}),
                   content_type="application/json")
        # Clear color
        client.put(f"/api/kanban/tickets/{tid}",
                   data=json.dumps({"color": ""}),
                   content_type="application/json")
        resp = client.get(f"/api/kanban/tickets/{tid}")
        assert resp.get_json()["color"] == ""

    def test_board_page_renders_ticket_color(self, client, admin_user):
        """Board page renders ticket with color style attribute."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        db.kanban_update_ticket(tid, color="#2196f3")
        resp = client.get(f"/kanban/{bid}")
        assert resp.status_code == 200
        assert b"border-left:3px solid #2196f3" in resp.data or b'class="kanban-ticket-color-bar" style="background:#2196f3"' in resp.data


class TestKanbanAssigneeValidation:
    """Users can only be assigned to a task if they have access to the board."""

    def test_assign_user_without_board_access_rejected_on_update(self, client, admin_user, regular_user):
        """Updating a ticket to assign a user without board access is rejected."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        db.kanban_set_board_visibility(bid, "private")
        # Regular user has no access to this board
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": regular_user}),
                          content_type="application/json")
        assert resp.status_code == 400
        assert b"does not have access" in resp.data

    def test_assign_admin_always_succeeds(self, client, admin_user):
        """Admins can always be assigned because they always have board access."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": admin_user}),
                          content_type="application/json")
        assert resp.status_code == 200

    def test_assign_user_with_board_access_succeeds(self, client, admin_user, editor_user):
        """A user with explicit board access can be assigned."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "role", "editor", "view")
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": editor_user}),
                          content_type="application/json")
        assert resp.status_code == 200

    def test_assign_on_create_ignored_if_no_access(self, client, admin_user, regular_user):
        """Assigning a user without access during ticket creation silently ignores it."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        db.kanban_set_board_visibility(bid, "private")
        resp = client.post(f"/api/kanban/columns/{cid}/tickets",
                           data=json.dumps({"title": "Task", "assigned_to": regular_user}),
                           content_type="application/json")
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["assigned_to"] is None

    def test_assign_individually_invited_user_succeeds(self, client, admin_user, regular_user):
        """A user individually invited to a board can be assigned to its tickets."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "user", regular_user, "view")
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": regular_user}),
                          content_type="application/json")
        assert resp.status_code == 200

    def test_unassign_always_succeeds(self, client, admin_user):
        """Clearing the assignee (setting to null) always succeeds."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        db.kanban_update_ticket(tid, assigned_to=admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": ""}),
                          content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["assigned_to"] is None


class TestKanbanGlobalAccessSync:
    """Changing global kanban_access revokes role shares and cleans up assignees."""

    def test_revoke_role_shares_on_access_change(self, client, admin_user, editor_user):
        """Changing kanban_access from 'all' to 'admin' revokes editor role shares."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("all")
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "role", "editor", "write")
        db.kanban_set_board_share(bid, "role", "user", "view")
        assert len(db.kanban_get_board_shares(bid)) == 2
        db.kanban_revoke_role_shares_for_restricted_roles("admin")
        shares = db.kanban_get_board_shares(bid)
        # Both editor and user role shares should be removed
        assert len(shares) == 0

    def test_remove_all_invalid_assignees_on_access_change(self, client, admin_user, editor_user):
        """Assignees who lose board access through global settings are removed."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "role", "editor", "write")
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        db.kanban_update_ticket(tid, assigned_to=editor_user)
        # Revoke editor role shares (simulate changing kanban_access to admin)
        db.kanban_revoke_role_shares_for_restricted_roles("admin")
        db.kanban_remove_all_invalid_assignees("admin")
        ticket = db.kanban_get_ticket(tid)
        assert ticket["assigned_to"] is None

    def test_admin_assignee_survives_global_access_change(self, client, admin_user, editor_user):
        """Admin assignees are not removed when global access changes."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        tid = db.kanban_create_ticket(cid, "T", "", admin_user)
        db.kanban_update_ticket(tid, assigned_to=admin_user)
        db.kanban_revoke_role_shares_for_restricted_roles("admin")
        db.kanban_remove_all_invalid_assignees("admin")
        ticket = db.kanban_get_ticket(tid)
        assert ticket["assigned_to"] == admin_user


class TestKanbanIndividualInvites:
    """Users individually invited to boards can access them regardless of role."""

    def test_individually_invited_user_can_access_kanban(self, client, admin_user, regular_user):
        """User without global access but with a board invite can access kanban."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        bid = db.kanban_create_board("Invited Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "user", regular_user, "view")
        _login(client, "user", "user123")
        resp = client.get("/kanban")
        assert resp.status_code == 200
        assert b"Invited Board" in resp.data

    def test_individually_invited_user_cannot_create_boards(self, client, admin_user, regular_user):
        """Individually invited users cannot create new boards."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "user", regular_user, "view")
        _login(client, "user", "user123")
        resp = client.post("/kanban/create", data={"title": "Nope"},
                           follow_redirects=True)
        assert b"do not have the required permissions" in resp.data

    def test_individually_invited_user_sees_only_shared_boards(self, client, admin_user, regular_user):
        """Individually invited users only see boards they are explicitly shared on."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        bid1 = db.kanban_create_board("Shared Board", "", admin_user)
        db.kanban_create_board("Not Shared Board", "", admin_user)
        db.kanban_set_board_share(bid1, "user", regular_user, "view")
        _login(client, "user", "user123")
        resp = client.get("/kanban")
        assert b"Shared Board" in resp.data
        assert b"Not Shared Board" not in resp.data

    def test_removing_all_shares_revokes_kanban_access(self, client, admin_user, regular_user):
        """User loses kanban access when removed from all boards."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_share(bid, "user", regular_user, "view")
        # Verify user has access
        _login(client, "user", "user123")
        resp = client.get("/kanban")
        assert resp.status_code == 200
        client.get("/logout")
        # Remove the share
        db.kanban_remove_board_share(bid, "user", regular_user)
        _login(client, "user", "user123")
        resp = client.get("/kanban")
        assert resp.status_code == 403

    def test_individual_user_share_write_can_create_tickets(self, client, admin_user, regular_user):
        """User with individual write share can create tickets."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        bid = db.kanban_create_board("Board", "", admin_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        db.kanban_set_board_share(bid, "user", regular_user, "write")
        _login(client, "user", "user123")
        resp = client.post(f"/api/kanban/columns/{cid}/tickets",
                           data=json.dumps({"title": "My Task"}),
                           content_type="application/json")
        assert resp.status_code == 201


class TestKanbanReadWriteSync:
    """Granting write access automatically grants read access."""

    def test_write_share_grants_view_access(self, client, admin_user, editor_user):
        """A user with write-only share can still view the board."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor")
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "role", "editor", "write")
        _login(client, "editor", "editor123")
        resp = client.get(f"/kanban/{bid}")
        assert resp.status_code == 200

    def test_write_user_share_grants_view(self, client, admin_user, regular_user):
        """A user with individual write share can view the board."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        bid = db.kanban_create_board("Board", "", admin_user)
        db.kanban_set_board_visibility(bid, "shared")
        db.kanban_set_board_share(bid, "user", regular_user, "write")
        _login(client, "user", "user123")
        resp = client.get(f"/kanban/{bid}")
        assert resp.status_code == 200


class TestKanbanAdminOverride:
    """Admins can see, edit, delete, and manage settings for all boards."""

    def test_admin_sees_all_boards(self, client, admin_user, editor_user):
        """Admin sees all boards including private ones owned by others."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Editor Private", "", editor_user)
        db.kanban_set_board_visibility(bid, "private")
        _login(client, "admin", "admin123")
        resp = client.get("/kanban")
        assert b"Editor Private" in resp.data

    def test_admin_can_edit_any_board(self, client, admin_user, editor_user):
        """Admin can edit a board created by another user."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Editor Board", "", editor_user)
        _login(client, "admin", "admin123")
        resp = client.post(f"/kanban/{bid}/edit", data={
            "title": "Admin Edited",
            "description": "Changed by admin",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Board has been successfully updated" in resp.data

    def test_admin_can_delete_any_board(self, client, admin_user, editor_user):
        """Admin can delete a board created by another user."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Editor Board", "", editor_user)
        _login(client, "admin", "admin123")
        resp = client.post(f"/kanban/{bid}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Board has been successfully deleted" in resp.data
        assert db.kanban_get_board(bid) is None

    def test_admin_can_update_settings_of_any_board(self, client, admin_user, editor_user):
        """Admin can update sharing settings on a board they don't own."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Editor Board", "", editor_user)
        _login(client, "admin", "admin123")
        resp = client.put(f"/api/kanban/{bid}/settings",
                          data=json.dumps({"visibility": "public"}),
                          content_type="application/json")
        assert resp.status_code == 200
        board = db.kanban_get_board(bid)
        assert board["visibility"] == "public"

    def test_admin_can_write_to_any_board(self, client, admin_user, editor_user):
        """Admin can create tickets on any board."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        bid = db.kanban_create_board("Editor Board", "", editor_user)
        cid = db.kanban_create_column(bid, "Col", 0)
        _login(client, "admin", "admin123")
        resp = client.post(f"/api/kanban/columns/{cid}/tickets",
                           data=json.dumps({"title": "Admin Task"}),
                           content_type="application/json")
        assert resp.status_code == 201


class TestKanbanRoleShareRestrictions:
    """Role shares are only allowed for roles with global kanban access."""

    def test_editor_role_share_blocked_when_access_admin_only(self, client, admin_user):
        """Cannot add editor role share when kanban_access is admin-only."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        resp = client.put(f"/api/kanban/{bid}/settings",
                          data=json.dumps({
                              "shares": [
                                  {"share_type": "role", "target": "editor", "access_level": "write"},
                              ]
                          }),
                          content_type="application/json")
        assert resp.status_code == 200
        shares = db.kanban_get_board_shares(bid)
        # Editor role share should be silently ignored
        assert len(shares) == 0

    def test_user_share_allowed_regardless_of_global_access(self, client, admin_user, regular_user):
        """User-specific shares are allowed even when global access is restricted."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin")
        _login(client, "admin", "admin123")
        bid = db.kanban_create_board("Board", "", admin_user)
        resp = client.put(f"/api/kanban/{bid}/settings",
                          data=json.dumps({
                              "shares": [
                                  {"share_type": "user", "target": regular_user, "access_level": "view"},
                              ]
                          }),
                          content_type="application/json")
        assert resp.status_code == 200
        shares = db.kanban_get_board_shares(bid)
        assert len(shares) == 1
        assert shares[0]["share_type"] == "user"


class TestKanbanBoardAccessEnforcement:
    """Verify that read-only ticket APIs enforce per-board access checks.

    A user who has global kanban access should NOT be able to read tickets,
    attachments, history, or comments from a private board they are not
    shared on.
    """

    def _setup_boards(self, admin_user, editor_user):
        """Create two boards: one shared with editor, one private to admin."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("all", "all")
        # Board A: shared with editor
        board_a = db.kanban_create_board("Board A", "", admin_user)
        db.kanban_set_board_visibility(board_a, "private")
        db.kanban_set_board_share(board_a, "user", editor_user, "view")
        col_a = db.kanban_create_column(board_a, "Col A", 0)
        ticket_a = db.kanban_create_ticket(col_a, "Ticket A", "Desc A", admin_user)
        # Board B: private, NOT shared with editor
        board_b = db.kanban_create_board("Board B", "", admin_user)
        db.kanban_set_board_visibility(board_b, "private")
        col_b = db.kanban_create_column(board_b, "Col B", 0)
        ticket_b = db.kanban_create_ticket(col_b, "Ticket B", "Desc B", admin_user)
        return board_a, ticket_a, board_b, ticket_b

    def test_get_ticket_denied_for_unshared_board(self, client, admin_user, editor_user):
        """GET /api/kanban/tickets/<id> returns 403 for tickets on unshared boards."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        _login(client, "editor", "editor123")
        resp = client.get(f"/api/kanban/tickets/{ticket_b}")
        assert resp.status_code == 403

    def test_get_ticket_allowed_for_shared_board(self, client, admin_user, editor_user):
        """GET /api/kanban/tickets/<id> returns 200 for tickets on shared boards."""
        import db
        _ba, ticket_a, _bb, _tb = self._setup_boards(admin_user, editor_user)
        _login(client, "editor", "editor123")
        resp = client.get(f"/api/kanban/tickets/{ticket_a}")
        assert resp.status_code == 200

    def test_list_attachments_denied_for_unshared_board(self, client, admin_user, editor_user):
        """GET /api/kanban/tickets/<id>/attachments returns 403 for unshared boards."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        _login(client, "editor", "editor123")
        resp = client.get(f"/api/kanban/tickets/{ticket_b}/attachments")
        assert resp.status_code == 403

    def test_ticket_history_denied_for_unshared_board(self, client, admin_user, editor_user):
        """GET /api/kanban/tickets/<id>/history returns 403 for unshared boards."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        _login(client, "editor", "editor123")
        resp = client.get(f"/api/kanban/tickets/{ticket_b}/history")
        assert resp.status_code == 403

    def test_list_comments_denied_for_unshared_board(self, client, admin_user, editor_user):
        """GET /api/kanban/tickets/<id>/comments returns 403 for unshared boards."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        _login(client, "editor", "editor123")
        resp = client.get(f"/api/kanban/tickets/{ticket_b}/comments")
        assert resp.status_code == 403

    def test_history_entry_denied_for_unshared_board(self, client, admin_user, editor_user):
        """GET /api/kanban/history/<id> returns 403 for entries on unshared boards."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        # Create a history entry on the unshared board's ticket
        db.kanban_add_ticket_history_entry(ticket_b, "old", "new", admin_user)
        entries = db.kanban_list_ticket_history(ticket_b)
        assert len(entries) >= 1
        entry_id = entries[0]["id"]
        _login(client, "editor", "editor123")
        resp = client.get(f"/api/kanban/history/{entry_id}")
        assert resp.status_code == 403

    def test_admin_can_always_view_private_tickets(self, client, admin_user, editor_user):
        """Admins bypass per-board access checks on ticket read endpoints."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        _login(client, "admin", "admin123")
        resp = client.get(f"/api/kanban/tickets/{ticket_b}")
        assert resp.status_code == 200

    def test_update_comment_denied_for_unshared_board(self, client, admin_user, editor_user):
        """PUT /api/kanban/comments/<id> returns 403 for comments on unshared boards."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        comment_id = db.kanban_add_ticket_comment(ticket_b, admin_user, "secret")
        _login(client, "editor", "editor123")
        resp = client.put(f"/api/kanban/comments/{comment_id}",
                          data=json.dumps({"content": "hacked"}),
                          content_type="application/json")
        assert resp.status_code == 403

    def test_delete_comment_denied_for_unshared_board(self, client, admin_user, editor_user):
        """DELETE /api/kanban/comments/<id> returns 403 for comments on unshared boards."""
        import db
        _ba, _ta, _bb, ticket_b = self._setup_boards(admin_user, editor_user)
        comment_id = db.kanban_add_ticket_comment(ticket_b, admin_user, "secret")
        _login(client, "editor", "editor123")
        resp = client.delete(f"/api/kanban/comments/{comment_id}")
        assert resp.status_code == 403

    def test_update_comment_allowed_for_shared_board(self, client, admin_user, editor_user):
        """PUT /api/kanban/comments/<id> returns 200 for own comments on shared boards."""
        import db
        _ba, ticket_a, _bb, _tb = self._setup_boards(admin_user, editor_user)
        comment_id = db.kanban_add_ticket_comment(ticket_a, editor_user, "my comment")
        _login(client, "editor", "editor123")
        resp = client.put(f"/api/kanban/comments/{comment_id}",
                          data=json.dumps({"content": "updated"}),
                          content_type="application/json")
        assert resp.status_code == 200

    def test_delete_comment_allowed_for_shared_board(self, client, admin_user, editor_user):
        """DELETE /api/kanban/comments/<id> returns 200 for own comments on shared boards."""
        import db
        _ba, ticket_a, _bb, _tb = self._setup_boards(admin_user, editor_user)
        comment_id = db.kanban_add_ticket_comment(ticket_a, editor_user, "my comment")
        _login(client, "editor", "editor123")
        resp = client.delete(f"/api/kanban/comments/{comment_id}")
        assert resp.status_code == 200


class TestKanbanAttachmentCleanup:
    """Deleting a column or ticket removes orphaned attachment files on disk."""

    def test_delete_ticket_removes_attachment_files(self, client, admin_user):
        """Deleting a ticket via API removes its attachment files from disk."""
        import os
        import config
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Col", 0)
        tid = db.kanban_create_ticket(col_id, "Ticket", "", admin_user)
        # Create a fake attachment file on disk and a DB record.
        os.makedirs(config.KANBAN_ATTACHMENT_FOLDER, exist_ok=True)
        fake_file = "deadbeef.txt"
        fpath = os.path.join(config.KANBAN_ATTACHMENT_FOLDER, fake_file)
        with open(fpath, "w") as f:
            f.write("test")
        db.kanban_add_ticket_attachment(tid, fake_file, "readme.txt", 4, admin_user)
        assert os.path.isfile(fpath)
        resp = client.delete(f"/api/kanban/tickets/{tid}")
        assert resp.status_code == 200
        assert not os.path.isfile(fpath)

    def test_delete_column_removes_attachment_files(self, client, admin_user):
        """Deleting a column via API removes attachment files of its tickets."""
        import os
        import config
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Col", 0)
        t1 = db.kanban_create_ticket(col_id, "T1", "", admin_user)
        t2 = db.kanban_create_ticket(col_id, "T2", "", admin_user)
        os.makedirs(config.KANBAN_ATTACHMENT_FOLDER, exist_ok=True)
        files = []
        for i, tid in enumerate([t1, t2]):
            fname = f"col_attach_{i}.txt"
            fpath = os.path.join(config.KANBAN_ATTACHMENT_FOLDER, fname)
            with open(fpath, "w") as f:
                f.write("data")
            db.kanban_add_ticket_attachment(tid, fname, f"file{i}.txt", 4, admin_user)
            files.append(fpath)
        for fp in files:
            assert os.path.isfile(fp)
        resp = client.delete(f"/api/kanban/columns/{col_id}")
        assert resp.status_code == 200
        for fp in files:
            assert not os.path.isfile(fp)

    def test_delete_ticket_db_returns_filenames(self, admin_user):
        """DB delete_ticket returns the list of attachment filenames."""
        import os
        import config
        import db
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Col", 0)
        tid = db.kanban_create_ticket(col_id, "Ticket", "", admin_user)
        db.kanban_add_ticket_attachment(tid, "f1.txt", "a.txt", 1, admin_user)
        db.kanban_add_ticket_attachment(tid, "f2.txt", "b.txt", 2, admin_user)
        result = db.kanban_delete_ticket(tid)
        assert set(result) == {"f1.txt", "f2.txt"}

    def test_delete_column_db_returns_filenames(self, admin_user):
        """DB delete_column returns the list of attachment filenames."""
        import db
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Col", 0)
        t1 = db.kanban_create_ticket(col_id, "T1", "", admin_user)
        t2 = db.kanban_create_ticket(col_id, "T2", "", admin_user)
        db.kanban_add_ticket_attachment(t1, "a.txt", "a.txt", 1, admin_user)
        db.kanban_add_ticket_attachment(t2, "b.txt", "b.txt", 1, admin_user)
        result = db.kanban_delete_column(col_id)
        assert set(result) == {"a.txt", "b.txt"}

    def test_delete_ticket_no_attachments_returns_empty(self, admin_user):
        """DB delete_ticket returns empty list when ticket has no attachments."""
        import db
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Col", 0)
        tid = db.kanban_create_ticket(col_id, "T", "", admin_user)
        result = db.kanban_delete_ticket(tid)
        assert result == []

    def test_delete_column_no_attachments_returns_empty(self, admin_user):
        """DB delete_column returns empty list when no tickets have attachments."""
        import db
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Col", 0)
        db.kanban_create_ticket(col_id, "T", "", admin_user)
        result = db.kanban_delete_column(col_id)
        assert result == []


class TestKanbanCreatorDisplay:
    """Tests that board creator/owner is displayed correctly."""

    def test_board_list_shows_creator(self, client, admin_user):
        """Board list page shows the board creator's username."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        db.kanban_create_board("Test Board", "", admin_user)
        resp = client.get("/kanban")
        assert resp.status_code == 200
        assert b"admin" in resp.data

    def test_board_view_shows_owner(self, client, admin_user):
        """Board view page shows the board owner."""
        import db
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Test Board", "", admin_user)
        resp = client.get(f"/kanban/{board_id}")
        assert resp.status_code == 200
        assert b"Owner:" in resp.data
        assert b"admin" in resp.data

    def test_list_boards_for_user_includes_creator_username(self, admin_user):
        """list_boards_for_user returns creator_username column."""
        import db
        _enable_kanban_plugin()
        db.kanban_create_board("Board A", "", admin_user)
        user = db.get_user_by_id(admin_user)
        boards = db.kanban_list_boards_for_user(user)
        assert len(boards) >= 1
        assert "creator_username" in boards[0].keys()
        assert boards[0]["creator_username"] == "admin"

    def test_list_boards_for_individual_user_includes_creator_username(
        self, admin_user, regular_user
    ):
        """list_boards_for_individual_user returns creator_username column."""
        import db
        _enable_kanban_plugin()
        board_id = db.kanban_create_board("Shared Board", "", admin_user)
        db.kanban_set_board_share(board_id, "user", regular_user, "view")
        boards = db.kanban_list_boards_for_individual_user(regular_user)
        assert len(boards) == 1
        assert "creator_username" in boards[0].keys()
        assert boards[0]["creator_username"] == "admin"


class TestKanbanSettingsAPIGlobalInfo:
    """Board settings API returns global kanban settings for frontend sync."""

    def test_settings_api_returns_global_kanban_access(self, client, admin_user):
        """Settings GET API returns kanban_access field."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "admin")
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        resp = client.get(f"/api/kanban/{board_id}/settings")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["kanban_access"] == "editor"
        assert data["kanban_write_access"] == "admin"

    def test_settings_api_returns_global_kanban_write_access(self, client, admin_user):
        """Settings GET API returns kanban_write_access field."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("all", "all")
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        resp = client.get(f"/api/kanban/{board_id}/settings")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["kanban_access"] == "all"
        assert data["kanban_write_access"] == "all"


class TestKanbanAssigneeGlobalAccess:
    """Assignee validation considers global kanban access."""

    def test_assign_user_without_global_access_rejected(self, client, admin_user, editor_user):
        """Assigning a user without global kanban access is rejected."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin", "admin")  # only admins
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "T", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": editor_user}),
                          content_type="application/json")
        assert resp.status_code == 400
        assert b"does not have access" in resp.data

    def test_assign_user_with_global_access_allowed(self, client, admin_user, editor_user):
        """Assigning a user with global kanban access succeeds."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "admin")  # editors have access
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "T", "", admin_user)
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": editor_user}),
                          content_type="application/json")
        assert resp.status_code == 200

    def test_assign_individually_shared_user_allowed(self, client, admin_user, regular_user):
        """Users with individual board shares can be assigned even without global access."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("admin", "admin")  # only admins
        _login(client, "admin", "admin123")
        board_id = db.kanban_create_board("Board", "", admin_user)
        col_id = db.kanban_create_column(board_id, "Todo", 0)
        tid = db.kanban_create_ticket(col_id, "T", "", admin_user)
        # Share the board with the regular user individually
        db.kanban_set_board_share(board_id, "user", regular_user, "view")
        resp = client.put(f"/api/kanban/tickets/{tid}",
                          data=json.dumps({"assigned_to": regular_user}),
                          content_type="application/json")
        assert resp.status_code == 200
        ticket = db.kanban_get_ticket(tid)
        assert ticket["assigned_to"] == regular_user

    def test_user_has_board_access_checks_global(self, admin_user, editor_user):
        """user_has_board_access respects kanban_access parameter."""
        import db
        _enable_kanban_plugin()
        board_id = db.kanban_create_board("Board", "", admin_user)
        # Editor without global access should be denied
        assert db.kanban_user_has_board_access(
            editor_user, board_id, kanban_access="admin"
        ) is False
        # Editor with global access should be allowed (public board)
        assert db.kanban_user_has_board_access(
            editor_user, board_id, kanban_access="editor"
        ) is True

    def test_board_creator_always_has_access(self, admin_user, editor_user):
        """Board creator always has access regardless of global settings."""
        import db
        _enable_kanban_plugin()
        _set_kanban_access("editor", "editor")
        board_id = db.kanban_create_board("Editor Board", "", editor_user)
        # Even if global access changes to admin-only, creator still has access
        assert db.kanban_user_has_board_access(
            editor_user, board_id, kanban_access="admin"
        ) is True


class TestKanbanBulkAPI:
    """Tests for the bulk ticket / column action endpoints."""

    def _setup_board(self, admin_user, n_tickets=3, n_columns=2):
        import db
        _enable_kanban_plugin()
        board_id = db.kanban_create_board("Bulk Board", "", admin_user)
        col_ids = [
            db.kanban_create_column(board_id, f"Col {i}", i)
            for i in range(n_columns)
        ]
        ticket_ids = [
            db.kanban_create_ticket(col_ids[0], f"T{i}", "", admin_user)
            for i in range(n_tickets)
        ]
        return board_id, col_ids, ticket_ids

    def test_bulk_assign_updates_all_tickets(self, client, admin_user, editor_user):
        """Bulk assign sets the same assignees on every selected ticket."""
        import db
        _set_kanban_access("editor", "admin")
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "assign",
                "ticket_ids": ticket_ids,
                "assignees": [editor_user],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["updated"] == len(ticket_ids)
        for tid in ticket_ids:
            ids = db.kanban_list_ticket_assignee_ids(tid)
            assert ids == [editor_user]

    def test_bulk_assign_clears_when_empty_list(self, client, admin_user, editor_user):
        """An empty assignees list clears all assignees on selected tickets."""
        import db
        _set_kanban_access("editor", "admin")
        board_id, _, ticket_ids = self._setup_board(admin_user)
        for tid in ticket_ids:
            db.kanban_set_ticket_assignees(tid, [editor_user])
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "assign",
                "ticket_ids": ticket_ids,
                "assignees": [],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for tid in ticket_ids:
            assert db.kanban_list_ticket_assignee_ids(tid) == []

    def test_bulk_assign_rejects_unknown_user(self, client, admin_user):
        """Bulk assign returns 400 if any assignee does not exist."""
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "assign",
                "ticket_ids": ticket_ids,
                "assignees": ["nonexistent"],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bulk_priority_sets_value(self, client, admin_user):
        """Bulk priority updates priority on every selected ticket."""
        import db
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "priority",
                "ticket_ids": ticket_ids,
                "priority": "high",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for tid in ticket_ids:
            assert db.kanban_get_ticket(tid)["priority"] == "high"

    def test_bulk_priority_rejects_invalid(self, client, admin_user):
        """Bulk priority rejects values outside the allow-list."""
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "priority",
                "ticket_ids": ticket_ids,
                "priority": "extreme",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bulk_move_changes_column(self, client, admin_user):
        """Bulk move relocates tickets to the target column."""
        import db
        board_id, col_ids, ticket_ids = self._setup_board(admin_user)
        target = col_ids[1]
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "move",
                "ticket_ids": ticket_ids,
                "column_id": target,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for tid in ticket_ids:
            assert db.kanban_get_ticket(tid)["column_id"] == target

    def test_bulk_move_rejects_foreign_column(self, client, admin_user):
        """Bulk move rejects a target column that belongs to another board."""
        import db
        board_id, _, ticket_ids = self._setup_board(admin_user)
        other_board = db.kanban_create_board("Other", "", admin_user)
        other_col = db.kanban_create_column(other_board, "Other", 0)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "move",
                "ticket_ids": ticket_ids,
                "column_id": other_col,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_bulk_color_sets_hex(self, client, admin_user):
        """Bulk color updates the color on every selected ticket."""
        import db
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "color",
                "ticket_ids": ticket_ids,
                "color": "#4caf50",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for tid in ticket_ids:
            assert db.kanban_get_ticket(tid)["color"] == "#4caf50"

    def test_bulk_color_rejects_invalid_hex(self, client, admin_user):
        """Bulk color rejects malformed colour values."""
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "color",
                "ticket_ids": ticket_ids,
                "color": "red",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bulk_due_date_sets_value(self, client, admin_user):
        """Bulk due_date updates the due_date on every selected ticket."""
        import db
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "due_date",
                "ticket_ids": ticket_ids,
                "due_date": "2030-01-15",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for tid in ticket_ids:
            assert db.kanban_get_ticket(tid)["due_date"] == "2030-01-15"

    def test_bulk_due_date_clears_with_blank(self, client, admin_user):
        """Sending an empty due_date clears the due date on selected tickets."""
        import db
        board_id, _, ticket_ids = self._setup_board(admin_user)
        for tid in ticket_ids:
            db.kanban_update_ticket(tid, due_date="2030-01-15")
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "due_date",
                "ticket_ids": ticket_ids,
                "due_date": "",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for tid in ticket_ids:
            assert db.kanban_get_ticket(tid)["due_date"] in (None, "")

    def test_bulk_delete_tickets_removes_them(self, client, admin_user):
        """Bulk delete removes every selected ticket."""
        import db
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "delete",
                "ticket_ids": ticket_ids,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for tid in ticket_ids:
            assert db.kanban_get_ticket(tid) is None

    def test_bulk_tickets_rejects_foreign_ticket(self, client, admin_user):
        """Bulk endpoint refuses tickets from another board."""
        import db
        board_id, _, ticket_ids = self._setup_board(admin_user)
        other = db.kanban_create_board("Other", "", admin_user)
        other_col = db.kanban_create_column(other, "Other", 0)
        other_tid = db.kanban_create_ticket(other_col, "Foreign", "", admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "priority",
                "ticket_ids": ticket_ids + [other_tid],
                "priority": "low",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bulk_tickets_rejects_empty_list(self, client, admin_user):
        """Bulk endpoint rejects missing or empty ticket_ids."""
        board_id, _, _ = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({"action": "priority", "ticket_ids": [], "priority": "low"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bulk_tickets_unknown_action(self, client, admin_user):
        """Unknown actions are rejected with a 400."""
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({"action": "explode", "ticket_ids": ticket_ids}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bulk_tickets_requires_write_access(self, client, admin_user, regular_user):
        """A read-only user cannot perform bulk actions."""
        _set_kanban_access("all", "admin")
        board_id, _, ticket_ids = self._setup_board(admin_user)
        _login(client, "user", "user123")
        resp = client.post(
            f"/api/kanban/{board_id}/tickets/bulk",
            data=json.dumps({
                "action": "priority",
                "ticket_ids": ticket_ids,
                "priority": "low",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_bulk_columns_delete_removes_columns_and_tickets(self, client, admin_user):
        """Bulk column delete cascades to tickets."""
        import db
        board_id, col_ids, ticket_ids = self._setup_board(admin_user)
        # Sanity: tickets exist before delete
        assert all(db.kanban_get_ticket(tid) is not None for tid in ticket_ids)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/columns/bulk",
            data=json.dumps({
                "action": "delete",
                "column_ids": col_ids,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        for cid in col_ids:
            assert db.kanban_get_column(cid) is None
        for tid in ticket_ids:
            assert db.kanban_get_ticket(tid) is None

    def test_bulk_columns_rejects_foreign_column(self, client, admin_user):
        """Bulk column delete refuses columns that belong to another board."""
        import db
        board_id, col_ids, _ = self._setup_board(admin_user)
        other = db.kanban_create_board("Other", "", admin_user)
        other_col = db.kanban_create_column(other, "Other", 0)
        _login(client, "admin", "admin123")
        resp = client.post(
            f"/api/kanban/{board_id}/columns/bulk",
            data=json.dumps({
                "action": "delete",
                "column_ids": col_ids + [other_col],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestKanbanActivityLogDate:
    """Activity log entries expose a parseable ``created_at`` string."""

    def test_activity_endpoint_returns_iso_created_at(self, client, admin_user):
        """``GET /api/kanban/<id>/activity`` returns ISO timestamps."""
        import db
        from datetime import datetime
        _enable_kanban_plugin()
        board_id = db.kanban_create_board("Board", "", admin_user)
        db.kanban_add_activity_log(board_id, admin_user, "test_event", "details")
        _login(client, "admin", "admin123")
        resp = client.get(f"/api/kanban/{board_id}/activity")
        assert resp.status_code == 200
        entries = resp.get_json()["entries"]
        assert entries, "expected at least one activity entry"
        created_at = entries[0]["created_at"]
        # Must be a non-empty string that ``datetime.fromisoformat`` can parse.
        assert isinstance(created_at, str) and created_at
        parsed = datetime.fromisoformat(created_at)
        assert parsed is not None

    def test_board_html_handles_iso_with_offset(self, client, admin_user):
        """The board template ships a date helper that tolerates ``+00:00``.

        Regression test for the ``Invalid Date`` bug where the previous
        implementation appended a literal ``Z`` to a timestamp that already
        carried a ``+00:00`` offset, producing a string JS could not parse.
        """
        import db
        _enable_kanban_plugin()
        board_id = db.kanban_create_board("Board", "", admin_user)
        _login(client, "admin", "admin123")
        resp = client.get(f"/kanban/{board_id}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # The new helper exists and the buggy ``+ 'Z'`` concatenation is gone.
        assert "function formatActivityDate" in body
        assert "new Date(e.created_at + 'Z')" not in body


class TestKanbanRealtimeSync:
    """Verify the events feed kicked off by kanban mutation routes."""

    def _make_board(self, client, admin_user, title="Sync Board"):
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        import db
        board_id = db.kanban_create_board(title, "desc", admin_user)
        return board_id

    def test_sync_endpoint_returns_empty_initially(self, client, admin_user):
        board_id = self._make_board(client, admin_user)
        resp = client.get(f"/api/kanban/{board_id}/sync?since=0")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["events"] == []
        assert body["seq"] == 0

    def test_column_create_emits_event(self, client, admin_user):
        board_id = self._make_board(client, admin_user)
        resp = client.post(
            f"/api/kanban/{board_id}/columns",
            data=json.dumps({"title": "Backlog"}),
            content_type="application/json",
            headers={"X-Kanban-Session": "writer"},
        )
        assert resp.status_code == 201
        # The session that produced the event is filtered out.
        own = client.get(
            f"/api/kanban/{board_id}/sync?since=0",
            headers={"X-Kanban-Session": "writer"},
        ).get_json()
        assert own["events"] == []
        # Other sessions see the column_created event.
        other = client.get(
            f"/api/kanban/{board_id}/sync?since=0",
            headers={"X-Kanban-Session": "reader"},
        ).get_json()
        kinds = [e["op_type"] for e in other["events"]]
        assert "column_created" in kinds

    def test_ticket_move_emits_event(self, client, admin_user):
        board_id = self._make_board(client, admin_user)
        import db
        col1_id = db.kanban_create_column(board_id, "Todo", 0)
        col2_id = db.kanban_create_column(board_id, "Done", 1)
        ticket_id = db.kanban_create_ticket(col1_id, "Task", "", admin_user,
                                            "medium", 0)
        resp = client.post(
            f"/api/kanban/tickets/{ticket_id}/move",
            data=json.dumps({"column_id": col2_id, "sort_order": 0}),
            content_type="application/json",
            headers={"X-Kanban-Session": "mover"},
        )
        assert resp.status_code == 200
        sync = client.get(
            f"/api/kanban/{board_id}/sync?since=0",
            headers={"X-Kanban-Session": "watcher"},
        ).get_json()
        kinds = [e["op_type"] for e in sync["events"]]
        assert "ticket_moved" in kinds
