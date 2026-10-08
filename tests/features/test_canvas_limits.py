# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Canvas: work, memory and storage stay bounded for large canvases, imports, exports and history."""

from __future__ import annotations

import io
import json
import secrets
import types
import zipfile

import pytest
from werkzeug.datastructures import FileStorage

from bananawiki.wiki import markdown
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import model, present, service, transfer


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


@pytest.fixture
def owner(make_user):
    return make_user("owner1", role="editor")


@pytest.fixture
def canvas(app, owner):
    return run(app, lambda: service.create("Board", "", owner["id"]))


@pytest.fixture
def editor(client, login, owner):
    login(client, owner)
    return client


def text_nodes(count, content=lambda i: f"note **{i}**"):
    token = secrets.token_hex(4)
    return [{"id": f"n{i}", "type": "text", "x": i, "y": 0, "content": f"{content(i)} {token}"} for i in range(count)]


def document(nodes):
    return model.clean_document({"nodes": nodes, "edges": []})


@pytest.fixture
def counted(monkeypatch):
    """Count calls of the Markdown renderer (and of the plain-text and excerpt helpers built on it)."""
    calls = {"render": 0, "excerpt": 0}
    render, excerpt = markdown.render, markdown.excerpt

    def counting_render(*args, **kwargs):
        calls["render"] += 1
        return render(*args, **kwargs)

    def counting_excerpt(*args, **kwargs):
        calls["excerpt"] += 1
        return excerpt(*args, **kwargs)

    monkeypatch.setattr(markdown, "render", counting_render)
    monkeypatch.setattr(markdown, "excerpt", counting_excerpt)
    return calls


# ── Rendering (R-18) ──────────────────────────────────────────────────────────


def test_reading_a_large_canvas_again_renders_nothing(app, counted):
    doc = document(text_nodes(model.MAX_NODES))
    first = run(app, lambda: present.present_document(doc, None))
    assert len(first["rendered"]) == model.MAX_NODES and counted["render"] == model.MAX_NODES
    counted["render"] = 0
    second = run(app, lambda: present.present_document(doc, None))
    assert counted["render"] == 0 and second["rendered"] == first["rendered"]


def test_outline_renders_text_once_and_never_node_html(app, counted):
    doc = document(text_nodes(50))
    items = run(app, lambda: present.outline(doc, None))
    assert counted["render"] == 50 and items[0]["text"].startswith("note 0")
    counted["render"] = 0
    run(app, lambda: present.outline(doc, None))
    assert counted["render"] == 0


def test_page_excerpts_are_reused(app, counted, owner):
    from tests.features.pages_support import make_page

    page = make_page(app, "Linked page", "Some **content** " + secrets.token_hex(4))
    doc = document([{"id": "w", "type": "wiki_page", "page_id": page["id"]}])
    shown = run(app, lambda: present.present_document(doc, owner))
    assert shown["pages"][str(page["id"])]["excerpt"].startswith("Some content")
    run(app, lambda: present.present_document(doc, owner))
    assert counted["excerpt"] == 1


