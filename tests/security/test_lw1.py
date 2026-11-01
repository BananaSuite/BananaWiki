"""Regression tests for LW1-1, LW1-2 and LW1-3 (full re-audit pass 6: search excerpts, kanban assignees and the
canvas export's download place)."""

from __future__ import annotations

import io
import json
import re
import time

from bananawiki.wiki.features.pages import search, service
from tests.features.pages_support import make_page
from tests.security.test_fw1 import _traced

# LW1-1: search excerpts cost the square of the query's matches in a page

WORDS = list("abcdefghijkl")


def _matches(statements: list[str]) -> list[str]:
    return [sql for sql in statements if "pages_fts MATCH" in sql]


def test_lw1_1_excerpts_do_not_cost_the_square_of_the_matches(app, admin_client, monkeypatch):
    # 24,000 matches of 12 different words: FTS5's snippet() took seconds for this one page.
    make_page(app, "Notes", "# Notes\n\n" + (" ".join(WORDS) + " ") * 2000)
    make_page(app, "Report", "intro " * 40 + "the **quarterly** figures")
    statements = _traced(monkeypatch)
    for url, scope in (("/search", "all"), ("/api/sidebar/search", "content")):
        started = time.monotonic()
        response = admin_client.get(url, query_string={"q": " ".join(WORDS), "scope": scope})
        assert response.status_code == 200 and time.monotonic() - started < 1.0
    assert statements and not any("snippet(" in sql for sql in statements)
    # The excerpt is cut around the first match and marked as before.
    found = admin_client.get("/api/sidebar/search", query_string={"q": "quarterly", "scope": "content"})
    snippet = found.get_json()["pages"][0]["snippet"]
    assert snippet.startswith("… intro") and snippet.endswith("the <mark>quarterly</mark> figures"), snippet
    page = admin_client.get("/search", query_string={"q": "quarterly"}).get_data(as_text=True)
    assert "the <mark>quarterly</mark> figures" in page


