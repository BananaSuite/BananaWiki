"""Canvas: editing, sync, versions, history, page links, import/export and embeds."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from PIL import Image

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import model, service
from bananawiki.wiki.features.pages import service as pages


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


def node(node_id="n1", **fields):
    return {"id": node_id, "type": "text", "x": 10, "y": 20, **fields}


def ops(client, canvas, *items, session="s1"):
    return client.post(f"/canvas/{canvas['slug']}/ops", json={"ops": list(items)},
                       headers={"X-Canvas-Session": session})


def stored(db, canvas):
    return json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (canvas["id"],)))


def test_ops_apply_bump_version_and_log_events(editor, canvas, db):
    response = ops(editor, canvas, {"op": "upsert_node", "node": node(content="**hi** `<b>`")},
                   {"op": "upsert_node", "node": node("n2")},
                   {"op": "upsert_edge", "edge": {"id": "e1", "from": "n1", "to": "n2", "label": "goes"}},
                   {"op": "upsert_edge", "edge": {"id": "e2", "from": "n1", "to": "missing"}})
    body = response.get_json()
    assert response.status_code == 200 and body["version"] == 2 and body["seq"] == 3
    html = body["rendered"]["n1"]["html"]
    assert "<strong>hi</strong>" in html and "&lt;b&gt;" in html and "&amp;lt;" not in html
    doc = stored(db, canvas)
    assert [n["id"] for n in doc["nodes"]] == ["n1", "n2"] and [e["id"] for e in doc["edges"]] == ["e1"]
    # Deleting a node removes its edges.
    ops(editor, canvas, {"op": "delete_node", "id": "n2"})
    assert stored(db, canvas)["edges"] == []


def test_sanitizes_fields_and_urls(editor, canvas, db):
    ops(editor, canvas,
        {"op": "upsert_node", "node": node("a", type="external_link", url="javascript:alert(1)",
                                           preview_html="<script>x</script>", width="99999", color="red")},
        {"op": "upsert_node", "node": node("b", type="image", url="https://example.org/x.png")},
        {"op": "upsert_node", "node": node("c", type="image", url="/static/uploads/../../etc/passwd")},
        {"op": "upsert_node", "node": node("d<script>")},
        {"op": "upsert_node", "node": node("e", type="title", text_size="large", shape="diamond")})
    doc = {n["id"]: n for n in stored(db, canvas)["nodes"]}
    assert "url" not in doc["a"] and "preview_html" not in doc["a"] and "color" not in doc["a"]
    assert doc["a"]["width"] == 4000
    assert doc["b"]["url"] == "https://example.org/x.png" and "url" not in doc["c"]
    assert "d<script>" not in doc
    assert doc["e"]["type"] == "text" and doc["e"]["text_size"] == 16 and doc["e"]["shape"] == "diamond"


def test_limits(editor, canvas):
    too_many = [{"op": "upsert_node", "node": node(f"n{i}")} for i in range(model.MAX_OPS + 1)]
    assert ops(editor, canvas, *too_many).status_code == 400
    assert editor.post(f"/canvas/{canvas['slug']}/ops", json={"ops": "nope"}).status_code == 400
    huge = node(content="x" * 20_000)
    for start in (0, 90):
        batch = [{"op": "upsert_node", "node": dict(huge, id=f"h{i}")} for i in range(start, start + 90)]
        assert ops(editor, canvas, *batch).status_code == 200
    batch = [{"op": "upsert_node", "node": dict(huge, id=f"h{i}")} for i in range(180, 270)]
    response = ops(editor, canvas, *batch)
    assert response.status_code == 400 and "5 MB" in response.get_json()["error"]


def test_sync_excludes_own_session_and_resumes(editor, canvas):
    ops(editor, canvas, {"op": "upsert_node", "node": node()}, session="mine")
    ops(editor, canvas, {"op": "upsert_node", "node": node("n2", content="theirs")}, session="other")
    own = editor.get(f"/canvas/{canvas['slug']}/sync?since=0", headers={"X-Canvas-Session": "mine"}).get_json()
    assert [e["payload"]["node"]["id"] for e in own["events"]] == ["n2"] and own["seq"] == 2
    assert own["rendered"]["n2"]["html"] == "<p>theirs</p>"
    later = editor.get(f"/canvas/{canvas['slug']}/sync?since=2", headers={"X-Canvas-Session": "mine"}).get_json()
    assert later["events"] == [] and not later["reset"]
    assert editor.get(f"/canvas/{canvas['slug']}/sync?since=99").get_json()["reset"] is True


def test_whole_document_save_with_version_check(editor, canvas, db):
    url = f"/canvas/{canvas['slug']}/data"
    data = {"nodes": [node()], "edges": [], "viewport": {"x": 1, "y": 2, "zoom": 1.5}}
    first = editor.post(url, json={"data": data, "expected_version": 1})
    assert first.status_code == 200 and first.get_json()["version"] == 2
    stale = editor.post(url, json={"data": data, "expected_version": 1})
    assert stale.status_code == 409 and stale.get_json()["version"] == 2
    assert editor.post(url, json={"data": {"nodes": []}}).status_code == 400
    loaded = editor.get(url).get_json()
    assert loaded["version"] == 2 and loaded["data"]["viewport"]["zoom"] == 1.5
    assert db.scalar("SELECT op_type FROM canvas__events WHERE layout_id = ? ORDER BY seq DESC LIMIT 1",
                     (canvas["id"],)) == "snapshot"


def test_history_coalesces_and_restores(editor, canvas, db):
    ops(editor, canvas, {"op": "upsert_node", "node": node(content="one")})
    ops(editor, canvas, {"op": "upsert_node", "node": node(content="two")})
    entries = service_history(db, canvas)
    assert [e["edit_message"] for e in entries] == ["created", "edited"]
    assert json.loads(entries[-1]["data"])["nodes"][0]["content"] == "two"
    created = entries[0]["id"]
    response = editor.post(f"/canvas/{canvas['slug']}/revert/{created}")
    assert response.status_code == 302
    assert stored(db, canvas)["nodes"] == []
    assert service_history(db, canvas)[-1]["is_revert"] == 1
    # The op log keeps growing (1.4 deleted it, so open editors lost their place).
    assert db.scalar("SELECT MAX(seq) FROM canvas__events WHERE layout_id = ?", (canvas["id"],)) == 3
    page = editor.get(f"/canvas/{canvas['slug']}/history")
    assert page.status_code == 200 and b"Restored revision" in page.data
    assert editor.get(f"/canvas/{canvas['slug']}/history/{created}").status_code == 200


def service_history(db, canvas):
    return db.all("SELECT * FROM canvas__history WHERE layout_id = ? ORDER BY id", (canvas["id"],))


def test_history_idor_and_admin_only_deletion(app, editor, canvas, owner, db, make_user, login, client):
    other = run(app, lambda: service.create("Other", "", owner["id"]))
    foreign = db.scalar("SELECT id FROM canvas__history WHERE layout_id = ?", (other["id"],))
    assert editor.get(f"/canvas/{canvas['slug']}/history/{foreign}").status_code == 404
    assert editor.post(f"/canvas/{canvas['slug']}/revert/{foreign}").status_code == 404
    own = db.scalar("SELECT id FROM canvas__history WHERE layout_id = ?", (canvas["id"],))
    assert editor.post(f"/canvas/{canvas['slug']}/history/{own}/delete").status_code == 403
    assert editor.post(f"/canvas/{canvas['slug']}/history/clear").status_code == 403
    client.get("/logout")
    login(client, make_user("admin9", role="admin"))
    assert client.post(f"/canvas/{canvas['slug']}/history/{foreign}/delete").status_code == 404
    assert client.post(f"/canvas/{canvas['slug']}/history/{own}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM canvas__history WHERE layout_id = ?", (canvas["id"],)) == 0


def test_history_and_event_pruning(app, canvas, db, owner):
    for _ in range(service.HISTORY_KEEP + 5):
        db.execute("INSERT INTO canvas__history (layout_id, title, data, edit_message, created_at) "
                   "VALUES (?, 't', '{}', 'old', '2020-01-01 00:00:00')", (canvas["id"],))
    for seq in range(1, 11):
        db.execute("INSERT INTO canvas__events (layout_id, seq, op_type, created_at) "
                   "VALUES (?, ?, 'snapshot', '2020-01-01 00:00:00')", (canvas["id"], seq))
    result = run(app, service.prune)
    assert result["history"] == 6
    assert db.scalar("SELECT COUNT(*) FROM canvas__history WHERE layout_id = ?", (canvas["id"],)) == service.HISTORY_KEEP
    assert db.column("SELECT seq FROM canvas__events WHERE layout_id = ?", (canvas["id"],)) == [10]


def test_page_links_follow_rename_and_delete(app, editor, canvas, db, owner):
    page = run(app, lambda: pages.create("Target page", "Some **content** here", author_id=None))
    legacy = {"id": "old", "type": "wiki_page", "page_slug": "target-page", "label": "stale"}
    ops(editor, canvas, {"op": "upsert_node", "node": {"id": "w", "type": "wiki_page", "page_id": page["id"]}},
        {"op": "upsert_node", "node": legacy})
    doc = {n["id"]: n for n in stored(db, canvas)["nodes"]}
    assert doc["w"]["page_id"] == page["id"] and doc["old"]["page_id"] == page["id"]
    assert not {"label", "page_slug"} & (doc["w"].keys() | doc["old"].keys())

    def shown():
        return {n["id"]: n for n in editor.get(f"/canvas/{canvas['slug']}/data").get_json()["data"]["nodes"]}

    assert shown()["w"]["label"] == "Target page"
    run(app, lambda: pages.change_slug(pages.get(page["id"]), "moved-page"))
    assert shown()["w"]["page_slug"] == "moved-page" and shown()["old"]["page_slug"] == "moved-page"

    run(app, lambda: pages.delete(pages.get(page["id"]), actor_id=None))
    nodes = shown()
    assert nodes["w"]["label"] == "" and nodes["w"]["restricted"] is True and "page_id" not in nodes["w"]
    assert {n["page_id"] for n in stored(db, canvas)["nodes"]} == {page["id"]}


def test_page_titles_are_hidden_from_readers_who_cannot_see_the_page(app, canvas, db, owner, make_user, login,
                                                                     client):
    from bananawiki.wiki.features.pages import categories

    secret_cat = run(app, lambda: categories.create("Secret"))
    page = run(app, lambda: pages.create("Classified plan", "x", category_id=secret_cat["id"], author_id=None))
    run(app, lambda: service.apply(canvas, [{"op": "upsert_node", "node": {
        "id": "w", "type": "wiki_page", "page_id": page["id"]}}], user_id=owner["id"], session_id=""))
    reader = make_user("reader1", role="editor")
    for kind in ("read", "write"):
        db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, 1)",
                   (reader["id"], kind))
    db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, 'canvas.view')", (reader["id"],))
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'view')",
               (canvas["id"], reader["id"]))
    login(client, reader)
    shown = client.get(f"/canvas/{canvas['slug']}/data").get_json()
    assert shown["pages"] == {} and b"Classified" not in json.dumps(shown).encode()
    assert shown["data"]["nodes"][0]["restricted"] is True
    client.get("/logout")
    login(client, owner)
    shown = client.get(f"/canvas/{canvas['slug']}/data").get_json()
    assert shown["pages"][str(page["id"])]["title"] == "Classified plan"


def test_render_video_and_code(editor, canvas):
    body = ops(editor, canvas,
               {"op": "upsert_node", "node": node("v", type="video", url="https://youtu.be/dQw4w9WgXcQ")},
               {"op": "upsert_node", "node": node("c", type="code", language="python", content="x = '<a>'")}
               ).get_json()
    assert body["rendered"]["v"]["embed"] == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    assert "codehilite" in body["rendered"]["c"]["html"] and "<a>" not in body["rendered"]["c"]["html"]
    preview = editor.post(f"/canvas/{canvas['slug']}/render", json={"node": {"type": "text", "content": "# T"}})
    assert "<h1" in preview.get_json()["html"]


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_export_and_import_roundtrip(editor, canvas, db, app):
    uploaded = editor.post(f"/canvas/{canvas['slug']}/upload",
                           data={"file": (io.BytesIO(png_bytes()), "pic.png")}, content_type="multipart/form-data")
    url = uploaded.get_json()["url"]
    assert url.startswith("/static/uploads/")
    bad = editor.post(f"/canvas/{canvas['slug']}/upload", data={"file": (io.BytesIO(b"<svg/>"), "x.svg")},
                      content_type="multipart/form-data")
    assert bad.status_code == 400
    ops(editor, canvas, {"op": "upsert_node", "node": node("img", type="image", url=url)})
    assert run(app, service.referenced_upload_names) == {url.rsplit("/", 1)[1]}

    export = editor.get(f"/canvas/{canvas['slug']}/export")
    assert export.mimetype == "application/zip"
    bundle = zipfile.ZipFile(io.BytesIO(export.data))
    assert "canvas.json" in bundle.namelist() and f"assets/{url.rsplit('/', 1)[1]}" in bundle.namelist()
    plain = editor.get(f"/canvas/{canvas['slug']}/export?format=json")
    assert plain.get_json()["title"] == "Board"

    db.execute("UPDATE site_settings SET canvas_write_access = 'editor', canvas_access = 'editor'")
    response = editor.post("/canvas/import", data={"import_file": (io.BytesIO(export.data), "b.canvas.zip")},
                           content_type="multipart/form-data")
    assert response.status_code == 302
    imported = db.one("SELECT * FROM canvas__layouts WHERE id != ? ORDER BY id DESC LIMIT 1", (canvas["id"],))
    new_url = json.loads(imported["data"])["nodes"][0]["url"]
    assert new_url.startswith("/static/uploads/") and new_url != url

    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../canvas.json", "{}")
    editor.post("/canvas/import", data={"import_file": (io.BytesIO(evil.getvalue()), "e.zip")},
                content_type="multipart/form-data")
    editor.post("/canvas/import", data={"import_file": (io.BytesIO(b"not json"), "x.json")},
                content_type="multipart/form-data")
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 2


def test_export_requires_edit(app, canvas, make_user, login, client, db):
    viewer = make_user("viewer1", role="editor")
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'view')",
               (canvas["id"], viewer["id"]))
    login(client, viewer)
    assert client.get(f"/canvas/{canvas['slug']}").status_code == 200
    assert client.get(f"/canvas/{canvas['slug']}/export").status_code == 403
    assert client.post(f"/canvas/{canvas['slug']}/upload").status_code == 403
    assert client.get(f"/canvas/{canvas['slug']}/pages?q=a").status_code == 403


def test_page_search_respects_visibility(app, editor, canvas):
    run(app, lambda: pages.create("Findable topic", "", author_id=None))
    found = editor.get(f"/canvas/{canvas['slug']}/pages?q=Findable").get_json()["pages"]
    assert [p["title"] for p in found] == ["Findable topic"]


def test_pages_render(editor, canvas):
    for path in ("", "/settings", "/history"):
        response = editor.get(f"/canvas/{canvas['slug']}{path}")
        assert response.status_code == 200, path
        assert b"<script>" not in response.data.replace(b'<script type="application/json"', b"")
    view = editor.get(f"/canvas/{canvas['slug']}").data
    assert b"canvas-editor.js" in view and b'data-can-edit="true"' in view


def test_embed_in_page_markup_and_endpoint(app, editor, canvas):
    from bananawiki.wiki import markdown

    html = markdown.render(f'[[canvas slug="{canvas["slug"]}"]]')
    assert 'class="bw-embed bw-embed-canvas"' in html and f'data-embed-ref="{canvas["slug"]}"' in html
    ops(editor, canvas, {"op": "upsert_node", "node": node(content="hello")})
    data = editor.get(f"/api/embed/canvas/{canvas['slug']}").get_json()
    assert data["title"] == "Board" and data["rendered"]["n1"]["html"] == "<p>hello</p>"
    assert editor.get("/api/embed/canvas/nope").status_code == 404
    assert editor.get(f"/api/embed/canvas/{canvas['slug']}/sync?since=0").status_code == 200
