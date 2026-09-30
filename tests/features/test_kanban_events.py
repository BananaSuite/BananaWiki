"""Kanban registry events (the Round 2 contract) and the server-side ticket filters."""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.kanban import filters, service, store
from bananawiki.wiki.features.kanban.fields import KanbanError

from .test_kanban import as_client, board_id_from, columns, new_ticket, set_settings

EVENTS = ("kanban.board.created", "kanban.board.updated", "kanban.board.deleted", "kanban.ticket.created",
          "kanban.ticket.updated", "kanban.ticket.moved", "kanban.ticket.deleted", "kanban.comment.created")


@pytest.fixture
def received(app):
    """Every kanban event emitted during the test, as ``(name, payload)``."""
    seen: list[tuple[str, dict]] = []
    handlers = app.extensions["bananawiki.registry"]._handlers
    for name in EVENTS:
        handlers.setdefault(name, []).append(("kanban", lambda _name=name, **payload: seen.append((_name, payload))))
    return seen


def names(received, prefix="kanban."):
    return [name for name, _ in received if name.startswith(prefix)]


def test_board_and_ticket_events_carry_rows_and_actor(admin_client, admin, received):
    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Launch"}))
    name, payload = received[-1]
    assert name == "kanban.board.created" and payload["board"]["id"] == board
    assert payload["board"]["title"] == "Launch" and payload["actor_id"] == admin["id"]

    received.clear()
    admin_client.post(f"/kanban/{board}/edit", data={"title": "Launch 2", "description": ""})
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "shared"})
    assert names(received) == ["kanban.board.updated", "kanban.board.updated"]
    assert received[0][1]["board"]["title"] == "Launch 2" and received[1][1]["board"]["visibility"] == "shared"

    cols = columns(admin_client, board)
    received.clear()
    ticket = new_ticket(admin_client, cols[0]["id"], "Write copy")
    name, payload = received[-1]
    assert name == "kanban.ticket.created"
    assert payload["ticket"]["id"] == ticket["id"] and payload["ticket"]["board_id"] == board
    assert payload["board"]["id"] == board and payload["actor_id"] == admin["id"]

    received.clear()
    admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"priority": "high"})
    assert names(received) == ["kanban.ticket.updated"] and received[0][1]["ticket"]["priority"] == "high"

    received.clear()
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/move", json={"column_id": cols[1]["id"], "position": 0})
    name, payload = received[-1]
    assert name == "kanban.ticket.moved"
    assert (payload["from_column_id"], payload["to_column_id"]) == (cols[0]["id"], cols[1]["id"])
    assert payload["ticket"]["column_id"] == cols[1]["id"]

    received.clear()
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "Looks good"})
    name, payload = received[-1]
    assert name == "kanban.comment.created" and payload["comment"]["content"] == "Looks good"
    assert payload["ticket"]["id"] == ticket["id"] and payload["board"]["id"] == board
    assert payload["actor_id"] == admin["id"]

    received.clear()
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/checklist", json={"text": "Proofread"})
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/archive")
    assert names(received) == ["kanban.ticket.updated", "kanban.ticket.updated"]
    assert received[-1][1]["ticket"]["archived_at"]

    received.clear()
    admin_client.delete(f"/api/kanban/tickets/{ticket['id']}")
    name, payload = received[-1]
    assert name == "kanban.ticket.deleted" and payload["ticket"]["id"] == ticket["id"]
    assert payload["ticket"]["title"] == "Write copy" and payload["board"]["id"] == board

    received.clear()
    admin_client.post(f"/kanban/{board}/delete")
    assert names(received) == ["kanban.board.deleted"]
    assert received[0][1]["board"]["title"] == "Launch 2" and received[0][1]["actor_id"] == admin["id"]


def test_bulk_actions_emit_one_event_per_ticket(admin_client, received):
    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Bulk"}))
    cols = columns(admin_client, board)
    ids = [new_ticket(admin_client, cols[0]["id"], title)["id"] for title in ("A", "B", "C")]
    url = f"/api/kanban/{board}/tickets/bulk"

    received.clear()
    admin_client.post(url, json={"ticket_ids": ids, "action": "priority", "priority": "low"})
    assert names(received) == ["kanban.ticket.updated"] * 3
    assert sorted(p["ticket"]["id"] for _, p in received) == sorted(ids)

    received.clear()
    admin_client.post(url, json={"ticket_ids": ids[:2], "action": "move", "column_id": cols[2]["id"]})
    assert names(received) == ["kanban.ticket.moved"] * 2
    assert {(p["from_column_id"], p["to_column_id"]) for _, p in received} == {(cols[0]["id"], cols[2]["id"])}

    received.clear()
    admin_client.post(url, json={"ticket_ids": ids, "action": "archive"})
    admin_client.post(url, json={"ticket_ids": ids, "action": "restore"})
    assert names(received) == ["kanban.ticket.updated"] * 6

    received.clear()
    admin_client.post(f"/api/kanban/{board}/columns/bulk", json={"action": "delete", "column_ids": [cols[0]["id"]]})
    assert names(received) == ["kanban.ticket.deleted", "kanban.board.updated"]

    received.clear()
    admin_client.post(url, json={"ticket_ids": ids[:2], "action": "delete"})
    assert names(received) == ["kanban.ticket.deleted"] * 2


