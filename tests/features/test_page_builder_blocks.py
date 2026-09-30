"""Page builder document version 2: new blocks, layout options, upgrades, checks and starters."""

from __future__ import annotations

import json

import pytest

from bananawiki.wiki.db import connection_scope

PNG = "/static/uploads/" + "a" * 32 + ".png"

V2 = {"version": 2, "blocks": [
    {"type": "hero", "title": "Hello <world>", "text": "Intro", "button_label": "Start", "button_url": "/page/home",
     "image": PNG, "image_alt": "A banana", "layout": {"align": "center", "background": "primary"}},
    {"type": "text", "format": "markdown", "text": "Some **bold** text <script>alert(1)</script>"},
    {"type": "cards", "columns": 2, "items": [
        {"title": "One", "text": "First <b>", "url": "https://example.org/x"},
        {"title": "Two", "text": "", "url": "", "image": PNG, "image_alt": "Picture"}]},
    {"type": "gallery", "columns": 4, "images": [{"url": PNG, "alt": "Alt one", "caption": "Cap"}]},
    {"type": "quote", "text": "Stay hungry", "cite": "Someone"},
    {"type": "faq", "items": [{"question": "Why?", "answer": "Because *reasons* <img src=x onerror=alert(1)>"}]},
    {"type": "table", "header": True, "caption": "Prices", "rows": [["Item", "Price"], ["Pipe | char", "<i>3</i>"]]},
    {"type": "code", "language": "python", "code": "print('<hi>')\n```"},
    {"type": "columns", "format": "markdown", "columns": ["**A**", "B", "C", "D"]},
    {"type": "image", "url": PNG, "alt": "", "decorative": True, "caption": "", "size": "small"},
    {"type": "callout", "tone": "danger", "title": "Careful", "text": "Hot"},
    {"type": "spacer", "layout": {"spacing": "spacious"}},
]}


@pytest.fixture
def builder_on(db):
    db.execute("UPDATE site_settings SET page_builder_enabled = 1, page_builder_access = 'editor'")


@pytest.fixture
def page(app, db):
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        return service.create("Landing", "Old **markdown**", author_id=None)


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


def publish(client, page, document):
    return post(client, f"/api/page/{page['slug']}/builder/publish",
                {"document": document, "base_revision": page["revision"]})


def fresh(db, page):
    return db.one("SELECT * FROM pages WHERE id = ?", (page["id"],))


# ── Schema ────────────────────────────────────────────────────────────────────


def test_version_1_documents_are_upgraded_with_the_same_markdown(app):
    from bananawiki.wiki.features.page_builder import document

    v1 = {"version": 1, "blocks": [
        {"type": "heading", "level": 1, "text": "Title"},
        {"type": "text", "text": "Plain *text*"},
        {"type": "image", "url": PNG, "alt": "", "caption": "Cap"},
        {"type": "columns", "columns": ["L", "R"]},
        {"type": "callout", "title": "T", "text": "a\nb", "tone": "bogus"},
        {"type": "divider"},
    ]}
    upgraded = document.load(json.dumps(v1))
    assert upgraded["version"] == 2
    assert upgraded["blocks"][1] == {"type": "text", "format": "plain", "text": "Plain *text*"}
    assert upgraded["blocks"][3]["format"] == "plain"
    assert upgraded["blocks"][4]["tone"] == "info"
    assert document.to_markdown(upgraded) == (
        "# Title\n\nPlain *text*\n\n![](" + PNG + ")\n*Cap*\n\nL\n\nR\n\n> **T**\n> a\n> b\n\n---")
    with app.test_request_context():
        html = str(document.render(upgraded))
    assert "Plain *text*" in html  # plain text stays literal
    # Version 1 knows neither the new blocks nor four columns nor layout options.
    for block in ({"type": "hero", "title": "x"}, {"type": "columns", "columns": ["a", "b", "c", "d"]}):
        with pytest.raises(document.DocumentError):
            document.validate({"version": 1, "blocks": [block]})
    assert document.validate({"version": 1, "blocks": [{"type": "divider", "layout": {"align": "x"}}]})["blocks"] == [
        {"type": "divider"}]


def test_stored_version_1_page_keeps_rendering(builder_on, app, db, page):
    from bananawiki.wiki import registry
    from bananawiki.wiki.features.page_builder import document

    v1 = {"version": 1, "blocks": [{"type": "heading", "level": 2, "text": "Old"}, {"type": "text", "text": "Body"}]}
    content = document.to_markdown(document.validate(v1))
    db.execute("UPDATE pages SET builder_json = ?, content = ? WHERE id = ?", (json.dumps(v1), content, page["id"]))
    with app.test_request_context(), connection_scope():
        html = str(registry.intercept("page.render", page=dict(fresh(db, page))))
    assert '<h2 id="old">Old</h2>' in html and "Body" in html


