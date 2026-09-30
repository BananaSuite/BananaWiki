"""Kanban boards: access per visibility and share, CRUD flows, validation and IDOR checks."""

from __future__ import annotations

import io

import pytest

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope


def board_id_from(response) -> int:
    assert response.status_code == 302, response.data[:300]
    return int(response.headers["Location"].rstrip("/").rsplit("/", 1)[1])


@pytest.fixture
def board(admin_client):
    """A public board owned by the administrator (three default columns)."""
    return board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap", "description": "Plans"}))


def columns(client, board_id):
    return client.get(f"/api/kanban/{board_id}/state").get_json()["columns"]


def new_ticket(client, column_id, title="Task", **extra):
    response = client.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": title, **extra})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def as_client(app, login, user):
    other = app.test_client()
    login(other, user)
    return other


def set_settings(db, **values):
    for name, value in values.items():
        db.execute(f"UPDATE site_settings SET {name} = ? WHERE id = 1", (value,))


# ── Global access ────────────────────────────────────────────────────────────


def test_default_access_is_admin_only(app, admin_client, board, make_user, login):
    assert admin_client.get("/kanban").status_code == 200
    editor = as_client(app, login, make_user("eddie", role="editor"))
    assert editor.get("/kanban").status_code == 403
    assert editor.get(f"/kanban/{board}").status_code == 404


def test_access_setting_opens_public_boards(app, db, board, make_user, login):
    set_settings(db, kanban_access="editor")
    editor = as_client(app, login, make_user("eddie", role="editor"))
    user = as_client(app, login, make_user("usr"))
    assert editor.get(f"/kanban/{board}").status_code == 200
    assert b"Roadmap" in editor.get("/kanban").data
    assert user.get(f"/kanban/{board}").status_code == 404
    set_settings(db, kanban_access="all")
    assert user.get(f"/kanban/{board}").status_code == 200


def test_view_only_access_cannot_write_or_create(app, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    user = as_client(app, login, make_user("usr"))
    column_id = columns(user, board)[0]["id"]
    assert user.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": "x"}).status_code == 403
    assert user.post("/kanban/create", data={"title": "Mine"}).status_code == 403


def test_create_needs_write_access_and_permission(app, db, make_user, login):
    set_settings(db, kanban_access="all", kanban_write_access="all")
    editor = as_client(app, login, make_user("eddie", role="editor"))
    assert editor.post("/kanban/create", data={"title": "E"}).status_code == 302
    user = as_client(app, login, make_user("usr"))
    # Plain users lack the kanban.create permission by default.
    assert user.post("/kanban/create", data={"title": "U"}).status_code == 403


def test_private_board_and_shares(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    bob_user = make_user("bob")
    bob = as_client(app, login, bob_user)
    carol = as_client(app, login, make_user("carol"))
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    assert bob.get(f"/kanban/{board}").status_code == 404
    admin_client.post(f"/kanban/{board}/share", data={"action": "add_user", "user_id": bob_user["id"],
                                                        "access_level": "view"})
    assert bob.get(f"/kanban/{board}").status_code == 200
    assert carol.get(f"/kanban/{board}").status_code == 404
    column_id = columns(bob, board)[0]["id"]
    assert bob.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": "x"}).status_code == 403
    admin_client.post(f"/kanban/{board}/share", data={"action": "add_user", "user_id": bob_user["id"],
                                                        "access_level": "write"})
    assert bob.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": "x"}).status_code == 201
    admin_client.post(f"/kanban/{board}/share", data={"action": "add_role", "role": "user", "access_level": "view"})
    assert carol.get(f"/kanban/{board}").status_code == 200
    # Only the owner and administrators manage sharing.
    assert bob.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "public"}).status_code == 403


def test_individual_share_bypasses_global_access(app, admin_client, board, make_user, login):
    bob_user = make_user("bob")
    bob = as_client(app, login, bob_user)
    other = admin_client.post("/kanban/create", data={"title": "Hidden"})
    assert bob.get("/kanban").status_code == 403
    admin_client.post(f"/kanban/{board}/share", data={"action": "add_user", "user_id": bob_user["id"]})
    listing = bob.get("/kanban")
    assert listing.status_code == 200 and b"Roadmap" in listing.data and b"Hidden" not in listing.data
    assert bob.get(f"/kanban/{board_id_from(other)}").status_code == 404


