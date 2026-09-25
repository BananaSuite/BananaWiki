"""Security regressions for custom pages, canvas and page attachments.

Each test class names the audit finding it covers:

- custom_page.manage is admin-only, and custom pages that carry author
  markup or styles are sandboxed (A#22)
- custom page files and page attachments cannot be served as script or
  active documents on the wiki origin (A#23)
- canvas writes never store derived HTML and the viewer never renders HTML
  from node JSON (A#24)
- the attachment.* permissions are enforced (A#25)
- canvas imports go through the same checks as normal image uploads, and
  the upload cleanup keeps images that canvases point at (A#26)
- published custom pages are a deliberate public surface (A#29)
- /api/canvas/list-order-version is behind the canvas access check (A#31)
"""

import io
import json
import os
import zipfile

import pytest


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def _png_bytes(size=(4, 4), noise=False):
    """Return a real PNG.  With *noise* the pixels are random, so the file
    stays about as large as the raw pixel data."""
    from PIL import Image
    width, height = size
    if noise:
        img = Image.frombytes("RGB", size, os.urandom(width * height * 3))
    else:
        img = Image.new("RGB", size, (200, 180, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def isolated_files(tmp_path, monkeypatch):
    """Point every upload folder at a private temporary directory."""
    import config
    folders = {
        "UPLOAD_FOLDER": tmp_path / "uploads",
        "ATTACHMENT_FOLDER": tmp_path / "attachments",
        "CUSTOM_PAGE_FILES_FOLDER": tmp_path / "custom_page_files",
    }
    for name, path in folders.items():
        path.mkdir()
        monkeypatch.setattr(config, name, str(path))
    return folders


# ---------------------------------------------------------------------------
# A#22: custom_page.manage is admin-only
# ---------------------------------------------------------------------------

class TestCustomPageManageIsAdminOnly:

    def test_permission_not_assignable_below_admin(self):
        from helpers._permissions import (
            is_permission_assignable_to_role,
            sanitize_permission_keys,
        )
        for role in ("user", "editor"):
            assert not is_permission_assignable_to_role("custom_page.manage", role)
            assert "custom_page.manage" not in sanitize_permission_keys(role, {"custom_page.manage"})
        for role in ("admin", "owner"):
            assert is_permission_assignable_to_role("custom_page.manage", role)

    def test_hidden_from_the_role_editor(self):
        from helpers._permissions import group_permissions_by_category
        for role in ("user", "editor"):
            keys = {
                perm[0]
                for category in group_permissions_by_category(role).values()
                for perm in category["permissions"]
            }
            assert "custom_page.manage" not in keys

    @pytest.mark.parametrize("base_role,fixture", [("user", "regular_user"), ("editor", "editor_user")])
    def test_stale_role_row_grants_nothing(self, request, client, admin_user, base_role, fixture):
        """A role row written before the change must not reopen the routes."""
        import db
        user_id = request.getfixturevalue(fixture)
        role_id = db.create_custom_role(f"cp-{base_role}", base_role=base_role, permission_keys=[])
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (?, ?)",
                (role_id, "custom_page.manage"),
            )
            conn.commit()
        db.assign_custom_role(user_id, role_id)
        assert not db.has_permission(db.get_user_by_id(user_id), "custom_page.manage")

        username, password = ("user", "user123") if base_role == "user" else ("editor", "editor123")
        _login(client, username, password)
        assert client.get("/admin/custom-pages").status_code == 403
        assert client.get("/admin/custom-pages/create").status_code == 403
        resp = client.post("/admin/custom-pages/create", data={
            "path": "/escalate", "title": "x", "content_type": "redirect",
            "redirect_url": "https://example.com", "is_published": "1",
        })
        assert resp.status_code == 403
        assert db.get_custom_page_by_path("/escalate") is None

    def test_stale_user_permission_row_grants_nothing(self, client, admin_user, editor_user):
        """Per-user permission rows are the other place a key can linger."""
        import db
        db.set_user_permissions(editor_user, {"page.view_all", "custom_page.manage"})
        assert "custom_page.manage" not in db.get_user_permissions(editor_user)["enabled_permissions"]
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)",
                (editor_user, "custom_page.manage"),
            )
            conn.commit()
        assert not db.has_permission(db.get_user_by_id(editor_user), "custom_page.manage")

        page_id = db.create_custom_page("/imprint", "Imprint", "plain_text", admin_user,
                                        content="Legal", is_published=1)
        _login(client, "editor", "editor123")
        resp = client.post(f"/admin/custom-pages/{page_id}/edit", data={
            "path": "/imprint", "title": "Imprint", "content_type": "redirect",
            "redirect_url": "https://example.com", "is_published": "1",
        })
        assert resp.status_code == 403
        assert client.post(f"/admin/custom-pages/{page_id}/delete").status_code == 403
        assert db.get_custom_page(page_id)["content_type"] == "plain_text"

    def test_admin_still_manages_custom_pages(self, client, admin_user):
        import db
        _login(client, "admin", "admin123")
        assert client.get("/admin/custom-pages").status_code == 200
        resp = client.post("/admin/custom-pages/create", data={
            "path": "/about", "title": "About", "content_type": "plain_text",
            "content": "hello", "is_published": "1",
        })
        assert resp.status_code == 302
        assert db.get_custom_page_by_path("/about") is not None


