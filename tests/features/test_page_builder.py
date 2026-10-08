"""Page builder: access rules, validation, rendering, drafts and publishing."""

from __future__ import annotations

import io
import json

import pytest

from bananawiki.wiki.db import connection_scope

DOC = {"version": 1, "blocks": [
    {"type": "heading", "level": 2, "text": "Welcome <b>"},
    {"type": "text", "text": "Line one\n<script>alert(1)</script>"},
    {"type": "button", "label": "Go", "url": "/page/home", "style": "outline"},
    {"type": "youtube", "url": "https://youtu.be/dQw4w9WgXcQ", "caption": "Video"},
    {"type": "list", "ordered": True, "items": ["a", "b"]},
    {"type": "callout", "title": "Note", "text": "Careful", "tone": "warning"},
    {"type": "columns", "columns": ["Left", "Right", "Middle"]},
    {"type": "divider"},
    {"type": "spacer"},
]}


@pytest.fixture
def builder_on(db):
    db.execute("UPDATE site_settings SET page_builder_enabled = 1, page_builder_access = 'editor'")


@pytest.fixture
def page(app, db):
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        return service.create("Landing", "Old **markdown**", author_id=None)


def post(client, url, body, method="post"):
    return getattr(client, method)(url, data=json.dumps(body), content_type="application/json")


def publish(client, page, document=DOC, **extra):
    body = {"document": document, "base_revision": page["revision"], **extra}
    return post(client, f"/api/page/{page['slug']}/builder/publish", body)


def fresh(db, page):
    return db.one("SELECT * FROM pages WHERE id = ?", (page["id"],))


# ── Validation ────────────────────────────────────────────────────────────────


def test_validation_rejects_active_content(app):
    from bananawiki.wiki.features.page_builder import document

    bad_blocks = [
        {"type": "script", "text": "x"},
        {"type": "button", "label": "x", "url": "javascript:alert(1)"},
        {"type": "button", "label": "x", "url": "//evil.example/x"},
        {"type": "button", "label": "x", "url": "/\\evil.example"},
        {"type": "image", "url": "https://tracker.example/pixel.png"},
        {"type": "image", "url": "/static/uploads/../../secret.png"},
        {"type": "youtube", "url": "https://evil.example/watch?v=dQw4w9WgXcQ"},
        {"type": "heading", "level": 7, "text": "x"},
        {"type": "heading", "level": True, "text": "x"},
        {"type": "columns", "columns": ["only one"]},
        {"type": "text", "text": {"nested": "object"}},
        {"type": "list", "items": ["x"] * 101},
        "not an object",
    ]
    for block in bad_blocks:
        with pytest.raises(document.DocumentError):
            document.validate({"version": 1, "blocks": [block]})
    with pytest.raises(document.DocumentError):
        document.validate({"version": 3, "blocks": []})
    with pytest.raises(document.DocumentError):
        document.validate({"version": 1, "blocks": [{"type": "divider"}] * 121})
    with pytest.raises(document.DocumentError):
        document.validate({"version": 1, "blocks": [{"type": "text", "text": "x" * 20_001}]})


def test_validation_drops_unknown_keys_and_relaxes_drafts(app):
    from bananawiki.wiki.features.page_builder import document

    result = document.validate({"version": 1, "blocks": [{"type": "text", "text": "hi", "onclick": "x"}]})
    assert result == {"version": 2, "blocks": [{"type": "text", "format": "plain", "text": "hi"}]}
    unfinished = {"version": 1, "blocks": [{"type": "image", "url": ""}, {"type": "heading", "text": ""}]}
    document.validate(unfinished, complete=False)
    with pytest.raises(document.DocumentError):
        document.validate(unfinished)


def test_render_escapes_everything(app):
    from bananawiki.wiki.features.page_builder import document

    with app.test_request_context():
        html = str(document.render(document.validate(DOC)))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Welcome &lt;b&gt;" in html
    assert "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ" in html
    assert "Line one<br>" in html and 'builder-columns builder-columns--3"' in html


