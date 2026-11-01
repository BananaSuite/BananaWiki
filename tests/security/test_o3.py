"""Regression tests for the OW3 findings of the full re-audit pass 9 (Kanban history pages, actions taken
while impersonating on boards and profiles, and the saved board order)."""

from __future__ import annotations

import io
import json

from bananawiki.wiki.features.kanban import history

# OW3-1: a snapshot's ticket descriptions are rendered within a budget, and the history pages are rate limited


def _import(client, db, document) -> tuple[int, int]:
    response = client.post("/kanban/import", data={"import_file": (io.BytesIO(json.dumps(document).encode()),
                                                                   "b.kanban.json")},
                           content_type="multipart/form-data")
    assert response.status_code == 302, response.data[:300]
    board_id = db.scalar("SELECT id FROM kanban_boards ORDER BY id DESC LIMIT 1")
    entry_id = db.scalar("SELECT id FROM kanban_board_history WHERE board_id = ? ORDER BY id DESC LIMIT 1",
                         (board_id,))
    return board_id, entry_id


def test_ow3_1_history_entry_renders_descriptions_within_a_budget(app, client, make_user, login, db, monkeypatch):
    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'editor' WHERE id = 1")
    monkeypatch.setattr(history, "MAX_SNAPSHOT_RENDER_CHARS", 100)
    tickets = [{"title": f"T{i}", "description": f"**bold {i}** " + "x" * 30} for i in range(8)]
    login(client, make_user("eddie", role="editor"))
    board_id, entry_id = _import(client, db, {"title": "Big", "columns": [{"title": "C", "tickets": tickets}]})
    reader = app.test_client()
    login(reader, make_user("reader"))
    page = reader.get(f"/kanban/{board_id}/history/{entry_id}").get_data(as_text=True)
    # The first two fit the budget and are rendered; the others are shown as typed, escaped.
    assert "<strong>bold 0</strong>" in page and "<strong>bold 1</strong>" in page
    assert "<strong>bold 2</strong>" not in page and "<pre>**bold 7** " in page


def test_ow3_1_history_pages_are_rate_limited(app, client, make_user, login, db, frozen_limiter_clock):
    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'editor' WHERE id = 1")
    login(client, make_user("eddie", role="editor"))
    board_id, entry_id = _import(client, db, {"title": "B", "columns": [{"title": "C", "tickets": [{"title": "T"}]}]})
    reader = app.test_client()
    login(reader, make_user("reader"))
    statuses = [reader.get(f"/kanban/{board_id}/history/{entry_id}").status_code for _ in range(30)]
    assert statuses == [200] * 30
    assert reader.get(f"/kanban/{board_id}/history/{entry_id}").status_code == 429
    assert reader.get(f"/kanban/{board_id}/history").status_code == 429
    # Another account keeps its own allowance.
    other = app.test_client()
    login(other, make_user("other"))
    assert other.get(f"/kanban/{board_id}/history/{entry_id}").status_code == 200


# OW3-3: Kanban, canvas, account and profile changes made while impersonating are audited


def _impersonated_entries(db, since: int) -> list[tuple]:
    rows = db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log "
                  "WHERE id > ? AND action LIKE '%.impersonated_action' ORDER BY id", (since,))
    return [(row["action"], row["actor_id"], row["target_type"], row["target_id"],
             json.loads(row["details"])["endpoint"], json.loads(row["details"])["status"]) for row in rows]


