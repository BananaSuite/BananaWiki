"""Custom pages made with the visual page builder."""

from __future__ import annotations

import json

import pytest

PNG = "/static/uploads/" + "b" * 32 + ".png"
DOC = {"version": 2, "blocks": [
    {"type": "hero", "title": "Welcome <friends>", "text": "Hello", "layout": {"align": "center"}},
    {"type": "text", "format": "markdown", "text": "Some **bold** <script>alert(1)</script>"},
    {"type": "button", "label": "Docs", "url": "/page/home"},
]}


@pytest.fixture
def enabled(db):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    db.execute("UPDATE site_settings SET page_builder_enabled = 1")


def form(**fields):
    data = {"path": "/landing", "title": "Landing", "content_type": "builder", "is_published": "1"}
    data.update(fields)
    return data


def create(client, **fields):
    return client.post("/admin/custom-pages/create", data=form(**fields), content_type="multipart/form-data")


def row(db, path="/landing"):
    return db.one("SELECT * FROM custom_pages WHERE path = ?", (path,))


def editor_state(client, page_id):
    html = client.get(f"/admin/custom-pages/{page_id}/builder").get_data(as_text=True)
    token = html.split('data-base-token="', 1)[1].split('"', 1)[0]
    initial = json.loads(html.split('id="builder-initial-document">', 1)[1].split("</script>", 1)[0])
    return token, initial, html


def save(client, page_id, token, document=DOC, **extra):
    body = {"document": document, "base_token": token, **extra}
    return client.post(f"/api/custom-pages/{page_id}/builder/save", data=json.dumps(body),
                       content_type="application/json")


def test_create_builder_page_and_serve_it_with_the_builder_renderer(enabled, admin_client, app, db):
    response = create(admin_client)
    page = row(db)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/admin/custom-pages/{page['id']}/builder")
    # Stored as a Markdown wiki page plus its document, so older releases still show it.
    assert page["content_type"] == "wiki_page" and json.loads(page["builder_json"]) == {"version": 2, "blocks": []}
    token, initial, html = editor_state(admin_client, page["id"])
    assert initial == {"version": 2, "blocks": []}
    assert f'data-publish-url="/api/custom-pages/{page["id"]}/builder/save"' in html
    assert "data-draft-url" not in html  # custom pages have no drafts

    assert save(admin_client, page["id"], token, title="Hello").get_json()["redirect"] == "/landing"
    stored = row(db)
    assert stored["title"] == "Hello" and stored["content"].startswith("## Welcome <friends>")

    visitor = app.test_client()
    served = visitor.get("/landing").get_data(as_text=True)
    assert '<section class="builder-page">' in served
    assert "Welcome &lt;friends&gt;" in served and "<script>alert" not in served
    assert "page_builder.css" in served


def test_save_refuses_stale_editors_invalid_documents_and_long_titles(enabled, admin_client, db):
    create(admin_client)
    page = row(db)
    token, _initial, _html = editor_state(admin_client, page["id"])
    unsafe = {"version": 2, "blocks": [{"type": "button", "label": "x", "url": "javascript:alert(1)"}]}
    assert save(admin_client, page["id"], token, unsafe).status_code == 400
    assert save(admin_client, page["id"], token, title="x" * 201).status_code == 400
    incomplete = {"version": 2, "blocks": [{"type": "heading", "level": 2, "text": ""}]}
    assert save(admin_client, page["id"], token, incomplete).status_code == 400
    # Someone saves the settings form meanwhile: the old editor may no longer save.
    admin_client.post(f"/admin/custom-pages/{page['id']}/edit", data=form(title="Changed"),
                      content_type="multipart/form-data")
    assert save(admin_client, page["id"], token).status_code == 409
    assert save(admin_client, page["id"], "").status_code == 409
    fresh_token, _initial, _html = editor_state(admin_client, page["id"])
    assert save(admin_client, page["id"], fresh_token).status_code == 200


def test_only_custom_page_managers_use_the_custom_builder(enabled, app, admin_client, db, make_user, login):
    create(admin_client)
    page = row(db)
    token, _initial, _html = editor_state(admin_client, page["id"])
    editor = app.test_client()
    login(editor, make_user("editor1", role="editor"))
    db.execute("UPDATE site_settings SET page_builder_access = 'editor'")
    assert editor.get(f"/admin/custom-pages/{page['id']}/builder").status_code == 403
    assert save(editor, page["id"], token).status_code == 403
    assert editor.post(f"/api/custom-pages/{page['id']}/builder/preview", data=json.dumps({"document": DOC}),
                       content_type="application/json").status_code == 403
    assert editor.post(f"/api/custom-pages/{page['id']}/builder/image").status_code == 403
    assert row(db)["builder_json"] == '{"version":2,"blocks":[]}'


