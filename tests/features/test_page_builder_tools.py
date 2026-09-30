"""Page builder tools: the embed picker, live embed previews, clipboard blocks and saved sections."""

from __future__ import annotations

import json

import pytest

from bananawiki.wiki.db import connection_scope


@pytest.fixture
def builder_on(db):
    db.execute("UPDATE site_settings SET page_builder_enabled = 1, page_builder_access = 'editor'")


@pytest.fixture
def page(app, db):
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        return service.create("Landing", "Text", author_id=None)


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


@pytest.fixture
def canvas(app, admin):
    from bananawiki.wiki.features.canvas import service

    return run(app, lambda: service.create("Roadmap diagram", "Where we go", admin["id"]))


@pytest.fixture
def board(admin_client):
    response = admin_client.post("/kanban/create", data={"title": "Release board", "description": "Tasks"})
    assert response.status_code == 302
    return int(response.headers["Location"].rstrip("/").rsplit("/", 1)[1])


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


def editor_client(app, make_user, login, name="editor1"):
    other = app.test_client()
    login(other, make_user(name, role="editor"))
    return other


# ── Embed picker ──────────────────────────────────────────────────────────────


def test_embeddables_list_what_the_editor_can_open(builder_on, admin_client, canvas, board):
    items = admin_client.get("/api/page-builder/embeddables").get_json()["items"]
    assert {"kind": "canvas", "ref": canvas["slug"]} in [{"kind": i["kind"], "ref": i["ref"]} for i in items]
    assert {"kind": "kanban", "ref": str(board)} in [{"kind": i["kind"], "ref": i["ref"]} for i in items]
    found = admin_client.get("/api/page-builder/embeddables?q=release").get_json()["items"]
    assert [(i["kind"], i["title"]) for i in found] == [("kanban", "Release board")]
    assert found[0]["url"] == f"/kanban/{board}"
    only_canvas = admin_client.get("/api/page-builder/embeddables?kind=canvas").get_json()["items"]
    assert {i["kind"] for i in only_canvas} == {"canvas"}
    assert admin_client.get("/api/page-builder/embeddables?kind=iframe").status_code == 400


def test_embeddables_respect_each_features_access_rules(builder_on, app, canvas, board, make_user, login):
    # Canvas and Kanban default to administrator-only access: an editor sees neither.
    editor = editor_client(app, make_user, login)
    assert editor.get("/api/page-builder/embeddables").get_json()["items"] == []


def test_embeddables_skip_switched_off_features(builder_on, admin_client, db, canvas, board):
    db.execute("UPDATE plugins SET enabled = 0 WHERE id = 'kanban'")
    items = admin_client.get("/api/page-builder/embeddables").get_json()["items"]
    assert items and {i["kind"] for i in items} == {"canvas"}


def test_embeddables_need_builder_access(builder_on, app, db, client, make_user, login):
    assert client.get("/api/page-builder/embeddables").status_code in (302, 401, 403)
    login(client, make_user("reader1"))
    assert client.get("/api/page-builder/embeddables").status_code == 403
    db.execute("UPDATE site_settings SET page_builder_enabled = 0")
    assert editor_client(app, make_user, login, "editor2").get("/api/page-builder/embeddables").status_code == 404


def test_embeddables_are_rate_limited(builder_on, admin_client, monkeypatch):
    from bananawiki.wiki.features.page_builder import routes

    monkeypatch.setattr(routes, "LOOKUPS_PER_MINUTE", 2)
    assert admin_client.get("/api/page-builder/embeddables").status_code == 200
    assert admin_client.get("/api/page-builder/embeddables").status_code == 200
    assert admin_client.get("/api/page-builder/embeddables").status_code == 429


# ── Live embed previews ───────────────────────────────────────────────────────


def test_embed_frame_shows_the_published_placeholder_with_embed_scripts(builder_on, admin_client, canvas):
    response = admin_client.get(f"/page-builder/embed-frame?kind=canvas&ref={canvas['slug']}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'class="bw-embed bw-embed-canvas" data-embed-type="canvas" data-embed-ref="{canvas["slug"]}"' in html
    assert "canvas-embed.js" in html  # the page.scripts slot, as on a published page
    assert "<script>" not in html  # CSP: no inline scripts
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]


def test_embed_frame_refuses_bad_input_and_outsiders(builder_on, app, db, admin_client, client, make_user, login):
    assert admin_client.get("/page-builder/embed-frame?kind=iframe&ref=x").status_code == 404
    assert admin_client.get('/page-builder/embed-frame?kind=canvas&ref=x"><b').status_code == 404
    db.execute("UPDATE plugins SET enabled = 0 WHERE id = 'canvas'")
    assert admin_client.get("/page-builder/embed-frame?kind=canvas&ref=abc").status_code == 404
    login(client, make_user("reader2"))
    assert client.get("/page-builder/embed-frame?kind=kanban&ref=1").status_code == 403