def test_ow3_3_kanban_and_profile_changes_while_impersonating_are_audited(app, make_user, login, db):
    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'all' WHERE id = 1")
    bob = make_user("bob")
    carol = app.test_client()
    login(carol, make_user("carol", role="editor"))
    assert carol.post("/kanban/create", data={"title": "Payments"}).status_code == 302
    board = db.scalar("SELECT id FROM kanban_boards")
    column = db.scalar("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY sort_order", (board,))
    ticket = carol.post(f"/api/kanban/columns/{column}/tickets", json={"title": "Invoice 42"}).get_json()["id"]
    admin_user = make_user("ada", role="admin")
    admin = app.test_client()
    login(admin, admin_user)
    assert admin.post(f"/admin/users/{bob['id']}/impersonate").status_code == 302
    since = db.scalar("SELECT MAX(id) FROM audit_log")
    assert admin.post(f"/api/kanban/tickets/{ticket}/comments", json={"content": "I approve. -- bob"}).status_code == 201
    assert admin.put(f"/api/kanban/tickets/{ticket}", json={"title": "Invoice 42 (paid)"}).status_code == 200
    assert admin.post("/api/kanban/board-order", json={"board_ids": [board]}).status_code == 200
    assert admin.post("/settings/profile", data={"real_name": "Bob", "bio": "changed", "birth_date": ""}).status_code \
        == 302
    assert admin.post("/settings/display", data={}).status_code in (200, 302)
    assert _impersonated_entries(db, since) == [
        ("kanban.impersonated_action", admin_user["id"], "ticket", str(ticket), "kanban.add_comment", 201),
        ("kanban.impersonated_action", admin_user["id"], "ticket", str(ticket), "kanban.update_ticket", 200),
        ("user.impersonated_action", admin_user["id"], "user", bob["id"], "users.edit_profile", 302),
    ]
    # The comment stays the member's: the entry is what tells the administrator's actions apart.
    assert db.scalar("SELECT user_id FROM kanban_ticket_comments") == bob["id"]
    # A member acting for themselves is not recorded.
    since = db.scalar("SELECT MAX(id) FROM audit_log")
    assert carol.post(f"/api/kanban/tickets/{ticket}/comments", json={"content": "Thanks"}).status_code == 201
    assert _impersonated_entries(db, since) == []


def test_ow3_3_canvas_changes_while_impersonating_are_audited(app, make_user, login, db):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.canvas import service as canvases

    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    dora = make_user("dora", role="editor")
    with app.test_request_context(), connection_scope():
        canvas = canvases.create("Plan", "", dora["id"])
    admin_user = make_user("ada", role="admin")
    admin = app.test_client()
    login(admin, admin_user)
    assert admin.post(f"/admin/users/{dora['id']}/impersonate").status_code == 302
    since = db.scalar("SELECT MAX(id) FROM audit_log")
    node = {"op": "upsert_node", "node": {"id": "a", "type": "text", "text": "Signed off -- dora"}}
    assert admin.post(f"/canvas/{canvas['slug']}/ops", json={"ops": [node]},
                      headers={"X-Canvas-Session": "s1"}).status_code == 200
    assert admin.post(f"/canvas/{canvas['slug']}/render", json={"node": node["node"]}).status_code == 200
    assert _impersonated_entries(db, since) == [
        ("canvas.impersonated_action", admin_user["id"], "canvas", canvas["slug"], "canvas.apply_ops", 200)]


# OW3-5: a personal board order stores the boards placed, not every board the user can see


def test_ow3_5_board_order_stores_only_the_boards_placed(app, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.kanban import service as kanban

    db.execute("UPDATE site_settings SET kanban_access = 'all' WHERE id = 1")
    owner = make_user("owner", role="editor")
    db.executemany("INSERT INTO kanban_boards (title, created_by, visibility, created_at) VALUES (?, ?, 'public', ?)",
                   [(f"Board {i}", owner["id"], f"2026-01-01 00:{i // 60:02d}:{i % 60:02d}") for i in range(300)])
    ids = db.column("SELECT id FROM kanban_boards ORDER BY created_at DESC, id DESC")
    reader = app.test_client()
    user = make_user("reader")
    login(reader, user)

    def saved() -> list[int]:
        return db.column("SELECT board_id FROM kanban_user_board_order WHERE user_id = ? ORDER BY sort_order",
                         (user["id"],))

    assert reader.post("/api/kanban/board-order", json={"board_ids": [ids[5]]}).status_code == 200
    assert saved() == [ids[5]]
    # Boards placed before keep their places after those sent; unknown ids are ignored.
    assert reader.post("/api/kanban/board-order", json={"board_ids": [ids[9], 999999, ids[9]]}).status_code == 200
    assert saved() == [ids[9], ids[5]]
    page = reader.get("/kanban").get_data(as_text=True)
    assert page.index(f"Board {299 - 9}<") < page.index(f"Board {299 - 5}<") < page.index("Board 299<")
    # The stored order is capped.
    monkeypatch.setattr(kanban, "MAX_ORDERED_BOARDS", 3)
    assert reader.post("/api/kanban/board-order", json={"board_ids": ids[20:25]}).status_code == 200
    assert saved() == ids[20:23]
