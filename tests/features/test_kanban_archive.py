"""Kanban archive (tickets and boards), history changes and the board page's view controls."""

from __future__ import annotations

import io
import json
import re

import pytest

from bananawiki.wiki.features.kanban import history, schema

from .test_kanban import as_client, board_id_from, columns, new_ticket, set_settings


@pytest.fixture
def board(admin_client):
    return board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap"}))


def state(client, board_id):
    return client.get(f"/api/kanban/{board_id}/state").get_json()


def archived(client, board_id):
    return client.get(f"/api/kanban/{board_id}/archived").get_json()


# ── Tickets ──────────────────────────────────────────────────────────────────


def test_archiving_a_ticket_takes_it_off_the_board(admin_client, board, db, admin):
    column_id = columns(admin_client, board)[0]["id"]
    one, two, three = (new_ticket(admin_client, column_id, title) for title in ("One", "Two", "Three"))
    admin_client.post(f"/api/kanban/tickets/{two['id']}/comments", json={"content": "keep me"})
    start = state(admin_client, board)["seq"]

    response = admin_client.post(f"/api/kanban/tickets/{two['id']}/archive")
    assert response.get_json() == {"updated": 1}
    assert admin_client.post(f"/api/kanban/tickets/{two['id']}/archive").get_json() == {"updated": 0}
    data = state(admin_client, board)
    assert data["columns"][0]["tickets"] == [one["id"], three["id"]] and str(two["id"]) not in data["tickets"]
    assert data["archived_count"] == 1
    event = admin_client.get(f"/api/kanban/{board}/sync?since={start}").get_json()["events"][-1]
    assert event["op"] == "tickets_archived" and event["payload"]["ticket_ids"] == [two["id"]]

    listing = archived(admin_client, board)
    assert listing["total"] == 1 and listing["tickets"][0]["id"] == two["id"]
    assert listing["tickets"][0]["column_title"] == "To do"
    assert listing["tickets"][0]["archived_by_username"] == admin["username"]
    detail = admin_client.get(f"/api/kanban/tickets/{two['id']}").get_json()
    assert detail["archived_at"] and detail["comment_count"] == 1

    # Archived tickets can be read and edited but not moved; new tickets go after the active ones.
    assert admin_client.put(f"/api/kanban/tickets/{two['id']}", json={"title": "Two!"}).status_code == 200
    moved = admin_client.post(f"/api/kanban/tickets/{two['id']}/move", json={"column_id": column_id, "position": 0})
    assert moved.status_code == 409
    four = new_ticket(admin_client, column_id, "Four")
    assert db.scalar("SELECT sort_order FROM kanban_tickets WHERE id = ?", (four["id"],)) == 2

    restored = admin_client.post(f"/api/kanban/tickets/{two['id']}/restore").get_json()
    assert restored["updated"] == 1 and restored["ticket"]["title"] == "Two!"
    data = state(admin_client, board)
    assert data["columns"][0]["tickets"] == [one["id"], three["id"], four["id"], two["id"]]
    assert data["archived_count"] == 0
    assert db.scalar("SELECT archived_by FROM kanban_tickets WHERE id = ?", (two["id"],)) is None


def test_archive_a_whole_column_and_bulk_actions(admin_client, board, db):
    cols = columns(admin_client, board)
    ids = [new_ticket(admin_client, cols[2]["id"], f"Done {n}")["id"] for n in range(3)]
    other = new_ticket(admin_client, cols[0]["id"], "Busy")
    assert admin_client.post(f"/api/kanban/columns/{cols[2]['id']}/archive").get_json() == {"updated": 3}
    assert state(admin_client, board)["columns"][2]["tickets"] == []
    assert [t["id"] for t in archived(admin_client, board)["tickets"]] == sorted(ids, reverse=True)

    url = f"/api/kanban/{board}/tickets/bulk"
    assert admin_client.post(url, json={"ticket_ids": ids[:2], "action": "restore"}).get_json() == {"updated": 2}
    assert state(admin_client, board)["columns"][2]["tickets"] == ids[:2]
    # Other bulk changes refuse archived tickets; deleting them for good works.
    refused = admin_client.post(url, json={"ticket_ids": [ids[2], other["id"]], "action": "priority", "priority": "low"})
    assert refused.status_code == 409
    assert admin_client.post(url, json={"ticket_ids": [ids[0], other["id"]], "action": "archive"}).get_json() == {
        "updated": 2}
    assert admin_client.post(url, json={"ticket_ids": [ids[2]], "action": "delete"}).get_json() == {"updated": 1}
    assert db.scalar("SELECT COUNT(*) FROM kanban_tickets WHERE id = ?", (ids[2],)) == 0
    assert archived(admin_client, board)["total"] == 2


