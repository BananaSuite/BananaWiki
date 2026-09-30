"""REST API for kanban boards and canvases: the web interface's access rules, IDOR checks, feature switches."""

from __future__ import annotations

import io

import pytest

from bananawiki.wiki.features.canvas import service as canvas_service
from bananawiki.wiki.features.kanban import service as kanban_service
from bananawiki.wiki.features.kanban import store as kanban_store

from .api_support import call, enable_api, issue
from .pages_support import in_app, set_feature


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


def settings(db, **values):
    for name, value in values.items():
        db.execute(f"UPDATE site_settings SET {name} = ? WHERE id = 1", (value,))


# ── Kanban ────────────────────────────────────────────────────────────────────


@pytest.fixture
def board(api_app, people):
    """A public board of the administrator with the three default columns."""
    return in_app(api_app, lambda: kanban_service.create_board(people["admin"], "Roadmap", "Plans"))


def board_columns(app, board_id):
    return in_app(app, lambda: kanban_store.columns_of(board_id))


def test_kanban_flow_as_board_writer(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    boards = call(client, "GET", "/kanban/boards", token).json
    assert [row["title"] for row in boards["boards"]] == ["Roadmap"] and boards["next_offset"] is None
    detail = call(client, "GET", f"/kanban/boards/{board['id']}", token).json
    assert len(detail["board"]["columns"]) == 3 and detail["tickets"] == []
    todo, doing = detail["board"]["columns"][0]["id"], detail["board"]["columns"][1]["id"]

    created = call(client, "POST", f"/kanban/columns/{todo}/tickets", token,
                   json={"title": "Write docs +docs !high", "description": "All of it", "labels": ["api"]})
    assert created.status_code == 201, created.json
    ticket = created.json["ticket"]
    assert ticket["title"] == "Write docs" and ticket["priority"] == "high"
    assert ticket["labels"] == ["api"] and ticket["description"] == "All of it"  # explicit fields win

    changed = call(client, "PUT", f"/kanban/tickets/{ticket['id']}", token,
                   json={"description": "Only the API", "due_date": "2030-01-02", "assignees": [people["admin"]["id"]]})
    assert changed.status_code == 200
    assert changed.json["ticket"]["due_date"] == "2030-01-02"
    assert changed.json["ticket"]["assignees"][0]["username"] == "boss"

    moved = call(client, "POST", f"/kanban/tickets/{ticket['id']}/move", token, json={"column_id": doing})
    assert moved.status_code == 200 and moved.json["ticket"]["column_id"] == doing
    assert moved.json["columns"][str(doing)] == [ticket["id"]]

    comment = call(client, "POST", f"/kanban/tickets/{ticket['id']}/comments", token, json={"content": "Started"})
    assert comment.status_code == 201
    comments = call(client, "GET", f"/kanban/tickets/{ticket['id']}/comments", token).json["comments"]
    assert [row["content"] for row in comments] == ["Started"] and comments[0]["author"] == "boss"
    edited = call(client, "PUT", f"/kanban/comments/{comment.json['comment']['id']}", token, json={"content": "Done"})
    assert edited.json["comment"]["content"] == "Done"
    assert call(client, "DELETE", f"/kanban/comments/{comment.json['comment']['id']}", token).status_code == 200

    column = call(client, "POST", f"/kanban/boards/{board['id']}/columns", token, json={"title": "Review"})
    assert column.status_code == 201
    renamed = call(client, "PUT", f"/kanban/columns/{column.json['column']['id']}", token, json={"title": "QA"})
    assert renamed.json["column"]["title"] == "QA"
    order = [column.json["column"]["id"], todo]
    reordered = call(client, "POST", f"/kanban/boards/{board['id']}/columns/reorder", token, json={"order": order})
    assert reordered.json["order"][:2] == order

    assert call(client, "DELETE", f"/kanban/tickets/{ticket['id']}", token).status_code == 200
    assert call(client, "GET", f"/kanban/tickets/{ticket['id']}", token).status_code == 404


def test_kanban_ticket_attachments(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    column = board_columns(api_app, board["id"])[0]["id"]
    ticket = call(client, "POST", f"/kanban/columns/{column}/tickets", token, json={"title": "Files"}).json["ticket"]
    assert call(client, "POST", f"/kanban/tickets/{ticket['id']}/attachments", token).json["code"] == "file_required"
    uploaded = client.post(f"/api/v1/kanban/tickets/{ticket['id']}/attachments",
                           headers={"Authorization": f"Bearer {token}"},
                           data={"file": (io.BytesIO(b"hello"), "notes.txt")}, content_type="multipart/form-data")
    assert uploaded.status_code == 201, uploaded.json
    attachment = uploaded.json["attachment"]
    assert attachment["name"] == "notes.txt" and attachment["size"] == 5
    listed = call(client, "GET", f"/kanban/tickets/{ticket['id']}/attachments", token).json["attachments"]
    assert [row["id"] for row in listed] == [attachment["id"]]
    download = call(client, "GET", f"/kanban/attachments/{attachment['id']}", token)
    assert download.status_code == 200 and download.data == b"hello"
    assert call(client, "DELETE", f"/kanban/attachments/{attachment['id']}", token).status_code == 200
    assert call(client, "GET", f"/kanban/attachments/{attachment['id']}", token).status_code == 404


def test_kanban_hidden_boards_are_404_and_read_only_is_403(api_app, client, db, board, people):
    member = issue(api_app, people["member"], ["kanban"])
    column = board_columns(api_app, board["id"])[0]["id"]
    ticket = in_app(api_app, lambda: kanban_service.create_ticket(board, board_columns(api_app, board["id"])[0],
                                                                  people["admin"], {"title": "Secret"}))
    # Default settings: only administrators reach kanban.
    assert call(client, "GET", "/kanban/boards", member).json["boards"] == []
    for method, path in (("GET", f"/kanban/boards/{board['id']}"), ("GET", f"/kanban/tickets/{ticket['id']}"),
                         ("GET", f"/kanban/tickets/{ticket['id']}/comments"),
                         ("PUT", f"/kanban/columns/{column}"), ("DELETE", f"/kanban/tickets/{ticket['id']}"),
                         ("POST", f"/kanban/tickets/{ticket['id']}/comments")):
        assert call(client, method, path, member, json={"title": "x", "content": "x"}).status_code == 404, path
    assert call(client, "POST", "/kanban/boards", member, json={"title": "Mine"}).json["code"] == "cannot_create_board"

    # Everyone may view, only administrators may write.
    settings(db, kanban_access="all", kanban_write_access="admin")
    assert call(client, "GET", f"/kanban/boards/{board['id']}", member).status_code == 200
    refused = call(client, "PUT", f"/kanban/tickets/{ticket['id']}", member, json={"title": "Mine"})
    assert refused.status_code == 403 and refused.json["code"] == "board_forbidden"
    comment = call(client, "POST", f"/kanban/tickets/{ticket['id']}/comments", member, json={"content": "Hi"})
    assert comment.status_code == 201
    admin = issue(api_app, people["admin"], ["kanban"])
    admin_comment = call(client, "POST", f"/kanban/tickets/{ticket['id']}/comments", admin, json={"content": "Mine"})
    other = call(client, "PUT", f"/kanban/comments/{admin_comment.json['comment']['id']}", member,
                 json={"content": "Hijacked"})
    assert other.status_code == 403 and other.json["code"] == "comment_forbidden"
    assert call(client, "PUT", f"/kanban/boards/{board['id']}", member, json={"title": "New"}).status_code == 403
    assert call(client, "DELETE", f"/kanban/boards/{board['id']}", member).status_code == 403


def test_kanban_moves_stay_on_the_board(api_app, client, board, people):
    token = issue(api_app, people["admin"], ["kanban"])
    other = in_app(api_app, lambda: kanban_service.create_board(people["admin"], "Other"))
    ticket = in_app(api_app, lambda: kanban_service.create_ticket(board, board_columns(api_app, board["id"])[0],
                                                                  people["admin"], {"title": "Stay"}))
    foreign = board_columns(api_app, other["id"])[0]["id"]
    moved = call(client, "POST", f"/kanban/tickets/{ticket['id']}/move", token, json={"column_id": foreign})
    assert moved.status_code == 404 and moved.json["code"] == "column_not_found"
    own = board_columns(api_app, board["id"])[1]["id"]
    assert call(client, "POST", f"/kanban/tickets/{ticket['id']}/move", token,
                json={"column_id": own, "position": -1}).json["field"] == "position"
    assert call(client, "PUT", f"/kanban/tickets/{ticket['id']}", token, json={"labels": "a,b"}).json["field"] \
        == "labels"
    assert call(client, "PUT", f"/kanban/tickets/{ticket['id']}", token, json={"priority": "urgent"}).status_code \
        == 400


def test_kanban_board_owner_actions(api_app, client, db, people):
    settings(db, kanban_access="editor", kanban_write_access="editor")
    token = issue(api_app, people["editor"], ["kanban"])
    created = call(client, "POST", "/kanban/boards", token, json={"title": "Team", "default_columns": False})
    assert created.status_code == 201 and created.json["board"]["is_owner"] is True
    board_id = created.json["board"]["id"]
    assert call(client, "GET", f"/kanban/boards/{board_id}", token).json["board"]["columns"] == []
    changed = call(client, "PUT", f"/kanban/boards/{board_id}", token, json={"visibility": "private", "title": "T2"})
    assert changed.json["board"]["visibility"] == "private" and changed.json["board"]["title"] == "T2"
    assert call(client, "PUT", f"/kanban/boards/{board_id}", token, json={"visibility": "secret"}).status_code == 400
    assert call(client, "DELETE", f"/kanban/boards/{board_id}", token).status_code == 200
    assert call(client, "GET", f"/kanban/boards/{board_id}", token).status_code == 404


def test_kanban_needs_its_scope_and_feature(api_app, client, board, people):
    pages_only = issue(api_app, people["admin"], ["pages"])
    assert call(client, "GET", "/kanban/boards", pages_only).json["code"] == "scope_missing"
    read_only = issue(api_app, people["admin"], ["kanban"], write=False)
    assert call(client, "POST", "/kanban/boards", read_only, json={"title": "X"}).status_code == 403
    set_feature(api_app, "kanban", False)
    token = issue(api_app, people["admin"], ["kanban"])
    assert call(client, "GET", "/kanban/boards", token).status_code == 404


# ── Canvas ────────────────────────────────────────────────────────────────────


@pytest.fixture
def layout(api_app, people):
    return in_app(api_app, lambda: canvas_service.create("Plan", "Ideas", people["admin"]["id"]))


NODE = {"id": "n1", "type": "text", "x": 10, "y": 20, "text": "Hello"}


def test_canvas_flow(api_app, client, layout, people):
    token = issue(api_app, people["admin"], ["canvas"])
    listed = call(client, "GET", "/canvas", token).json["canvases"]
    assert [row["slug"] for row in listed] == [layout["slug"]]
    got = call(client, "GET", f"/canvas/{layout['slug']}", token)
    assert got.status_code == 200 and got.json["data"]["nodes"] == []
    etag = got.headers["ETag"]
    assert etag == f'"v{got.json["canvas"]["version"]}"'

    saved = call(client, "PUT", f"/canvas/{layout['slug']}/document", token,
                 json={"data": {"nodes": [NODE], "edges": []}}, headers={"If-Match": etag})
    assert saved.status_code == 200, saved.json
    assert saved.headers["ETag"] != etag
    stale = call(client, "PUT", f"/canvas/{layout['slug']}/document", token,
                 json={"data": {"nodes": [], "edges": []}}, headers={"If-Match": etag})
    assert stale.status_code == 412 and stale.json["version"] == saved.json["canvas"]["version"]
    conflict = call(client, "PUT", f"/canvas/{layout['slug']}/document", token,
                    json={"data": {"nodes": [], "edges": []}, "expected_version": 1})
    assert conflict.status_code == 409 and conflict.json["code"] == "edit_conflict"

    ops = call(client, "POST", f"/canvas/{layout['slug']}/ops", token,
               json={"ops": [{"op": "upsert_node", "node": {**NODE, "id": "n2", "text": "More"}}]})
    assert ops.status_code == 200 and ops.json["applied"] == 1
    nodes = call(client, "GET", f"/canvas/{layout['slug']}", token).json["data"]["nodes"]
    assert sorted(node["id"] for node in nodes) == ["n1", "n2"]
    assert call(client, "GET", f"/canvas/{layout['slug']}/history", token).json["history"]

    renamed = call(client, "PUT", f"/canvas/{layout['slug']}", token, json={"title": "Plan B"})
    assert renamed.json["canvas"]["title"] == "Plan B" and renamed.json["canvas"]["slug"] == layout["slug"]
    assert call(client, "PUT", f"/canvas/{layout['slug']}/document", token, json={"data": []}).json["field"] == "data"
    assert call(client, "DELETE", f"/canvas/{layout['slug']}", token).status_code == 200
    assert call(client, "GET", f"/canvas/{layout['slug']}", token).status_code == 404


def test_canvas_access_rules(api_app, client, db, layout, people):
    member = issue(api_app, people["member"], ["canvas"])
    assert call(client, "GET", "/canvas", member).json["canvases"] == []
    assert call(client, "GET", f"/canvas/{layout['slug']}", member).status_code == 404
    assert call(client, "POST", "/canvas", member, json={"title": "Mine"}).json["code"] == "cannot_create_canvas"
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'view')",
               (layout["id"], people["member"]["id"]))
    assert call(client, "GET", f"/canvas/{layout['slug']}", member).json["canvas"]["can_edit"] is False
    refused = call(client, "PUT", f"/canvas/{layout['slug']}/document", member,
                   json={"data": {"nodes": [], "edges": []}})
    assert refused.status_code == 403 and refused.json["code"] == "canvas_forbidden"
    db.execute("UPDATE canvas__permissions SET permission = 'edit' WHERE user_id = ?", (people["member"]["id"],))
    assert call(client, "POST", f"/canvas/{layout['slug']}/ops", member,
                json={"ops": [{"op": "upsert_node", "node": NODE}]}).status_code == 200
    assert call(client, "PUT", f"/canvas/{layout['slug']}", member, json={"visibility": "public"}).status_code == 403
    assert call(client, "DELETE", f"/canvas/{layout['slug']}", member).status_code == 403


