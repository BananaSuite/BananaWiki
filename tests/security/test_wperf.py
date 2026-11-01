"""Regression tests for the resource-exhaustion findings fixed by wperf (AUDIT.md W2-1..W2-4, W3-5, W4-1, W4-7).

Each test reproduces the attack with its limits scaled down and asserts the bounded behaviour.
"""

from __future__ import annotations

from tests.features.chat_support import JSON, create_group, send

# W4-7: chat polling


def test_w4_7_poll_reports_a_bounded_list_of_recent_deletions(client, login, make_user, db, monkeypatch):
    from bananawiki.wiki.features.chat import store

    login(client, make_user("owner"))
    group_id = create_group(client)
    for index in range(5):
        send(client, f"/groups/{group_id}/send", f"message {index}")
    ids = db.column("SELECT id FROM group_messages WHERE group_id = ? AND is_system = 0 ORDER BY id", (group_id,))
    for index, message_id in enumerate(ids):
        # One deletion long ago, the others recent.
        when = "2000-01-01 00:00:00" if index == 0 else f"2999-01-01 00:00:0{index}"
        db.execute("UPDATE group_messages SET content = '', is_deleted = 1, deleted_at = ? WHERE id = ?",
                   (when, message_id))
    monkeypatch.setattr(store, "MAX_DELETED_IDS", 2)

    payload = client.get(f"/groups/{group_id}/messages?after={ids[-1]}&since=1990-01-01", headers=JSON).json

    assert payload["deleted"] == [ids[4], ids[3]]


# W3-5: API listings are paged


def test_w3_5_category_detail_pages_its_pages(app, client, make_user, db):
    from tests.features.api_support import call, enable_api, issue
    from tests.features.pages_support import make_category, make_page

    enable_api(app, db)
    admin = make_user("apiadmin", role="admin")
    token = issue(app, admin, scopes=("categories",), write=False)
    category = make_category(app, "Big")
    for index in range(5):
        make_page(app, f"Page {index}", category_id=category["id"])

    first = call(client, "GET", f"/categories/{category['id']}?limit=2", token).json
    last = call(client, "GET", f"/categories/{category['id']}?limit=2&offset=4", token).json
    everything = call(client, "GET", f"/categories/{category['id']}", token).json

    assert [page["title"] for page in first["category"]["pages"]] == ["Page 0", "Page 1"]
    assert (first["limit"], first["offset"], first["next_offset"]) == (2, 0, 2)
    assert [page["title"] for page in last["category"]["pages"]] == ["Page 4"]
    assert last["next_offset"] is None
    assert len(everything["category"]["pages"]) == 5 and everything["next_offset"] is None


def test_w3_5_listings_read_only_their_window(app, make_user, monkeypatch):
    """Canvas and board lists stop reading once their window is filled; the order stays the saved one."""
    from bananawiki.wiki.db import db
    from bananawiki.wiki.features.canvas import access as canvas_access
    from bananawiki.wiki.features.canvas import service as canvases
    from bananawiki.wiki.features.kanban import service as kanban
    from tests.features.pages_support import in_app

    owner = make_user("lister", role="admin")

    def run():
        for index in range(7):
            canvases.create(f"Canvas {index}", "", owner["id"])
            kanban.create_board(owner, f"Board {index}")
        boards = kanban.ordered_boards(owner)
        kanban.save_board_order(owner, [boards[3]["id"], boards[1]["id"]])
        expected = [board["id"] for board in kanban.ordered_boards(owner)]
        assert expected[:2] == [boards[3]["id"], boards[1]["id"]]
        assert [b["id"] for b in kanban.ordered_boards(owner, limit=3, offset=1)] == expected[1:4]

        monkeypatch.setattr(canvas_access, "_LIST_BATCH", 2)
        statements: list[str] = []

        class Spy:
            def __getattr__(self, name):
                return getattr(db, name)

            def all(self, sql, *args, **kwargs):
                statements.append(sql)
                return db.all(sql, *args, **kwargs)

        monkeypatch.setattr(canvas_access, "db", Spy())
        window = canvas_access.visible_layouts(owner, limit=2, offset=1)
        assert [layout["title"] for layout in window] == ["Canvas 5", "Canvas 4"]
        assert sum("canvas__layouts" in sql for sql in statements) == 2  # 4 of the 7 canvases read

    in_app(app, run)


# W2-4: a page rename rewrites linking pages


def test_w2_4_rename_copies_a_bounded_amount_of_linking_pages_into_history(app, client, login, make_user, db,
                                                                         monkeypatch):
    from bananawiki.wiki.features.pages import service
    from tests.features.pages_support import make_page

    monkeypatch.setattr(service, "MAX_REWRITE_HISTORY_CHARS", 25_000)
    editor = make_user("renamer", role="editor")
    login(client, editor)
    make_page(app, "Hub", "x", author_id=editor["id"])
    body = "[hub](/page/hub) " + "A" * 10_000
    for index in range(6):
        make_page(app, f"Linker {index}", body, author_id=editor["id"])
    before = db.scalar("SELECT SUM(length(content)) FROM page_history")

    client.post("/page/hub/rename", data={"new_slug": "hub2"})

    added = db.scalar("SELECT SUM(length(content)) FROM page_history") - before
    assert added <= 25_000
    # Every linking page still gets the new link.
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE content LIKE '%/page/hub2)%'") == 6