def test_archive_permissions_and_idor(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"])
    column_id = columns(admin_client, board)[0]["id"]
    reader = as_client(app, login, make_user("reader"))
    assert reader.get(f"/api/kanban/{board}/archived").status_code == 200
    for url in (f"/api/kanban/tickets/{ticket['id']}/archive", f"/api/kanban/tickets/{ticket['id']}/restore",
                f"/api/kanban/columns/{column_id}/archive"):
        assert reader.post(url).status_code == 403
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    assert reader.get(f"/api/kanban/{board}/archived").status_code == 404
    assert reader.post(f"/api/kanban/tickets/{ticket['id']}/archive").status_code == 404
    assert db.scalar("SELECT archived_at FROM kanban_tickets WHERE id = ?", (ticket["id"],)) is None

    # Bulk ids must belong to the board in the URL.
    second = board_id_from(admin_client.post("/kanban/create", data={"title": "Other"}))
    response = admin_client.post(f"/api/kanban/{second}/tickets/bulk", json={"ticket_ids": [ticket["id"]],
                                                                             "action": "archive"})
    assert response.status_code == 404


def test_my_tickets_skip_archived_tickets_and_boards(app, admin_client, db, board, admin):
    column_id = columns(admin_client, board)[0]["id"]
    kept = new_ticket(admin_client, column_id, "Kept @admin_user")
    shelved = new_ticket(admin_client, column_id, "Shelved @admin_user")
    admin_client.post(f"/api/kanban/tickets/{shelved['id']}/archive")
    assert [t["id"] for t in admin_client.get("/api/kanban/my-tickets").get_json()["tickets"]] == [kept["id"]]
    admin_client.post(f"/kanban/{board}/archive")
    assert admin_client.get("/api/kanban/my-tickets").get_json()["tickets"] == []
    assert "Kept" not in admin_client.get("/kanban/mine").get_data(as_text=True)


def test_history_and_revert_round_trip_the_archive(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    one = new_ticket(admin_client, column_id, "One")
    two = new_ticket(admin_client, column_id, "Two")
    before = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board,))
    admin_client.post(f"/api/kanban/tickets/{one['id']}/archive")
    archived_entry = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board,))
    snapshot = history.decode(db.scalar("SELECT snapshot FROM kanban_board_history WHERE id = ?", (archived_entry,)))
    items = snapshot["columns"][0]["tickets"]
    assert [(t["title"], t.get("archived", False)) for t in items] == [("Two", False), ("One", True)]

    admin_client.post(f"/kanban/{board}/revert/{before}")
    assert state(admin_client, board)["columns"][0]["tickets"] == [one["id"], two["id"]]
    admin_client.post(f"/kanban/{board}/revert/{archived_entry}")
    assert state(admin_client, board)["columns"][0]["tickets"] == [two["id"]]
    assert db.scalar("SELECT archived_at FROM kanban_tickets WHERE id = ?", (one["id"],))

    # A column holding only archived tickets survives a revert to a version without it.
    extra = admin_client.post(f"/api/kanban/{board}/columns", json={"title": "Later"}).get_json()
    parked = new_ticket(admin_client, extra["id"], "Parked")
    admin_client.post(f"/api/kanban/tickets/{parked['id']}/archive")
    admin_client.post(f"/kanban/{board}/revert/{archived_entry}")
    assert db.scalar("SELECT COUNT(*) FROM kanban_tickets WHERE id = ?", (parked["id"],)) == 1


