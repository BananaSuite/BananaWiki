"""Custom pages: administration, serving at arbitrary paths, sandboxing and files."""

from __future__ import annotations

import io
import json

import pytest


@pytest.fixture
def enabled(db):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")


def create(client, **fields):
    data = {"path": "/about", "title": "About", "content_type": "wiki_page", "is_published": "1"}
    data.update(fields)
    response = client.post("/admin/custom-pages/create", data=data, content_type="multipart/form-data")
    return response


def row(db, path):
    return db.one("SELECT * FROM custom_pages WHERE path = ?", (path,))


def insert(db, **values):
    base = {"path": "/p", "title": "T", "content_type": "wiki_page", "is_published": 1}
    base.update(values)
    return db.insert("custom_pages", base)


# ── Feature switch and permissions ────────────────────────────────────────────


def test_disabled_by_default(admin_client, db):
    insert(db, path="/hello", content="hi")
    assert admin_client.get("/admin/custom-pages").status_code == 404
    assert admin_client.get("/hello").status_code == 404


def test_only_admins_manage(enabled, client, make_user, login):
    editor = make_user("editor1", role="editor")
    login(client, editor)
    assert client.get("/admin/custom-pages").status_code == 403
    assert create(client).status_code == 403


def test_permission_is_admin_only():
    from bananawiki.wiki import permissions

    assert "custom_page.manage" not in permissions.assignable("editor")


# ── Creating and validating ───────────────────────────────────────────────────


def test_create_and_serve_wiki_page_anonymously(enabled, admin_client, db, app):
    response = create(admin_client, content="# Hi\n\n<script>alert(1)</script>**bold**")
    assert response.status_code == 302
    anon = app.test_client()
    page = anon.get("/about")
    assert page.status_code == 200
    assert b"<strong>bold</strong>" in page.data and b"<script>alert" not in page.data


@pytest.mark.parametrize("path", ["/", "/admin", "/admin/x", "/static/a", "/api/v1", "/page/x", "/login",
                                  "/_cpf/1", "/LOGIN", "/robots.txt", "/_probe/anything"])
def test_reserved_paths_refused(enabled, admin_client, db, path):
    response = create(admin_client, path=path)
    assert response.status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM custom_pages") == 0


def test_path_normalised_and_unique(enabled, admin_client, db):
    assert create(admin_client, path=" docs//imprint/ ").status_code == 302
    assert row(db, "/docs/imprint")
    assert create(admin_client, path="/docs/imprint").status_code == 400
    assert create(admin_client, path="/bad path/<x>").status_code == 400


def test_invalid_content_type(enabled, admin_client, db):
    assert create(admin_client, content_type="php").status_code == 400


@pytest.mark.parametrize("fields", [
    {"content_type": "redirect", "redirect_url": "javascript:alert(1)"},
    {"content_type": "redirect", "redirect_url": "java\tscript:alert(1)"},
    {"content_type": "iframe_embed", "iframe_url": "http://example.com"},
    {"content_type": "iframe_embed", "iframe_url": "https://e.com", "iframe_height": "1px;}body{color:red"},
    {"content_type": "iframe_embed", "iframe_url": "https://e.com", "iframe_sandbox": "allow-evil"},
    {"content_type": "link_list", "links_json": '[{"title": "x", "url": "javascript:alert(1)"}]'},
    {"content_type": "link_list", "links_json": "not json"},
    {"content_type": "youtube_video", "video_url": "https://evil.example/watch?v=abcdefghijk"},
    {"content_type": "json_content", "content": "{broken"},
    {"content_type": "code_snippet", "code_language": "py<script>"},
])
def test_field_validation(enabled, admin_client, db, fields):
    assert create(admin_client, **fields).status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM custom_pages") == 0