def test_canvas_create_and_feature_switch(api_app, client, db, people):
    settings(db, canvas_access="editor", canvas_write_access="editor")
    token = issue(api_app, people["editor"], ["canvas"])
    created = call(client, "POST", "/canvas", token, json={"title": "Board", "data": {"nodes": [NODE], "edges": []}})
    assert created.status_code == 201 and created.json["canvas"]["is_owner"] is True
    slug = created.json["canvas"]["slug"]
    assert [node["id"] for node in call(client, "GET", f"/canvas/{slug}", token).json["data"]["nodes"]] == ["n1"]
    assert call(client, "POST", "/canvas", token, json={"title": ""}).json["field"] == "title"
    set_feature(api_app, "canvas", False)
    assert call(client, "GET", f"/canvas/{slug}", token).status_code == 404


def test_canvas_locked_elements_refuse_the_whole_change(api_app, client, layout, people):
    token = issue(api_app, people["admin"], ["canvas"])
    slug = layout["slug"]
    locked = {**NODE, "locked": True}
    other = {**NODE, "id": "n2", "text": "Free"}
    assert call(client, "PUT", f"/canvas/{slug}/document", token,
                json={"data": {"nodes": [locked, other], "edges": []}}).status_code == 200

    moved = call(client, "POST", f"/canvas/{slug}/ops", token, json={"ops": [
        {"op": "upsert_node", "node": {**other, "x": 99}},
        {"op": "upsert_node", "node": {**locked, "x": 500}}]})
    assert moved.status_code == 400 and moved.json["code"] == "locked"
    assert moved.json["locked"] == ["n1"] and moved.json["locked_count"] == 1
    dropped = call(client, "PUT", f"/canvas/{slug}/document", token, json={"data": {"nodes": [other], "edges": []}})
    assert dropped.status_code == 400 and dropped.json["locked"] == ["n1"]
    nodes = {node["id"]: node for node in call(client, "GET", f"/canvas/{slug}", token).json["data"]["nodes"]}
    assert nodes["n1"]["x"] == NODE["x"] and nodes["n2"]["x"] == NODE["x"]  # nothing of the batch was applied

    unlocked = {key: value for key, value in locked.items() if key != "locked"}
    assert call(client, "POST", f"/canvas/{slug}/ops", token,
                json={"ops": [{"op": "upsert_node", "node": unlocked}]}).status_code == 200
    assert call(client, "POST", f"/canvas/{slug}/ops", token,
                json={"ops": [{"op": "delete_node", "id": "n1"}]}).json["applied"] == 1