def test_export_import_keep_archive_state(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    new_ticket(admin_client, column_id, "Active")
    gone = new_ticket(admin_client, column_id, "Archived")
    admin_client.post(f"/api/kanban/tickets/{gone['id']}/archive")
    admin_client.post(f"/kanban/{board}/archive")
    document = json.loads(admin_client.get(f"/kanban/{board}/export?format=json").data)
    assert document["archived"] is True
    assert [(t["title"], t["archived"]) for t in document["columns"][0]["tickets"]] == [("Active", False),
                                                                                       ("Archived", True)]
    imported = board_id_from(admin_client.post(
        "/kanban/import", data={"import_file": (io.BytesIO(json.dumps(document).encode()), "b.kanban.json")},
        content_type="multipart/form-data"))
    data = state(admin_client, imported)
    assert data["board"]["archived_at"] and data["archived_count"] == 1
    assert [data["tickets"][str(i)]["title"] for i in data["columns"][0]["tickets"]] == ["Active"]
    assert db.scalar("SELECT sort_order FROM kanban_tickets WHERE title = 'Active' AND id > ?", (gone["id"],)) == 0


# ── Boards ───────────────────────────────────────────────────────────────────


def test_archived_board_is_hidden_and_read_only(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all", kanban_write_access="all")
    writer = as_client(app, login, make_user("writer"))
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    assert writer.post(f"/kanban/{board}/archive").status_code == 403

    response = admin_client.post(f"/kanban/{board}/archive")
    assert response.status_code == 302 and response.headers["Location"].endswith("/kanban")
    listing = writer.get("/kanban").get_data(as_text=True)
    assert "Roadmap" not in listing and "Show archived boards (1)" in listing
    shown = writer.get("/kanban?archived=1").get_data(as_text=True)
    assert 'id="kanban-archived-boards"' in shown and "Roadmap" in shown
    assert f"/kanban/{board}/restore" not in shown  # only owners can restore
    assert f"/kanban/{board}/restore" in admin_client.get("/kanban?archived=1").get_data(as_text=True)

    page = writer.get(f"/kanban/{board}").get_data(as_text=True)
    assert 'id="kanban-archived-notice"' in page and re.search(r'"can_write":\s*false', page)
    for client in (writer, admin_client):
        assert client.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": "x"}).status_code == 403
        assert client.put(f"/api/kanban/tickets/{ticket['id']}", json={"title": "x"}).status_code == 403
        assert client.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "x"}).status_code == 403
    assert admin_client.get(f"/kanban/{board}/export?format=json").status_code == 200
    assert writer.get(f"/api/kanban/tickets/{ticket['id']}").status_code == 200

    admin_client.post(f"/kanban/{board}/restore")
    assert "Roadmap" in writer.get("/kanban").get_data(as_text=True)
    assert writer.put(f"/api/kanban/tickets/{ticket['id']}", json={"title": "Back"}).status_code == 200


def test_board_order_survives_archiving(admin_client, db, board):
    second = board_id_from(admin_client.post("/kanban/create", data={"title": "Second"}))
    third = board_id_from(admin_client.post("/kanban/create", data={"title": "Third"}))
    admin_client.post("/api/kanban/board-order", json={"board_ids": [board, second, third]})
    admin_client.post(f"/kanban/{second}/archive")
    admin_client.post("/api/kanban/board-order", json={"board_ids": [third, board]})
    order = db.column("SELECT board_id FROM kanban_user_board_order ORDER BY sort_order")
    assert order == [third, board, second]


