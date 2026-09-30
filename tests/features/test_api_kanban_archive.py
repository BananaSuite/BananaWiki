"""REST API for kanban archiving (tickets, columns, boards) and board filters: rules and IDOR checks."""

from __future__ import annotations

import pytest

from bananawiki.sdk.client import WikiClient
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
        "editor": make_user("editor1", role="editor", api_access_enabled=1),
        "member": make_user("member1", api_access_enabled=1),
    }


@pytest.fixture
def board(api_app, people):
    return in_app(api_app, lambda: kanban_service.create_board(people["admin"], "Roadmap", "Plans"))


def settings(db, **values):
    for name, value in values.items():
        db.execute(f"UPDATE site_settings SET {name} = ? WHERE id = 1", (value,))


def columns(app, board):
    return in_app(app, lambda: kanban_store.columns_of(board["id"]))


def new_ticket(app, board, user, column=0, **data):
    target = columns(app, board)[column]
    return in_app(app, lambda: kanban_service.create_ticket(board, target, user, {"title": "Task", **data}))


def test_ticket_archive_and_restore(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    first = new_ticket(api_app, board, people["admin"], title="First")
    second = new_ticket(api_app, board, people["admin"], title="Second")
    archived = call(client, "POST", f"/kanban/tickets/{first['id']}/archive", token)
    assert archived.status_code == 200 and archived.json["changed"] is True
    assert archived.json["ticket"]["archived_at"]
    assert call(client, "POST", f"/kanban/tickets/{first['id']}/archive", token).json["changed"] is False

    state = call(client, "GET", f"/kanban/boards/{board['id']}", token).json
    assert [row["id"] for row in state["tickets"]] == [second["id"]]
    assert state["board"]["columns"][0]["tickets"] == [second["id"]] and state["tickets"][0]["archived_at"] is None
    # Still readable, editable and commentable, but not movable.
    assert call(client, "GET", f"/kanban/tickets/{first['id']}", token).json["ticket"]["archived_at"]
    assert call(client, "PUT", f"/kanban/tickets/{first['id']}", token, json={"title": "Renamed"}).status_code == 200
    assert call(client, "POST", f"/kanban/tickets/{first['id']}/comments", token,
                json={"content": "Note"}).status_code == 201
    target = columns(api_app, board)[1]["id"]
    moved = call(client, "POST", f"/kanban/tickets/{first['id']}/move", token, json={"column_id": target})
    assert moved.status_code == 409 and moved.json["code"] == "ticket_archived"

    listed = call(client, "GET", f"/kanban/boards/{board['id']}/archived-tickets", token).json
    assert [row["id"] for row in listed["tickets"]] == [first["id"]] and listed["total"] == 1
    assert listed["tickets"][0]["archived_by_username"] == "boss" and listed["tickets"][0]["column_title"]

    restored = call(client, "POST", f"/kanban/tickets/{first['id']}/restore", token).json
    assert restored["changed"] is True and restored["ticket"]["archived_at"] is None
    state = call(client, "GET", f"/kanban/boards/{board['id']}", token).json
    assert state["board"]["columns"][0]["tickets"] == [second["id"], first["id"]]  # back at the end


def test_bulk_and_column_archive(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    done = [new_ticket(api_app, board, people["admin"], column=2, title=f"Done {n}") for n in range(3)]
    keep = new_ticket(api_app, board, people["admin"], title="Keep")
    column = columns(api_app, board)[2]["id"]
    assert call(client, "POST", f"/kanban/columns/{column}/archive", token).json["archived"] == 3
    assert call(client, "POST", f"/kanban/columns/{column}/archive", token).json["archived"] == 0
    ids = [row["id"] for row in done[:2]]
    path = f"/kanban/boards/{board['id']}/tickets"
    assert call(client, "POST", f"{path}/restore", token, json={"ids": ids}).json["restored"] == 2
    assert call(client, "POST", f"{path}/archive", token, json={"ids": ids + [keep["id"]]}).json["archived"] == 3
    assert call(client, "POST", f"{path}/archive", token, json={"ids": []}).json["field"] == "ids"
    assert call(client, "POST", f"{path}/archive", token, json={"ids": ["x"]}).status_code == 400

    other = in_app(api_app, lambda: kanban_service.create_board(people["admin"], "Other"))
    foreign = new_ticket(api_app, other, people["admin"])
    refused = call(client, "POST", f"{path}/restore", token, json={"ids": [foreign["id"]]})
    assert refused.status_code == 404 and refused.json["code"] == "not_on_board"
    assert in_app(api_app, lambda: kanban_store.get_ticket(foreign["id"]))["archived_at"] is None


def test_archive_needs_board_write_access(api_app, client, db, board, people):
    member = issue(api_app, people["member"], ["kanban"])
    ticket = new_ticket(api_app, board, people["admin"])
    column = columns(api_app, board)[0]["id"]
    probes = (("POST", f"/kanban/tickets/{ticket['id']}/archive"),
              ("POST", f"/kanban/tickets/{ticket['id']}/restore"),
              ("POST", f"/kanban/columns/{column}/archive"),
              ("POST", f"/kanban/boards/{board['id']}/tickets/archive"),
              ("POST", f"/kanban/boards/{board['id']}/tickets/restore"),
              ("POST", f"/kanban/boards/{board['id']}/archive"),
              ("POST", f"/kanban/boards/{board['id']}/restore"),
              ("GET", f"/kanban/boards/{board['id']}/archived-tickets"))
    for method, path in probes:
        assert call(client, method, path, member, json={"ids": [ticket["id"]]}).status_code == 404, path
    settings(db, kanban_access="all", kanban_write_access="admin")
    for method, path in probes[:-1]:
        answer = call(client, method, path, member, json={"ids": [ticket["id"]]})
        assert answer.status_code == 403 and answer.json["code"] == "board_forbidden", path
    assert call(client, "GET", probes[-1][1], member).status_code == 200
    assert in_app(api_app, lambda: kanban_store.get_ticket(ticket["id"]))["archived_at"] is None


def test_board_archive_is_owner_only_and_read_only(api_app, client, db, people, make_user):
    settings(db, kanban_access="editor", kanban_write_access="editor")
    owner = issue(api_app, people["editor"], ["kanban"])
    board = call(client, "POST", "/kanban/boards", owner, json={"title": "Team"}).json["board"]
    other_editor = issue(api_app, make_user("editor2", role="editor", api_access_enabled=1), ["kanban"])
    assert call(client, "POST", f"/kanban/boards/{board['id']}/archive", other_editor).status_code == 403

    archived = call(client, "POST", f"/kanban/boards/{board['id']}/archive", owner)
    assert archived.status_code == 200 and archived.json["board"]["archived_at"]
    assert archived.json["board"]["can_write"] is False
    assert [row["id"] for row in call(client, "GET", "/kanban/boards", owner).json["boards"]] == []
    assert [row["id"] for row in call(client, "GET", "/kanban/boards?archived=1", owner).json["boards"]] \
        == [board["id"]]
    assert call(client, "GET", "/kanban/boards?archived=maybe", owner).status_code == 400

    column = call(client, "GET", f"/kanban/boards/{board['id']}", owner).json["board"]["columns"][0]["id"]
    for method, path, body in (("POST", f"/kanban/columns/{column}/tickets", {"title": "New"}),
                               ("PUT", f"/kanban/boards/{board['id']}", {"title": "Renamed"}),
                               ("POST", f"/kanban/boards/{board['id']}/columns", {"title": "More"})):
        answer = call(client, method, path, owner, json=body)
        assert answer.status_code == 403 and answer.json["code"] == "board_forbidden", path
    # The owner keeps visibility, restore and delete.
    assert call(client, "PUT", f"/kanban/boards/{board['id']}", owner,
                json={"visibility": "private"}).json["board"]["visibility"] == "private"
    restored = call(client, "POST", f"/kanban/boards/{board['id']}/restore", owner).json["board"]
    assert restored["archived_at"] is None and restored["can_write"] is True
    assert call(client, "POST", f"/kanban/columns/{column}/tickets", owner, json={"title": "New"}).status_code == 201
    call(client, "POST", f"/kanban/boards/{board['id']}/archive", owner)
    assert call(client, "DELETE", f"/kanban/boards/{board['id']}", owner).status_code == 200



def test_filters_on_board_and_my_tickets(api_app, client, db, board, people):
    settings(db, kanban_access="all", kanban_write_access="all")
    member = people["member"]
    urgent = new_ticket(api_app, board, people["admin"], title="Fix login", priority="critical",
                        labels=["bug"], assignees=[member["id"]])
    new_ticket(api_app, board, people["admin"], title="Write docs", priority="low", assignees=[member["id"]])
    token = issue(api_app, member, ["kanban"])
    path = f"/kanban/boards/{board['id']}"
    everything = call(client, "GET", path, token).json
    assert len(everything["tickets"]) == 2 and "shown" not in everything
    narrowed = call(client, "GET", f"{path}?priority=critical", token).json
    assert [row["id"] for row in narrowed["tickets"]] == [urgent["id"]]
    assert narrowed["shown"] == 1 and narrowed["total"] == 2 and narrowed["filter"]["priority"] == "critical"
    assert narrowed["board"]["columns"][0]["tickets"] == [urgent["id"]]
    assert [row["id"] for row in call(client, "GET", f"{path}?q=login&who=me", token).json["tickets"]] \
        == [urgent["id"]]
    assert call(client, "GET", f"{path}?label=nope", token).json["tickets"] == []
    bad = call(client, "GET", f"{path}?due=someday", token)
    assert bad.status_code == 400 and bad.json["code"] == "invalid_filter"

    mine = call(client, "GET", "/kanban/my-tickets?label=bug", token).json["tickets"]
    assert [row["id"] for row in mine] == [urgent["id"]]
    assert call(client, "GET", "/kanban/my-tickets?priority=urgent", token).json["code"] == "invalid_filter"
    in_app(api_app, lambda: kanban_service.archive_tickets(board, [kanban_store.get_ticket(urgent["id"])],
                                                           people["admin"]))
    assert [row["title"] for row in call(client, "GET", "/kanban/my-tickets", token).json["tickets"]] \
        == ["Write docs"]


def test_sdk_archive_helpers(monkeypatch):
    sent = []
    wiki = WikiClient("https://wiki.example.org", "token")

    def fake(method, path, payload=None, **options):
        sent.append((method, path, payload, options.get("query")))
        return {"board": {}, "ticket": {}, "archived": 1, "restored": 1, "tickets": [], "boards": [],
                "next_offset": None}

    monkeypatch.setattr(wiki, "request", fake)
    wiki.archive_board(2)
    wiki.archive_tickets(2, [5, 6])
    wiki.restore_ticket(5)
    wiki.archive_column(3)
    wiki.board(2, priority="high")
    wiki.boards(archived=True)
    assert sent[:5] == [("POST", "/kanban/boards/2/archive", None, None),
                        ("POST", "/kanban/boards/2/tickets/archive", {"ids": [5, 6]}, None),
                        ("POST", "/kanban/tickets/5/restore", None, None),
                        ("POST", "/kanban/columns/3/archive", None, None),
                        ("GET", "/kanban/boards/2", None, {"priority": "high"})]
    assert sent[5][3]["archived"] == 1