def test_markdown_twin(app):
    from bananawiki.wiki.features.page_builder import document

    text = document.to_markdown(document.validate(DOC))
    assert text.startswith("## Welcome <b>")
    assert "[Go](/page/home)" in text and "1. a\n2. b" in text and "> **Note**" in text
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in text


# ── Access (audit #7: the builder is not a second, weaker edit path) ─────────


def test_disabled_by_default(admin_client, page):
    assert admin_client.get(f"/page/{page['slug']}/builder").status_code == 404
    assert publish(admin_client, page).status_code == 404


def test_host_switch_forbids_builder(app_factory, db, make_user, login):
    app = app_factory(environ={"BW_FORBID_PAGE_BUILDER": "1"})
    with app.app_context(), connection_scope() as session:
        session.execute("UPDATE site_settings SET page_builder_enabled = 1")
        from bananawiki.wiki import registry
        from bananawiki.wiki.features.page_builder import service

        assert registry.is_enabled("page_builder") and not service.is_active()
    client = app.test_client()
    assert client.get("/page/home/builder").status_code in (302, 404)


def test_plain_users_cannot_publish_even_with_user_access(builder_on, db, client, make_user, login, page):
    db.execute("UPDATE site_settings SET page_builder_access = 'user'")
    login(client, make_user("reader1"))
    assert client.get(f"/page/{page['slug']}/builder").status_code in (302, 403)
    response = publish(client, page)
    assert response.status_code == 403
    assert fresh(db, page)["builder_json"] == ""
    assert post(client, f"/api/page/{page['slug']}/builder/draft",
                {"document": DOC, "base_revision": page["revision"]}).status_code == 403


def test_editor_needs_category_write_access(builder_on, app, db, client, make_user, login):
    from bananawiki.wiki.features.pages import categories, service

    with app.test_request_context(), connection_scope():
        allowed = categories.create("Allowed")
        other = categories.create("Other")
        mine = service.create("Mine", "x", category_id=allowed["id"], author_id=None)
        theirs = service.create("Theirs", "x", category_id=other["id"], author_id=None)
    editor = make_user("editor2", role="editor")
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', 1)",
               (editor["id"],))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (editor["id"],))
    db.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'write')",
               (editor["id"], allowed["id"]))
    for key in ("page.edit_all", "page.view_all"):
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (editor["id"], key))
    login(client, editor)
    assert publish(client, theirs).status_code == 403
    assert publish(client, mine).status_code == 200


def test_access_setting_minimum_role(builder_on, db, client, make_user, login, page):
    db.execute("UPDATE site_settings SET page_builder_access = 'admin'")
    login(client, make_user("editor3", role="editor"))
    assert client.get(f"/page/{page['slug']}/builder").status_code == 403
    assert publish(client, page).status_code == 403


def test_edit_blocked_interceptor_honoured(builder_on, admin_client, db, page):
    db.execute("UPDATE pages SET pending_deletion = 1 WHERE id = ?", (page["id"],))
    response = publish(admin_client, page)
    assert response.status_code == 409
    assert fresh(db, page)["builder_json"] == ""


# ── Editor, drafts, publishing ────────────────────────────────────────────────


def test_editor_renders_existing_markdown_as_text_block(builder_on, admin_client, page):
    response = admin_client.get(f"/page/{page['slug']}/builder")
    assert response.status_code == 200
    assert b"Old **markdown**" in response.data


def test_publish_goes_through_pages_service(builder_on, admin_client, db, page):
    response = publish(admin_client, page, title="New title", edit_message="Built it")
    assert response.status_code == 200, response.data
    row = fresh(db, page)
    assert row["title"] == "New title" and row["revision"] == page["revision"] + 1
    assert json.loads(row["builder_json"])["blocks"][0]["text"] == "Welcome <b>"
    assert row["content"].startswith("## Welcome")
    history = db.one("SELECT * FROM page_history WHERE page_id = ? ORDER BY id DESC", (page["id"],))
    assert history["edit_message"] == "Built it" and history["builder_json"] == row["builder_json"]


