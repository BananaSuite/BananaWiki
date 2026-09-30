"""Kanban sync events, history and revert, attachment files, export/import and embeds."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.kanban import events, extras, history

from .test_kanban import board_id_from, columns, new_ticket, set_settings


@pytest.fixture
def board(admin_client):
    return board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap"}))


def upload(client, ticket_id, name="notes.txt", data=b"hello"):
    return client.post(f"/api/kanban/tickets/{ticket_id}/attachments", data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def folder(app) -> Path:
    return Path(app.config["BW"].folders.kanban_attachments)


# ── Sync ─────────────────────────────────────────────────────────────────────


def test_sync_returns_incremental_events(admin_client, board, db):
    start = admin_client.get(f"/api/kanban/{board}/state").get_json()["seq"]
    column_id = columns(admin_client, board)[0]["id"]
    headers = {"X-Kanban-Session": "tab-a"}
    created = admin_client.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": "Live"}, headers=headers)
    ticket_id = created.get_json()["id"]
    admin_client.put(f"/api/kanban/tickets/{ticket_id}", json={"priority": "high"}, headers=headers)

    feed = admin_client.get(f"/api/kanban/{board}/sync?since={start}", headers={"X-Kanban-Session": "tab-b"}).get_json()
    assert feed["reset"] is False and feed["seq"] == start + 2
    first, second = feed["events"]
    assert first["op"] == "ticket_upsert" and first["payload"]["ticket"]["title"] == "Live"
    assert first["payload"]["columns"] == {str(column_id): [ticket_id]}
    assert second["payload"]["ticket"]["priority"] == "high"
    # The writer's own session does not get its echoes, but its cursor still advances.
    own = admin_client.get(f"/api/kanban/{board}/sync?since={start}", headers=headers).get_json()
    assert own["events"] == [] and own["seq"] == start + 2
    # Polling never deletes events.
    count = db.scalar("SELECT COUNT(*) FROM kanban_events WHERE board_id = ?", (board,))
    admin_client.get(f"/api/kanban/{board}/sync?since=0")
    assert db.scalar("SELECT COUNT(*) FROM kanban_events WHERE board_id = ?", (board,)) == count


def test_sync_asks_for_reset_when_events_are_gone(app, admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    for index in range(3):
        new_ticket(admin_client, column_id, f"T{index}")
    head = admin_client.get(f"/api/kanban/{board}/state").get_json()["seq"]
    db.execute("UPDATE kanban_events SET created_at = '2000-01-01 00:00:00'")
    with app.app_context(), connection_scope():
        events.prune()
    assert db.column("SELECT seq FROM kanban_events WHERE board_id = ?", (board,)) == [head]
    assert admin_client.get(f"/api/kanban/{board}/sync?since=1").get_json()["reset"] is True
    assert admin_client.get(f"/api/kanban/{board}/sync?since={head + 5}").get_json()["reset"] is True
    new_ticket(admin_client, column_id, "After")
    assert db.scalar("SELECT MAX(seq) FROM kanban_events WHERE board_id = ?", (board,)) == head + 1


def test_event_sequence_is_unique_per_board(admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    for index in range(5):
        new_ticket(admin_client, column_id, f"T{index}")
    seqs = db.column("SELECT seq FROM kanban_events WHERE board_id = ? ORDER BY seq", (board,))
    assert seqs == list(range(1, len(seqs) + 1))


# ── History and revert ───────────────────────────────────────────────────────


def test_revert_keeps_comments_attachments_and_newer_tickets(app, admin_client, board, db):
    todo, doing, done = (c["id"] for c in columns(admin_client, board))
    keep = new_ticket(admin_client, todo, "Keep me")
    doomed = new_ticket(admin_client, todo, "Deleted later")
    admin_client.put(f"/api/kanban/tickets/{keep['id']}", json={"description": "first"})
    target = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board,))

    admin_client.post(f"/api/kanban/tickets/{keep['id']}/comments", json={"content": "a comment"})
    assert upload(admin_client, keep["id"]).status_code == 201
    admin_client.put(f"/api/kanban/tickets/{keep['id']}", json={"title": "Renamed", "description": "second",
                                                                "priority": "critical"})
    admin_client.post(f"/api/kanban/tickets/{keep['id']}/move", json={"column_id": done, "position": 0})
    admin_client.put(f"/api/kanban/columns/{todo}", json={"title": "Backlog"})
    admin_client.delete(f"/api/kanban/tickets/{doomed['id']}")
    newer = new_ticket(admin_client, doing, "Newer ticket")
    empty_column = admin_client.post(f"/api/kanban/{board}/columns", json={"title": "Empty"}).get_json()["id"]

    response = admin_client.post(f"/kanban/{board}/revert/{target}")
    assert response.status_code == 302

    ticket = db.one("SELECT * FROM kanban_tickets WHERE id = ?", (keep["id"],))
    assert (ticket["title"], ticket["description"], ticket["priority"], ticket["column_id"]) == \
        ("Keep me", "first", "medium", todo)
    assert db.scalar("SELECT title FROM kanban_columns WHERE id = ?", (todo,)) == "To do"
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_comments WHERE ticket_id = ?", (keep["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_attachments WHERE ticket_id = ?", (keep["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_history WHERE ticket_id = ?", (keep["id"],)) == 3
    assert db.scalar("SELECT column_id FROM kanban_tickets WHERE id = ?", (newer["id"],)) == doing
    titles = db.column("SELECT t.title FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
                       "WHERE c.board_id = ? ORDER BY t.column_id, t.sort_order", (board,))
    assert titles == ["Keep me", "Deleted later", "Newer ticket"]
    assert db.scalar("SELECT COUNT(*) FROM kanban_columns WHERE id = ?", (empty_column,)) == 0
    assert db.scalar("SELECT is_revert FROM kanban_board_history WHERE board_id = ? ORDER BY id DESC LIMIT 1",
                     (board,)) == 1
    last_event = db.scalar("SELECT op_type FROM kanban_events WHERE board_id = ? ORDER BY seq DESC LIMIT 1", (board,))
    assert last_event == "board_reset"


def test_revert_of_legacy_snapshot_matches_by_title(admin_client, board, db):
    todo = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, todo, "Legacy")
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": "survives"})
    legacy = {"title": "Old title", "description": "", "columns": [
        {"title": "To do", "sort_order": 0, "tickets": [
            {"title": "Legacy", "description": "from 1.4", "priority": "low", "sort_order": 0,
             "labels": ["x"], "assignee_usernames": ["admin_user"]}]}]}
    entry = db.insert("kanban_board_history", {"board_id": board, "title": "Old title", "snapshot": json.dumps(legacy),
                                               "created_at": "2024-01-01 00:00:00"})
    admin_client.post(f"/kanban/{board}/revert/{entry}")
    row = db.one("SELECT * FROM kanban_tickets WHERE id = ?", (ticket["id"],))
    assert row["description"] == "from 1.4" and row["priority"] == "low"
    assert db.scalar("SELECT title FROM kanban_boards WHERE id = ?", (board,)) == "Old title"
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_comments WHERE ticket_id = ?", (ticket["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_assignees WHERE ticket_id = ?", (ticket["id"],)) == 1


def test_history_is_pruned_and_moves_coalesce(admin_client, board, db, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_KEPT", 5)
    todo, doing, _done = (c["id"] for c in columns(admin_client, board))
    ticket = new_ticket(admin_client, todo)
    before = db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board,))
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/move", json={"column_id": doing})
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/move", json={"column_id": todo})
    admin_client.post(f"/api/kanban/tickets/{ticket['id']}/move", json={"column_id": doing})
    assert db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board,)) <= before + 3
    for index in range(10):
        admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"title": f"Title {index}"})
    assert db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board,)) == 5


def test_history_pages_and_admin_clear(admin_client, board, db):
    page = admin_client.get(f"/kanban/{board}/history")
    assert page.status_code == 200
    entry = db.scalar("SELECT id FROM kanban_board_history WHERE board_id = ?", (board,))
    assert admin_client.get(f"/kanban/{board}/history/{entry}").status_code == 200
    assert admin_client.get(f"/kanban/{board}/history/999999").status_code == 404
    admin_client.post(f"/kanban/{board}/history/clear")
    assert db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board,)) == 0


# ── Attachment files ─────────────────────────────────────────────────────────


def _file_with_blob(app, admin_client, db, ticket_id):
    stored = upload(admin_client, ticket_id).get_json()
    blob = db.insert("file_blobs", {"filename": "x", "content": b"x"})
    db.execute("UPDATE kanban_ticket_attachments SET blob_id = ? WHERE id = ?", (blob, stored["id"]))
    name = db.scalar("SELECT filename FROM kanban_ticket_attachments WHERE id = ?", (stored["id"],))
    assert (folder(app) / name).is_file()
    return name, blob


@pytest.mark.parametrize("target", ["ticket", "column", "board", "attachment"])
def test_deleting_removes_files_and_blobs(app, admin_client, board, db, target):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id)
    name, blob = _file_with_blob(app, admin_client, db, ticket["id"])
    if target == "ticket":
        admin_client.delete(f"/api/kanban/tickets/{ticket['id']}")
    elif target == "column":
        admin_client.delete(f"/api/kanban/columns/{column_id}")
    elif target == "board":
        admin_client.post(f"/kanban/{board}/delete")
    else:
        attachment = db.scalar("SELECT id FROM kanban_ticket_attachments WHERE ticket_id = ?", (ticket["id"],))
        admin_client.delete(f"/api/kanban/attachments/{attachment}")
    assert not (folder(app) / name).exists()
    assert db.scalar("SELECT COUNT(*) FROM file_blobs WHERE id = ?", (blob,)) == 0


def test_upload_rules_and_download(app, admin_client, board, db):
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"])
    assert upload(admin_client, ticket["id"], "virus.exe").status_code == 400
    stored = upload(admin_client, ticket["id"], "page.html", b"<script>alert(1)</script>").get_json()
    response = admin_client.get(f"/api/kanban/attachments/{stored['id']}/download?inline=1")
    assert response.status_code == 200 and response.mimetype == "application/octet-stream"
    assert "attachment" in response.headers["Content-Disposition"]
    card = admin_client.get(f"/api/kanban/{board}/state").get_json()["tickets"][str(ticket["id"])]
    assert card["attachment_count"] == 1


def test_orphan_sweep(app, admin_client, board):
    orphan = folder(app) / "0123456789abcdef0123456789abcdef.txt"
    folder(app).mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"left over")
    with app.app_context(), connection_scope():
        assert extras.sweep_orphan_files(min_age_seconds=0) == 1
    assert not orphan.exists()


# ── Export, import, embeds, pages ────────────────────────────────────────────


def test_export_import_roundtrip(app, admin_client, board, db):
    column_id = columns(admin_client, board)[0]["id"]
    ticket = new_ticket(admin_client, column_id, "With file +tag")
    upload(admin_client, ticket["id"], "notes.txt", b"content")
    response = admin_client.get(f"/kanban/{board}/export")
    assert response.mimetype == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    document = json.loads(archive.read("board.json"))
    assert document["columns"][0]["tickets"][0]["labels"] == ["tag"]
    plain = admin_client.get(f"/kanban/{board}/export?format=json")
    assert plain.mimetype == "application/json"

    imported = admin_client.post("/kanban/import", data={"import_file": (io.BytesIO(response.data), "b.kanban.zip")},
                                 content_type="multipart/form-data")
    new_board = board_id_from(imported)
    assert new_board != board
    names = db.column("SELECT a.original_name FROM kanban_ticket_attachments a JOIN kanban_tickets t ON t.id = a.ticket_id "
                      "JOIN kanban_columns c ON c.id = t.column_id WHERE c.board_id = ?", (new_board,))
    assert names == ["notes.txt"]
    bad = admin_client.post("/kanban/import", data={"import_file": (io.BytesIO(b"not json"), "x.json")},
                            content_type="multipart/form-data")
    assert bad.status_code == 302 and db.scalar("SELECT COUNT(*) FROM kanban_boards") == 2


def test_import_refuses_path_traversal(admin_client, db):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("board.json", json.dumps({"title": "Evil", "columns": []}))
        archive.writestr("../evil.txt", "x")
    admin_client.post("/kanban/import", data={"import_file": (io.BytesIO(buffer.getvalue()), "e.zip")},
                      content_type="multipart/form-data")
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0


def test_embed_respects_permissions(app, admin_client, board, db, make_user, login):
    data = admin_client.get(f"/api/embed/kanban/{board}").get_json()
    assert data["board"]["title"] == "Roadmap" and data["url"] == f"/kanban/{board}"
    assert admin_client.get("/api/embed/kanban/not-a-number").status_code == 404
    reader = app.test_client()
    login(reader, make_user("reader"))
    assert reader.get(f"/api/embed/kanban/{board}").status_code == 404
    set_settings(db, kanban_access="all")
    assert reader.get(f"/api/embed/kanban/{board}").status_code == 200
    assert reader.get(f"/api/embed/kanban/{board}/sync?since=0").status_code == 200


def test_pages_have_no_inline_scripts(admin_client, board):
    for url in ("/kanban", f"/kanban/{board}", f"/kanban/{board}/history"):
        html = admin_client.get(url).get_data(as_text=True)
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag or 'type="application/json"' in tag, (url, tag)
        assert "kanban-embed.js" in html