def test_code_without_a_language_is_never_guessed(app, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("guess_lexer called")

    monkeypatch.setattr(markdown, "guess_lexer", refuse)
    doc = document([{"id": "c", "type": "code", "content": "def f():\n    return 1  # " + secrets.token_hex(4)}])
    shown = run(app, lambda: present.present_document(doc, None))
    assert "codehilite" in shown["rendered"]["c"]["html"] and "return 1" in shown["rendered"]["c"]["html"]


def test_rendering_stops_at_the_budget_and_the_rest_is_sent_as_text(app, monkeypatch, editor, canvas):
    monkeypatch.setattr(present, "RENDER_LIMIT", 1000)
    nodes = text_nodes(5, lambda i: "x" * 380)
    editor.post(f"/canvas/{canvas['slug']}/ops", json={"ops": [{"op": "upsert_node", "node": n} for n in nodes]})
    for _ in range(2):  # the same answer whether the renderings are cached or not
        body = editor.get(f"/canvas/{canvas['slug']}/data").get_json()
        assert sorted(body["rendered"]) == ["n0", "n1"]
        assert [n["content"] for n in body["data"]["nodes"]] == [n["content"] for n in nodes]
    outline = run(app, lambda: present.outline(document(nodes), None))
    assert [item["text"].startswith("x") for item in outline] == [True] * 5


def test_a_page_linked_many_times_is_paid_for_once(app, monkeypatch, counted, owner):
    from tests.features.pages_support import make_page

    page = make_page(app, "Linked page", "word " * 60 + secrets.token_hex(4))
    monkeypatch.setattr(present, "RENDER_LIMIT", 1000)
    links = [{"id": f"w{i}", "type": "wiki_page", "x": i, "y": 0, "page_id": page["id"]} for i in range(10)]
    shown = run(app, lambda: present.present_document(document(links + text_nodes(1)), owner))
    assert list(shown["rendered"]) == ["n0"] and shown["pages"][str(page["id"])]["excerpt"].startswith("word")
    assert counted["excerpt"] == 1


def test_the_rendering_cache_counts_bytes_not_characters(monkeypatch):
    monkeypatch.setattr(present, "CACHE_LIMIT", 10_000)
    cache = present._Cache()
    for i in range(10):
        cache.put(("text", bytes([i])), "\U0001f600" * 1000)  # four bytes per character in memory
    assert len(cache._items) == 2


# ── Imports (R-20) ────────────────────────────────────────────────────────────


def upload(data, name="c.canvas.json"):
    return FileStorage(io.BytesIO(data), filename=name)


def zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def refused(app, data, name="c.canvas.zip"):
    with pytest.raises((transfer.CanvasImportError, model.DocumentError)) as error:
        run(app, lambda: transfer.read_import(upload(data, name)))
    return error.value


@pytest.fixture
def cleaned(monkeypatch):
    calls = []
    clean_node, clean_edge = model.clean_node, model.clean_edge
    monkeypatch.setattr(model, "clean_node", lambda raw: calls.append("node") or clean_node(raw))
    monkeypatch.setattr(model, "clean_edge", lambda raw: calls.append("edge") or clean_edge(raw))
    return calls


def test_documents_listing_too_many_nodes_or_edges_are_refused_before_cleaning(app, cleaned, canvas, owner):
    nodes = [{"id": f"n{i}"} for i in range(model.MAX_NODES + 1)]
    edges = [{"id": f"e{i}", "from": "a", "to": "b"} for i in range(model.MAX_EDGES + 1)]
    for data, key in (({"nodes": nodes, "edges": []}, "canvas.error.too_many_nodes"),
                      ({"nodes": [], "edges": edges}, "canvas.error.too_many_edges")):
        assert refused(app, json.dumps({"title": "Big", "data": data}).encode(), "c.canvas.json").key == key
        with pytest.raises(model.DocumentError):
            run(app, lambda data=data: service.save_document(canvas, data, expected_version=None,
                                                             user_id=owner["id"], session_id=""))
        with pytest.raises(model.DocumentError):
            run(app, lambda data=data: service.create("Big", "", owner["id"], data=data))
    assert cleaned == []


def test_zip_listing_more_members_than_an_export_is_refused_before_it_is_read(app, monkeypatch):
    members = {"canvas.json": b'{"title": "x", "data": {"nodes": [], "edges": []}}'}
    members.update({f"assets/{i}.png": b"" for i in range(model.MAX_NODES + 1)})
    data = zip_bytes(members)
    opened = []
    real = zipfile.ZipFile
    monkeypatch.setattr(transfer.zipfile, "ZipFile",
                        lambda *args, **kwargs: opened.append(1) or real(*args, **kwargs))
    assert refused(app, data).key == "canvas.import.too_many_files" and opened == []


def test_zip_whose_end_record_understates_its_members_is_refused(app):
    members = {"canvas.json": b"{}", **{f"assets/{i}.png": b"" for i in range(model.MAX_NODES + 1)}}
    data = bytearray(zip_bytes(members))
    end = data.rfind(b"PK\x05\x06")
    data[end + 8:end + 12] = (1).to_bytes(2, "little") * 2  # members on this disk / in total
    assert refused(app, bytes(data)).key == "canvas.import.too_many_files"


@pytest.mark.parametrize("missing", ["_EndRecData", "_ECD_ENTRIES_TOTAL", "_ECD_SIZE"])
def test_zips_still_import_where_zipfile_lacks_a_name_the_end_record_check_uses(app, monkeypatch, missing):
    without = types.ModuleType("zipfile")  # zipfile as a future Python might have it
    vars(without).update({key: value for key, value in vars(zipfile).items() if key != missing})
    monkeypatch.setattr(transfer, "zipfile", without)
    data = zip_bytes({"canvas.json": json.dumps({"title": "Small", "data": {"nodes": [], "edges": []}})})
    assert run(app, lambda: transfer.read_import(upload(data, "c.canvas.zip")))[0] == "Small"


def test_canvas_json_larger_than_any_export_is_not_read(app):
    big = b'{"title": "x", "description": "' + b"a" * (model.MAX_DOCUMENT_BYTES + 3 * 1024 * 1024) + b'"}'
    for data, name in ((zip_bytes({"canvas.json": big}), "c.canvas.zip"), (big, "c.canvas.json")):
        error = refused(app, data, name)
        assert error.key == "canvas.import.too_large" and error.values == {"limit_mb": 7}


def test_the_largest_canvas_exports_and_imports_again(app, editor, canvas, db, owner):
    nodes = [{"id": f"n{i}", "type": "text", "x": i, "y": i, "content": ""} for i in range(model.MAX_NODES)]
    edges = [{"id": f"e{i}", "from": f"n{i % model.MAX_NODES}", "to": f"n{(i * 7 + 1) % model.MAX_NODES}",
              "label": "x"} for i in range(model.MAX_EDGES)]
    edges = [edge for edge in edges if edge["from"] != edge["to"]]
    base = len(model.serialize(model.clean_document({"nodes": nodes, "edges": edges})).encode())
    for node in nodes:
        node["content"] = "y" * ((model.MAX_DOCUMENT_BYTES - base - 1000) // model.MAX_NODES)
    run(app, lambda: service.save_document(canvas, {"nodes": nodes, "edges": edges}, expected_version=None,
                                           user_id=owner["id"], session_id=""))
    export = editor.get(f"/canvas/{canvas['slug']}/export?format=json").data
    assert model.MAX_DOCUMENT_BYTES < len(export) <= transfer.MAX_JSON_BYTES and b"\n  " in export
    db.execute("UPDATE site_settings SET canvas_write_access = 'editor', canvas_access = 'editor'")
    response = editor.post("/canvas/import", data={"import_file": (io.BytesIO(export), "b.canvas.json")},
                           content_type="multipart/form-data")
    assert response.status_code == 302
    imported = db.scalar("SELECT data FROM canvas__layouts WHERE id != ?", (canvas["id"],))
    assert len(json.loads(imported)["nodes"]) == model.MAX_NODES


def test_an_export_too_large_to_indent_is_written_compactly(app, monkeypatch):
    layout = {"title": "T", "description": "", "version": 1, "slug": "t"}
    doc = document(text_nodes(20))
    indented = json.dumps(transfer.export_payload(layout, doc), ensure_ascii=False, indent=2).encode()
    monkeypatch.setattr(transfer, "MAX_JSON_BYTES", len(indented) - 1)
    text = transfer.export_json(layout, doc)
    assert b"\n" not in text and len(text) <= transfer.MAX_JSON_BYTES
    assert json.loads(text)["data"] == doc


# ── Exports (R-21) ────────────────────────────────────────────────────────────


def png_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def image_name(editor, canvas):
    """The upload-folder name of an image shown on the canvas."""
    url = editor.post(f"/canvas/{canvas['slug']}/upload", data={"file": (io.BytesIO(png_bytes()), "pic.png")},
                      content_type="multipart/form-data").get_json()["url"]
    editor.post(f"/canvas/{canvas['slug']}/ops",
                json={"ops": [{"op": "upsert_node", "node": {"id": "img", "type": "image", "url": url}}]})
    return url.rsplit("/", 1)[1]


def test_export_zip_is_written_to_a_temporary_file_with_images_stored(app, editor, canvas, image_name):
    layout = run(app, lambda: service.get(canvas["id"]))
    doc = run(app, lambda: service.document(canvas["id"]))
    file, mimetype, filename = run(app, lambda: transfer.build_export(layout, doc, plain=False))
    with file:
        assert not isinstance(file, io.BytesIO) and mimetype == "application/zip"
        bundle = zipfile.ZipFile(file)
        assert bundle.getinfo(f"assets/{image_name}").compress_type == zipfile.ZIP_STORED
        assert bundle.getinfo("canvas.json").compress_type == zipfile.ZIP_DEFLATED
    response = editor.get(f"/canvas/{canvas['slug']}/export")
    assert response.status_code == 200 and filename in response.headers["Content-Disposition"]
    assert zipfile.ZipFile(io.BytesIO(response.data)).namelist() == ["canvas.json", f"assets/{image_name}"]
    plain = editor.get(f"/canvas/{canvas['slug']}/export?format=json")
    assert plain.mimetype == "application/json" and plain.get_json()["data"]["nodes"][0]["id"] == "img"


def test_export_leaves_out_files_beyond_the_cap_and_says_why(monkeypatch, editor, canvas, image_name):
    monkeypatch.setattr(transfer, "EXPORT_MAX_ASSET_BYTES", 10)
    bundle = zipfile.ZipFile(io.BytesIO(editor.get(f"/canvas/{canvas['slug']}/export").data))
    assert bundle.namelist() == ["canvas.json", "README.txt"]
    assert "are not included" in bundle.read("README.txt").decode()
    assert json.loads(bundle.read("canvas.json"))["data"]["nodes"][0]["url"].endswith(image_name)


# ── History (R-22) ────────────────────────────────────────────────────────────


def history(db, canvas):
    return db.all("SELECT id, edited_by, edit_message, length(data) AS size FROM canvas__history "
                  "WHERE layout_id = ? ORDER BY id", (canvas["id"],))


def test_alternating_edits_saves_and_title_changes_share_one_entry(editor, canvas, db):
    for round_ in range(4):
        editor.post(f"/canvas/{canvas['slug']}/ops",
                    json={"ops": [{"op": "upsert_node", "node": {"id": "a", "content": f"edit {round_}"}}]})
        editor.post(f"/canvas/{canvas['slug']}/data",
                    json={"data": {"nodes": [{"id": "a", "content": f"save {round_}"}], "edges": []}})
        editor.post(f"/canvas/{canvas['slug']}/edit", data={"title": f"Board {round_}", "description": ""})
    entries = history(db, canvas)
    assert [entry["edit_message"] for entry in entries] == ["created", "edited"]
    latest = db.one("SELECT title, data FROM canvas__history WHERE id = ?", (entries[-1]["id"],))
    assert latest["title"] == "Board 3" and "save 3" in latest["data"]


def test_people_editing_together_share_one_entry(app, canvas, db, owner, make_user):
    friend = make_user("friend1", role="editor")
    for round_ in range(6):
        user = (owner, friend)[round_ % 2]
        run(app, lambda round_=round_, user=user: service.apply(
            canvas, [{"op": "upsert_node", "node": {"id": "a", "content": f"v{round_}"}}],
            user_id=user["id"], session_id=user["id"]))
    entries = history(db, canvas)
    assert [(entry["edit_message"], entry["edited_by"]) for entry in entries] == [
        ("created", owner["id"]), ("edited", owner["id"])]
    # Later, a change by someone else starts a new entry.
    db.execute("UPDATE canvas__history SET created_at = '2000-01-01 00:00:00' WHERE layout_id = ?", (canvas["id"],))
    run(app, lambda: service.apply(canvas, [{"op": "upsert_node", "node": {"id": "a", "content": "later"}}],
                                   user_id=friend["id"], session_id="x"))
    assert history(db, canvas)[-1]["edited_by"] == friend["id"]


def test_history_keeps_only_as_many_snapshots_as_fit_in_the_byte_cap(app, canvas, db, owner, monkeypatch):
    # raising=False: the test also runs against code without the cap, where it must fail.
    monkeypatch.setattr(service, "HISTORY_MAX_BYTES", 50_000, raising=False)
    for round_ in range(8):
        db.execute("UPDATE canvas__history SET created_at = '2000-01-01 00:00:00' WHERE layout_id = ?",
                   (canvas["id"],))
        run(app, lambda round_=round_: service.apply(
            canvas, [{"op": "upsert_node", "node": {"id": "a", "content": str(round_) * 20_000}}],
            user_id=owner["id"], session_id=""))
    entries = history(db, canvas)
    assert len(entries) == 2 and sum(entry["size"] for entry in entries) <= 50_000
    assert '"content":"7777' in db.scalar("SELECT data FROM canvas__history WHERE id = ?", (entries[-1]["id"],))


def test_pruning_applies_the_byte_cap_to_older_histories(app, canvas, db, owner, monkeypatch):
    monkeypatch.setattr(service, "HISTORY_MAX_BYTES", 50_000, raising=False)
    other = run(app, lambda: service.create("Other", "", owner["id"]))
    for layout in (canvas, other):
        for _ in range(5):
            db.execute("INSERT INTO canvas__history (layout_id, title, data, edit_message, created_at) "
                       "VALUES (?, 't', ?, 'old', '2020-01-01 00:00:00')", (layout["id"], "x" * 30_000))
    run(app, service.prune)
    for layout in (canvas, other):
        sizes = [entry["size"] for entry in history(db, layout)]
        assert sizes == [30_000]  # the newest entry stays even alone above the cap