def test_new_blocks_validate_and_normalise(app):
    from bananawiki.wiki.features.page_builder import document

    result = document.validate(V2)
    assert result["blocks"][0]["layout"] == {"align": "center", "background": "primary"}
    assert result["blocks"][6]["rows"] == [["Item", "Price"], ["Pipe | char", "<i>3</i>"]]
    assert result["blocks"][9]["decorative"] is True
    # Default layout values are not stored.
    plain = document.validate({"version": 2, "blocks": [
        {"type": "divider", "layout": {"align": "left", "background": "none", "spacing": "normal"}}]})
    assert plain["blocks"] == [{"type": "divider"}]
    # Ragged tables are padded; version 2 text defaults to Markdown.
    table = document.validate({"version": 2, "blocks": [{"type": "table", "rows": [["a"], ["b", "c"]]},
                                                        {"type": "text", "text": "x"}]})
    assert table["blocks"][0]["rows"] == [["a", ""], ["b", "c"]]
    assert table["blocks"][1]["format"] == "markdown"


@pytest.mark.parametrize("block", [
    {"type": "text", "layout": {"align": "justify"}},
    {"type": "text", "layout": {"background": "url(https://evil.example)"}},
    {"type": "text", "layout": "center"},
    {"type": "text", "format": "html", "text": "x"},
    {"type": "gallery", "images": [{"url": "https://evil.example/a.png", "alt": "x"}]},
    {"type": "gallery", "images": ["not an object"]},
    {"type": "gallery", "columns": 7, "images": []},
    {"type": "hero", "title": "x", "image": "javascript:alert(1)"},
    {"type": "hero", "title": "x", "button_label": "Go", "button_url": "javascript:alert(1)"},
    {"type": "hero", "title": "x", "button_label": "Go"},
    {"type": "cards", "items": [{"title": "x", "url": "data:text/html,x"}]},
    {"type": "cards", "items": [{"title": "x"}] * 13},
    {"type": "faq", "items": [{"question": "x"}] * 51},
    {"type": "table", "rows": [["x"] * 9]},
    {"type": "table", "rows": [["x"]] * 51},
    {"type": "table", "rows": [[{"cell": 1}]]},
    {"type": "code", "language": "py thon<", "code": "x"},
    {"type": "pages", "source": "selected", "slugs": ["../admin"]},
    {"type": "pages", "source": "category", "category_id": True},
    {"type": "pages", "source": "everything"},
    {"type": "pages", "source": "recent", "limit": 500},
    {"type": "embed", "kind": "canvas", "ref": 'x" onload="alert(1)'},
    {"type": "embed", "kind": "iframe", "ref": "x"},
    {"type": "image", "url": PNG, "alt": ""},
    {"type": "gallery", "images": [{"url": PNG, "alt": ""}]},
    {"type": "hero", "title": "x", "image": PNG, "image_alt": ""},
    {"type": "cards", "items": [{"title": "x", "image": PNG}]},
    {"type": "cards", "items": []},
])
def test_invalid_new_blocks_are_refused(app, block):
    from bananawiki.wiki.features.page_builder import document

    with pytest.raises(document.DocumentError):
        document.validate({"version": 2, "blocks": [block]})


def test_drafts_accept_unfinished_new_blocks(app):
    from bananawiki.wiki.features.page_builder import document

    unfinished = {"version": 2, "blocks": [
        {"type": "hero", "title": "", "button_label": "Go"},
        {"type": "gallery", "images": [{"url": PNG, "alt": ""}]},
        {"type": "cards", "items": [{"title": ""}]},
        {"type": "faq", "items": [{"question": ""}]},
        {"type": "pages", "source": "selected"},
        {"type": "embed"},
        {"type": "code"},
    ]}
    assert len(document.validate(unfinished, complete=False)["blocks"]) == 7
    with pytest.raises(document.DocumentError):
        document.validate(unfinished)


# ── Rendering ─────────────────────────────────────────────────────────────────


def test_new_blocks_render_safely(app):
    from bananawiki.wiki.features.page_builder import document
    from bananawiki.wiki.features.pages import rendering

    with app.test_request_context():
        html = str(document.render(document.validate(V2)))
    assert "<script>" not in html and "onerror" not in html
    assert "<strong>bold</strong>" in html  # Markdown text blocks are formatted
    assert "Hello &lt;world&gt;" in html and 'id="hello-world"' in html
    assert "is-align-center has-bg-primary" in html
    assert "&lt;i&gt;3&lt;/i&gt;" in html and '<th scope="col">Item</th>' in html and "<caption>Prices</caption>" in html
    assert "&lt;b&gt;" in html and 'href="https://example.org/x"' in html
    assert "<summary>Why?</summary>" in html and "<em>reasons</em>" in html
    assert "codehilite" in html and "&lt;hi&gt;" in html
    assert "builder-columns--4" in html and "builder-grid--4" in html
    assert 'alt=""' in html and "builder-image--small" in html
    assert "builder-callout--danger" in html and "is-space-spacious" in html
    assert [entry["id"] for entry in rendering.table_of_contents(html)][:1] == ["hello-world"]