def test_import_revert_and_column_changes_emit_board_events(admin_client, db, received):
    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Source"}))
    column = admin_client.post(f"/api/kanban/{board}/columns", json={"title": "QA"}).get_json()
    admin_client.put(f"/api/kanban/columns/{column['id']}", json={"wip_limit": 3})
    assert names(received)[-2:] == ["kanban.board.updated", "kanban.board.updated"]

    received.clear()
    entry = db.scalar("SELECT MIN(id) FROM kanban_board_history WHERE board_id = ?", (board,))
    admin_client.post(f"/kanban/{board}/revert/{entry}")
    assert "kanban.board.updated" in names(received)

    received.clear()
    document = admin_client.get(f"/kanban/{board}/export?format=json").data
    admin_client.post("/kanban/import", data={"import_file": (io.BytesIO(document), "b.kanban.json")},
                      content_type="multipart/form-data")
    assert names(received) == ["kanban.board.created"]


def test_failing_handler_never_breaks_the_action(app, admin_client, db, received):
    def broken(**_payload):
        raise RuntimeError("handler exploded")

    handlers = app.extensions["bananawiki.registry"]._handlers
    for name in EVENTS:
        handlers[name].insert(0, ("kanban", broken))
    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Robust"}))
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"], "Still saved")
    assert admin_client.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "hi"}).status_code == 201
    assert admin_client.delete(f"/api/kanban/tickets/{ticket['id']}").status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM kanban_tickets") == 0
    # The healthy handler after the broken one still ran for every event.
    assert names(received)[:2] == ["kanban.board.created", "kanban.ticket.created"]
    assert "kanban.comment.created" in names(received) and "kanban.ticket.deleted" in names(received)


def test_service_calls_outside_views_emit_with_their_actor(app, admin, received):
    with app.test_request_context(), connection_scope():
        board = service.create_board(admin, "Direct")
        column = store.columns_of(board["id"])[0]
        card = service.create_ticket(board, column, admin, {"title": "From a script"})
        service.archive_board(store.get_board(board["id"]), admin)
        service.delete_board(store.get_board(board["id"]))
    assert names(received) == ["kanban.board.created", "kanban.ticket.created", "kanban.board.updated",
                               "kanban.board.deleted"]
    assert received[1][1]["ticket"]["id"] == card["id"]
    assert received[2][1]["board"]["archived_at"] and received[-1][1]["actor_id"] is None


# ── Server-side filters ──────────────────────────────────────────────────────


@pytest.fixture
def filtered_board(app, admin_client, db, make_user):
    set_settings(db, kanban_access="all")
    bob = make_user("bob")
    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Filters"}))
    cols = columns(admin_client, board)
    with app.test_request_context(), connection_scope():
        today = filters.site_today()
    tickets = {
        "late": new_ticket(admin_client, cols[0]["id"], "Late invoice @bob +finance !high",
                           due_date=(today - timedelta(days=3)).isoformat()),
        "soon": new_ticket(admin_client, cols[0]["id"], "Soon report @admin_user +Finance",
                           due_date=(today + timedelta(days=1)).isoformat()),
        "week": new_ticket(admin_client, cols[1]["id"], "Weekly sync", due_date=(today + timedelta(days=6)).isoformat()),
        "free": new_ticket(admin_client, cols[1]["id"], "Free text !low"),
    }
    return board, bob, tickets