def test_publish_detects_conflicts(builder_on, admin_client, db, page):
    assert publish(admin_client, page).status_code == 200
    stale = publish(admin_client, page, document={"version": 1, "blocks": [{"type": "text", "text": "late"}]})
    assert stale.status_code == 409


def test_publish_rejects_invalid_documents(builder_on, admin_client, db, page):
    bad = {"version": 1, "blocks": [{"type": "button", "label": "x", "url": "javascript:alert(1)"}]}
    assert publish(admin_client, page, document=bad).status_code == 400
    assert publish(admin_client, page, document="nope").status_code == 400
    response = post(admin_client, f"/api/page/{page['slug']}/builder/publish", {"document": DOC})
    assert response.status_code == 400  # revision required
    assert fresh(db, page)["builder_json"] == ""


def test_public_flag_admin_only(builder_on, admin_client, db, client, make_user, login, page):
    assert publish(admin_client, page, public=True).status_code == 200
    assert fresh(db, page)["builder_public"] == 1
    editor_client = client.application.test_client()
    login(editor_client, make_user("editor4", role="editor"))
    current = fresh(db, page)
    assert publish(editor_client, current, public=True).status_code == 200
    assert fresh(db, page)["builder_public"] == 0


def test_public_flag_refused_by_host(app_factory, make_user, login):
    app = app_factory(environ={"BW_FORBID_PUBLIC_BUILDER_PAGES": "1"})
    with app.app_context(), connection_scope() as session:
        session.execute("UPDATE site_settings SET page_builder_enabled = 1")
        from bananawiki.wiki.features.pages import service

        with app.test_request_context():
            page = service.create("P", "x", author_id=None)
    from bananawiki.wiki import accounts

    with app.test_request_context(), connection_scope():
        admin = accounts.create("admin9", "correct horse battery", role="admin", emit_event=False)
    client = app.test_client()
    login(client, admin)
    assert publish(client, page, public=True).status_code == 200
    with app.app_context(), connection_scope() as session:
        assert session.scalar("SELECT builder_public FROM pages WHERE id = ?", (page["id"],)) == 0


def test_drafts_are_revision_checked_and_cleared_on_publish(builder_on, admin_client, db, page):
    url = f"/api/page/{page['slug']}/builder/draft"
    unfinished = {"version": 1, "blocks": [{"type": "image", "url": ""}]}
    assert post(admin_client, url, {"document": unfinished, "base_revision": page["revision"]}).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM page_builder_drafts") == 1
    editor = admin_client.get(f"/page/{page['slug']}/builder")
    assert b'"type":"image"' in editor.data
    assert publish(admin_client, page).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM page_builder_drafts") == 0
    late = post(admin_client, url, {"document": unfinished, "base_revision": page["revision"]})
    assert late.status_code == 409
    assert db.scalar("SELECT COUNT(*) FROM page_builder_drafts") == 0


def test_discard_draft(builder_on, admin_client, db, page):
    url = f"/api/page/{page['slug']}/builder/draft"
    post(admin_client, url, {"document": DOC, "base_revision": page["revision"]})
    assert admin_client.delete(url).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM page_builder_drafts") == 0


def test_stale_draft_is_flagged(builder_on, admin_client, db, app, page):
    url = f"/api/page/{page['slug']}/builder/draft"
    post(admin_client, url, {"document": DOC, "base_revision": page["revision"]})
    db.execute("UPDATE pages SET revision = revision + 1 WHERE id = ?", (page["id"],))
    response = admin_client.get(f"/page/{page['slug']}/builder")
    assert f'data-base-revision="{page["revision"]}"'.encode() in response.data
    assert b"alert--warning" in response.data


def test_preview_is_sanitised(builder_on, admin_client, page):
    response = post(admin_client, f"/api/page/{page['slug']}/builder/preview", {"document": DOC})
    html = response.get_json()["html"]
    assert "<script>" not in html and "builder-callout--warning" in html


