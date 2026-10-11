"""Content area fixes of the 2.x release audit: preview access, stale page updates, file names."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bananawiki.wiki import storage
from bananawiki.wiki.features.pages import service

from .pages_support import get_page, in_app, make_page

# ── Live preview ─────────────────────────────────────────────────────────────


def test_preview_is_for_accounts_that_can_edit(app, client, make_user, login):
    login(client, make_user("reader"))
    assert client.post("/api/preview", json={"content": "# Hi"}).status_code == 403
    editor = app.test_client()
    login(editor, make_user("writer", role="editor"))
    response = editor.post("/api/preview", json={"content": "# Hi"})
    assert response.status_code == 200 and "<h1" in response.get_json()["html"]


def test_preview_rate_limit_is_tighter_than_before(app, make_user, login, frozen_limiter_clock):
    editor = app.test_client()
    login(editor, make_user("typist", role="editor"))
    statuses = [editor.post("/api/preview", json={"content": "x"}).status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60 and statuses[60] == 429


# ── Page updates ─────────────────────────────────────────────────────────────


def test_title_change_keeps_content_saved_after_the_callers_snapshot(app):
    page = make_page(app, "Plans", "first draft")
    snapshot = dict(page)
    in_app(app, lambda: service.update(get_page(app, page["id"]), author_id=None, content="second draft"))
    renamed = in_app(app, lambda: service.update(snapshot, author_id=None, title="Better plans"))
    assert renamed["title"] == "Better plans"
    assert renamed["content"] == "second draft"
    assert get_page(app, page["id"])["content"] == "second draft"


def test_builder_change_keeps_title_saved_after_the_callers_snapshot(app):
    page = make_page(app, "Landing", "text")
    snapshot = dict(page)
    in_app(app, lambda: service.update(get_page(app, page["id"]), author_id=None, title="Renamed"))
    updated = in_app(app, lambda: service.update(snapshot, author_id=None, content="new text"))
    assert updated["title"] == "Renamed" and updated["content"] == "new text"


# ── Unicode file names ───────────────────────────────────────────────────────


def _upload(client, slug, name, data=b"%PDF-1.4 test"):
    return client.post(f"/page/{slug}/attachments", data={"files": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


@pytest.mark.parametrize("raw, cleaned", [
    ("отчёт.pdf", "отчёт.pdf"),
    ("报告.docx", "报告.docx"),
    ("Αναφορά 2024.xlsx", "Αναφορά 2024.xlsx"),
    ("cafe\u0301.pdf", "café.pdf"),  # NFC
    ("C:\\Users\\me\\東京.png", "東京.png"),
    ("../../etc/passwd", "passwd"),
    ("evil\u202egnp.exe", "evilgnp.exe"),
    ("line\nbreak\t.txt", "line break .txt"),
    ('a<b>:"c"|?*.txt', "a_b___c____.txt"),
    ("...hidden", "hidden"),
    ("", "file"),
    ("\x00\x01", "file"),
])
def test_clean_filename_keeps_every_script(raw, cleaned):
    assert storage.clean_filename(raw) == cleaned


def test_clean_filename_bounds_the_length_but_keeps_the_extension():
    name = storage.clean_filename("я" * 400 + ".pdf")
    assert name.endswith("я.pdf") and len(name) <= storage.MAX_NAME_CHARS


@pytest.mark.parametrize("name, ext", [
    ("отчёт.PDF", "pdf"), ("a.tar.gz", "gz"), ("noext", ""), ("файл.пдф", ""), ("x.\u212a", ""),
    ("dir.d/file", ""), ("x.", ""),
])
def test_extension_is_ascii_only(name, ext):
    assert storage.extension(name) == ext


@pytest.mark.parametrize("name, expected", [
    ("report.pdf", 'attachment; filename=report.pdf'),
    ("отчёт.pdf", "attachment; filename=download.pdf; filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf"),
    ("café menu.pdf", "attachment; filename=\"cafe menu.pdf\"; filename*=UTF-8''caf%C3%A9%20menu.pdf"),
])
def test_content_disposition_is_latin1_safe(name, expected):
    header = storage.content_disposition(name)
    header.encode("latin-1")
    assert header == expected


def test_attachments_with_non_latin_names_are_stored_and_served(app, admin_client, db):
    page = make_page(app, "Docs", "text")
    assert _upload(admin_client, page["slug"], "отчёт.pdf").status_code == 302
    assert _upload(admin_client, page["slug"], "报告.pdf").status_code == 302
    rows = db.all("SELECT id, original_name FROM page_attachments WHERE page_id = ? ORDER BY id", (page["id"],))
    assert [row["original_name"] for row in rows] == ["отчёт.pdf", "报告.pdf"]
    download = admin_client.get(f"/page/{page['slug']}/attachments/{rows[0]['id']}/download")
    assert download.status_code == 200
    disposition = download.headers["Content-Disposition"]
    disposition.encode("latin-1")
    assert "filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf" in disposition


def test_editor_images_with_non_latin_names_are_accepted(admin_client):
    data = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 200, 10)).save(data, "PNG")
    data.seek(0)
    response = admin_client.post("/api/upload", data={"file": (data, "東京.png")}, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_json()


def test_canvas_outline_download_of_a_non_latin_title(app, make_user, login):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.canvas import service as canvas

    owner = make_user("planner", role="editor")
    with app.test_request_context(), connection_scope():
        layout = canvas.create("東京 計画", "", owner["id"])
    client = app.test_client()
    login(client, owner)
    response = client.get(f"/canvas/{layout['slug']}/outline?format=md")
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    disposition.encode("latin-1")
    assert disposition.startswith("attachment;") and "filename*=UTF-8''" in disposition


def test_kanban_json_export_of_a_non_latin_title(admin_client):
    from .test_kanban import board_id_from

    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Задачи"}))
    response = admin_client.get(f"/kanban/{board}/export?format=json")
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    disposition.encode("latin-1")
    assert "filename*=UTF-8''%D0%B7%D0%B0%D0%B4%D0%B0%D1%87%D0%B8.kanban.json" in disposition


# ── Rendering cost ───────────────────────────────────────────────────────────


def test_unchanged_pages_are_not_laid_out_as_pdf_again(app, monkeypatch):
    from bananawiki.wiki.features.page_export import pdf

    calls = []
    original = pdf._render
    monkeypatch.setattr(pdf, "_render", lambda **kwargs: calls.append(1) or original(**kwargs))
    options = {"title": "Cached", "content": "# Big page\n\n" + "words " * 2000, "site_name": "Wiki",
               "base_url": "https://wiki.example/", "meta": ["by someone"]}
    with app.test_request_context():
        first = pdf.render(**options)
        assert pdf.render(**options) == first and len(calls) == 1
        pdf.render(**{**options, "meta": ["by someone else"]})
    assert len(calls) == 2


def test_viewing_a_page_twice_renders_its_markdown_once(app, admin_client, monkeypatch):
    from bananawiki.wiki import markdown

    markdown.clear_render_cache()
    calls = []
    original = markdown._render
    monkeypatch.setattr(markdown, "_render", lambda *args: calls.append(1) or original(*args))
    page = make_page(app, "Long read", "# Long read\n\n" + "A paragraph with some words.\n\n" * 300)
    for _ in range(3):
        assert admin_client.get(f"/page/{page['slug']}").status_code == 200
    assert len(calls) == 1


def test_code_highlighting_is_for_accounts_that_can_edit(app, client, make_user, login):
    login(client, make_user("reader"))
    body = {"content": "x = 1", "language": "python"}
    assert client.post("/api/code/highlight", json=body).status_code == 403
    editor = app.test_client()
    login(editor, make_user("writer", role="editor"))
    response = editor.post("/api/code/highlight", json=body)
    assert response.status_code == 200 and "codehilite" in response.get_json()["html"]


def test_a_builder_page_highlights_within_one_budget(app, monkeypatch):
    from markdown.extensions import codehilite

    from bananawiki.wiki import markdown
    from bananawiki.wiki.features.page_builder import document

    markdown.clear_render_cache()
    sizes: list[int] = []
    for module in (codehilite, markdown):
        original = module.highlight
        monkeypatch.setattr(module, "highlight",
                            lambda source, *a, _original=original: sizes.append(len(source)) or _original(source, *a))
    dense = "a+b-c*d/e%f&g|h^i<j>k!l~m?n:o;p,q.r(s)t[u]v{w}x=y\n"
    blocks = [{"type": "text", "format": "markdown", "text": f"```c\n{dense * 30}```\n\n{number}"}
              for number in range(50)]
    blocks += [{"type": "code", "language": "c", "code": f"{dense * 30}{number}"} for number in range(50)]
    validated = document.validate({"version": 2, "blocks": blocks})
    with app.test_request_context():
        html = str(document.render(validated))
    assert sum(sizes) + markdown.HIGHLIGHT_BLOCK_COST * len(sizes) <= markdown.MAX_HIGHLIGHT_DOCUMENT_CHARS
    assert html.count("a+b-c*d/e%f&amp;g") >= 30 * 50  # blocks past the budget stay readable code