# ── Clipboard ─────────────────────────────────────────────────────────────────


def test_clipboard_blocks_are_validated_by_the_server(builder_on, admin_client):
    body = {"document": {"version": 2, "blocks": [
        {"type": "heading", "level": 2, "text": "Copied", "onclick": "alert(1)"},
        {"type": "text", "text": ""}]}}
    result = post(admin_client, "/api/page-builder/clipboard", body).get_json()
    assert result["blocks"] == [{"type": "heading", "level": 2, "text": "Copied"},
                                {"type": "text", "format": "markdown", "text": ""}]
    bad = {"document": {"version": 2, "blocks": [{"type": "button", "label": "x", "url": "javascript:alert(1)"}]}}
    assert post(admin_client, "/api/page-builder/clipboard", bad).status_code == 400
    assert post(admin_client, "/api/page-builder/clipboard", {"document": {"version": 2, "blocks": []}}).status_code == 400
    assert post(admin_client, "/api/page-builder/clipboard", {"document": "nope"}).status_code == 400


def test_clipboard_needs_builder_access(builder_on, client, make_user, login):
    login(client, make_user("reader3"))
    body = {"document": {"version": 2, "blocks": [{"type": "divider"}]}}
    assert post(client, "/api/page-builder/clipboard", body).status_code == 403


# ── Saved sections ────────────────────────────────────────────────────────────


SECTION = {"version": 2, "blocks": [{"type": "callout", "title": "Note", "text": "Reusable"}, {"type": "divider"}]}


def test_admins_save_sections_everyone_with_the_builder_sees_them(builder_on, app, admin_client, page, make_user, login):
    response = post(admin_client, "/api/page-builder/sections", {"name": "  Contact   box ", "document": SECTION})
    assert response.status_code == 200
    section = response.get_json()["section"]
    assert section["name"] == "Contact box"
    editor = editor_client(app, make_user, login)
    html = editor.get(f"/page/{page['slug']}/builder").get_data(as_text=True)
    options = json.loads(html.split('id="builder-options">', 1)[1].split("</script>", 1)[0])
    assert [s["name"] for s in options["sections"]] == ["Contact box"]
    assert options["sections"][0]["document"]["blocks"][0]["title"] == "Note"
    assert options["manage_sections"] is False
    assert 'id="builder-section-save"' not in html
    # Editors may neither create nor delete sections.
    assert post(editor, "/api/page-builder/sections", {"name": "Mine", "document": SECTION}).status_code == 403
    assert editor.delete(f"/api/page-builder/sections/{section['id']}").status_code == 403
    assert admin_client.delete(f"/api/page-builder/sections/{section['id']}").status_code == 200
    assert admin_client.delete(f"/api/page-builder/sections/{section['id']}").status_code == 404


def test_section_validation(builder_on, admin_client, db, monkeypatch):
    from bananawiki.wiki.features.page_builder import service

    url = "/api/page-builder/sections"
    assert post(admin_client, url, {"name": "", "document": SECTION}).status_code == 400
    assert post(admin_client, url, {"name": "x" * 81, "document": SECTION}).status_code == 400
    assert post(admin_client, url, {"name": "Empty", "document": {"version": 2, "blocks": []}}).status_code == 400
    evil = {"version": 2, "blocks": [{"type": "image", "url": "https://evil.example/x.png"}]}
    assert post(admin_client, url, {"name": "Evil", "document": evil}).status_code == 400
    monkeypatch.setattr(service, "MAX_SECTIONS", 1)
    assert post(admin_client, url, {"name": "One", "document": SECTION}).status_code == 200
    assert post(admin_client, url, {"name": "Two", "document": SECTION}).status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM page_builder_sections") == 1


def test_editor_page_exposes_the_new_endpoints(builder_on, admin_client, page):
    html = admin_client.get(f"/page/{page['slug']}/builder").get_data(as_text=True)
    for attribute in ('data-embeddables-url="/api/page-builder/embeddables"',
                      'data-embed-frame-url="/page-builder/embed-frame"',
                      'data-clipboard-url="/api/page-builder/clipboard"',
                      f'data-draft-url="/api/page/{page["slug"]}/builder/draft"',
                      'data-base-revision="'):
        assert attribute in html
    assert 'id="builder-section-save"' in html