def test_builder_endpoints_only_for_builder_pages(enabled, admin_client, db):
    admin_client.post("/admin/custom-pages/create", data=form(path="/plain", content_type="wiki_page", content="Hi"),
                      content_type="multipart/form-data")
    plain = row(db, "/plain")
    assert admin_client.get(f"/admin/custom-pages/{plain['id']}/builder").status_code == 404
    assert save(admin_client, plain["id"], "x").status_code == 404
    assert admin_client.get("/admin/custom-pages/9999/builder").status_code == 404


def test_preview_renders_for_the_manager(enabled, admin_client, db):
    create(admin_client)
    page = row(db)
    response = admin_client.post(f"/api/custom-pages/{page['id']}/builder/preview",
                                 data=json.dumps({"document": DOC}), content_type="application/json")
    assert response.status_code == 200
    assert "Welcome &lt;friends&gt;" in response.get_json()["html"]


def test_converting_a_markdown_page_keeps_its_text_and_back(enabled, admin_client, db):
    admin_client.post("/admin/custom-pages/create", data=form(content_type="wiki_page", content="# Old\n\nBody"),
                      content_type="multipart/form-data")
    page = row(db)
    admin_client.post(f"/admin/custom-pages/{page['id']}/edit", data=form(), content_type="multipart/form-data")
    converted = row(db)
    assert json.loads(converted["builder_json"])["blocks"] == [{"type": "text", "format": "markdown", "text": "# Old\n\nBody"}]
    edit = admin_client.get(f"/admin/custom-pages/{page['id']}/edit").get_data(as_text=True)
    assert '<option value="builder" data-help=' in edit and "selected>" in edit.split('value="builder"', 1)[1][:200]
    # Choosing another type ends the builder page; its Markdown stays as the content.
    admin_client.post(f"/admin/custom-pages/{page['id']}/edit",
                      data=form(content_type="wiki_page", content=converted["content"]),
                      content_type="multipart/form-data")
    back = row(db)
    assert back["builder_json"] == "" and back["content"] == "# Old\n\nBody"


def test_switched_off_builder_shows_the_markdown_twin(enabled, admin_client, app, db):
    create(admin_client)
    page = row(db)
    token, _initial, _html = editor_state(admin_client, page["id"])
    save(admin_client, page["id"], token)
    db.execute("UPDATE site_settings SET page_builder_enabled = 0")
    served = app.test_client().get("/landing").get_data(as_text=True)
    assert '<section class="builder-page">' not in served
    assert "Welcome" in served and "<friends>" not in served  # the Markdown twin, sanitised
    assert admin_client.get(f"/admin/custom-pages/{page['id']}/builder").status_code == 404
    # New builder pages need the builder.
    response = create(admin_client, path="/other")
    assert response.status_code == 400 and row(db, "/other") is None
    edit = admin_client.get(f"/admin/custom-pages/{page['id']}/edit").get_data(as_text=True)
    assert 'value="builder"' in edit  # an existing builder page keeps its type in the form


def test_markdown_changed_elsewhere_wins_over_the_document(enabled, admin_client, app, db):
    create(admin_client)
    page = row(db)
    token, _initial, _html = editor_state(admin_client, page["id"])
    save(admin_client, page["id"], token)
    db.execute("UPDATE custom_pages SET content = 'Edited by an older release' WHERE id = ?", (page["id"],))
    served = app.test_client().get("/landing").get_data(as_text=True)
    assert "Edited by an older release" in served and '<section class="builder-page">' not in served
    _token, initial, _html = editor_state(admin_client, page["id"])
    assert initial["blocks"] == [{"type": "text", "format": "markdown", "text": "Edited by an older release"}]


def test_existing_custom_page_types_are_untouched(enabled, admin_client, app, db):
    admin_client.post("/admin/custom-pages/create", data=form(path="/md", content_type="wiki_page", content="**Hi**"),
                      content_type="multipart/form-data")
    page = row(db, "/md")
    assert page["builder_json"] == ""
    assert "<strong>Hi</strong>" in app.test_client().get("/md").get_data(as_text=True)
    listing = admin_client.get("/admin/custom-pages").get_data(as_text=True)
    assert f"custom-pages/{page['id']}/builder" not in listing


def test_manager_uploads_images_for_the_custom_builder(enabled, admin_client, db):
    import io

    from PIL import Image

    create(admin_client)
    page = row(db)
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(buffer, format="PNG")
    buffer.seek(0)
    response = admin_client.post(f"/api/custom-pages/{page['id']}/builder/image",
                                 data={"file": (buffer, "a.png")}, content_type="multipart/form-data")
    assert response.status_code == 200 and response.get_json()["url"].startswith("/static/uploads/")
    bad = admin_client.post(f"/api/custom-pages/{page['id']}/builder/image",
                            data={"file": (io.BytesIO(b"<svg/>"), "a.svg")}, content_type="multipart/form-data")
    assert bad.status_code == 400 and bad.get_json()["ok"] is False
