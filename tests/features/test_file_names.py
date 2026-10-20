"""File names in any script: cleaning, extensions, Content-Disposition and the downloads that use them."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bananawiki.wiki import storage

from .pages_support import make_page


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
