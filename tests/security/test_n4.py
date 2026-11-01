"""Regression tests for the n4 findings of the full re-audit pass 8 (kanban, assessments, canvas list)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

# NW3-2: kanban ticket attachments and board imports count against the daily upload quota


def _kanban_ticket(app, db, owner):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.kanban import service, store

    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'editor' WHERE id = 1")
    with app.test_request_context(), connection_scope():
        board = service.create_board(owner, "B", "")
        return service.create_ticket(board, store.columns_of(board["id"])[0], owner, {"title": "T"})


def _kanban_files(app) -> list[str]:
    folder = Path(app.config["BW"].folders.kanban_attachments)
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))


def _attach(client, ticket_id: int, n: int, size: int = 10):
    return client.post(f"/api/kanban/tickets/{ticket_id}/attachments",
                       data={"file": (io.BytesIO(bytes([n]) * size), f"f{n}.pdf")}, content_type="multipart/form-data")


def _counters(db, user) -> tuple[int, int]:
    row = db.one("SELECT upload_window_count, upload_window_bytes FROM users WHERE id = ?", (user["id"],))
    return int(row["upload_window_count"] or 0), int(row["upload_window_bytes"] or 0)


def test_nw3_2_ticket_attachments_count_against_the_daily_quota(app, client, make_user, login, db,
                                                                 frozen_limiter_clock):
    eddie = make_user("eddie", role="editor")
    ticket = _kanban_ticket(app, db, eddie)
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 2 WHERE id = 1")
    login(client, eddie)
    codes = [_attach(client, ticket["id"], n).status_code for n in range(4)]
    assert codes == [201, 201, 429, 429]
    assert "daily upload limit" in _attach(client, ticket["id"], 9).get_json()["error"]
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_attachments") == 2
    assert _counters(db, eddie) == (2, 20)
    assert len(_kanban_files(app)) == 2


def test_nw3_2_ticket_attachments_obey_the_daily_byte_quota(app, client, make_user, login, db):
    eddie = make_user("eddie", role="editor")
    ticket = _kanban_ticket(app, db, eddie)
    db.execute("UPDATE site_settings SET upload_quota_per_day_bytes = 1048576 WHERE id = 1")
    login(client, eddie)
    assert _attach(client, ticket["id"], 1, 600_000).status_code == 201
    assert _attach(client, ticket["id"], 2, 600_000).status_code == 429  # read, then refused and removed
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_attachments") == 1
    assert len(_kanban_files(app)) == 1
    assert _counters(db, eddie) == (1, 600_000)


def test_nw3_2_rest_ticket_attachments_share_the_quota(app, client, make_user, login, db):
    from tests.features.api_support import call, enable_api, issue

    eddie = make_user("eddie", role="editor")
    ticket = _kanban_ticket(app, db, eddie)
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 2 WHERE id = 1")
    enable_api(app, db)
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (eddie["id"],))
    token = issue(app, db.one("SELECT id FROM users WHERE id = ?", (eddie["id"],)), scopes=("kanban",))
    login(client, eddie)
    assert _attach(client, ticket["id"], 0).status_code == 201
    api = app.test_client()

    def rest(n):
        return call(api, "POST", f"/kanban/tickets/{ticket['id']}/attachments", token,
                    data={"file": (io.BytesIO(b"x" * 10), f"r{n}.pdf")}, content_type="multipart/form-data")

    assert rest(1).status_code == 201
    refused = rest(2)
    assert refused.status_code == 429
    assert refused.get_json()["code"] == "upload_refused"
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_attachments") == 2


def _board_zip(count: int, size: int = 10) -> bytes:
    paths = [f"attachments/1/f{n}.pdf" for n in range(count)]
    document = {"title": "Imported", "columns": [{"title": "To do", "tickets": [
        {"title": "T", "attachments": [{"bundle_path": p, "original_name": p.rsplit("/", 1)[-1]} for p in paths]}]}]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("board.json", json.dumps(document))
        for n, path in enumerate(paths):
            bundle.writestr(path, bytes([n]) * size)
    return buffer.getvalue()


def _import_board(client, body: bytes):
    return client.post("/kanban/import", data={"import_file": (io.BytesIO(body), "board.kanban.zip")},
                       content_type="multipart/form-data")


def test_nw3_2_board_import_counts_its_files_against_the_daily_quota(app, client, make_user, login, db,
                                                                     frozen_limiter_clock):
    eddie = make_user("eddie", role="editor")
    _kanban_ticket(app, db, eddie)
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 5 WHERE id = 1")
    login(client, eddie)
    assert _import_board(client, _board_zip(3)).status_code == 302
    assert _counters(db, eddie) == (3, 30)
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_attachments") == 3
    boards = db.scalar("SELECT COUNT(*) FROM kanban_boards")

    # Three more do not fit: the import is refused whole, stores nothing and counts nothing.
    response = _import_board(client, _board_zip(3))
    assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == boards
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_attachments") == 3
    assert len(_kanban_files(app)) == 3
    assert _counters(db, eddie) == (3, 30)
    assert "daily upload limit" in client.get("/kanban").get_data(as_text=True)

    # A board without files still imports.
    assert _import_board(client, _board_zip(0)).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == boards + 1


def test_nw3_2_board_import_obeys_the_byte_quota(app, client, make_user, login, db):
    eddie = make_user("eddie", role="editor")
    _kanban_ticket(app, db, eddie)
    db.execute("UPDATE site_settings SET upload_quota_per_day_bytes = 1048576 WHERE id = 1")
    login(client, eddie)
    boards = db.scalar("SELECT COUNT(*) FROM kanban_boards")
    _import_board(client, _board_zip(2, 600_000))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == boards
    assert _kanban_files(app) == []
    assert _counters(db, eddie) == (0, 0)


def test_nw3_2_a_board_import_refused_after_storing_its_files_costs_no_quota(app, client, make_user, login, db,
                                                                             monkeypatch):
    from bananawiki.wiki.features.kanban import fields

    eddie = make_user("eddie", role="editor")
    _kanban_ticket(app, db, eddie)
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 5 WHERE id = 1")
    login(client, eddie)
    monkeypatch.setattr(fields, "MAX_BOARD_BYTES", 1)  # refused when the board is created, after its files
    boards = db.scalar("SELECT COUNT(*) FROM kanban_boards")
    assert _import_board(client, _board_zip(2)).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == boards
    assert _kanban_files(app) == []
    assert _counters(db, eddie) == (0, 0)


# NW3-3: stored attempts are capped per person and assessment, and the results list is paged


def _quiz(app, db, client, login, editor):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.pages import service as pages

    with app.test_request_context(), connection_scope():
        page = pages.create("Practice", "Try again.", author_id=None)
    login(client, editor)
    response = client.post(f"/page/{page['slug']}/assessment/manage", data={
        "title": "Practice", "allow_multiple_attempts": "1",
        "questions-0-prompt": "2 + 2 = ?", "questions-0-type": "single_choice", "questions-0-options": "3\n4",
        "questions-0-correct": "4", "questions-0-points_correct": "1", "questions-0-required": "1",
        "questions-1-prompt": "Notes", "questions-1-type": "free_text",
    })
    assert response.status_code == 302, response.data[:2000]
    client.post("/logout")
    assessment = db.one("SELECT * FROM assessments WHERE page_id = ?", (page["id"],))
    questions = db.column("SELECT id FROM assessment_questions WHERE assessment_id = ? ORDER BY sort_order",
                          (assessment["id"],))
    return page, assessment, questions


def test_nw3_3_old_attempts_are_pruned_keeping_the_best(app, client, make_user, login, db, monkeypatch,
                                                         frozen_limiter_clock):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.assessments import service

    monkeypatch.setattr(service, "MAX_KEPT_ATTEMPTS", 3, raising=False)
    page, assessment, (choice, notes) = _quiz(app, db, client, login, make_user("editor1", role="editor"))
    reader = make_user("reader1")
    login(client, reader)
    url = f"/page/{page['slug']}/assessment/submit"
    assert client.post(url, data={f"q_{choice}": "1", f"q_{notes}": "best"}).status_code == 302  # right: 1 point
    for _ in range(5):
        assert client.post(url, data={f"q_{choice}": "0", f"q_{notes}": "x" * 2000}).status_code == 302
    kept = db.column("SELECT attempt_number FROM assessment_attempts WHERE user_id = ? ORDER BY attempt_number",
                     (reader["id"],))
    assert kept == [1, 4, 5, 6]  # the best one and the latest three
    assert db.scalar("SELECT COUNT(*) FROM assessment_answers") == 4 * 2  # answers of removed attempts went too
    with app.test_request_context(), connection_scope():
        assert service.total_points(reader["id"]) == 1
    assert client.post(url, data={f"q_{choice}": "0"}).status_code == 302
    assert db.column("SELECT attempt_number FROM assessment_attempts ORDER BY attempt_number") == [1, 5, 6, 7]
    assert "latest 3 attempts" in client.get(f"/page/{page['slug']}/assessment").get_data(as_text=True)


def test_nw3_3_results_are_paged_and_the_manage_page_counts(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.assessments import routes

    monkeypatch.setattr(routes, "RESULTS_PER_PAGE", 2, raising=False)
    editor = make_user("editor1", role="editor")
    page, assessment, _questions = _quiz(app, db, client, login, editor)
    people = [make_user(f"taker{n}") for n in range(3)]
    db.executemany(
        "INSERT INTO assessment_attempts (assessment_id, user_id, attempt_number, total_points, max_points) "
        "VALUES (?, ?, ?, 0, 1)", [(assessment["id"], person["id"], n) for person in people for n in (1, 2)])
    login(client, editor)
    base = f"/page/{page['slug']}/assessment/results"
    marker = f'href="/page/{page["slug"]}/assessment/attempts/'
    first = client.get(base).get_data(as_text=True)
    assert first.count(marker) == 2
    assert "page=2" in first
    assert client.get(base + "?page=3").get_data(as_text=True).count(marker) == 2
    assert client.get(base + "?page=99").get_data(as_text=True).count(marker) == 2  # the last page
    assert client.get(base + "?page=x").status_code == 200
    shown = set()
    for number in (1, 2, 3):
        body = client.get(f"{base}?page={number}").get_data(as_text=True)
        shown.update(part.split('"', 1)[0] for part in body.split(marker)[1:])
    assert len(shown) == 6  # every attempt appears on exactly one page
    assert "6 attempts" in client.get(f"/page/{page['slug']}/assessment/manage").get_data(as_text=True)


# NW3-4: canvas creation has one budget for the browser and the REST API, and the canvas list is paged


def test_nw3_4_rest_canvas_creation_shares_the_per_account_limit(app, client, make_user, login, db,
                                                                 frozen_limiter_clock):
    from tests.features.api_support import call, enable_api, issue

    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    enable_api(app, db)
    eddie = make_user("eddie", role="editor")
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (eddie["id"],))
    token = issue(app, db.one("SELECT id FROM users WHERE id = ?", (eddie["id"],)), scopes=("canvas",))
    api = app.test_client()
    codes = [call(api, "POST", "/canvas", token, json={"title": f"C{n}"}).status_code for n in range(8)]
    assert codes == [201] * 8
    login(client, eddie)
    web = [client.post("/canvas/create", data={"title": f"W{n}"}).status_code for n in range(3)]
    assert web == [302, 302, 429]
    refused = call(api, "POST", "/canvas", token, json={"title": "More"})
    assert refused.status_code == 429
    assert refused.get_json()["code"] == "rate_limited"
    assert refused.headers["Retry-After"] == "60"
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 10


def _canvases(app, db, owner, count: int) -> list[int]:
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.canvas import service

    ids = []
    with app.test_request_context(), connection_scope():
        for n in range(count):
            ids.append(service.create(f"Canvas {n}", "", owner["id"])["id"])
    for n, layout_id in enumerate(ids):  # oldest change first
        db.execute("UPDATE canvas__layouts SET updated_at = ?, visibility = 'public' WHERE id = ?",
                   (f"2026-01-01 00:00:{n:02d}", layout_id))
    return ids


def _listed(client, query: str = "") -> tuple[list[int], str]:
    import re

    body = client.get("/canvas" + query).get_data(as_text=True)
    return [int(i) for i in re.findall(r'data-layout-id="(\d+)"', body)], body


def test_nw3_4_canvas_list_is_paged(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.canvas import routes

    monkeypatch.setattr(routes, "LIST_PAGE_SIZE", 3, raising=False)
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    ids = _canvases(app, db, make_user("owner1", role="editor"), 7)
    newest = ids[::-1]
    reader = make_user("reader1")
    login(client, reader)
    first, body = _listed(client)
    assert first == newest[:3]
    assert "page=2" in body and 'data-move="up"' in body
    second, body = _listed(client, "?page=2")
    assert second == newest[3:6]
    assert 'data-move="up"' not in body and "data-order-url" not in body  # only the first page is reordered
    assert _listed(client, "?page=3")[0] == newest[6:]
    assert _listed(client, "?page=99")[0] == newest[6:]
    assert _listed(client, "?page=x")[0] == newest[:3]

    # Anonymous visitors of public canvases get pages too.
    client.post("/logout")
    db.execute("UPDATE site_settings SET public_mode = 1, canvas_public_access_enabled = 1 WHERE id = 1")
    assert _listed(client)[0] == newest[:3]
    assert _listed(client, "?page=3")[0] == newest[6:]


def test_nw3_4_reordering_the_first_page_keeps_the_order_of_later_pages(app, client, make_user, login, db,
                                                                         monkeypatch):
    from bananawiki.wiki.features.canvas import routes

    monkeypatch.setattr(routes, "LIST_PAGE_SIZE", 2, raising=False)
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    c1, c2, c3, c4, c5 = _canvases(app, db, make_user("owner1", role="editor"), 5)
    login(client, make_user("reader1"))
    assert client.post("/api/canvas/layout-order", json={"layout_ids": [c1, c2, c3, c4, c5]}).status_code == 200
    assert _listed(client)[0] == [c1, c2]
    assert _listed(client, "?page=2")[0] == [c3, c4]
    # The first page sends only its own canvases: the others keep their places after them.
    assert client.post("/api/canvas/layout-order", json={"layout_ids": [c2, c1]}).status_code == 200
    assert _listed(client)[0] == [c2, c1]
    assert _listed(client, "?page=2")[0] == [c3, c4]
    assert _listed(client, "?page=3")[0] == [c5]


def test_nw3_4_saving_a_large_shared_order_takes_linear_time(app, client, make_user, login, db, monkeypatch):
    """Under open access every save rewrites the shared order, which can hold many canvases."""
    import time

    from bananawiki.wiki.features.canvas import access, routes, service

    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor', "
               "canvas_open_access = 1 WHERE id = 1")
    monkeypatch.setattr(routes, "ORDER_LIMIT", 50_000)  # as large as an order saved before the limit
    count = 40_000
    visible = list(range(1, count + 1))
    hidden = list(range(count + 1, count + 101))  # in the shared order, not visible to this user
    shared = visible[:20_000:-1] + hidden + [visible[0]]  # the shared rows have no unique key: a repeat is dropped
    saved: dict = {}
    # New int objects on every read, as from the database (identical objects compare without a call).
    monkeypatch.setattr(access, "ordered_ids", lambda user, owner: [int(str(i)) for i in visible])
    monkeypatch.setattr(service, "order_ids", lambda owner: [int(str(i)) for i in shared])
    monkeypatch.setattr(service, "save_order", lambda owner, ids: saved.update(owner=owner, ids=ids))
    login(client, make_user("reader1"))
    sent = [5, 3, 5, 99_999] + visible[:4000]
    started = time.perf_counter()
    response = client.post("/api/canvas/layout-order", json={"layout_ids": sent})
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert saved["owner"] is None
    # The sent canvases, then the rest of the shared order (hidden rows in their places); the
    # canvases never placed (4001-20001 here) are not stored and keep sorting after them.
    assert saved["ids"] == [5, 3, 1, 2, 4] + visible[5:4000] + visible[:20_000:-1] + hidden
    assert elapsed < 0.3, elapsed  # linear: about 0.02 s; the quadratic membership check took 1.6 s


def test_nw3_4_saving_the_order_stores_only_placed_canvases(app, client, make_user, login, db, monkeypatch):
    """A save stores the canvases sent and those already placed, not every visible canvas."""
    from bananawiki.wiki.features.canvas import routes

    monkeypatch.setattr(routes, "LIST_PAGE_SIZE", 2, raising=False)
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    c1, c2, c3, c4, c5, c6 = _canvases(app, db, make_user("owner1", role="editor"), 6)
    reader = make_user("reader1")
    login(client, reader)

    def stored() -> list[int]:
        return db.column("SELECT layout_id FROM canvas_user_layout_order WHERE user_id = ? ORDER BY sort_order",
                         (reader["id"],))

    assert client.post("/api/canvas/layout-order", json={"layout_ids": [c1]}).status_code == 200
    assert stored() == [c1]  # not all six visible canvases
    assert _listed(client)[0] == [c1, c6]
    assert _listed(client, "?page=2")[0] == [c5, c4]
    assert client.post("/api/canvas/layout-order", json={"layout_ids": [c6, c3]}).status_code == 200
    assert stored() == [c6, c3, c1]
    assert [_listed(client, f"?page={n}")[0] for n in (1, 2, 3)] == [[c6, c3], [c1, c5], [c4, c2]]

    # The saved order is bounded: a save sends at most ORDER_LIMIT canvases and keeps as many.
    monkeypatch.setattr(routes, "ORDER_LIMIT", 3)
    assert client.post("/api/canvas/layout-order", json={"layout_ids": [c5, c4]}).status_code == 200
    assert stored() == [c5, c4, c6]
    assert client.post("/api/canvas/layout-order", json={"layout_ids": [c1, c2, c3, c4]}).status_code == 400


def test_nw3_4_embed_picker_stops_reading_canvases_once_it_has_its_results(app, client, make_user, login, db,
                                                                          monkeypatch):
    from bananawiki.wiki.features.canvas import access
    from bananawiki.wiki.features.page_builder import embeds

    real_db = access.db
    read: list[int] = []

    class Counting:
        def __getattr__(self, name):
            return getattr(real_db, name)

        def all(self, sql, *args):
            rows = real_db.all(sql, *args)
            if "FROM canvas__layouts" in sql:
                read.append(len(rows))
            return rows

    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor', "
               "page_builder_enabled = 1, page_builder_access = 'editor' WHERE id = 1")
    ids = _canvases(app, db, make_user("owner1", role="editor"), 9)
    monkeypatch.setattr(access, "_LIST_BATCH", 2)
    monkeypatch.setattr(access, "db", Counting())
    monkeypatch.setattr(embeds, "MAX_RESULTS", 3)
    login(client, make_user("editor1", role="editor"))
    items = client.get("/api/page-builder/embeddables?kind=canvas").get_json()["items"]
    assert [item["title"] for item in items] == ["Canvas 8", "Canvas 7", "Canvas 6"]
    assert sum(read) == 4  # two batches, not all nine canvases
    read.clear()
    assert [item["title"] for item in client.get("/api/page-builder/embeddables?kind=canvas&q=canvas 0")
            .get_json()["items"]] == ["Canvas 0"]
    assert sum(read) == len(ids)  # a search that finds little reads each canvas once
    # The canvas list's windows come from the same batches.
    from bananawiki.wiki.db import connection_scope

    reader = make_user("reader1")
    with app.test_request_context(), connection_scope():
        assert [layout["id"] for layout in access.visible_layouts(reader, limit=3, offset=2)] == ids[::-1][2:5]
        assert [layout["id"] for layout in access.visible_layouts(reader)] == ids[::-1]


def test_nw3_4_embed_picker_search_costs_one_pass_over_the_canvases(app, make_user, db, monkeypatch):
    """A picker search that matches nothing reads every batch: together they cost about one read of the table.

    Measured in SQLite VM steps (a progress handler): batches paged with OFFSET over an unindexed order
    sorted the whole table again for every batch, quadratic in the number of canvases.
    """
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.db import db as wiki_db
    from bananawiki.wiki.features.canvas import access
    from bananawiki.wiki.features.page_builder import embeds

    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor', "
               "page_builder_enabled = 1, page_builder_access = 'editor' WHERE id = 1")
    owner = make_user("owner1", role="editor")
    editor = make_user("editor1", role="editor")
    count = 3000
    db.executemany(
        "INSERT INTO canvas__layouts (slug, title, description, creator_id, updated_at, visibility) "
        "VALUES (?, ?, ?, ?, ?, 'public')",
        [(f"canvas-{n}", f"Canvas {n}", "d" * 500, owner["id"], f"2026-01-01 {n // 3600:02d}:{n // 60 % 60:02d}:"
          f"{n % 60:02d}") for n in range(count)])
    monkeypatch.setattr(access, "_LIST_BATCH", 50)  # 60 batches

    def steps(run) -> int:
        counted = [0]

        def tick():
            counted[0] += 1
            return 0

        conn = wiki_db.session.conn
        conn.set_progress_handler(tick, 100)
        try:
            run()
        finally:
            conn.set_progress_handler(None, 0)
        return counted[0]

    with app.test_request_context(), connection_scope():
        # What the picker cost before it read in batches: one SELECT of every canvas.
        one_read = steps(lambda: wiki_db.all(f"{access.LAYOUT_SELECT}{access._LIST_ORDER}"))
        assert one_read > 0
        found: list = []
        searched = steps(lambda: found.extend(embeds.search(editor, "nothing matches", "canvas")))
        assert found == []
        assert searched <= 2 * one_read, (searched, one_read)
        # A search that matches the newest canvases still stops early.
        assert steps(lambda: embeds.search(editor, "canvas", "canvas")) * 10 < one_read
        assert [layout["id"] for layout in access.visible_layouts(editor, limit=3, offset=99)] == \
            [layout["id"] for layout in wiki_db.all(f"{access.LAYOUT_SELECT}{access._LIST_ORDER} LIMIT 3 OFFSET 99")]
        # Every batch walks the index; none sorts the table.
        plan = " ".join(row["detail"] for row in wiki_db.all(
            f"EXPLAIN QUERY PLAN {access.LAYOUT_SELECT} WHERE (l.updated_at, l.id) < (?, ?){access._LIST_ORDER} "
            "LIMIT ?", ("2026-01-01 01:00:00", 1, 50)))
        assert "idx_canvas_layouts_updated" in plan and "TEMP B-TREE" not in plan, plan


def test_nw3_4_upgrade_notes_say_1_7_3_cannot_open_the_migrated_database(tmp_path):
    import pytest

    from bananawiki.core.sqlite import Database, DatabaseUnavailable
    from bananawiki.wiki import migrations

    # A database migrated by this release is refused by 1.7.3 (schema 15).
    path = tmp_path / "wiki.db"
    Database(path, application_id=migrations.APPLICATION_ID, migrations=migrations.MIGRATIONS,
             baseline=migrations.BASELINE, bootstrap=migrations.bootstrap).initialize()
    released = Database(path, application_id=migrations.APPLICATION_ID, migrations=migrations.MIGRATIONS[:12],
                        baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)
    assert migrations.BASELINE + 12 == 15 < migrations.LATEST
    with pytest.raises(DatabaseUnavailable, match="newer than this release"):
        released.initialize()

    # So the upgrade notes say so and ask for a backup first, as every earlier migration's notes do.
    root = Path(__file__).resolve().parents[2]
    upgrading = (root / "UPGRADING.md").read_text(encoding="utf-8")
    section = upgrading.split("\n## Upgrading from 1.7.3 to 1.7.6\n", 1)[1].split("\n## ", 1)[0]
    assert f"schema 15 to {migrations.LATEST}" in section
    assert "1.7.3 cannot open the database" in section and "`sudo bananawiki backup`" in section
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## 1.7.6", 1)[1].split("\n## ", 1)[0]
    assert f"schema {migrations.LATEST}" in unreleased and "1.7.3 cannot open" in unreleased