def test_lw1_1_excerpts_are_bounded_by_the_scan_and_the_word_count(app):
    marked = service._excerpt("x" * 5000 + " \x02word\x03 " + "tail " * 100, 16, more=True)
    # A run without spaces counts as many words, so an excerpt has at most 16 pieces of 30 characters.
    assert marked.startswith("… ") and marked.endswith(" …") and len(marked) < 16 * 31 + 10
    assert "\x02word\x03" in marked
    with app.test_request_context():
        page = make_page(app, "Long", "start " + "filler " * (service.EXCERPT_SCAN // 7) + "needle")
        excerpts = service.excerpts(['"needle"'], [page["id"]], 14)
    # A match past the scanned start is not looked for: the excerpt is the start of the page.
    assert excerpts[page["id"]].startswith("start filler") and "\x02" not in excerpts[page["id"]]


def test_lw1_1_repeated_words_and_long_phrases_are_bounded(app, admin_client, monkeypatch):
    make_page(app, "Notes", " ".join(WORDS))
    statements = _traced(monkeypatch)
    assert admin_client.get("/search", query_string={"q": " ".join(["a"] * 12)}).status_code == 200
    assert admin_client.get("/api/sidebar/search", query_string={"q": " ".join(["b"] * 12),
                                                                 "scope": "content"}).status_code == 200
    first, second = _matches(statements)[0], _matches(statements)[-1]
    assert first.count('"a"') == 1 and second.count('"b"') == 1, (first, second)

    statements.clear()
    phrase = '"' + " ".join(["a", "the"] * 74) + '"'
    assert admin_client.get("/search", query_string={"q": f"{phrase} c -{phrase}"}).status_code == 200
    expression = re.search(r"MATCH '(.*?)'", _matches(statements)[0]).group(1)
    assert len(re.findall(r"\b(?:a|the|c)\b", expression)) <= search.MAX_WORDS, expression


def test_lw1_1_a_search_that_runs_too_long_is_stopped(app, admin, admin_client, db, monkeypatch):
    from tests.features.api_support import call, enable_api, issue

    for number in range(30):
        make_page(app, f"Report {number}", "quarterly figures " * 50)
    enable_api(app, db)
    token = issue(app, admin)
    monkeypatch.setattr(service, "SEARCH_SECONDS", -1.0)  # already past the deadline
    monkeypatch.setattr(service, "_PROGRESS_STEPS", 1)
    page = admin_client.get("/search", query_string={"q": "quarterly"})
    assert page.status_code == 503 and "This search took too long" in page.get_data(as_text=True)
    sidebar = admin_client.get("/api/sidebar/search", query_string={"q": "quarterly", "scope": "content"})
    assert sidebar.status_code == 503 and "too long" in sidebar.get_json()["error"]
    assert call(admin_client, "GET", "/search?q=quarterly", token).status_code == 503
    monkeypatch.setattr(service, "SEARCH_SECONDS", 5.0)
    assert admin_client.get("/search", query_string={"q": "quarterly"}).status_code == 200


# LW1-2: ticket assignees are outside the board caps


def _board(app, client, make_user, login, db, people=25):
    """A board of an administrator, shared for writing with a plain user signed in on *client*, on a site where
    every account may open boards; returns the board, its first column and the ids of *people* other accounts."""
    from bananawiki.wiki import settings
    from bananawiki.wiki.db import connection_scope

    with app.test_request_context(), connection_scope():
        settings.update({"kanban_access": "all"}, internal=True)
    owner = app.test_client()
    login(owner, make_user("boss", role="admin"))
    mallory = make_user("mallory", role="user")
    others = [make_user(f"member{n:02d}")["id"] for n in range(people)]
    assert owner.post("/kanban/create", data={"title": "B"}).status_code == 302
    board_id = db.scalar("SELECT MAX(id) FROM kanban_boards")
    assert owner.post(f"/kanban/{board_id}/share", data={"action": "add_user", "user_id": mallory["id"],
                                                         "access_level": "write"}).status_code == 302
    login(client, mallory)
    column_id = db.scalar("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY sort_order LIMIT 1", (board_id,))
    return owner, board_id, column_id, others


def _assignees(db, ticket_id):
    return db.scalar("SELECT COUNT(*) FROM kanban_ticket_assignees WHERE ticket_id = ?", (ticket_id,))


def test_lw1_2_a_ticket_has_at_most_twenty_assignees(app, client, make_user, login, db):
    owner, board_id, column_id, others = _board(app, client, make_user, login, db)
    too_many = others[:21]
    created = client.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": "t", "assignees": too_many})
    assert created.status_code == 400 and "at most 20 assignees" in created.get_json()["error"]
    ticket = client.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": "t", "assignees": others[:20]})
    assert ticket.status_code == 201
    ticket_id = ticket.get_json()["id"]
    assert client.put(f"/api/kanban/tickets/{ticket_id}", json={"assignees": too_many}).status_code == 400
    bulk = client.post(f"/api/kanban/{board_id}/tickets/bulk",
                       json={"action": "assign", "ticket_ids": [ticket_id], "assignees": too_many})
    assert bulk.status_code == 400 and _assignees(db, ticket_id) == 20

    # A snapshot naming more (edited, or from an import) is cut to the first twenty when reverted to.
    entry = db.one("SELECT id, snapshot FROM kanban_board_history WHERE board_id = ? ORDER BY id DESC LIMIT 1",
                   (board_id,))
    state = json.loads(entry["snapshot"])
    state["columns"][0]["tickets"][0]["assignee_ids"] = others
    db.execute("UPDATE kanban_board_history SET snapshot = ? WHERE id = ?", (json.dumps(state), entry["id"]))
    assert client.post(f"/kanban/{board_id}/revert/{entry['id']}").status_code == 302
    assert _assignees(db, ticket_id) == 20

    names = [f"member{n:02d}" for n in range(25)]
    doc = {"title": "Imported", "columns": [{"title": "Todo", "tickets": [{"title": "x", "assignee_usernames": names}]}]}
    owner.post("/kanban/import", data={"import_file": (io.BytesIO(json.dumps(doc).encode()), "b.json")},
               content_type="multipart/form-data")
    imported = db.scalar("SELECT t.id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
                         "JOIN kanban_boards b ON b.id = c.board_id WHERE b.title = 'Imported'")
    assert imported and _assignees(db, imported) == 20


def test_lw1_2_assignees_count_towards_the_board_caps(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.kanban import fields, history

    _owner, board_id, column_id, others = _board(app, client, make_user, login, db, people=20)
    ids = [client.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": f"t{n}"}).get_json()["id"]
           for n in range(3)]

    def size():
        with app.test_request_context(), connection_scope():
            return history.board_bytes(board_id)

    empty = size()
    # Twenty people (8-character ids and names) are 440 bytes of every snapshot: room for one ticket's.
    monkeypatch.setattr(fields, "MAX_BOARD_BYTES", empty + 600)
    refused = client.post(f"/api/kanban/{board_id}/tickets/bulk",
                          json={"action": "assign", "ticket_ids": ids, "assignees": others})
    assert refused.status_code == 409 and "MB of text" in refused.get_json()["error"]
    assert sum(_assignees(db, ticket_id) for ticket_id in ids) == 0
    assert client.put(f"/api/kanban/tickets/{ids[0]}", json={"assignees": others}).status_code == 200
    assert client.put(f"/api/kanban/tickets/{ids[1]}", json={"assignees": others}).status_code == 409
    assert client.put(f"/api/kanban/tickets/{ids[0]}", json={"assignees": others[:1]}).status_code == 200

    # A board large through its assignees is snapshotted at most once a minute, like any large board.
    monkeypatch.setattr(fields, "MAX_BOARD_BYTES", 8 * 1024 * 1024)
    assert client.post(f"/api/kanban/{board_id}/tickets/bulk",
                       json={"action": "assign", "ticket_ids": ids, "assignees": others}).status_code == 200
    assert size() > empty + 1_000
    monkeypatch.setattr(history, "LARGE_BOARD_BYTES", empty + 1_000)
    entries = db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board_id,))
    assert client.put(f"/api/kanban/tickets/{ids[2]}", json={"title": "renamed"}).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board_id,)) == entries


# LW1-3: the canvas export takes its download place before it is built


def test_lw1_3_a_refused_canvas_export_is_not_built_or_counted(app, admin_client, db, monkeypatch):
    from bananawiki.core import downloads
    from bananawiki.wiki.features.canvas import transfer

    assert admin_client.post("/canvas/create", data={"title": "Plan"}).status_code == 302
    slug = db.scalar("SELECT slug FROM canvas__layouts ORDER BY id DESC LIMIT 1")
    built = []

    def counted(layout, doc, *, plain):  # stands for the zip of the canvas's images
        built.append(layout["slug"])
        return io.BytesIO(b"PK" + b"x" * downloads.LARGE_BODY), "application/zip", "plan.canvas.zip"

    monkeypatch.setattr(transfer, "build_export", counted)
    places = app.extensions["bananawiki.downloads"] = downloads.Places(1)
    release, _ = places.take("network:192.0.2.9")  # slow readers elsewhere hold every place
    assert [admin_client.get(f"/canvas/{slug}/export").status_code for _ in range(11)] == [503] * 11
    assert built == [], "refused before the export is built"
    release()
    answer = admin_client.get(f"/canvas/{slug}/export", buffered=True)
    assert answer.status_code == 200 and built == [slug], "the refused exports did not count against the limit"