def state(client, board, **params):
    response = client.get(f"/api/kanban/{board}/state", query_string=params)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_state_endpoint_applies_the_filter_bar_rules(admin_client, filtered_board):
    board, bob, tickets = filtered_board
    ids = {name: ticket["id"] for name, ticket in tickets.items()}

    def shown(**params):
        data = state(admin_client, board, **params)
        visible = {int(key) for key in data["tickets"]}
        assert visible == {tid for column in data["columns"] for tid in column["tickets"]}
        assert data["total"] == 4 and data["shown"] == len(visible) and data["filter"]["q"] == params.get("q", "")
        return visible

    assert "filter" not in state(admin_client, board)
    assert shown(q="invoice") == {ids["late"]}
    assert shown(q="+finance") == {ids["late"], ids["soon"]}
    assert shown(q=f"#{ids['week']}") == {ids["week"]}
    assert shown(q="@bob late") == {ids["late"]} and shown(q="@bob soon") == set()
    assert shown(label="FINANCE") == {ids["late"], ids["soon"]}
    assert shown(who="me") == {ids["soon"]} and shown(who=bob["id"]) == {ids["late"]}
    assert shown(who="none") == {ids["week"], ids["free"]}
    assert shown(priority="low") == {ids["free"]}
    assert shown(due="overdue") == {ids["late"]}
    assert shown(due="soon") == {ids["late"], ids["soon"]}
    assert shown(due="week") == {ids["soon"], ids["week"]}
    assert shown(due="none") == {ids["free"]}
    assert shown(q="report", due="soon", label="finance") == {ids["soon"]}


@pytest.mark.parametrize("params", [{"priority": "urgent"}, {"due": "yesterday"}, {"q": "x" * 201},
                                    {"q": " ".join(["w"] * 21)}, {"label": "l" * 41}, {"who": "u" * 65}])
def test_invalid_filters_are_refused(admin_client, filtered_board, params):
    board, _bob, _tickets = filtered_board
    assert admin_client.get(f"/api/kanban/{board}/state", query_string=params).status_code == 400
    assert admin_client.get("/api/kanban/my-tickets", query_string=params).status_code == 400
    assert admin_client.get("/kanban/mine", query_string=params).status_code == 200


def test_filtered_state_still_needs_view_access(app, admin_client, filtered_board, login, make_user):
    board, _bob, _tickets = filtered_board
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    outsider = as_client(app, login, make_user("outsider"))
    assert outsider.get(f"/api/kanban/{board}/state?q=invoice").status_code == 404
    assert app.test_client().get(f"/api/kanban/{board}/state?q=invoice",
                                 headers={"Accept": "application/json"}).status_code in (401, 302)


def test_my_tickets_filters(app, admin_client, filtered_board, login):
    board, bob_user, tickets = filtered_board
    bob = as_client(app, login, bob_user)
    admin_client.put(f"/api/kanban/tickets/{tickets['week']['id']}", json={"assignees": [bob_user["id"]]})

    def mine(**params):
        return [t["id"] for t in bob.get("/api/kanban/my-tickets", query_string=params).get_json()["tickets"]]

    assert mine() == [tickets["late"]["id"], tickets["week"]["id"]]
    assert mine(due="overdue") == [tickets["late"]["id"]]
    assert mine(q="sync") == [tickets["week"]["id"]] and mine(label="finance") == [tickets["late"]["id"]]
    assert mine(priority="high", due="week") == []
    # "who" is ignored: these are always the caller's own tickets.
    assert mine(who="none") == mine()
    page = bob.get("/kanban/mine?q=sync").get_data(as_text=True)
    assert "Weekly sync" in page and "Late invoice" not in page and 'value="sync"' in page
    assert "No ticket matches the filter" in bob.get("/kanban/mine?q=nothing").get_data(as_text=True)


def test_filter_rules_unit():
    today = date(2026, 3, 10)
    card = {"id": 7, "title": "Fix Login", "labels": ["Auth"], "priority": "high", "due_date": "2026-03-12",
            "assignees": [{"id": "u1", "username": "Ann"}]}
    match = filters.matches
    assert match(card, filters.parse({"q": "fix #7 +auth @ann"}), user_id=None, today=today)
    assert not match(card, filters.parse({"q": "fix logout"}), user_id=None, today=today)
    assert match(card, filters.parse({"who": "me"}), user_id="u1", today=today)
    assert not match(card, filters.parse({"who": "me"}), user_id=None, today=today)
    assert match(card, filters.parse({"due": "soon"}), user_id=None, today=today)
    assert not match(card, filters.parse({"due": "soon"}), user_id=None, today=today - timedelta(days=1))
    assert match(card, filters.parse({"due": "week"}), user_id=None, today=today)
    assert not match({**card, "due_date": "2026-03-01"}, filters.parse({"due": "week"}), user_id=None, today=today)
    assert filters.parse({"q": "  a   b "}).q == "a b" and not filters.parse({}).active
    with pytest.raises(KanbanError):
        filters.parse({"due": "later"})