def test_role_share_limited_to_roles_with_access(admin_client, board):
    admin_client.post(f"/kanban/{board}/share", data={"action": "add_role", "role": "editor"})
    settings = admin_client.get(f"/api/kanban/{board}/settings").get_json()
    assert settings["shares"] == [] and settings["shareable_roles"] == ["admin"]


def test_access_change_revokes_role_shares_and_assignees(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="editor")
    editor = make_user("eddie", role="editor")
    admin_client.post(f"/kanban/{board}/share", data={"action": "add_role", "role": "editor"})
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id, assignees=[editor["id"]])
    assert [a["id"] for a in ticket["assignees"]] == [editor["id"]]
    admin_client.post("/kanban/settings", data={"kanban_access": "admin", "kanban_write_access": "admin"})
    assert db.scalar("SELECT COUNT(*) FROM kanban_board_shares WHERE board_id = ?", (board,)) == 0
    assert db.scalar("SELECT assigned_to FROM kanban_tickets WHERE id = ?", (ticket["id"],)) is None


def test_feature_switch_hides_everything(app, admin_client, board):
    with app.test_request_context(), connection_scope():
        registry.set_enabled("kanban", False)
    assert admin_client.get("/kanban").status_code == 404
    assert admin_client.get(f"/api/kanban/{board}/state").status_code == 404


def test_public_mode_visitors_read_public_boards(app, admin_client, db, board):
    visitor = app.test_client()
    set_settings(db, public_mode=1)
    assert visitor.get(f"/kanban/{board}").status_code == 302  # kanban's own switch is off
    set_settings(db, kanban_public_access_enabled=1)
    assert visitor.get(f"/kanban/{board}").status_code == 200
    column_id = columns(visitor, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    assert visitor.get(f"/api/kanban/tickets/{ticket['id']}").status_code == 200
    assert visitor.get(f"/api/kanban/{board}/sync?since=0").status_code == 200
    assert visitor.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "hi"}).status_code == 401
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "shared"})
    assert visitor.get(f"/kanban/{board}").status_code == 302


# ── Boards, columns, tickets ─────────────────────────────────────────────────


def test_board_crud_and_validation(admin_client, board, db):
    assert [c["title"] for c in columns(admin_client, board)] == ["To do", "In progress", "Done"]
    assert admin_client.get(f"/kanban/{board}").status_code == 200
    admin_client.post(f"/kanban/{board}/edit", data={"title": "Renamed", "description": "x"})
    assert db.scalar("SELECT title FROM kanban_boards WHERE id = ?", (board,)) == "Renamed"
    admin_client.post(f"/kanban/{board}/edit", data={"title": "x" * 500})
    assert db.scalar("SELECT title FROM kanban_boards WHERE id = ?", (board,)) == "Renamed"
    assert admin_client.post("/kanban/create", data={"title": "  "}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 1
    admin_client.post(f"/kanban/{board}/delete")
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0


def test_columns_and_tickets(admin_client, board, db):
    created = admin_client.post(f"/api/kanban/{board}/columns", json={"title": "Review"})
    assert created.status_code == 201
    review = created.get_json()["id"]
    assert admin_client.post(f"/api/kanban/{board}/columns", json={"title": ""}).status_code == 400
    admin_client.put(f"/api/kanban/columns/{review}", json={"title": "QA"})
    ids = [c["id"] for c in columns(admin_client, board)]
    admin_client.post(f"/api/kanban/{board}/columns/reorder", json={"order": [review, *ids[:-1]]})
    assert [c["title"] for c in columns(admin_client, board)][0] == "QA"

    first = new_ticket(admin_client, review, "One @admin_user +ui !high due:2030-01-02 color:red")
    assert first["title"] == "One" and first["priority"] == "high" and first["labels"] == ["ui"]
    assert first["due_date"] == "2030-01-02" and first["color"] == "#e91e63"
    assert first["assignees"][0]["username"] == "admin_user"
    second = new_ticket(admin_client, review, "Two")
    update = admin_client.put(f"/api/kanban/tickets/{second['id']}",
                              json={"description": "**bold**", "priority": "critical", "labels": "+a b"})
    assert update.status_code == 200 and "<strong>bold</strong>" in update.get_json()["description_html"]
    assert admin_client.put(f"/api/kanban/tickets/{second['id']}", json={"priority": "urgent"}).status_code == 400
    assert admin_client.put(f"/api/kanban/tickets/{second['id']}", json={"color": "red;x"}).status_code == 400
    assert admin_client.put(f"/api/kanban/tickets/{second['id']}", json={"due_date": "soon"}).status_code == 400

    done = ids[2]
    moved = admin_client.post(f"/api/kanban/tickets/{first['id']}/move", json={"column_id": done, "position": 0})
    assert moved.get_json()["columns"] == {str(done): [first["id"]], str(review): [second["id"]]}
    admin_client.post(f"/api/kanban/columns/{review}/tickets/reorder", json={"order": [first["id"], second["id"]]})
    assert db.scalar("SELECT column_id FROM kanban_tickets WHERE id = ?", (first["id"],)) == done
    assert admin_client.delete(f"/api/kanban/tickets/{second['id']}").status_code == 200
    history = admin_client.get(f"/api/kanban/tickets/{first['id']}/history").get_json()
    assert history == []


def test_description_history_and_diff(admin_client, board):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"description": "<script>x</script> old"})
    admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"description": "new"})
    entries = admin_client.get(f"/api/kanban/tickets/{ticket['id']}/history").get_json()
    assert len(entries) == 2
    detail = admin_client.get(f"/api/kanban/history/{entries[0]['id']}").get_json()
    assert "&lt;script&gt;" in detail["diff_html"] and "<script>" not in detail["diff_html"]