def test_edit_and_delete(enabled, admin_client, db):
    create(admin_client)
    page = row(db, "/about")
    response = admin_client.post(f"/admin/custom-pages/{page['id']}/edit", content_type="multipart/form-data",
                                 data={"path": "/about-us", "title": "About us", "content_type": "plain_text",
                                       "content": "hello"})
    assert response.status_code == 302
    assert row(db, "/about-us")["content"] == "hello"
    assert row(db, "/about-us")["is_published"] == 0
    assert admin_client.post(f"/admin/custom-pages/{page['id']}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM custom_pages") == 0
    assert admin_client.get(f"/admin/custom-pages/{page['id']}/edit").status_code == 404


def test_create_form_renders(enabled, admin_client, db):
    insert(db, path="/x", content_type="html")
    assert admin_client.get("/admin/custom-pages").status_code == 200
    assert admin_client.get("/admin/custom-pages/create").status_code == 200
    page_id = row(db, "/x")["id"]
    assert admin_client.get(f"/admin/custom-pages/{page_id}/edit").status_code == 200


def test_video_size_setting(enabled, admin_client, db):
    admin_client.post("/admin/custom-pages/settings", data={"custom_pages_max_video_size_mb": "250"})
    assert db.scalar("SELECT custom_pages_max_video_size_mb FROM site_settings") == 250
    admin_client.post("/admin/custom-pages/settings", data={"custom_pages_max_video_size_mb": "0"})
    assert db.scalar("SELECT custom_pages_max_video_size_mb FROM site_settings") == 250


# ── Publishing and visibility ─────────────────────────────────────────────────


def test_unpublished_only_for_admins(enabled, client, admin, make_user, login, db):
    insert(db, path="/draft", content="secret", is_published=0)
    assert client.get("/draft").status_code == 404
    login(client, make_user("bob"))
    assert client.get("/draft").status_code == 404
    other = client.application.test_client()
    login(other, admin)
    assert other.get("/draft").status_code == 200


def test_published_pages_public_on_private_wiki(enabled, client, db):
    insert(db, path="/imprint", content="Company", title="Imprint")
    response = client.get("/imprint")
    assert response.status_code == 200 and b"Company" in response.data
    # Anything else still sends anonymous visitors to sign in or 404s.
    assert client.get("/_probe/private").status_code == 302
    assert client.get("/nothing-here").status_code == 404


def test_routes_win_over_custom_pages(enabled, client, db):
    insert(db, path="/_probe/public-read", content="shadow")
    assert client.get("/_probe/public-read").status_code == 302  # the real route (login redirect)


def test_unknown_paths_keep_404_and_405(enabled, admin_client):
    assert admin_client.get("/no/such/page").status_code == 404
    assert admin_client.post("/no/such/page").status_code == 404
    assert admin_client.get("/admin/custom-pages/settings").status_code == 405


def test_post_to_custom_page_not_served(enabled, client, db):
    insert(db, path="/form", content="x")
    assert client.post("/form").status_code == 405


def test_trailing_slash_redirects(enabled, client, db):
    insert(db, path="/docs", content="x")
    response = client.get("/docs/")
    assert response.status_code == 308 and response.headers["Location"].endswith("/docs")


# ── Content types ─────────────────────────────────────────────────────────────


def test_redirects(enabled, client, db):
    insert(db, path="/go", content_type="redirect", redirect_url="https://example.com/x", redirect_code=301)
    insert(db, path="/go2", content_type="redirect", redirect_url="/page/home")
    insert(db, path="/bad", content_type="redirect", redirect_url="javascript:alert(1)")
    insert(db, path="/empty", content_type="redirect", redirect_url="")
    first = client.get("/go")
    assert first.status_code == 301 and first.headers["Location"] == "https://example.com/x"
    assert client.get("/go2").status_code == 302
    assert client.get("/bad").status_code == 404
    assert client.get("/empty").status_code == 404


@pytest.mark.parametrize("kind,mime", [("plain_text", "text/plain"), ("json_content", "application/json"),
                                       ("xml_content", "application/xml")])