# W2-2: PDF export memory


def _noisy_jpeg(path, size):
    import io
    import os

    from PIL import Image

    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buffer, format="JPEG", quality=95)
    path.write_bytes(buffer.getvalue())
    return len(buffer.getvalue())


def test_w2_2_pdf_pictures_are_decoded_in_the_slot_downscaled_and_budgeted(app, monkeypatch):
    from contextlib import contextmanager

    from bananawiki.wiki import storage
    from bananawiki.wiki.features.page_export import pdf

    monkeypatch.setattr(pdf, "IMAGE_BOX", (160, 120))
    monkeypatch.setattr(pdf, "MAX_IMAGE_BYTES", 30_000)
    slots = []
    real_slot = storage.image_slot

    @contextmanager
    def counted_slot():
        with real_slot():
            slots.append(1)
            yield

    monkeypatch.setattr(storage, "image_slot", counted_slot)
    with app.test_request_context():
        names, sizes = [], []
        for index in range(4):
            path = storage.folder_path("uploads") / f"{index}a0c2a9e1d7b4c3a8e6f.jpg"
            sizes.append(_noisy_jpeg(path, (800, 600)))
            names.append(path.name)
        content = "\n\n".join(f"![photo {index}](/static/uploads/{name})" for index, name in enumerate(names))
        data = pdf.render(title="Photos", content=content, site_name="Wiki", base_url="https://wiki.example/",
                          meta=[])
    assert data.startswith(b"%PDF")
    assert slots  # every picture is decoded in the process's image slot
    assert len(data) < 30_000 + 60_000 < sum(sizes)  # the originals are not embedded, and the budget holds


def test_w2_2_one_pdf_is_laid_out_at_a_time(app, admin_client, monkeypatch):
    from bananawiki.wiki.features.page_export import pdf
    from tests.features.pages_support import make_page

    make_page(app, "Busy", "# Text")
    monkeypatch.setattr(pdf, "BUILD_WAIT_SECONDS", 0.01)
    assert pdf._build_slot.acquire(timeout=1)
    try:
        response = admin_client.get("/page/busy/export-pdf")
    finally:
        pdf._build_slot.release()
    assert response.status_code == 303
    assert admin_client.get("/page/busy/export-pdf").data.startswith(b"%PDF")


# W2-3: "download all attachments"


def test_w2_3_attachment_zip_is_streamed_stored_and_limited_per_process(app, admin_client, db, monkeypatch):
    import io
    import os
    import tempfile
    import zipfile

    from bananawiki.wiki.features.attachments import service
    from tests.features.pages_support import make_page

    page = make_page(app, "Files")
    payload = os.urandom(300_000)
    for name in ("one.bin", "two.bin"):
        admin_client.post(f"/page/{page['slug']}/attachments", data={"files": (io.BytesIO(payload), name)},
                          content_type="multipart/form-data")
    assert db.scalar("SELECT COUNT(*) FROM page_attachments WHERE page_id = ?", (page["id"],)) == 2
    temporaries = []
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda *a, **k: temporaries.append(1) or io.BytesIO())

    response = admin_client.get(f"/page/{page['slug']}/attachments/download-all", buffered=False)
    pieces = list(response.response)
    response.close()
    assert len(pieces) > 2 and temporaries == []  # sent while it is made, nothing written to disk
    with zipfile.ZipFile(io.BytesIO(b"".join(pieces))) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_STORED}
        assert archive.read("one.bin") == payload

    held = [service._zip_streams.acquire(blocking=False) for _ in range(service.MAX_ZIP_STREAMS)]
    try:
        assert all(held)
        assert admin_client.get(f"/page/{page['slug']}/attachments/download-all").status_code == 303
    finally:
        for _ in held:
            service._zip_streams.release()


# W2-1: syntax highlighting is bounded in time


def test_w2_1_highlighting_stops_at_deadline(app, client, make_user, login, monkeypatch):
    """A lexer quadratic on its input (csharp on "x\\n" lines) is cut off by the document's time."""
    import time

    from bananawiki.wiki import markdown

    monkeypatch.setattr(markdown, "MAX_HIGHLIGHT_SECONDS", 0.1)
    markdown.clear_render_cache()
    login(client, make_user("previewer", role="editor"))
    source = "```csharp\n" + "x\n" * 4000 + "```\n"  # about 5 s of lexing before the fix

    started = time.thread_time()
    response = client.post("/api/preview", json={"content": source})
    spent = time.thread_time() - started

    assert response.status_code == 200
    assert spent < 1.5
    assert response.json["html"].count("x\n") >= 3999  # the code is all there, as plain code