def test_assignees_must_reach_board(app, admin_client, db, board, make_user):
    outsider = make_user("outsider")
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id, assignees=[outsider["id"]])
    assert ticket["assignees"] == []
    response = admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"assignees": [outsider["id"]]})
    assert response.status_code == 400
    admin_client.post(f"/kanban/{board}/share", data={"action": "add_user", "user_id": outsider["id"]})
    response = admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"assignees": [outsider["id"]]})
    assert response.status_code == 200
    assert db.scalar("SELECT assigned_to FROM kanban_tickets WHERE id = ?", (ticket["id"],)) == outsider["id"]
    admin_client.post(f"/kanban/{board}/share", data={"action": "remove_user", "user_id": outsider["id"]})
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_assignees WHERE ticket_id = ?", (ticket["id"],)) == 0


def test_legacy_single_assignee_is_readable(admin_client, db, board, admin):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    db.execute("UPDATE kanban_tickets SET assigned_to = ?, labels = 'a,b' WHERE id = ?", (admin["id"], ticket["id"]))
    card = admin_client.get(f"/api/kanban/{board}/state").get_json()["tickets"][str(ticket["id"])]
    assert card["assignees"] == [{"id": admin["id"], "username": admin["username"]}]
    assert card["labels"] == ["a", "b"]


def test_bulk_actions(admin_client, board, db):
    cols = columns(admin_client, board)
    one = new_ticket(admin_client, cols[0]["id"], "A")
    two = new_ticket(admin_client, cols[0]["id"], "B")
    ids = [one["id"], two["id"]]
    url = f"/api/kanban/{board}/tickets/bulk"
    assert admin_client.post(url, json={"ticket_ids": ids, "action": "priority", "priority": "low"}).get_json() == {"updated": 2}
    admin_client.post(url, json={"ticket_ids": ids, "action": "move", "column_id": cols[2]["id"]})
    assert db.column("SELECT id FROM kanban_tickets WHERE column_id = ? ORDER BY sort_order", (cols[2]["id"],)) == ids
    admin_client.post(url, json={"ticket_ids": ids, "action": "delete"})
    assert db.scalar("SELECT COUNT(*) FROM kanban_tickets") == 0
    response = admin_client.post(f"/api/kanban/{board}/columns/bulk", json={"action": "delete", "column_ids": [cols[1]["id"]]})
    assert response.get_json() == {"updated": 1}


