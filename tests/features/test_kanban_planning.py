"""Kanban planning tools: WIP limits, checklists, "my tickets", the filter bar and their permission checks."""

from __future__ import annotations

import io
import json
import re

import pytest

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.kanban import fields, history, schema

from .test_kanban import as_client, board_id_from, columns, new_ticket, set_settings


@pytest.fixture
def board(admin_client):
    return board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap"}))


def checklist_url(ticket_id):
    return f"/api/kanban/tickets/{ticket_id}/checklist"


def add_item(client, ticket_id, text="Step"):
    response = client.post(checklist_url(ticket_id), json={"text": text})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


# ── WIP limits ───────────────────────────────────────────────────────────────


def test_wip_limit_is_saved_validated_and_synced(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    start = admin_client.get(f"/api/kanban/{board}/state").get_json()["seq"]
    response = admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 3})
    assert response.status_code == 200 and response.get_json()["wip_limit"] == 3
    assert columns(admin_client, board)[0]["wip_limit"] == 3
    event = admin_client.get(f"/api/kanban/{board}/sync?since={start}").get_json()["events"][-1]
    assert event["op"] == "column_upsert" and event["payload"]["column"]["wip_limit"] == 3

    for bad in (-1, 1000, "many", True, 2.5):
        assert admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": bad}).status_code == 400
    assert columns(admin_client, board)[0]["wip_limit"] == 3
    assert admin_client.put(f"/api/kanban/columns/{column_id}", json={}).status_code == 400

    # Renaming alone keeps the limit; 0, "" and null remove it.
    admin_client.put(f"/api/kanban/columns/{column_id}", json={"title": "Backlog"})
    assert columns(admin_client, board)[0] == {**columns(admin_client, board)[0], "title": "Backlog", "wip_limit": 3}
    for empty in (0, "", None):
        admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 5})
        admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": empty})
        assert db.scalar("SELECT wip_limit FROM kanban_columns WHERE id = ?", (column_id,)) is None


def test_wip_limit_needs_write_access(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    reader = as_client(app, login, make_user("reader"))
    column_id = columns(reader, board)[0]["id"]
    assert reader.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 2}).status_code == 403
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    assert reader.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 2}).status_code == 404
    assert db.scalar("SELECT wip_limit FROM kanban_columns WHERE id = ?", (column_id,)) is None


def test_wip_limit_field_parsing():
    assert fields.wip_limit("4") == 4 and fields.wip_limit(" ") is None and fields.wip_limit(0) is None
    assert fields.stored_wip_limit("nonsense") is None and fields.stored_wip_limit(7) == 7


# ── Checklists ───────────────────────────────────────────────────────────────


