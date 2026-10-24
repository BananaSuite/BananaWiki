"""Uploads: what an interrupted upload leaves behind, the progress-bar request and the editor's file list.

An upload cut off mid-way (the connection drops, the person cancels) must
leave no file, no half-written temporary file and no database row behind.
"""

from __future__ import annotations

import io
import os
import time

import pytest
from werkzeug.datastructures import FileStorage
from werkzeug.test import EnvironBuilder

from bananawiki.wiki import storage

from .pages_support import in_app, make_page

BOUNDARY = "----bwtestboundary"


def _multipart(name: str, field: str, data: bytes) -> bytes:
    return (
        f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{BOUNDARY}--\r\n".encode()


def _every_file(app) -> list[str]:
    folders = app.config["BW"].folders
    found = []
    for name in ("attachments", "uploads"):
        for root, _dirs, files in os.walk(getattr(folders, name)):
            found += [os.path.join(root, f) for f in files]
    return found


def _rows(db) -> int:
    return db.scalar("SELECT COUNT(*) FROM page_attachments")


@pytest.mark.parametrize("path", ["/api/page/{id}/attachments", "/page/{slug}/attachments", "/api/upload"])
def test_a_body_cut_off_mid_upload_leaves_nothing_behind(app, admin_client, db, path):
    page = make_page(app, "Cut off")
    field = "files" if path.startswith("/page/") else "file"
    body = _multipart("report.pdf" if field == "files" or "attachments" in path else "photo.png", field,
                      os.urandom(400_000))
    sent = body[: len(body) // 2]  # the connection drops half way: Content-Length promises the whole body
    url = path.format(id=page["id"], slug=page["slug"])
    builder = EnvironBuilder(path=url, method="POST", input_stream=io.BytesIO(sent),
                             content_type=f"multipart/form-data; boundary={BOUNDARY}", content_length=len(body),
                             headers={"Accept": "application/json"})
    response = admin_client.open(builder)
    # 400 Bad Request (the test configuration answers a form's 400 with a redirect back).
    assert response.status_code == (302 if field == "files" else 400)
    assert _every_file(app) == []
    assert _rows(db) == 0


def test_a_stream_that_fails_while_the_file_is_copied_leaves_no_temporary_file(app):
    class Breaks(io.RawIOBase):
        def __init__(self):
            self.calls = 0

        def readable(self):
            return True

        def readinto(self, buffer):
            self.calls += 1
            if self.calls > 2:
                raise OSError("connection reset by peer")
            buffer[: 1024] = b"x" * 1024
            return 1024

    upload = FileStorage(io.BufferedReader(Breaks(), 1024), filename="half.bin")
    with pytest.raises(OSError):
        in_app(app, lambda: storage.save(upload, "attachments", allowed=None, max_bytes=10 * 1024 * 1024))
    assert _every_file(app) == []


def test_a_half_written_file_from_a_killed_worker_is_swept_after_a_day(app):
    folder = app.config["BW"].folders.attachments
    os.makedirs(folder, exist_ok=True)
    old, fresh = os.path.join(folder, ".upload-old"), os.path.join(folder, ".upload-fresh")
    for path in (old, fresh):
        with open(path, "wb") as out:
            out.write(b"partial")
    day_ago = time.time() - storage.TEMP_MAX_AGE_SECONDS - 60
    os.utime(old, (day_ago, day_ago))
    assert in_app(app, storage.sweep_stale_temp_files) == 1
    assert not os.path.exists(old) and os.path.exists(fresh), "an upload still being written stays"


def test_a_progress_bar_upload_gets_its_redirect_as_json_and_the_message_still_shows(app, admin_client, db):
    page = make_page(app, "Progress")
    response = admin_client.post(f"/page/{page['slug']}/attachments",
                                 data={"files": (io.BytesIO(b"hello"), "notes.txt")},
                                 content_type="multipart/form-data", headers={"X-BW-Upload": "1"})
    assert response.status_code == 200
    assert response.get_json() == {"redirect": f"/page/{page['slug']}#attachments"}
    assert _rows(db) == 1
    assert "Attached 1 file." in admin_client.get(f"/page/{page['slug']}").get_data(as_text=True)
    plain = admin_client.post(f"/page/{page['slug']}/attachments", data={"files": (io.BytesIO(b"hi"), "b.txt")},
                              content_type="multipart/form-data")
    assert plain.status_code == 302, "a plain form submit is redirected as before"


def test_a_page_without_files_shows_no_upload_form_the_editor_has_it(app, admin_client):
    page = make_page(app, "No files")
    view = admin_client.get(f"/page/{page['slug']}").get_data(as_text=True)
    assert 'id="attachments"' not in view and "Add files" not in view
    editor = admin_client.get(f"/page/{page['slug']}/edit").get_data(as_text=True)
    assert 'id="editor-attachments"' in editor and "data-attachments-input" in editor
    assert f"/page/{page['slug']}?attach=1#attachments" in editor, "the no-script way still exists"
    fallback = admin_client.get(f"/page/{page['slug']}?attach=1").get_data(as_text=True)
    assert 'id="attachments-files"' in fallback


def test_a_new_page_offers_to_come_back_to_its_editor_for_files(app, admin_client):
    form = admin_client.get("/create-page").get_data(as_text=True)
    assert 'name="then" value="edit#editor-attachments"' in form
    response = admin_client.post("/create-page", data={"title": "With files", "content": "x",
                                                       "then": "edit#editor-attachments"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/page/with-files/edit#editor-attachments")
    odd = admin_client.post("/create-page", data={"title": "Odd anchor", "content": "x",
                                                  "then": 'edit#"><script>'})
    assert odd.headers["Location"].endswith("/page/odd-anchor/edit")
    other = admin_client.post("/create-page", data={"title": "Elsewhere", "content": "x", "then": "https://x.test"})
    assert other.headers["Location"].endswith("/page/elsewhere")


def test_the_editor_knows_what_the_server_accepts(app, admin_client, db):
    db.execute("UPDATE site_settings SET upload_mode = 'whitelist', upload_whitelist = 'pdf, PNG,txt', "
               "upload_max_size_mb = 2")
    rules = in_app(app, lambda: storage.client_rules(None, storage.max_upload_bytes(50 * 1024 * 1024)))
    assert rules["allow"] == ["pdf", "png", "txt"] and rules["maxBytes"] == 2 * 1024 * 1024
    from bananawiki.wiki.features.pages import uploads

    images = in_app(app, uploads.client_rules)
    assert images["allow"] == ["png"], "only the pictures the site's list also allows"
    db.execute("UPDATE site_settings SET upload_mode = 'blacklist', upload_blacklist = 'exe'")
    rules = in_app(app, lambda: storage.client_rules(None, 0))
    assert "allow" not in rules and "exe" in rules["deny"] and "maxBytes" not in rules
    page = make_page(app, "Rules")
    editor = admin_client.get(f"/page/{page['slug']}/edit").get_data(as_text=True)
    assert "data-upload-rules='{&#34;deny&#34;: [&#34;exe&#34;" in editor or '"deny": ["exe"' in editor