def test_image_upload(builder_on, admin_client, page):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), "blue").save(buffer, format="PNG")
    buffer.seek(0)
    response = admin_client.post(f"/api/page/{page['slug']}/builder/image",
                                 data={"file": (buffer, "a.png")}, content_type="multipart/form-data")
    assert response.status_code == 200
    url = response.get_json()["url"]
    assert url.startswith("/static/uploads/") and url.endswith(".png")
    bad = admin_client.post(f"/api/page/{page['slug']}/builder/image",
                            data={"file": (io.BytesIO(b"<svg/>"), "a.svg")}, content_type="multipart/form-data")
    assert bad.status_code == 400


# ── Rendering on the page (page.render interceptor) ───────────────────────────


def test_page_render_interceptor(builder_on, admin_client, app, db, page):
    publish(admin_client, page)
    from bananawiki.wiki import registry

    with app.test_request_context(), connection_scope():
        row = dict(fresh(db, page))
        html = str(registry.intercept("page.render", page=row))
        assert "builder-page" in html and "&lt;script&gt;" in html
        # Markdown edited elsewhere wins over a stale builder document.
        row["content"] = "Rewritten in Markdown"
        assert registry.intercept("page.render", page=row) is None
    db.execute("UPDATE site_settings SET page_builder_enabled = 0")
    with app.test_request_context(), connection_scope():
        assert registry.intercept("page.render", page=dict(fresh(db, page))) is None


def test_legacy_builder_pages_render_from_json(builder_on, app, db, page):
    legacy_json = json.dumps({"version": 1, "blocks": [{"type": "text", "text": "From 1.4"}]})
    db.execute("UPDATE pages SET builder_json = ?, content = ? WHERE id = ?",
               (legacy_json, '<section class="builder-page">\n\n<div class="builder-text">From 1.4</div>', page["id"]))
    from bananawiki.wiki import registry

    with app.test_request_context(), connection_scope():
        html = str(registry.intercept("page.render", page=dict(fresh(db, page))))
    assert "From 1.4" in html


def test_page_view_shows_builder_button_only_to_builders(builder_on, admin_client, client, make_user, login, page):
    response = admin_client.get(f"/page/{page['slug']}")
    assert f"/page/{page['slug']}/builder".encode() in response.data
    other = client.application.test_client()
    login(other, make_user("reader2"))
    assert f"/page/{page['slug']}/builder".encode() not in other.get(f"/page/{page['slug']}").data


def test_anonymous_never_sees_private_builder_pages(builder_on, admin_client, app, db, page):
    publish(admin_client, page)
    db.execute("UPDATE site_settings SET public_mode = 1")
    anon = app.test_client()
    assert anon.get(f"/page/{page['slug']}").status_code in (302, 403, 404)


def test_settings_page(builder_on, admin_client, db):
    assert admin_client.get("/admin/page-builder").status_code == 200
    admin_client.post("/admin/page-builder", data={"page_builder_access": "user"})
    assert db.scalar("SELECT page_builder_access FROM site_settings") == "user"
    admin_client.post("/admin/page-builder", data={"page_builder_access": "owner"})
    assert db.scalar("SELECT page_builder_access FROM site_settings") == "user"


def test_settings_page_admin_only(builder_on, client, make_user, login):
    login(client, make_user("editor5", role="editor"))
    assert client.get("/admin/page-builder").status_code == 403


# ── Links to a renamed page, mentions of a merged account ─────────────────────

LINKING = {"version": 2, "blocks": [
    {"type": "hero", "title": "Start here", "button_label": "Read the guide", "button_url": "/page/guide"},
    {"type": "text", "format": "markdown", "text": "See [the guide](/page/guide) or [older](/page/guide-archive)."},
    {"type": "cards", "items": [{"title": "Read on", "text": "", "url": "/page/guide"}]},
    {"type": "pages", "source": "selected", "slugs": ["guide", "guide-archive"]},
]}