def test_board_order_is_per_user_and_validated(app, admin_client, db, board):
    second = board_id_from(admin_client.post("/kanban/create", data={"title": "Second"}))
    admin_client.post("/api/kanban/board-order", json={"board_ids": [board, 99999, second]})
    rows = db.all("SELECT user_id, board_id FROM kanban_user_board_order ORDER BY sort_order")
    assert [r["board_id"] for r in rows] == [board, second] and all(r["user_id"] for r in rows)
    page = admin_client.get("/kanban").data
    assert page.index(b"Roadmap") < page.index(b"Second")
    version = admin_client.get("/api/kanban/list-order-version").get_json()["list_order_version"]
    set_settings(db, kanban_open_access=1)
    admin_client.post("/api/kanban/board-order", json={"board_ids": [second, board]})
    assert db.scalar("SELECT COUNT(*) FROM kanban_user_board_order WHERE user_id IS NULL") == 2
    assert admin_client.get("/api/kanban/list-order-version").get_json()["list_order_version"] != version
    assert db.scalar("SELECT list_order_version FROM site_settings") == 0  # not shared with canvas


# ── IDOR ─────────────────────────────────────────────────────────────────────


def test_objects_of_hidden_boards_are_unreachable(app, admin_client, db, make_user, login):
    set_settings(db, kanban_access="all", kanban_write_access="all")
    editor_user = make_user("eddie", role="editor")
    editor = as_client(app, login, editor_user)
    private = board_id_from(admin_client.post("/kanban/create", data={"title": "Secret"}))
    admin_client.post(f"/kanban/{private}/share", data={"action": "set_visibility", "visibility": "private"})
    mine = board_id_from(editor.post("/kanban/create", data={"title": "Mine"}))
    secret_column = columns(admin_client, private)[0]["id"]
    ticket = new_ticket(admin_client, secret_column)
    comment = admin_client.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "psst"}).get_json()
    upload = admin_client.post(f"/api/kanban/tickets/{ticket['id']}/attachments",
                               data={"file": (io.BytesIO(b"hello"), "a.txt")},
                               content_type="multipart/form-data")
    attachment = upload.get_json()["id"]
    history_id = db.scalar("SELECT id FROM kanban_board_history WHERE board_id = ? LIMIT 1", (private,))
    checks = [
        ("get", f"/api/kanban/tickets/{ticket['id']}"),
        ("put", f"/api/kanban/tickets/{ticket['id']}"),
        ("delete", f"/api/kanban/tickets/{ticket['id']}"),
        ("get", f"/api/kanban/tickets/{ticket['id']}/comments"),
        ("get", f"/api/kanban/tickets/{ticket['id']}/attachments"),
        ("get", f"/api/kanban/attachments/{attachment}/download"),
        ("delete", f"/api/kanban/attachments/{attachment}"),
        ("put", f"/api/kanban/comments/{comment['id']}"),
        ("delete", f"/api/kanban/comments/{comment['id']}"),
        ("put", f"/api/kanban/columns/{secret_column}"),
        ("delete", f"/api/kanban/columns/{secret_column}"),
        ("post", f"/api/kanban/columns/{secret_column}/tickets"),
        ("get", f"/api/kanban/{private}/sync"),
        ("get", f"/api/kanban/{private}/activity"),
        ("get", f"/api/embed/kanban/{private}"),
        ("get", f"/kanban/{private}/history"),
        ("get", f"/kanban/{private}/export"),
        ("post", f"/kanban/{private}/revert/{history_id}"),
    ]
    for method, url in checks:
        response = getattr(editor, method)(url, json={"title": "x", "content": "x"})
        assert response.status_code == 404, (method, url, response.status_code)
    # Moving a ticket of the editor's own board into a hidden board's column is refused.
    own_ticket = new_ticket(editor, columns(editor, mine)[0]["id"])
    response = editor.post(f"/api/kanban/tickets/{own_ticket['id']}/move", json={"column_id": secret_column})
    assert response.status_code == 404
    response = editor.post(f"/api/kanban/{mine}/tickets/bulk", json={"ticket_ids": [ticket["id"]], "action": "delete"})
    assert response.status_code == 404
    assert db.scalar("SELECT COUNT(*) FROM kanban_tickets WHERE id = ?", (ticket["id"],)) == 1


def test_comment_permissions(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    reader = as_client(app, login, make_user("reader"))
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    own = reader.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "mine"})
    assert own.status_code == 201
    theirs = admin_client.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "admin"}).get_json()
    assert reader.put(f"/api/kanban/comments/{theirs['id']}", json={"content": "x"}).status_code == 403
    assert reader.put(f"/api/kanban/comments/{own.get_json()['id']}", json={"content": "edited"}).status_code == 200
    assert reader.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "x" * 2001}).status_code == 400
    assert admin_client.delete(f"/api/kanban/comments/{own.get_json()['id']}").status_code == 200