def test_checklist_lifecycle_updates_the_card(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    first = add_item(admin_client, ticket["id"], "  Write   tests ")
    assert first["checklist"] == [{"id": first["checklist"][0]["id"], "text": "Write tests", "done": False}]
    assert first["ticket"]["checklist_total"] == 1 and first["ticket"]["checklist_done"] == 0
    second = add_item(admin_client, ticket["id"], "Ship")["checklist"][1]["id"]
    one = first["checklist"][0]["id"]

    toggled = admin_client.put(f"/api/kanban/checklist/{one}", json={"done": True}).get_json()
    assert toggled["ticket"]["checklist_done"] == 1 and toggled["checklist"][0]["done"] is True
    renamed = admin_client.put(f"/api/kanban/checklist/{second}", json={"text": "Release"}).get_json()
    assert [item["text"] for item in renamed["checklist"]] == ["Write tests", "Release"]
    reordered = admin_client.post(f"{checklist_url(ticket['id'])}/reorder", json={"order": [second, 99999, one]})
    assert [item["id"] for item in reordered.get_json()["checklist"]] == [second, one]

    state = admin_client.get(f"/api/kanban/{board}/state").get_json()["tickets"][str(ticket["id"])]
    assert (state["checklist_done"], state["checklist_total"]) == (1, 2)
    detail = admin_client.get(f"/api/kanban/tickets/{ticket['id']}").get_json()
    assert [item["id"] for item in detail["checklist"]] == [second, one]
    assert admin_client.get(checklist_url(ticket["id"])).get_json()["checklist"] == detail["checklist"]

    deleted = admin_client.delete(f"/api/kanban/checklist/{second}").get_json()
    assert [item["id"] for item in deleted["checklist"]] == [one] and deleted["ticket"]["checklist_total"] == 1
    admin_client.delete(f"/api/kanban/tickets/{ticket['id']}")
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_checklist") == 0


def test_checklist_validation(admin_client, board, monkeypatch):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    assert admin_client.post(checklist_url(ticket["id"]), json={"text": "   "}).status_code == 400
    assert admin_client.post(checklist_url(ticket["id"]), json={"text": "x" * 301}).status_code == 400
    item = add_item(admin_client, ticket["id"])["checklist"][0]["id"]
    assert admin_client.put(f"/api/kanban/checklist/{item}", json={"done": "yes"}).status_code == 400
    assert admin_client.put(f"/api/kanban/checklist/{item}", json={}).status_code == 400
    assert admin_client.put("/api/kanban/checklist/99999", json={"done": True}).status_code == 404
    assert admin_client.post(f"{checklist_url(ticket['id'])}/reorder", json={"order": "x"}).status_code == 400
    monkeypatch.setattr(fields, "MAX_CHECKLIST_ITEMS", 1)
    assert admin_client.post(checklist_url(ticket["id"]), json={"text": "Too many"}).status_code == 400


def test_checklist_changes_reach_other_viewers(admin_client, board):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    start = admin_client.get(f"/api/kanban/{board}/state").get_json()["seq"]
    admin_client.post(checklist_url(ticket["id"]), json={"text": "Step"}, headers={"X-Kanban-Session": "tab-a"})
    feed = admin_client.get(f"/api/kanban/{board}/sync?since={start}", headers={"X-Kanban-Session": "tab-b"})
    event = feed.get_json()["events"][0]
    assert event["op"] == "ticket_upsert" and event["payload"]["ticket"]["checklist_total"] == 1


def test_checklist_permissions_and_idor(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    reader = as_client(app, login, make_user("reader"))
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    item = add_item(admin_client, ticket["id"])["checklist"][0]["id"]
    # Viewers read but cannot change.
    assert reader.get(checklist_url(ticket["id"])).status_code == 200
    assert reader.post(checklist_url(ticket["id"]), json={"text": "x"}).status_code == 403
    assert reader.put(f"/api/kanban/checklist/{item}", json={"done": True}).status_code == 403
    assert reader.delete(f"/api/kanban/checklist/{item}").status_code == 403
    assert reader.post(f"{checklist_url(ticket['id'])}/reorder", json={"order": [item]}).status_code == 403
    # A hidden board's items do not exist for outsiders.
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    assert reader.get(checklist_url(ticket["id"])).status_code == 404
    assert reader.put(f"/api/kanban/checklist/{item}", json={"done": True}).status_code == 404
    assert reader.delete(f"/api/kanban/checklist/{item}").status_code == 404
    assert db.scalar("SELECT done FROM kanban_ticket_checklist WHERE id = ?", (item,)) == 0
    # A writer on another board cannot reorder this ticket's items through their own ticket.
    other_board = board_id_from(admin_client.post("/kanban/create", data={"title": "Other"}))
    other_ticket = new_ticket(admin_client, columns(admin_client, other_board)[0]["id"])
    add_item(admin_client, other_ticket["id"], "Mine")
    admin_client.post(f"{checklist_url(other_ticket['id'])}/reorder", json={"order": [item]})
    assert db.scalar("SELECT ticket_id FROM kanban_ticket_checklist WHERE id = ?", (item,)) == ticket["id"]
    anonymous = app.test_client()
    assert anonymous.put(f"/api/kanban/checklist/{item}", json={"done": True},
                         headers={"Accept": "application/json"}).status_code in (401, 302, 403)


# ── History, revert, export and import ───────────────────────────────────────


def test_revert_restores_limits_and_checklists(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    item = add_item(admin_client, ticket["id"], "Plan")["checklist"][0]["id"]
    admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 2})
    entry_id = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board,))
    state = history.decode(db.scalar("SELECT snapshot FROM kanban_board_history WHERE id = ?", (entry_id,)))
    assert state["columns"][0]["wip_limit"] == 2
    assert state["columns"][0]["tickets"][0]["checklist"] == [{"text": "Plan", "done": False}]

    admin_client.put(f"/api/kanban/checklist/{item}", json={"done": True, "text": "Changed"})
    admin_client.post(checklist_url(ticket["id"]), json={"text": "Extra"})
    admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": None})
    admin_client.post(f"/kanban/{board}/revert/{entry_id}")
    assert columns(admin_client, board)[0]["wip_limit"] == 2
    items = admin_client.get(checklist_url(ticket["id"])).get_json()["checklist"]
    assert [(i["text"], i["done"]) for i in items] == [("Plan", False)]


def test_revert_of_older_snapshot_keeps_checklist(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id, "Old")
    entry_id = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board,))
    snapshot = json.loads(db.scalar("SELECT snapshot FROM kanban_board_history WHERE id = ?", (entry_id,)))
    for column in snapshot["columns"]:
        column.pop("wip_limit")
        for item in column["tickets"]:
            item.pop("checklist")
    db.execute("UPDATE kanban_board_history SET snapshot = ? WHERE id = ?", (json.dumps(snapshot), entry_id))
    add_item(admin_client, ticket["id"], "Kept")
    admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 4})
    admin_client.post(f"/kanban/{board}/revert/{entry_id}")
    assert columns(admin_client, board)[0]["wip_limit"] == 4
    assert [i["text"] for i in admin_client.get(checklist_url(ticket["id"])).get_json()["checklist"]] == ["Kept"]