def test_heading_anchors_are_unique(app):
    from bananawiki.wiki.features.page_builder import document

    doc = document.validate({"version": 2, "blocks": [{"type": "heading", "text": "Intro"}] * 3})
    with app.test_request_context():
        html = str(document.render(doc))
    assert 'id="intro"' in html and 'id="intro-2"' in html and 'id="intro-3"' in html


def test_markdown_twin_of_new_blocks(app):
    from bananawiki.wiki.features.page_builder import document

    text = document.to_markdown(document.validate({**V2, "blocks": [*V2["blocks"], {
        "type": "pages", "source": "selected", "slugs": ["home", "guide"]}, {"type": "embed", "kind": "kanban", "ref": "7"}]}))
    assert "## Hello <world>\n\nIntro\n\n[Start](/page/home)" in text
    assert "### [One](https://example.org/x)\n\nFirst <b>" in text
    assert "> Stay hungry\n> — Someone" in text
    assert "**Why?**\n\nBecause" in text
    assert "| Item | Price |\n| --- | --- |\n| Pipe \\| char | <i>3</i> |" in text
    assert "````python\nprint('<hi>')\n```\n````" in text
    assert "- [home](/page/home)\n- [guide](/page/guide)" in text
    assert '[[kanban board="7"]]' in text


def test_empty_blocks_are_not_rendered(app):
    from bananawiki.wiki.features.page_builder import document

    doc = document.validate({"version": 2, "blocks": [{"type": "table", "rows": [["", ""]]},
                                                      {"type": "columns", "columns": ["", ""]}]}, complete=False)
    with app.test_request_context():
        html = str(document.render(doc))
    assert "builder-table" not in html and "builder-columns" not in html


def test_embed_block_renders_placeholder_only_when_feature_is_on(app):
    from bananawiki.wiki import registry
    from bananawiki.wiki.features.page_builder import document

    doc = document.validate({"version": 2, "blocks": [{"type": "embed", "kind": "canvas", "ref": "my-canvas"}]})
    with app.test_request_context(), connection_scope():
        html = str(document.render(doc))
        enabled = registry.is_enabled("canvas")
        checks = [hint["key"] for hint in document.audit(doc)]
    marker = 'class="bw-embed bw-embed-canvas" data-embed-type="canvas" data-embed-ref="my-canvas"'
    assert (marker in html) is enabled
    assert ("page_builder.check.embed_off" in checks) is not enabled


def test_audit_hints(app):
    from bananawiki.wiki.features.page_builder import document

    doc = document.validate({"version": 2, "blocks": [
        {"type": "heading", "level": 1, "text": "Big"},
        {"type": "heading", "level": 3, "text": "Skips"},
        {"type": "image", "url": PNG, "alt": ""},
        {"type": "table", "header": False, "rows": [["a"]]},
        {"type": "text", "text": ""},
        {"type": "image", "url": PNG, "alt": "", "decorative": True},
    ]}, complete=False)
    with app.test_request_context(), connection_scope():
        hints = {(hint["block"], hint["key"].rsplit(".", 1)[1]) for hint in document.audit(doc)}
    assert hints == {(0, "h1"), (1, "heading_skip"), (2, "alt"), (3, "table_header"), (4, "empty")}
    doc = document.validate({"version": 2, "blocks": [{"type": "heading", "level": 3, "text": "Deep"}]})
    with app.test_request_context(), connection_scope():
        assert [hint["key"] for hint in document.audit(doc)] == ["page_builder.check.heading_skip"]


def test_from_markdown_keeps_long_pages_whole(app):
    from bananawiki.wiki.features.page_builder import document

    paragraphs = [f"Paragraph {n} " + "word " * 400 for n in range(30)]
    content = "\n\n".join(paragraphs[:10]) + "\n\n```\n" + "code\n\n" * 3000 + "```\n\n" + "\n\n".join(paragraphs[10:])
    doc = document.from_markdown(content)
    assert len(doc["blocks"]) > 1
    assert all(block["format"] == "markdown" and len(block["text"]) <= document.MAX_TEXT for block in doc["blocks"])
    joined = " ".join(block["text"] for block in doc["blocks"])
    assert joined.split() == content.split()
    assert document.validate(doc)["blocks"] == doc["blocks"]
    assert document.from_markdown("") == document.empty()


# ── Page lists respect the reader's permissions ──────────────────────────────


def _restricted_reader(db, make_user, category_id):
    reader = make_user("limited_reader")
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 1)",
               (reader["id"],))
    db.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'read')",
               (reader["id"], category_id))
    return reader


