import json
import io
import zipfile

import pytest
import db

def test_kanban_export_import(client, admin_user):
    """Test exporting a board and importing it back."""
    # 1. Setup: Create a board to export
    _enable_plugin("kanban")
    _login(client, "admin", "admin123")

    user = db.get_user_by_username("admin")
    user_id = user["id"]

    board_title = "Export Test Board"
    board_desc = "Testing export and import"
    board_id = db.kanban_create_board(board_title, board_desc, user_id)

    col_id = db.kanban_create_column(board_id, "Test Column", 0)
    db.kanban_create_ticket(col_id, "Test Ticket", "Ticket description", user_id, "high")

    # 2. Export the board
    resp = client.get(f"/kanban/{board_id}/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/json"

    export_data = json.loads(resp.data)
    assert export_data["title"] == board_title
    assert export_data["description"] == board_desc
    assert len(export_data["columns"]) == 1
    assert export_data["columns"][0]["title"] == "Test Column"
    assert len(export_data["columns"][0]["tickets"]) == 1
    assert export_data["columns"][0]["tickets"][0]["title"] == "Test Ticket"
    assert export_data["columns"][0]["tickets"][0]["priority"] == "high"

    # 3. Import the board
    import_filename = "test_import.json"
    import_file_data = (io.BytesIO(json.dumps(export_data).encode("utf-8")), import_filename)

    resp = client.post(
        "/kanban/import",
        data={"import_file": import_file_data},
        content_type="multipart/form-data",
        follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"successfully imported" in resp.data

    # 4. Verify imported board
    boards = db.kanban_list_boards()
    new_board = next(b for b in boards if b["id"] != board_id)
    assert new_board["title"] == board_title
    assert new_board["description"] == board_desc

    columns = db.kanban_list_columns(new_board["id"])
    assert len(columns) == 1
    assert columns[0]["title"] == "Test Column"

    tickets = db.kanban_list_tickets(columns[0]["id"])
    assert len(tickets) == 1
    assert tickets[0]["title"] == "Test Ticket"
    assert tickets[0]["priority"] == "high"

def test_kanban_multi_assignee_create_and_update(client, admin_user, editor_user):
    """A ticket should support multiple assignees via the new ``assignees`` field."""
    _enable_plugin("kanban")
    db.update_site_settings(kanban_access="all", kanban_write_access="all")
    _login(client, "admin", "admin123")

    user = db.get_user_by_username("admin")
    editor = db.get_user_by_username("editor")
    board_id = db.kanban_create_board("MA Board", "", user["id"])
    col_id = db.kanban_create_column(board_id, "Col", 0)

    # Create a ticket with two assignees up-front.
    resp = client.post(
        "/api/kanban/columns/" + str(col_id) + "/tickets",
        json={"title": "T1", "assignees": [user["id"], editor["id"]]},
    )
    assert resp.status_code == 201, resp.data
    payload = resp.get_json()
    ticket_id = payload["id"]
    assert {a["id"] for a in payload["assignees"]} == {user["id"], editor["id"]}
    # Legacy single field should map to the first assignee.
    assert payload["assigned_to"] == user["id"]

    # GET surfaces both assignees.
    resp = client.get("/api/kanban/tickets/" + str(ticket_id))
    assert resp.status_code == 200
    body = resp.get_json()
    assert {a["id"] for a in body["assignees"]} == {user["id"], editor["id"]}

    # PUT replacing the set with just one user clears the other.
    resp = client.put(
        "/api/kanban/tickets/" + str(ticket_id),
        json={"assignees": [editor["id"]]},
    )
    assert resp.status_code == 200
    body = client.get("/api/kanban/tickets/" + str(ticket_id)).get_json()
    assert [a["id"] for a in body["assignees"]] == [editor["id"]]
    assert body["assigned_to"] == editor["id"]

    # PUT with an empty list unassigns everyone.
    resp = client.put(
        "/api/kanban/tickets/" + str(ticket_id),
        json={"assignees": []},
    )
    assert resp.status_code == 200
    body = client.get("/api/kanban/tickets/" + str(ticket_id)).get_json()
    assert body["assignees"] == []
    assert body["assigned_to"] is None


def test_kanban_zip_export_with_attachments_round_trip(client, admin_user, tmp_path, monkeypatch):
    """When a ticket has attachments, export should be a ZIP and re-import
    must restore the attachments to the new ticket."""
    import config

    _enable_plugin("kanban")
    _login(client, "admin", "admin123")

    # Force the kanban attachment folder to a tmp dir so the test stays
    # isolated from real uploads.
    monkeypatch.setattr(config, "KANBAN_ATTACHMENT_FOLDER", str(tmp_path))

    user = db.get_user_by_username("admin")
    board_id = db.kanban_create_board("AttBoard", "with attachments", user["id"])
    col_id = db.kanban_create_column(board_id, "Col", 0)
    ticket_id = db.kanban_create_ticket(col_id, "T", "", user["id"], "medium", 0)

    # Drop an actual file in the attachment folder + register it in the DB.
    stored_name = "abc123.txt"
    (tmp_path / stored_name).write_bytes(b"hello bundle")
    db.kanban_add_ticket_attachment(ticket_id, stored_name, "hello.txt", 12, user["id"])

    # Export should now be a ZIP.
    resp = client.get(f"/kanban/{board_id}/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/zip")
    # ZIP magic
    assert resp.data[:4] == b"PK\x03\x04"
    # ZIP must contain board.json + the attachment under attachments/<ticket_id>/
    with zipfile.ZipFile(io.BytesIO(resp.data)) as z:
        names = z.namelist()
        assert "board.json" in names
        assert any(n.endswith("/" + stored_name) and n.startswith("attachments/") for n in names)
        meta = json.loads(z.read("board.json"))
        assert meta["columns"][0]["tickets"][0]["attachments"][0]["original_name"] == "hello.txt"

    # Re-import the same ZIP.
    resp = client.post(
        "/kanban/import",
        data={"import_file": (io.BytesIO(resp.data), "round.kanban.zip")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"successfully imported" in resp.data

    # The new board should have a ticket with one restored attachment.
    boards = [b for b in db.kanban_list_boards() if b["id"] != board_id]
    assert boards, "imported board should exist"
    new_board = boards[-1]
    new_cols = db.kanban_list_columns(new_board["id"])
    new_tickets = db.kanban_list_tickets(new_cols[0]["id"])
    new_atts = db.kanban_list_ticket_attachments(new_tickets[0]["id"])
    assert len(new_atts) == 1
    assert new_atts[0]["original_name"] == "hello.txt"
    # The restored file must exist on disk under the new stored filename.
    assert (tmp_path / new_atts[0]["filename"]).exists()
    assert (tmp_path / new_atts[0]["filename"]).read_bytes() == b"hello bundle"


def test_kanban_zip_export_preserves_assignees(client, admin_user, editor_user, tmp_path, monkeypatch):
    """JSON export should record assignee usernames; import maps them to local users."""
    import config
    _enable_plugin("kanban")
    db.update_site_settings(kanban_access="all", kanban_write_access="all")
    _login(client, "admin", "admin123")
    monkeypatch.setattr(config, "KANBAN_ATTACHMENT_FOLDER", str(tmp_path))

    admin = db.get_user_by_username("admin")
    editor = db.get_user_by_username("editor")
    board_id = db.kanban_create_board("AssBoard", "", admin["id"])
    col_id = db.kanban_create_column(board_id, "Col", 0)
    tk = db.kanban_create_ticket(col_id, "T", "", admin["id"], "medium", 0)
    db.kanban_set_ticket_assignees(tk, [admin["id"], editor["id"]])

    resp = client.get(f"/kanban/{board_id}/export?format=json")
    assert resp.status_code == 200
    body = resp.get_json()
    names = body["columns"][0]["tickets"][0].get("assignee_usernames")
    assert names is not None
    assert set(names) == {"admin", "editor"}

    # Re-import → assignees restored
    resp = client.post(
        "/kanban/import",
        data={"import_file": (io.BytesIO(json.dumps(body).encode()), "ass.kanban.json")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    new_board = [b for b in db.kanban_list_boards() if b["id"] != board_id][-1]
    new_cols = db.kanban_list_columns(new_board["id"])
    new_tk = db.kanban_list_tickets(new_cols[0]["id"])[0]
    restored_ids = {a["id"] for a in db.kanban_list_ticket_assignees(new_tk["id"])}
    assert restored_ids == {admin["id"], editor["id"]}


def test_kanban_export_requires_write_access(client, admin_user, regular_user):
    """A read-only viewer must not be able to download a board's contents.

    Read access lets non-editor users *see* the board (when the kanban
    plugin allows it), but exporting bundles every ticket, attachment
    and assignee list into a single file, so the route now also
    requires write access.
    """
    _enable_plugin("kanban")
    db.update_site_settings(kanban_access="all", kanban_write_access="editor")
    admin = db.get_user_by_username("admin")
    board_id = db.kanban_create_board("RO Board", "", admin["id"])
    col_id = db.kanban_create_column(board_id, "Col", 0)
    db.kanban_create_ticket(col_id, "Secret Ticket", "", admin["id"], "medium")

    # Regular user can view the board but cannot export it.
    _login(client, "user", "user123")
    resp = client.get(f"/kanban/{board_id}/export?format=json", follow_redirects=False)
    assert resp.status_code == 403


def _enable_plugin(plugin_id):
    with db.get_db_context() as conn:
        conn.execute("INSERT OR REPLACE INTO plugins (id, name, version, builtin, enabled) VALUES (?, ?, '1.0', 1, 1)", (plugin_id, plugin_id.capitalize()))
        conn.commit()

def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