@pytest.mark.parametrize("builder_off_meanwhile", [False, True])
def test_renaming_a_page_keeps_linking_builder_pages_current(builder_on, admin_client, app, db, page,
                                                            builder_off_meanwhile):
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        service.create("Guide", "The guide", author_id=None)
        service.create("Guide archive", "Older", author_id=None)
    assert publish(admin_client, page, document=LINKING).status_code == 200
    published = fresh(db, page)
    entries = db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],))
    if builder_off_meanwhile:
        db.execute("UPDATE site_settings SET page_builder_enabled = 0")
    response = admin_client.post("/page/guide/rename", data={"new_slug": "guide-v2"})
    assert response.headers["Location"].endswith("/page/guide-v2")
    db.execute("UPDATE site_settings SET page_builder_enabled = 1")

    row = fresh(db, page)
    blocks = json.loads(row["builder_json"])["blocks"]
    assert blocks[0]["button_url"] == "/page/guide-v2" and blocks[2]["items"][0]["url"] == "/page/guide-v2"
    assert blocks[1]["text"] == "See [the guide](/page/guide-v2) or [older](/page/guide-archive)."
    assert blocks[3]["slugs"] == ["guide-v2", "guide-archive"]
    # Document and Markdown twin change together, in one revision.
    assert row["revision"] == published["revision"] + 1
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],)) == entries + 1
    shown = admin_client.get(f"/page/{page['slug']}").data
    assert b"builder-section--hero" in shown and b'<a href="/page/guide-v2">Guide</a>' in shown


def test_renaming_a_page_updates_1_4_builder_pages(builder_on, admin_client, app, db, page):
    from bananawiki.wiki import registry
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        service.create("A", "", author_id=None)
    legacy_json = json.dumps({"version": 1, "blocks": [{"type": "button", "label": "Go", "url": "/page/a"}]})
    compiled = '<section class="builder-page">\n\n<a class="builder-button" href="/page/a">Go</a>'
    db.execute("UPDATE pages SET builder_json = ?, content = ? WHERE id = ?", (legacy_json, compiled, page["id"]))
    admin_client.post("/page/a/rename", data={"new_slug": "a-new"})
    row = fresh(db, page)
    assert row["content"] == compiled.replace("/page/a", "/page/a-new")
    with app.test_request_context(), connection_scope():
        html = str(registry.intercept("page.render", page=dict(row)))
    assert 'href="/page/a-new"' in html and 'href="/page/a"' not in html

def test_a_builder_page_that_cannot_take_the_new_link_stays_whole(builder_on, admin_client, app, db, page):
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        service.create("A", "", author_id=None)
    full_heading = "x" * (300 - len(" /page/a")) + " /page/a"
    document = {"version": 2, "blocks": [{"type": "hero", "title": "Hero"},
                                         {"type": "heading", "level": 2, "text": full_heading}]}
    assert publish(admin_client, page, document=document).status_code == 200
    published = fresh(db, page)
    response = admin_client.post("/page/a/rename", data={"new_slug": "a-longer-address"})
    assert response.headers["Location"].endswith("/page/a-longer-address")
    assert fresh(db, page) == published
    assert b"builder-section--hero" in admin_client.get(f"/page/{page['slug']}").data


def test_merged_mentions_keep_builder_pages_current(builder_on, admin_client, app, db, page):
    from bananawiki.wiki.features.pages import service

    document = {"version": 2, "blocks": [{"type": "hero", "title": "Team"},
                                         {"type": "text", "format": "markdown", "text": "Ask @src, not @srcbot."}]}
    assert publish(admin_client, page, document=document).status_code == 200
    with app.test_request_context(), connection_scope():
        assert service.rewrite_mentions("src", "@target") == 1
    assert json.loads(fresh(db, page)["builder_json"])["blocks"][1]["text"] == "Ask @target, not @srcbot."
    assert b"builder-section--hero" in admin_client.get(f"/page/{page['slug']}").data