def test_export_import_keeps_limits_and_checklists(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id, "Exported")
    item = add_item(admin_client, ticket["id"], "One")["checklist"][0]["id"]
    admin_client.put(f"/api/kanban/checklist/{item}", json={"done": True})
    add_item(admin_client, ticket["id"], "Two")
    admin_client.put(f"/api/kanban/columns/{column_id}", json={"wip_limit": 6})
    document = json.loads(admin_client.get(f"/kanban/{board}/export?format=json").data)
    assert document["columns"][0]["wip_limit"] == 6
    assert document["columns"][0]["tickets"][0]["checklist"] == [{"text": "One", "done": True},
                                                                 {"text": "Two", "done": False}]
    document["columns"][1]["wip_limit"] = "junk"
    document["columns"][0]["tickets"][0]["checklist"].append({"text": "", "done": True})
    document["columns"][0]["tickets"][0]["checklist"].append("junk")
    imported = board_id_from(admin_client.post(
        "/kanban/import", data={"import_file": (io.BytesIO(json.dumps(document).encode()), "b.kanban.json")},
        content_type="multipart/form-data"))
    state = admin_client.get(f"/api/kanban/{imported}/state").get_json()
    assert [c["wip_limit"] for c in state["columns"]] == [6, None, None]
    new_id = state["columns"][0]["tickets"][0]
    items = admin_client.get(checklist_url(new_id)).get_json()["checklist"]
    assert [(i["text"], i["done"]) for i in items] == [("One", True), ("Two", False)]


# ── My tickets ───────────────────────────────────────────────────────────────


def test_my_tickets_lists_assigned_tickets_on_visible_boards(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    bob_user = make_user("bob")
    bob = as_client(app, login, bob_user)
    cols = columns(admin_client, board)
    late = new_ticket(admin_client, cols[0]["id"], "Late @bob due:2000-01-01 !high")
    later = new_ticket(admin_client, cols[1]["id"], "Later @bob due:2999-01-01")
    undated = new_ticket(admin_client, cols[0]["id"], "Undated @bob")
    new_ticket(admin_client, cols[0]["id"], "Not mine")
    legacy = new_ticket(admin_client, cols[0]["id"], "Legacy")
    db.execute("UPDATE kanban_tickets SET assigned_to = ? WHERE id = ?", (bob_user["id"], legacy["id"]))

    data = bob.get("/api/kanban/my-tickets").get_json()["tickets"]
    assert [t["id"] for t in data] == [late["id"], later["id"], undated["id"], legacy["id"]]
    assert data[0]["board_title"] == "Roadmap" and data[0]["column_title"] == cols[0]["title"]
    assert data[1]["column_title"] == cols[1]["title"]
    assert data[0]["url"] == f"/kanban/{board}#ticket-{late['id']}"

    page = bob.get("/kanban/mine").get_data(as_text=True)
    assert page.index("Late") < page.index("Later") < page.index("Undated")
    assert "Not mine" not in page and 'id="kanban-mine-overdue"' in page and 'id="kanban-mine-none"' in page

    # Once the board is hidden from bob, its tickets leave his list.
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    assert bob.get("/api/kanban/my-tickets").get_json()["tickets"] == []
    assert "Late" not in bob.get("/kanban/mine").get_data(as_text=True)


def test_my_tickets_access(app, admin_client, board):
    anonymous = app.test_client()
    assert anonymous.get("/kanban/mine").status_code == 302
    assert anonymous.get("/api/kanban/my-tickets", headers={"Accept": "application/json"}).status_code in (401, 302)
    assert admin_client.get("/kanban/mine").status_code == 200
    assert b"/kanban/mine" in admin_client.get("/kanban").data
    with app.test_request_context(), connection_scope():
        registry.set_enabled("kanban", False)
    assert admin_client.get("/kanban/mine").status_code == 404
    assert admin_client.get("/api/kanban/my-tickets").status_code == 404


# ── Pages and schema ─────────────────────────────────────────────────────────


def test_board_page_has_filter_bar_and_checklist(admin_client, board):
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    assert 'id="kanban-filter"' in html and 'role="search"' in html and "kanban-filter.js" in html
    assert 'id="kanban-ticket-checklist"' in html
    for url in (f"/kanban/{board}", "/kanban/mine"):
        page = admin_client.get(url).get_data(as_text=True)
        for tag in re.findall(r"<script\b[^>]*>", page):
            assert "src=" in tag or 'type="application/json"' in tag, (url, tag)


def test_schema_upgrade_is_idempotent(db):
    schema.upgrade_v4(db.conn)
    schema.upgrade_v4(db.conn)
    assert db.scalar("SELECT COUNT(*) FROM pragma_table_info('kanban_columns') WHERE name = 'wip_limit'") == 1