class TestCustomPageMarkupIsContained:

    def test_markdown_page_is_sandboxed(self, client, admin_user):
        import db
        db.create_custom_page("/md", "M", "markdown", admin_user, content="hello", is_published=1)
        resp = client.get("/md")
        assert resp.status_code == 200
        assert resp.headers["Content-Security-Policy"].startswith("sandbox allow-scripts")

    def test_xml_page_is_sandboxed(self, client, admin_user):
        import db
        db.create_custom_page("/feed.xml", "X", "xml_content", admin_user,
                              content="<root/>", is_published=1)
        resp = client.get("/feed.xml")
        assert resp.status_code == 200
        assert resp.headers["Content-Security-Policy"].startswith("sandbox allow-scripts")

    def test_css_field_cannot_close_style_element(self, client, admin_user):
        import db
        css = 'body{color:red}</style><b id="cpmarker">x</b><style>'
        db.create_custom_page("/styled", "S", "markdown", admin_user,
                              content="hello", css=css, is_published=1)
        body = client.get("/styled").get_data(as_text=True)
        assert '<b id="cpmarker">' not in body
        assert "\\3c /style>" in body
        # The page still has exactly one style element and it still applies.
        assert body.count("</style>") == 1
        assert "body{color:red}" in body

    def test_css_strings_keep_their_meaning(self):
        from routes.custom_pages import _escape_style_text
        assert _escape_style_text('a::before{content:"<"}') == 'a::before{content:"\\3c "}'
        assert _escape_style_text("p{color:blue}") == "p{color:blue}"

    @pytest.mark.parametrize("target", [
        "javascript:alert(1)",
        " JavaScript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,<b>x</b>",
        "file:///etc/passwd",
    ])
    def test_redirect_form_refuses_other_schemes(self, client, admin_user, target):
        import db
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/create", data={
            "path": "/go", "title": "Go", "content_type": "redirect",
            "redirect_url": target, "is_published": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_custom_page_by_path("/go") is None

    def test_redirect_edit_refuses_other_schemes(self, client, admin_user):
        import db
        page_id = db.create_custom_page("/go", "Go", "redirect", admin_user,
                                        redirect_url="/page/home", is_published=1)
        _login(client, "admin", "admin123")
        client.post(f"/admin/custom-pages/{page_id}/edit", data={
            "path": "/go", "title": "Go", "content_type": "redirect",
            "redirect_url": "javascript:alert(1)", "is_published": "1",
        })
        assert db.get_custom_page(page_id)["redirect_url"] == "/page/home"

    def test_stored_unsafe_redirect_is_not_served(self, client, admin_user):
        import db
        db.create_custom_page("/old", "Old", "redirect", admin_user,
                              redirect_url="javascript:alert(1)", is_published=1)
        assert client.get("/old").status_code == 404

    @pytest.mark.parametrize("target", ["https://example.com/x", "/page/home", "about"])
    def test_valid_redirects_still_work(self, client, admin_user, target):
        import db
        _login(client, "admin", "admin123")
        client.post("/admin/custom-pages/create", data={
            "path": "/go", "title": "Go", "content_type": "redirect",
            "redirect_url": target, "is_published": "1",
        })
        assert db.get_custom_page_by_path("/go") is not None
        client.post("/logout")
        resp = client.get("/go")
        assert resp.status_code == 302
        assert target in resp.headers["Location"]

    def test_link_list_drops_script_links(self, client, admin_user):
        import db
        links = json.dumps([
            {"title": "Good", "url": "https://example.com"},
            {"title": "Mail", "url": "mailto:team@example.com"},
            {"title": "Bad", "url": "javascript:alert(1)"},
            "not an object",
        ])
        db.create_custom_page("/links", "Links", "link_list", admin_user,
                              links_json=links, is_published=1)
        body = client.get("/links").get_data(as_text=True)
        assert "https://example.com" in body
        assert "mailto:team@example.com" in body
        assert "javascript:" not in body