def test_w2_1_lexers_that_answer_late_see_short_pieces(monkeypatch):
    """Lexers that yield nothing until a long scan is done are given pieces of at most 1 KB."""
    from bananawiki.wiki import highlighter, markdown

    seen: list[int] = []
    original = highlighter.highlight

    def spy(chunks, *args):
        seen.extend(len(chunk) for chunk in chunks)
        return original(chunks, *args)

    monkeypatch.setattr(highlighter, "highlight", spy)
    markdown.clear_render_cache()
    source = "```c\n" + "\n".join(f"int value_{n} = {n}; /* line {n} */" for n in range(1000)) + "\n```\n"

    html = markdown.render(source)

    assert "<span" in html and max(seen) <= markdown.HIGHLIGHT_CHUNK_CHARS
    # A language whose lexer is slow on any piece (Maple: exponential) is shown without colours.
    assert "<span class=\"" not in markdown.highlight_code('"' + "\\a" * 40, "maple").replace(
        '<span class="w">', "")


def test_w2_1_canvas_nodes_share_one_highlighting_time(app, make_user, monkeypatch):
    from bananawiki.wiki import markdown
    from bananawiki.wiki.features.canvas import present

    monkeypatch.setattr(markdown, "MAX_HIGHLIGHT_SECONDS", 0.05)
    markdown.clear_render_cache()
    present._cache.clear()
    nodes = [{"id": f"n{n}", "type": "code", "language": "csharp", "content": "x\n" * 1500 + f"// {n}"}
             for n in range(6)]
    with app.test_request_context():
        rendered = present.present_nodes(nodes, make_user("viewer", role="admin"))["rendered"]
    plain = [key for key, entry in rendered.items() if "<span" not in entry["html"]]
    assert len(plain) >= 5  # one time for the answer, not one per node
    # What the shared time left plain is not kept: alone, with time enough, a node is highlighted.
    monkeypatch.setattr(markdown, "MAX_HIGHLIGHT_SECONDS", 10)
    with app.test_request_context():
        assert "codehilite" in present.rendering(nodes[-1])["html"]


def test_w2_1_concurrent_first_readers_render_once(monkeypatch):
    import threading

    from bananawiki.wiki import markdown

    markdown.clear_render_cache()
    calls = []
    original = markdown._render
    gate = threading.Event()

    def slow_render(*args):
        calls.append(1)
        gate.wait(1)
        return original(*args)

    monkeypatch.setattr(markdown, "_render", slow_render)
    source = "# Shared\n\n" + "text " * 1000
    threads = [threading.Thread(target=markdown.render, args=(source,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join()
    assert len(calls) == 1


def test_w2_1_the_lexers_were_measured_with_the_installed_pygments():
    import pygments

    from bananawiki.wiki import markdown

    assert pygments.__version__ == markdown.MEASURED_PYGMENTS_VERSION, (
        "run scripts/measure_lexers.py and update markdown._UNBOUNDED_LEXERS")


# W4-1: kanban writes snapshot the whole board


def test_w4_1_boards_are_capped_and_large_ones_snapshot_at_most_once_a_minute(app, make_user, monkeypatch):
    import pytest

    from bananawiki.wiki.db import db
    from bananawiki.wiki.features.kanban import fields, history, service, store
    from bananawiki.wiki.features.kanban.fields import KanbanError
    from tests.features.pages_support import in_app

    monkeypatch.setattr(fields, "MAX_TICKETS_PER_BOARD", 4)
    monkeypatch.setattr(fields, "MAX_BOARD_BYTES", 25_000)
    monkeypatch.setattr(history, "LARGE_BOARD_BYTES", 5_000)
    owner = make_user("boarder", role="admin")

    def run():
        board = service.create_board(owner, "Big")
        column = store.columns_of(board["id"])[0]
        tickets = [service.create_ticket(board, column, owner, {"title": f"T{n}", "description": "d" * 9_000})
                   for n in range(2)]
        with pytest.raises(KanbanError) as refused:  # a third long description would pass 25,000 bytes
            service.create_ticket(board, column, owner, {"title": "T2", "description": "d" * 9_000})
        assert refused.value.key == "kanban.error.board_full"
        service.create_ticket(board, column, owner, {"title": "T2"})
        service.create_ticket(board, column, owner, {"title": "T3"})
        with pytest.raises(KanbanError) as refused:
            service.create_ticket(board, column, owner, {"title": "T4"})
        assert refused.value.key == "kanban.error.board_full_tickets"

        rows = db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board["id"],))
        for priority in ("low", "high", "low", "critical", "high"):
            service.update_ticket(board, store.get_ticket(tickets[0]["id"]), owner, {"priority": priority})
        # Within a minute of its latest entry a large board is not snapshotted again...
        assert db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board["id"],)) == rows
        # ...and a revert first records the state those changes left.
        first = history.entry(board["id"], history.entries(board["id"])[-1]["id"])
        service.revert(board, first, owner)
        kept = [history.entry(board["id"], row["id"])["state"] for row in history.entries(board["id"])[:2]]
        before_revert = {t["title"]: t["priority"] for c in kept[1]["columns"] for t in c["tickets"]}
        assert before_revert["T0"] == "high"

    in_app(app, run)