def test_data_types_are_sandboxed(enabled, client, db, kind, mime):
    insert(db, path="/data", content_type=kind, content="<x/>")
    response = client.get("/data")
    assert response.mimetype == mime
    assert response.headers["Content-Security-Policy"].startswith("sandbox")
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("kind", ["html", "html_styled", "html_full", "markdown"])
def test_author_html_only_inside_sandboxed_frame(enabled, client, db, kind):
    page_id = insert(db, path="/landing", content_type=kind, content="<h1>Hi</h1><script>steal()</script>",
                     css="body{color:red}</style><script>x()</script>", js="run('</script><b>')")
    wrapper = client.get("/landing")
    assert wrapper.status_code == 200
    assert b"steal()" not in wrapper.data and b"body{color:red}" not in wrapper.data
    assert f'src="/_cpd/{page_id}"'.encode() in wrapper.data
    assert b"allow-same-origin" not in wrapper.data
    document = client.get(f"/_cpd/{page_id}")
    csp = document.headers["Content-Security-Policy"]
    assert csp.startswith("sandbox ") and "allow-same-origin" not in csp
    assert "frame-ancestors 'self'" in csp
    if kind == "html_full":
        assert "allow-scripts" in csp and b"run('<\\/script><b>')" in document.data
        assert b"sandbox=\"allow-scripts" in wrapper.data
    else:
        assert "allow-scripts" not in csp and "script-src 'none'" in csp
    if kind in ("html_styled", "html_full"):
        assert b"</style><script>x()" not in document.data
    if kind == "markdown":
        assert b"steal()" not in document.data


def test_sandbox_document_not_for_other_types_or_unpublished(enabled, client, db):
    wiki_id = insert(db, path="/w", content_type="wiki_page")
    hidden_id = insert(db, path="/h", content_type="html", is_published=0, content="x")
    assert client.get(f"/_cpd/{wiki_id}").status_code == 404
    assert client.get(f"/_cpd/{hidden_id}").status_code == 404


def test_youtube_uses_nocookie_player(enabled, client, db):
    insert(db, path="/yt", content_type="youtube_video", video_url="https://youtu.be/dQw4w9WgXcQ",
           video_autoplay=1, video_controls=1)
    response = client.get("/yt")
    assert b"https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&amp;mute=1" in response.data
    assert "https://www.youtube-nocookie.com" in response.headers["Content-Security-Policy"]


def test_iframe_embed_widens_frame_src_for_its_origin_only(enabled, client, db):
    insert(db, path="/embed", content_type="iframe_embed", iframe_url="https://maps.example.org/x",
           iframe_height="80vh")
    insert(db, path="/legacy", content_type="page_embed", iframe_url="javascript:alert(1)",
           iframe_height="1px}body{x")
    response = client.get("/embed")
    assert "https://maps.example.org" in response.headers["Content-Security-Policy"]
    assert b'sandbox="allow-scripts allow-same-origin allow-popups"' in response.data
    assert b"height: 80vh" in response.data
    legacy = client.get("/legacy")
    assert b"javascript:" not in legacy.data and b"body{x" not in legacy.data


def test_link_list_drops_unsafe_links(enabled, client, db):
    links = [{"title": "Good", "url": "https://example.com"}, {"title": "Bad", "url": "javascript:alert(1)"},
             {"title": "Mail", "url": "mailto:a@b.c"}]
    insert(db, path="/links", content_type="link_list", links_json=json.dumps(links))
    body = client.get("/links").data
    assert b"Good" in body and b"Mail" in body and b"javascript" not in body


def test_code_snippet_escaped(enabled, client, db):
    insert(db, path="/code", content_type="code_snippet", content="<script>alert(1)</script>",
           code_language="html")
    body = client.get("/code").data
    assert b"<script>alert(1)</script>" not in body and b"codehilite" in body