def test_hidden_archived_boards_stay_hidden(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    admin_client.post(f"/kanban/{board}/archive")
    outsider = as_client(app, login, make_user("outsider"))
    assert "Roadmap" not in outsider.get("/kanban?archived=1").get_data(as_text=True)
    assert outsider.post(f"/kanban/{board}/restore").status_code == 404
    assert db.scalar("SELECT archived_at FROM kanban_boards WHERE id = ?", (board,))


# ── History page ─────────────────────────────────────────────────────────────


def test_history_entry_shows_limits_checklists_archive_and_changes(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id, "Ship it")
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/checklist", json={"text": "Write notes"})
    admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 4})
    admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"priority": "high", "labels": ["release"]})
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/archive")
    entries = db.column("SELECT id FROM kanban_board_history WHERE board_id = ? ORDER BY id", (board,))

    def page(entry_id):
        return admin_client.get(f"/kanban/{board}/history/{entry_id}").get_data(as_text=True)

    assert "This is the oldest recorded version." in page(entries[0])
    latest = page(entries[-1])
    assert "Ticket “Ship it” archived" in latest and "Archived" in latest
    assert "0 / 4" in latest and "Checklist: 0 of 1 done" in latest and "Write notes" in latest
    joined = "".join(page(entry_id) for entry_id in entries)
    assert "Work-in-progress limit of “To do” set to 4" in joined
    assert "Checklist of “Ship it” changed (0 of 1 done)" in joined
    assert re.search(r"Ticket “Ship it”: [a-z, ]*priority[a-z, ]* changed", joined)
    assert "Ticket “Ship it” added to To do" in joined


def test_history_changes_unit():
    old = {"title": "B", "description": "", "columns": [
        {"id": 1, "title": "To do", "wip_limit": None, "tickets": [
            {"id": 5, "title": "T", "priority": "low", "checklist": [], "labels": []},
            {"id": 6, "title": "Gone"}]},
        {"id": 2, "title": "Old", "tickets": []}]}
    new = {"title": "B2", "description": "x", "columns": [
        {"id": 1, "title": "Backlog", "wip_limit": 3, "tickets": []},
        {"id": 3, "title": "Doing", "tickets": [
            {"id": 5, "title": "T", "priority": "high", "archived": True, "labels": [],
             "checklist": [{"text": "a", "done": True}]}]}]}
    keys = [key for key, _ in history.changes(old, new)]
    assert keys == ["kanban.diff.board_title", "kanban.diff.board_description", "kanban.diff.column_renamed",
                    "kanban.diff.column_limit", "kanban.diff.column_added", "kanban.diff.column_removed",
                    "kanban.diff.ticket_moved", "kanban.diff.ticket_archived", "kanban.diff.ticket_changed",
                    "kanban.diff.checklist", "kanban.diff.ticket_removed"]
    assert history.changes(new, new) == []


# ── Board page and schema ────────────────────────────────────────────────────


def test_board_page_has_lanes_archive_and_no_inline_scripts(admin_client, board):
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    assert 'id="kanban-lanes"' in html and 'value="assignee"' in html and 'value="label"' in html
    assert 'id="kanban-archive-toggle"' in html and 'data-bulk="archive"' in html
    assert "kanban-lanes.js" in html and "kanban-archive.js" in html
    assert 'id="kanban-ticket-archive"' in html and f"/kanban/{board}/archive" in html
    for url in (f"/kanban/{board}", "/kanban?archived=1", "/kanban/mine?q=x"):
        page = admin_client.get(url).get_data(as_text=True)
        for tag in re.findall(r"<script\b[^>]*>", page):
            assert "src=" in tag or 'type="application/json"' in tag, (url, tag)
        assert " onclick=" not in page


def test_schema_adds_archive_columns_idempotently(db):
    schema.upgrade_v4(db.conn)
    schema.upgrade_v4(db.conn)
    for table in ("kanban_tickets", "kanban_boards"):
        found = db.column(f"SELECT name FROM pragma_table_info('{table}') WHERE name IN ('archived_at', 'archived_by')")
        assert sorted(found) == ["archived_at", "archived_by"]