def test_page_lists_only_show_pages_the_reader_may_open(builder_on, app, db, admin_client, client, make_user, login):
    from bananawiki.wiki.features.pages import categories, service

    with app.test_request_context(), connection_scope():
        open_cat = categories.create("Open")
        secret_cat = categories.create("Secret")
        landing = service.create("Portal", "x", category_id=open_cat["id"], author_id=None)
        service.create("Visible guide", "Visible body", category_id=open_cat["id"], author_id=None)
        service.create("Hidden plans", "Hidden body", category_id=secret_cat["id"], author_id=None)
    doc = {"version": 2, "blocks": [
        {"type": "pages", "source": "selected", "slugs": ["visible-guide", "hidden-plans", "missing-page", "portal"]},
        {"type": "pages", "source": "recent", "limit": 10, "display": "list"},
        {"type": "pages", "source": "category", "category_id": secret_cat["id"]},
    ]}
    assert publish(admin_client, landing, doc).status_code == 200
    as_admin = admin_client.get("/page/portal").get_data(as_text=True)
    assert as_admin.count("Hidden plans") >= 3 and "Visible body" in as_admin
    assert 'href="/page/portal"' not in as_admin.split('class="builder-page"', 1)[1]  # the page itself is left out

    reader = client.application.test_client()
    login(reader, _restricted_reader(db, make_user, open_cat["id"]))
    as_reader = reader.get("/page/portal").get_data(as_text=True)
    assert "Visible guide" in as_reader
    assert "Hidden plans" not in as_reader and "Hidden body" not in as_reader


# ── Editor and API ────────────────────────────────────────────────────────────


def test_publish_and_view_new_blocks(builder_on, admin_client, db, page):
    response = publish(admin_client, page, V2)
    assert response.status_code == 200, response.data
    stored = json.loads(fresh(db, page)["builder_json"])
    assert stored["version"] == 2 and stored["blocks"][0]["type"] == "hero"
    view = admin_client.get(f"/page/{page['slug']}").get_data(as_text=True)
    assert "builder-hero" in view and "<script>alert(1)</script>" not in view


def test_publish_requires_alt_text(builder_on, admin_client, db, page):
    doc = {"version": 2, "blocks": [{"type": "image", "url": PNG, "alt": ""}]}
    response = publish(admin_client, page, doc)
    assert response.status_code == 400
    assert "alternative text" in response.get_json()["error"]
    assert fresh(db, page)["builder_json"] == ""


def test_preview_returns_checks(builder_on, admin_client, page):
    doc = {"version": 2, "blocks": [{"type": "heading", "level": 1, "text": "Big"}]}
    result = post(admin_client, f"/api/page/{page['slug']}/builder/preview", {"document": doc}).get_json()
    assert result["ok"] and '<h1 id="big">' in result["html"]
    assert result["checks"] and result["checks"][0]["block"] == 0 and result["checks"][0]["message"]


def test_preview_and_drafts_need_builder_access(builder_on, client, make_user, login, page):
    login(client, make_user("plain_reader"))
    assert post(client, f"/api/page/{page['slug']}/builder/preview", {"document": V2}).status_code == 403
    assert post(client, f"/api/page/{page['slug']}/builder/draft",
                {"document": V2, "base_revision": page["revision"]}).status_code == 403


def test_preview_is_rate_limited(builder_on, admin_client, app, page):
    from bananawiki.wiki.features.page_builder import routes

    url = f"/api/page/{page['slug']}/builder/preview"
    body = {"document": {"version": 2, "blocks": []}}
    statuses = [post(admin_client, url, body).status_code for _ in range(routes.PREVIEWS_PER_MINUTE + 1)]
    assert statuses[-1] == 429 and set(statuses[:-1]) == {200}


def test_editor_offers_blocks_starters_and_markdown(builder_on, admin_client, page):
    from bananawiki.wiki.features.page_builder import document

    html = admin_client.get(f"/page/{page['slug']}/builder").get_data(as_text=True)
    for kind in document.BLOCK_TYPES:
        assert f'data-block-type="{kind}"' in html
    assert 'data-starter="landing"' in html and 'id="builder-options"' in html
    initial = html.split('id="builder-initial-document">', 1)[1].split("</script>", 1)[0]
    assert json.loads(initial) == {"version": 2, "blocks": [
        {"type": "text", "format": "markdown", "text": "Old **markdown**"}]}


def test_starters_are_complete_documents(app):
    from bananawiki.wiki.features.page_builder import document, starters

    with app.test_request_context(), connection_scope():
        listed = starters.starters()
    assert [entry["id"] for entry in listed] == list(starters.STARTERS)
    for entry in listed:
        assert entry["name"] and not entry["name"].startswith("page_builder.")
        assert document.validate(entry["document"]) == entry["document"]