# ── Files ─────────────────────────────────────────────────────────────────────


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_and_download_files(enabled, admin_client, db, app):
    response = create(admin_client, path="/files", content_type="file_listing",
                      file=[(io.BytesIO(b"hello"), "notes.txt"), (io.BytesIO(b"<svg onload=x>"), "evil.svg")])
    assert response.status_code == 302
    files = db.all("SELECT * FROM custom_page_files")
    assert [f["original_name"] for f in files] == ["notes.txt"]  # svg refused
    anon = app.test_client()
    listing = anon.get("/files")
    assert b"notes.txt" in listing.data
    download = anon.get(f"/_cpf/{files[0]['id']}/notes.txt")
    assert download.status_code == 200 and download.data == b"hello"
    assert download.headers["Content-Security-Policy"].startswith("default-src 'none'; sandbox")


def test_image_page_requires_images_and_image_type_serves_directly(enabled, admin_client, db, app):
    create(admin_client, path="/logo", content_type="image", file=(io.BytesIO(_png()), "logo.png"))
    create(admin_client, path="/pics", content_type="image_page", file=(io.BytesIO(b"text"), "doc.txt"))
    response = app.test_client().get("/logo")
    assert response.status_code == 200 and response.mimetype == "image/png"
    pics = row(db, "/pics")
    assert db.scalar("SELECT COUNT(*) FROM custom_page_files WHERE custom_page_id = ?", (pics["id"],)) == 0


def test_html_file_downloaded_not_rendered(enabled, admin_client, db, app):
    create(admin_client, path="/dl", content_type="file_download", file=(io.BytesIO(b"a,b"), "data.csv"))
    response = app.test_client().get("/dl")
    assert response.mimetype == "application/octet-stream"
    assert "attachment" in response.headers["Content-Disposition"]


def test_files_of_unpublished_pages_hidden(enabled, admin_client, db, app):
    create(admin_client, path="/private", content_type="file_listing", is_published="",
           file=(io.BytesIO(b"x"), "a.txt"))
    file_id = db.scalar("SELECT id FROM custom_page_files")
    assert app.test_client().get(f"/_cpf/{file_id}/a.txt").status_code == 404
    assert admin_client.get(f"/_cpf/{file_id}/a.txt").status_code == 200


def test_video_size_limit(enabled, admin_client, db):
    db.execute("UPDATE site_settings SET custom_pages_max_video_size_mb = 1")
    big = io.BytesIO(b"\0" * (1024 * 1024 + 10))
    create(admin_client, path="/vid", content_type="video_hosted", file=(big, "clip.mp4"))
    assert db.scalar("SELECT COUNT(*) FROM custom_page_files") == 0
    create(admin_client, path="/vid2", content_type="video_hosted", file=(io.BytesIO(b"\0" * 100), "clip.mp4"))
    assert db.scalar("SELECT COUNT(*) FROM custom_page_files") == 1


def test_delete_file_and_page_removes_storage(enabled, admin_client, db, app):
    create(admin_client, path="/f", content_type="file_listing",
           file=[(io.BytesIO(b"1"), "a.txt"), (io.BytesIO(b"2"), "b.txt")])
    rows = db.all("SELECT * FROM custom_page_files ORDER BY id")
    folder = app.config["BW"].folders.custom_page_files
    import os

    assert all(os.path.exists(os.path.join(folder, r["filename"])) for r in rows)
    admin_client.post(f"/admin/custom-pages/files/{rows[0]['id']}/delete")
    assert not os.path.exists(os.path.join(folder, rows[0]["filename"]))
    admin_client.post(f"/admin/custom-pages/{rows[0]['custom_page_id']}/delete")
    assert not os.path.exists(os.path.join(folder, rows[1]["filename"]))
    assert db.scalar("SELECT COUNT(*) FROM custom_page_files") == 0


def test_file_routes_404_when_disabled(admin_client, db):
    page_id = insert(db, path="/x", content_type="file_listing")
    file_id = db.insert("custom_page_files", {"custom_page_id": page_id, "filename": "a.txt",
                                              "original_name": "a.txt"})
    assert admin_client.get(f"/_cpf/{file_id}/a.txt").status_code == 404