# ---------------------------------------------------------------------------
# A#23: files are never served as script or active documents
# ---------------------------------------------------------------------------

def _create_file_page(client, path, filename, payload, content_type="file_listing"):
    import db
    resp = client.post("/admin/custom-pages/create", data={
        "path": path, "title": "Files", "content_type": content_type,
        "is_published": "1", "file": (io.BytesIO(payload), filename),
    }, content_type="multipart/form-data", follow_redirects=True)
    page = db.get_custom_page_by_path(path)
    return resp, page, db.list_custom_page_files(page["id"]) if page else []


class TestCustomPageFileServing:

    @pytest.mark.parametrize("filename,payload", [
        ("x.js", b"document.title='owned'"),
        ("x.mjs", b"export default 1"),
        ("x.css", b"body{display:none}"),
        ("data.rdf", b"<?xml version='1.0'?><root/>"),
        ("doc.pdf", b"%PDF-1.4"),
    ])
    def test_non_media_files_download_as_octet_stream(self, client, admin_user, isolated_files,
                                                      filename, payload):
        _login(client, "admin", "admin123")
        _resp, _page, files = _create_file_page(client, "/files", filename, payload)
        assert len(files) == 1
        client.post("/logout")
        resp = client.get(f"/_cpf/{files[0]['id']}/{filename}")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert resp.headers["Content-Disposition"].startswith("attachment")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Content-Security-Policy"].startswith("sandbox")

    def test_disk_fallback_uses_the_same_policy(self, client, admin_user, isolated_files):
        """Rows without a blob copy are served from disk with the same headers."""
        import db
        page_id = db.create_custom_page("/files", "F", "file_listing", admin_user, is_published=1)
        (isolated_files["CUSTOM_PAGE_FILES_FOLDER"] / "stored.js").write_bytes(b"alert(1)")
        file_id = db.add_custom_page_file(page_id, "stored.js", "x.js", "text/javascript", 8)
        resp = client.get(f"/_cpf/{file_id}/x.js")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert resp.headers["Content-Disposition"].startswith("attachment")

    def test_images_and_text_stay_inline(self, client, admin_user, isolated_files):
        _login(client, "admin", "admin123")
        _resp, _page, pngs = _create_file_page(client, "/pics", "pic.png", _png_bytes())
        _resp, _page, texts = _create_file_page(client, "/notes", "notes.txt", b"just text")
        client.post("/logout")
        resp = client.get(f"/_cpf/{pngs[0]['id']}/pic.png")
        assert resp.mimetype == "image/png"
        assert resp.headers["Content-Disposition"].startswith("inline")
        resp = client.get(f"/_cpf/{texts[0]['id']}/notes.txt")
        assert resp.mimetype == "text/plain"
        assert resp.headers["Content-Disposition"].startswith("inline")

    def test_image_content_type_uses_the_allowlist(self, client, admin_user, isolated_files):
        _login(client, "admin", "admin123")
        _create_file_page(client, "/logo", "logo.js", b"alert(1)", content_type="image")
        client.post("/logout")
        resp = client.get("/logo")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert resp.headers["Content-Disposition"].startswith("attachment")

    @pytest.mark.parametrize("filename", ["page.html", "icon.svg", "setup.exe", "README"])
    def test_upload_refuses_blocked_extensions(self, client, admin_user, isolated_files, filename):
        _login(client, "admin", "admin123")
        resp, page, files = _create_file_page(client, "/blocked", filename, b"<b>x</b>")
        assert page is not None  # the page itself is still saved
        assert files == []
        assert b"File type not allowed" in resp.data
        assert os.listdir(isolated_files["CUSTOM_PAGE_FILES_FOLDER"]) == []

    def test_upload_follows_the_admin_upload_mode(self, client, admin_user, isolated_files):
        import db
        db.update_site_settings(upload_mode="blacklist", upload_blacklist="zip")
        _login(client, "admin", "admin123")
        _resp, _page, files = _create_file_page(client, "/zip", "bundle.zip", b"PK")
        assert files == []

    def test_page_attachment_download_is_octet_stream(self, client, admin_user, isolated_files):
        import db
        page_id = db.create_page("Att", "att", "body", category_id=None, user_id=admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(f"/api/page/{page_id}/attachments",
                           data={"file": (io.BytesIO(b"alert(1)"), "tool.js")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200, resp.get_json()
        attachment_id = resp.get_json()["id"]
        resp = client.get(f"/page/att/attachments/{attachment_id}/download")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert resp.headers["Content-Disposition"].startswith("attachment")
        # Disk fallback when the blob copy is gone.
        with db.get_db_context() as conn:
            conn.execute("UPDATE page_attachments SET blob_id = NULL WHERE id = ?", (attachment_id,))
            conn.commit()
        resp = client.get(f"/page/att/attachments/{attachment_id}/download")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"


# ---------------------------------------------------------------------------
# A#24: canvas writes never store derived HTML
# ---------------------------------------------------------------------------

def _canvas_node(**extra):
    node = {"id": "n1", "type": "code", "x": 1, "y": 2, "content": "print(1)",
            "language": "python", "label": "code"}
    node.update(extra)
    return node


def _stored_nodes(layout_id):
    import db
    return json.loads(db.canvas_get_layout(layout_id)["data"])["nodes"]


class TestCanvasStoresNoHtml:

    @pytest.fixture
    def editor_canvas(self, client, admin_user, editor_user):
        import db
        db.update_site_settings(canvas_access="editor", canvas_write_access="editor")
        layout_id = db.canvas_create_layout("Shared", editor_user)
        _login(client, "editor", "editor123")
        return layout_id, db.canvas_get_layout(layout_id)["slug"]

    def test_data_save_drops_html_fields(self, client, editor_canvas):
        import db
        layout_id, slug = editor_canvas
        node = _canvas_node(highlighted_html="<b>x</b>", page_preview_html="<i>y</i>",
                            onclick="alert(1)")
        resp = client.post(f"/canvas/{slug}/data", json={"data": {
            "nodes": [node, {"id": 7, "type": "text", "x": 0, "y": 0, "content": "*hi*"}],
            "edges": [{"id": "e1", "from": "n1", "to": 7, "label": "rel"}],
            "viewport": {"x": 10, "y": "bad", "zoom": 2},
            "extra": "<script>",
        }})
        assert resp.status_code == 200
        stored = json.loads(db.canvas_get_layout(layout_id)["data"])
        first, second = stored["nodes"]
        assert "highlighted_html" not in first
        assert "page_preview_html" not in first
        assert "onclick" not in first
        # Fields the viewer really uses survive, including numeric ids.
        assert first["content"] == "print(1)" and first["language"] == "python"
        assert second["id"] == 7 and second["content"] == "*hi*"
        assert stored["edges"] == [{"id": "e1", "from": "n1", "to": 7, "label": "rel"}]
        assert stored["viewport"] == {"x": 10, "y": 0, "zoom": 2}
        assert "extra" not in stored

    def test_ops_keep_content_and_language(self, client, editor_canvas):
        layout_id, slug = editor_canvas
        resp = client.post(f"/canvas/{slug}/ops", json={"ops": [
            {"type": "upsert_node", "node": _canvas_node(highlighted_html="<b>x</b>")},
        ]})
        assert resp.status_code == 200
        node = _stored_nodes(layout_id)[0]
        assert node["content"] == "print(1)" and node["language"] == "python"
        assert "highlighted_html" not in node

    def test_legacy_rows_are_filtered_on_read(self, client, editor_canvas):
        import db
        layout_id, slug = editor_canvas
        legacy = {"nodes": [_canvas_node(highlighted_html="<b>x</b>")], "edges": []}
        with db.get_db_context() as conn:
            conn.execute("UPDATE canvas__layouts SET data = ? WHERE id = ?",
                         (json.dumps(legacy), layout_id))
            conn.commit()
        data = client.get(f"/canvas/{slug}/data").get_json()["data"]
        assert "highlighted_html" not in data["nodes"][0]
        export = json.loads(client.get(f"/canvas/{slug}/export").data)
        assert "highlighted_html" not in export["data"]["nodes"][0]
        embed = client.get(f"/api/embed/canvas/{slug}").get_json()["data"]
        assert "highlighted_html" not in embed["nodes"][0]

    def test_import_drops_html_fields(self, client, admin_user):
        import db
        _login(client, "admin", "admin123")
        payload = json.dumps({"title": "Imp", "data": {
            "nodes": [_canvas_node(page_preview_html="<i>y</i>")], "edges": [],
        }}).encode()
        client.post("/canvas/import", data={"import_file": (io.BytesIO(payload), "x.canvas.json")},
                    content_type="multipart/form-data")
        layout = next(l for l in db.canvas_list_layouts() if l["title"] == "Imp")
        node = json.loads(layout["data"])["nodes"][0]
        assert "page_preview_html" not in node
        assert node["content"] == "print(1)"

    def test_revert_drops_html_fields_from_old_history(self, client, admin_user):
        import db
        _login(client, "admin", "admin123")
        layout_id = db.canvas_create_layout("Hist", admin_user)
        slug = db.canvas_get_layout(layout_id)["slug"]
        with db.get_db_context() as conn:
            cur = conn.execute(
                "INSERT INTO canvas__history (layout_id, title, description, data, edited_by, "
                "edit_message, is_revert, created_at) VALUES (?, 'Hist', '', ?, ?, 'old', 0, "
                "'2024-01-01 00:00:00')",
                (layout_id, json.dumps({"nodes": [_canvas_node(highlighted_html="<b>x</b>")],
                                        "edges": []}), admin_user),
            )
            entry_id = cur.lastrowid
            conn.commit()
        resp = client.post(f"/canvas/{slug}/revert/{entry_id}")
        assert resp.status_code == 302
        node = _stored_nodes(layout_id)[0]
        assert "highlighted_html" not in node
        assert node["content"] == "print(1)"

    def test_viewer_never_renders_html_from_node_json(self):
        """The canvas viewer must take preview and highlight HTML only from
        the server endpoints, never from node fields."""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "app", "templates", "canvas", "view.html"), encoding="utf-8") as f:
            source = f.read()
        assert "node.highlighted_html" not in source
        assert "node.page_preview_html" not in source
        assert "function safeLinkHref" in source


# ---------------------------------------------------------------------------
# A#25: attachment.* permissions are enforced
# ---------------------------------------------------------------------------

class TestAttachmentPermissions:

    @pytest.fixture
    def page_with_admin_file(self, admin_user, isolated_files):
        import db
        page_id = db.create_page("Att", "att", "body", category_id=None, user_id=admin_user)
        attachment_id = db.add_page_attachment(page_id, "a.txt", "a.txt", 1, admin_user)
        (isolated_files["ATTACHMENT_FOLDER"] / "a.txt").write_bytes(b"a")
        return page_id, attachment_id

    def _editor_role(self, editor_user, keys):
        import db
        role_id = db.create_custom_role("restricted-editor", base_role="editor",
                                        permission_keys=["page.view_all", "page.edit_all", *keys])
        db.assign_custom_role(editor_user, role_id)

    def _upload(self, client, page_id, name="note.txt"):
        return client.post(f"/api/page/{page_id}/attachments",
                           data={"file": (io.BytesIO(b"hello"), name)},
                           content_type="multipart/form-data")

    def test_upload_requires_attachment_upload(self, client, editor_user, page_with_admin_file):
        page_id, _attachment_id = page_with_admin_file
        self._editor_role(editor_user, [])
        _login(client, "editor", "editor123")
        assert self._upload(client, page_id).status_code == 403

    def test_upload_allowed_with_permission(self, client, editor_user, page_with_admin_file):
        page_id, _attachment_id = page_with_admin_file
        self._editor_role(editor_user, ["attachment.upload"])
        _login(client, "editor", "editor123")
        assert self._upload(client, page_id).status_code == 200

    def test_delete_of_other_users_file_needs_delete_any(self, client, editor_user, page_with_admin_file):
        import db
        _page_id, attachment_id = page_with_admin_file
        self._editor_role(editor_user, ["attachment.delete_own"])
        _login(client, "editor", "editor123")
        assert client.delete(f"/api/attachments/{attachment_id}").status_code == 403
        assert db.get_page_attachment(attachment_id) is not None

    def test_delete_any_allows_other_users_file(self, client, editor_user, page_with_admin_file):
        import db
        _page_id, attachment_id = page_with_admin_file
        self._editor_role(editor_user, ["attachment.delete_any"])
        _login(client, "editor", "editor123")
        assert client.delete(f"/api/attachments/{attachment_id}").status_code == 200
        assert db.get_page_attachment(attachment_id) is None

    def test_delete_own_needs_delete_own(self, client, editor_user, page_with_admin_file):
        import db
        page_id, _attachment_id = page_with_admin_file
        self._editor_role(editor_user, ["attachment.upload"])
        _login(client, "editor", "editor123")
        own_id = self._upload(client, page_id).get_json()["id"]
        assert client.delete(f"/api/attachments/{own_id}").status_code == 403
        role_id = db.get_user_by_id(editor_user)["custom_role_id"]
        db.update_custom_role(role_id, permission_keys=["page.view_all", "page.edit_all",
                                                        "attachment.upload", "attachment.delete_own"])
        assert client.delete(f"/api/attachments/{own_id}").status_code == 200

    def test_default_editor_keeps_upload_and_own_delete(self, client, editor_user, page_with_admin_file):
        page_id, attachment_id = page_with_admin_file
        _login(client, "editor", "editor123")
        resp = self._upload(client, page_id)
        assert resp.status_code == 200
        assert client.delete(f"/api/attachments/{resp.get_json()['id']}").status_code == 200
        # Default editors do not hold attachment.delete_any.
        assert client.delete(f"/api/attachments/{attachment_id}").status_code == 403

    def test_download_requires_attachment_view(self, client, regular_user, page_with_admin_file):
        import db
        page_id, attachment_id = page_with_admin_file
        db.add_page_attachment(page_id, "b.txt", "b.txt", 1, None)
        _login(client, "user", "user123")
        assert client.get(f"/page/att/attachments/{attachment_id}/download").status_code == 200
        role_id = db.create_custom_role("no-files", base_role="user",
                                        permission_keys=["page.view_all", "category.view_all"])
        db.assign_custom_role(regular_user, role_id)
        assert client.get(f"/page/att/attachments/{attachment_id}/download").status_code == 403
        assert client.get("/page/att/attachments/download-all").status_code == 403

    def test_controls_follow_attachment_permissions(self, client, editor_user, page_with_admin_file):
        """The page and editor hide the buttons the routes would refuse."""
        _page_id, attachment_id = page_with_admin_file
        self._editor_role(editor_user, [])
        _login(client, "editor", "editor123")
        page = client.get("/page/att").get_data(as_text=True)
        assert f"/attachments/{attachment_id}/download" not in page
        assert f'data-att-id="{attachment_id}"' not in page
        edit = client.get("/page/att/edit").get_data(as_text=True)
        assert 'id="attachment-file-input"' not in edit
        assert f'data-att-id="{attachment_id}"' not in edit
        assert 'data-can-view="0"' in edit

    def test_default_editor_sees_upload_and_download_but_not_delete_of_others(
        self, client, editor_user, page_with_admin_file,
    ):
        _page_id, attachment_id = page_with_admin_file
        _login(client, "editor", "editor123")
        page = client.get("/page/att").get_data(as_text=True)
        assert f"/attachments/{attachment_id}/download" in page
        assert f'data-att-id="{attachment_id}"' not in page
        edit = client.get("/page/att/edit").get_data(as_text=True)
        assert 'id="attachment-file-input"' in edit
        assert f'data-att-id="{attachment_id}"' not in edit


class TestCategoryModalDeletePages:

    def test_delete_pages_option_needs_page_delete(self, client, admin_user, editor_user):
        import db
        cat_id = db.create_category("Modal")
        db.create_page("In modal", "in-modal", "body", category_id=cat_id, user_id=admin_user)
        _login(client, "editor", "editor123")
        html = client.get(f"/api/category/{cat_id}/management").get_json()["html"]
        assert 'value="move"' in html
        assert 'value="delete"' not in html

    def test_admin_still_sees_delete_pages_option(self, client, admin_user):
        import db
        cat_id = db.create_category("Modal")
        db.create_page("In modal", "in-modal", "body", category_id=cat_id, user_id=admin_user)
        _login(client, "admin", "admin123")
        html = client.get(f"/api/category/{cat_id}/management").get_json()["html"]
        assert 'value="delete"' in html


# ---------------------------------------------------------------------------
# A#26: canvas import follows the image upload rules
# ---------------------------------------------------------------------------

def _bundle(nodes, assets):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("canvas.json", json.dumps({"title": "Bundle", "data": {"nodes": nodes, "edges": []}}))
        for name, data in assets.items():
            z.writestr(f"assets/{name}", data)
    return buf.getvalue()


def _image_node(name, node_id="i1"):
    return {"id": node_id, "type": "image", "x": 0, "y": 0, "url": f"/static/uploads/{name}"}


def _import(client, payload):
    return client.post("/canvas/import",
                       data={"import_file": (io.BytesIO(payload), "b.canvas.zip")},
                       content_type="multipart/form-data")


def _bundle_layouts():
    import db
    return [l for l in db.canvas_list_layouts() if l["title"] == "Bundle"]


class TestCanvasImportAssets:

    @pytest.fixture(autouse=True)
    def _admin(self, client, admin_user, isolated_files):
        _login(client, "admin", "admin123")
        self.uploads = isolated_files["UPLOAD_FOLDER"]

    def test_referenced_image_gets_a_uuid_name(self, client):
        resp = _import(client, _bundle([_image_node("chosen.png")], {"chosen.png": _png_bytes()}))
        assert resp.status_code == 302 and "/canvas/" in resp.headers["Location"]
        files = os.listdir(self.uploads)
        assert len(files) == 1 and files[0] != "chosen.png" and files[0].endswith(".png")
        node = json.loads(_bundle_layouts()[0]["data"])["nodes"][0]
        assert node["url"] == f"/static/uploads/{files[0]}"

    def test_unreferenced_assets_are_not_written(self, client):
        payload = _bundle([], {"fill0.png": b"\0" * (5 * 1024 * 1024)})
        assert len(payload) < 100 * 1024  # highly compressible
        resp = _import(client, payload)
        assert resp.status_code == 302
        assert os.listdir(self.uploads) == []
        assert len(_bundle_layouts()) == 1

    def test_non_image_asset_refuses_the_import(self, client):
        resp = _import(client, _bundle([_image_node("fake.png")], {"fake.png": b"\0" * 1024}))
        assert resp.status_code == 302
        assert os.listdir(self.uploads) == []
        assert _bundle_layouts() == []

    def test_assets_over_the_upload_limit_refuse_the_import(self, client):
        import db
        db.update_site_settings(upload_max_size_mb=1)
        big = _png_bytes(size=(700, 700), noise=True)
        assert len(big) > 1024 * 1024
        resp = _import(client, _bundle([_image_node("big.png")], {"big.png": big}))
        assert resp.status_code == 302
        assert os.listdir(self.uploads) == []
        assert _bundle_layouts() == []

    def test_daily_upload_quota_applies(self, client):
        import db
        # The quota columns are set by operators, not through update_site_settings.
        with db.get_db_context() as conn:
            conn.execute("UPDATE site_settings SET upload_quota_per_day_count = 1 WHERE id = 1")
            conn.commit()
        nodes = [_image_node("a.png", "i1"), _image_node("b.png", "i2")]
        resp = _import(client, _bundle(nodes, {"a.png": _png_bytes(), "b.png": _png_bytes()}))
        assert resp.status_code == 302
        assert os.listdir(self.uploads) == []
        assert _bundle_layouts() == []

    def test_storage_limit_applies(self, client, monkeypatch):
        import routes.canvas as canvas_routes
        monkeypatch.setattr(canvas_routes, "mutation_would_exceed_quota",
                            lambda incoming=0: (True, 0, 0))
        resp = _import(client, _bundle([_image_node("a.png")], {"a.png": _png_bytes()}))
        assert resp.status_code == 302
        assert os.listdir(self.uploads) == []
        assert _bundle_layouts() == []

    def test_oversized_canvas_json_is_refused(self, client, monkeypatch):
        import routes.canvas as canvas_routes
        monkeypatch.setattr(canvas_routes, "_MAX_CANVAS_IMPORT_JSON_BYTES", 1024)
        payload = _bundle([{"id": f"n{i}", "type": "text", "x": 0, "y": 0, "content": "x" * 50}
                           for i in range(100)], {})
        resp = _import(client, payload)
        assert resp.status_code == 302
        assert _bundle_layouts() == []

    def test_existing_upload_is_never_overwritten(self, client):
        (self.uploads / "victim.png").write_bytes(b"original")
        resp = _import(client, _bundle([_image_node("victim.png")], {"victim.png": _png_bytes()}))
        assert resp.status_code == 302
        assert (self.uploads / "victim.png").read_bytes() == b"original"
        node = json.loads(_bundle_layouts()[0]["data"])["nodes"][0]
        assert node["url"] != "/static/uploads/victim.png"

    def test_understated_zip_size_does_not_pass_the_limit(self, client):
        """The zip headers claim 10 bytes for an asset that inflates to
        about 3 MB; the bytes written must still be held to the limit."""
        import db
        db.update_site_settings(upload_max_size_mb=1)
        payload = bytearray(_bundle([_image_node("a.png")],
                                    {"a.png": _png_bytes() + b"\0" * (3 * 1024 * 1024)}))
        name = b"assets/a.png"
        local = payload.find(name) - 30
        central = payload.find(name, local + 31) - 46
        assert payload[local:local + 4] == b"PK\x03\x04"
        assert payload[central:central + 4] == b"PK\x01\x02"
        # Uncompressed size field of the local and of the central header.
        payload[local + 22:local + 26] = (10).to_bytes(4, "little")
        payload[central + 24:central + 28] = (10).to_bytes(4, "little")
        resp = _import(client, bytes(payload))
        assert resp.status_code == 302
        assert os.listdir(self.uploads) == []
        assert _bundle_layouts() == []

    def test_corrupt_zip_is_refused(self, client):
        payload = b"PK\x03\x04" + b"\0" * 64
        resp = _import(client, payload)
        assert resp.status_code == 302
        assert _bundle_layouts() == []

    def test_imported_image_survives_the_upload_cleanup(self, client):
        """Page edits run the upload cleanup; it must not delete an image
        that only a canvas points at."""
        _import(client, _bundle([_image_node("kept.png")], {"kept.png": _png_bytes()}))
        stored = os.listdir(self.uploads)
        assert len(stored) == 1
        resp = client.post("/create-page", data={"title": "After import", "content": "x"})
        assert resp.status_code == 302
        assert os.listdir(self.uploads) == stored


class TestUploadCleanupKeepsCanvasImages:

    def test_layouts_and_history_are_scanned(self, admin_user, isolated_files):
        import db
        from routes.uploads import cleanup_unused_uploads
        uploads = isolated_files["UPLOAD_FOLDER"]
        for name in ("current.png", "old.png", "orphan.png"):
            (uploads / name).write_bytes(_png_bytes())
        layout_id = db.canvas_create_layout("Pics", admin_user, data={
            "nodes": [_image_node("old.png")], "edges": [],
        })
        db.canvas_record_layout_history(layout_id, admin_user, "first")
        db.canvas_save_layout_data(layout_id, {
            "nodes": [_image_node("current.png"),
                      {"id": "t", "type": "text", "x": 0, "y": 0,
                       "content": 'see "/static/uploads/current.png"'}],
            "edges": [],
        })
        cleanup_unused_uploads()
        assert sorted(os.listdir(uploads)) == ["current.png", "old.png"]

    def test_scan_tolerates_missing_canvas_tables(self, monkeypatch):
        import sqlite3
        from db import _canvas

        class _NoTables:
            def execute(self, *_args):
                raise sqlite3.OperationalError("no such table")

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(_canvas, "get_db_context", lambda: _NoTables())
        assert _canvas.referenced_upload_filenames() == set()


# ---------------------------------------------------------------------------
# A#29: published custom pages are public on purpose
# ---------------------------------------------------------------------------

class TestCustomPagesArePublic:

    def test_no_view_permission_in_the_catalog(self):
        from helpers._permissions import PERMISSION_PLUGIN_MAP, get_all_permission_keys
        assert "custom_page.view" not in get_all_permission_keys()
        assert "custom_page.view" not in PERMISSION_PLUGIN_MAP

    def test_published_page_is_served_on_a_private_wiki(self, client, admin_user):
        import db
        db.update_site_settings(public_mode=0)
        db.create_custom_page("/imprint", "Imprint", "plain_text", admin_user,
                              content="Legal", is_published=1)
        db.create_custom_page("/draft", "Draft", "plain_text", admin_user,
                              content="Hidden", is_published=0)
        assert client.get("/imprint").status_code == 200
        assert client.get("/draft").status_code == 404


# ---------------------------------------------------------------------------
# A#31: list-order-version is behind the canvas access check
# ---------------------------------------------------------------------------

class TestCanvasListOrderVersion:

    def test_anonymous_on_private_wiki_is_sent_to_login(self, client, admin_user):
        import db
        db.update_site_settings(public_mode=0)
        resp = client.get("/api/canvas/list-order-version")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_signed_in_user_without_canvas_gets_404(self, client, admin_user, regular_user):
        import db
        db.disable_plugin("canvas")
        _login(client, "user", "user123")
        assert client.get("/api/canvas/list-order-version").status_code == 404

    def test_admin_still_reads_the_counter(self, client, admin_user):
        _login(client, "admin", "admin123")
        resp = client.get("/api/canvas/list-order-version")
        assert resp.status_code == 200
        assert "list_order_version" in resp.get_json()
