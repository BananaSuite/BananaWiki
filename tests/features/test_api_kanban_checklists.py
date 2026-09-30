"""REST API for kanban checklists, column WIP limits and "my tickets": access rules and IDOR checks."""

from __future__ import annotations

import pytest

from bananawiki.wiki.features.kanban import service as kanban_service
from bananawiki.wiki.features.kanban import store as kanban_store

from .api_support import call, enable_api, issue
from .pages_support import in_app


@pytest.fixture
def api_app(app, db):
    enable_api(app, db)
    return app


@pytest.fixture
def people(make_user):
    return {
        "admin": make_user("boss", role="admin"),
        "member": make_user("member1", api_access_enabled=1),
    }


@pytest.fixture
def board(api_app, people):
    return in_app(api_app, lambda: kanban_service.create_board(people["admin"], "Roadmap", "Plans"))


def settings(db, **values):
    for name, value in values.items():
        db.execute(f"UPDATE site_settings SET {name} = ? WHERE id = 1", (value,))


def new_ticket(app, board, user, **data):
    column = in_app(app, lambda: kanban_store.columns_of(board["id"]))[0]
    return in_app(app, lambda: kanban_service.create_ticket(board, column, user, {"title": "Task", **data}))


def test_checklist_lifecycle(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    ticket = new_ticket(api_app, board, people["admin"])
    base = f"/kanban/tickets/{ticket['id']}/checklist"
    assert call(client, "GET", base, token).json["checklist"] == []

    first = call(client, "POST", base, token, json={"text": "Write tests"})
    assert first.status_code == 201, first.json
    second = call(client, "POST", base, token, json={"text": "Ship"}).json
    items = second["checklist"]
    assert [item["text"] for item in items] == ["Write tests", "Ship"]
    assert second["ticket"]["checklist_total"] == 2 and second["ticket"]["checklist_done"] == 0

    ticked = call(client, "PUT", f"/kanban/checklist/{items[0]['id']}", token, json={"done": True, "text": "Tests"})
    assert ticked.status_code == 200
    assert ticked.json["checklist"][0] == {"id": items[0]["id"], "text": "Tests", "done": True}
    assert ticked.json["ticket"]["checklist_done"] == 1

    reordered = call(client, "POST", f"{base}/reorder", token, json={"order": [items[1]["id"], items[0]["id"]]})
    assert [item["id"] for item in reordered.json["checklist"]] == [items[1]["id"], items[0]["id"]]

    card = call(client, "GET", f"/kanban/tickets/{ticket['id']}", token).json["ticket"]
    assert card["checklist_total"] == 2 and card["checklist_done"] == 1
    listed = call(client, "GET", f"/kanban/boards/{board['id']}", token).json["tickets"]
    assert listed[0]["checklist_total"] == 2

    deleted = call(client, "DELETE", f"/kanban/checklist/{items[1]['id']}", token)
    assert deleted.status_code == 200 and deleted.json["deleted"] is True
    assert [item["text"] for item in deleted.json["checklist"]] == ["Tests"]
    assert call(client, "DELETE", f"/kanban/checklist/{items[1]['id']}", token).json["code"] \
        == "checklist_item_not_found"


def test_checklist_input_is_validated(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    ticket = new_ticket(api_app, board, people["admin"])
    base = f"/kanban/tickets/{ticket['id']}/checklist"
    for body in ({}, {"text": "   "}, {"text": 5}, {"text": "x" * 301}):
        answer = call(client, "POST", base, token, json=body)
        assert answer.status_code == 400 and answer.json["field"] == "text", body
    item = call(client, "POST", base, token, json={"text": "One"}).json["checklist"][0]
    assert call(client, "PUT", f"/kanban/checklist/{item['id']}", token, json={"done": "yes"}).json["field"] == "done"
    assert call(client, "PUT", f"/kanban/checklist/{item['id']}", token, json={}).status_code == 400
    assert call(client, "POST", f"{base}/reorder", token, json={"order": "1,2"}).json["field"] == "order"


def test_checklists_follow_the_board_rules(api_app, client, db, board, people):
    member = issue(api_app, people["member"], ["kanban"])
    admin = issue(api_app, people["admin"], ["kanban"])
    ticket = new_ticket(api_app, board, people["admin"])
    item = call(client, "POST", f"/kanban/tickets/{ticket['id']}/checklist", admin,
                json={"text": "Hidden"}).json["checklist"][0]
    probes = (("GET", f"/kanban/tickets/{ticket['id']}/checklist"),
              ("POST", f"/kanban/tickets/{ticket['id']}/checklist"),
              ("POST", f"/kanban/tickets/{ticket['id']}/checklist/reorder"),
              ("PUT", f"/kanban/checklist/{item['id']}"), ("DELETE", f"/kanban/checklist/{item['id']}"))
    body = {"text": "x", "order": [item["id"]], "done": True}
    # Default settings: members cannot see the board at all.
    for method, path in probes:
        assert call(client, method, path, member, json=body).status_code == 404, path

    settings(db, kanban_access="all", kanban_write_access="admin")
    assert call(client, "GET", probes[0][1], member).json["checklist"][0]["text"] == "Hidden"
    for method, path in probes[1:]:
        answer = call(client, method, path, member, json=body)
        assert answer.status_code == 403 and answer.json["code"] == "board_forbidden", path
    read_only = issue(api_app, people["admin"], ["kanban"], write=False)
    assert call(client, "PUT", f"/kanban/checklist/{item['id']}", read_only, json=body).json["code"] \
        == "scope_missing"
    assert call(client, "GET", f"/kanban/tickets/{ticket['id']}/checklist", admin).json["checklist"][0]["done"] \
        is False


def test_column_wip_limit(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    column = in_app(api_app, lambda: kanban_store.columns_of(board["id"]))[0]
    path = f"/kanban/columns/{column['id']}"
    limited = call(client, "PUT", path, token, json={"wip_limit": 3})
    assert limited.status_code == 200 and limited.json["column"]["wip_limit"] == 3
    assert limited.json["column"]["title"] == column["title"]
    columns = call(client, "GET", f"/kanban/boards/{board['id']}", token).json["board"]["columns"]
    assert columns[0]["wip_limit"] == 3
    both = call(client, "PUT", path, token, json={"title": "Next", "wip_limit": None}).json["column"]
    assert both["title"] == "Next" and both["wip_limit"] is None
    for value in ("3", 1.5, True, -1, 1000):
        assert call(client, "PUT", path, token, json={"wip_limit": value}).status_code == 400, value
    assert call(client, "PUT", path, token, json={}).status_code == 400
    member = issue(api_app, people["member"], ["kanban"])
    assert call(client, "PUT", path, member, json={"wip_limit": 2}).status_code == 404


def test_my_tickets(api_app, client, db, board, people):
    settings(db, kanban_access="all", kanban_write_access="all")
    member = people["member"]
    mine = new_ticket(api_app, board, people["admin"], title="Mine", assignees=[member["id"]], due_date="2030-01-01")
    new_ticket(api_app, board, people["admin"], title="Not mine")
    token = issue(api_app, member, ["kanban"])
    answer = call(client, "GET", "/kanban/my-tickets", token).json
    assert [row["id"] for row in answer["tickets"]] == [mine["id"]]
    row = answer["tickets"][0]
    assert row["board_title"] == "Roadmap" and row["board_id"] == board["id"] and row["column_title"]
    assert row["due_date"] == "2030-01-01" and "description" not in row and answer["next_offset"] is None

    second = new_ticket(api_app, board, people["admin"], title="Later", assignees=[member["id"]])
    paged = call(client, "GET", "/kanban/my-tickets?limit=1", token).json
    assert [row["id"] for row in paged["tickets"]] == [mine["id"]] and paged["next_offset"] == 1
    rest = call(client, "GET", "/kanban/my-tickets?limit=1&offset=1", token).json
    assert [row["id"] for row in rest["tickets"]] == [second["id"]] and rest["next_offset"] is None

    in_app(api_app, lambda: kanban_service.set_visibility(board, "private"))
    assert call(client, "GET", "/kanban/my-tickets", token).json["tickets"] == []
    settings(db, kanban_access="admin", kanban_write_access="admin")
    in_app(api_app, lambda: kanban_service.set_visibility(board, "public"))
    assert call(client, "GET", "/kanban/my-tickets", token).json["tickets"] == []
