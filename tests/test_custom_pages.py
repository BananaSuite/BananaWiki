"""Tests for the custom pages plugin."""

import io
import json

import pytest


def _login(client, username, password):
    """Log in via the test client."""
    return client.post("/login", data={"username": username, "password": password})


def _enable_custom_pages_plugin():
    """Enable the custom_pages plugin in the database."""
    import db
    db.enable_plugin("custom_pages")


def _disable_custom_pages_plugin():
    """Disable the custom_pages plugin in the database."""
    import db
    db.disable_plugin("custom_pages")


class TestCustomPagesPluginGating:
    """Custom pages routes return 404 when the plugin is disabled."""

    def test_admin_page_404_when_disabled(self, client, admin_user):
        """Admin custom pages page returns 404 when plugin is disabled."""
        import db
        db.disable_plugin("custom_pages")
        _login(client, "admin", "admin123")
        resp = client.get("/admin/custom-pages")
        assert resp.status_code == 404

    def test_admin_page_accessible_when_enabled(self, client, admin_user):
        """Admin custom pages page is accessible when plugin is enabled."""
        _enable_custom_pages_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/admin/custom-pages")
        assert resp.status_code == 200
        assert b"Custom Pages" in resp.data

    def test_public_page_not_served_when_disabled(self, client, admin_user):
        """Custom pages are not served when the plugin is disabled."""
        import db
        _enable_custom_pages_plugin()
        db.create_custom_page("/test-disabled", "Test", "plain_text",
                              admin_user, content="hello")
        _disable_custom_pages_plugin()
        resp = client.get("/test-disabled")
        assert resp.status_code == 404

    def test_public_page_served_when_enabled(self, client, admin_user):
        """Custom pages are served when the plugin is enabled."""
        import db
        _enable_custom_pages_plugin()
        db.create_custom_page("/test-enabled", "Test", "plain_text",
                              admin_user, content="hello world")
        resp = client.get("/test-enabled")
        assert resp.status_code == 200
        assert b"hello world" in resp.data

    def test_cpf_route_404_when_disabled(self, client, admin_user):
        """File download route returns 404 when plugin is disabled."""
        _disable_custom_pages_plugin()
        resp = client.get("/_cpf/999/test.txt")
        assert resp.status_code == 404


class TestCustomPageDB:
    """Tests for db._custom_pages functions."""

    def test_create_and_get(self, admin_user):
        """Creating a custom page returns a retrievable record."""
        import db
        page_id = db.create_custom_page("/db-test", "DB Test", "html",
                                        admin_user, content="<p>hi</p>")
        assert page_id is not None
        page = db.get_custom_page(page_id)
        assert page is not None
        assert page["path"] == "/db-test"
        assert page["title"] == "DB Test"
        assert page["content_type"] == "html"
        assert page["content"] == "<p>hi</p>"

    def test_get_by_path(self, admin_user):
        """Retrieving a custom page by path works."""
        import db
        db.create_custom_page("/by-path", "By Path", "plain_text",
                              admin_user, content="found")
        page = db.get_custom_page_by_path("/by-path")
        assert page is not None
        assert page["title"] == "By Path"

    def test_get_by_path_not_found(self):
        """Querying a non-existent path returns None."""
        import db
        assert db.get_custom_page_by_path("/nonexistent") is None

    def test_update_custom_page(self, admin_user):
        """Updating fields persists the changes."""
        import db
        pid = db.create_custom_page("/update-test", "Old", "html",
                                    admin_user)
        db.update_custom_page(pid, title="New", content="updated")
        page = db.get_custom_page(pid)
        assert page["title"] == "New"
        assert page["content"] == "updated"

    def test_delete_custom_page(self, admin_user):
        """Deleting a custom page removes it."""
        import db
        pid = db.create_custom_page("/delete-test", "Del", "html",
                                    admin_user)
        assert db.delete_custom_page(pid) is True
        assert db.get_custom_page(pid) is None

    def test_delete_nonexistent(self):
        """Deleting a nonexistent page returns False."""
        import db
        assert db.delete_custom_page(99999) is False

    def test_list_custom_pages(self, admin_user):
        """Listing pages returns all created pages ordered by path."""
        import db
        db.create_custom_page("/list-b", "B", "html", admin_user)
        db.create_custom_page("/list-a", "A", "html", admin_user)
        pages = db.list_custom_pages()
        paths = [p["path"] for p in pages]
        assert "/list-a" in paths
        assert "/list-b" in paths
        # Check ordering
        idx_a = paths.index("/list-a")
        idx_b = paths.index("/list-b")
        assert idx_a < idx_b

    def test_count_custom_pages(self, admin_user):
        """count_custom_pages returns the correct count."""
        import db
        before = db.count_custom_pages()
        db.create_custom_page("/count-test", "Count", "html", admin_user)
        assert db.count_custom_pages() == before + 1

    def test_content_types_constant(self):
        """CONTENT_TYPES has at least 15 entries."""
        import db
        assert len(db.CUSTOM_PAGE_CONTENT_TYPES) >= 15

    def test_add_and_list_files(self, admin_user):
        """Adding and listing files for a custom page works."""
        import db
        pid = db.create_custom_page("/files-test", "Files", "file_listing",
                                    admin_user)
        fid = db.add_custom_page_file(pid, "stored.txt", "original.txt",
                                      "text/plain", 100)
        assert fid is not None
        files = db.list_custom_page_files(pid)
        assert len(files) == 1
        assert files[0]["original_name"] == "original.txt"

    def test_get_file(self, admin_user):
        """Getting a file by ID returns the correct record."""
        import db
        pid = db.create_custom_page("/file-get", "F", "image", admin_user)
        fid = db.add_custom_page_file(pid, "s.png", "o.png",
                                      "image/png", 200)
        f = db.get_custom_page_file(fid)
        assert f is not None
        assert f["filename"] == "s.png"

    def test_delete_file(self, admin_user):
        """Deleting a file record removes it."""
        import db
        pid = db.create_custom_page("/file-del", "FD", "image", admin_user)
        fid = db.add_custom_page_file(pid, "s.png", "o.png",
                                      "image/png", 200)
        assert db.delete_custom_page_file(fid) is True
        assert db.get_custom_page_file(fid) is None

    def test_delete_page_cascades_files(self, admin_user):
        """Deleting a page also removes its file records."""
        import db
        pid = db.create_custom_page("/cascade-test", "C", "file_listing",
                                    admin_user)
        fid = db.add_custom_page_file(pid, "s.txt", "o.txt",
                                      "text/plain", 50)
        db.delete_custom_page(pid)
        assert db.get_custom_page_file(fid) is None


class TestCustomPagesAdminRoutes:
    """Tests for admin CRUD routes."""

    @pytest.fixture(autouse=True)
    def setup_plugin(self):
        _enable_custom_pages_plugin()

    def test_create_page_via_form(self, client, admin_user):
        """Creating a page via form POST redirects to listing."""
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/create", data={
            "path": "/form-create",
            "title": "Form Test",
            "content_type": "html",
            "content": "<p>hello</p>",
            "is_published": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Custom page created successfully" in resp.data

    def test_create_page_reserved_path(self, client, admin_user):
        """Creating a page on a reserved path shows error."""
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/create", data={
            "path": "/admin/something",
            "title": "Bad Path",
            "content_type": "html",
        }, follow_redirects=True)
        assert b"reserved by the system" in resp.data

    def test_create_page_duplicate_path(self, client, admin_user):
        """Creating a page with duplicate path shows error."""
        import db
        db.create_custom_page("/dup-path", "Dup", "html", admin_user)
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/create", data={
            "path": "/dup-path",
            "title": "Dup 2",
            "content_type": "html",
        }, follow_redirects=True)
        assert b"already exists" in resp.data

    def test_create_page_invalid_content_type(self, client, admin_user):
        """Creating a page with invalid content type shows error."""
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/create", data={
            "path": "/bad-type",
            "title": "Bad Type",
            "content_type": "invalid_type",
        }, follow_redirects=True)
        assert b"Invalid content type" in resp.data

    def test_create_page_empty_path(self, client, admin_user):
        """Creating a page with empty path shows error."""
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/create", data={
            "path": "",
            "title": "No Path",
            "content_type": "html",
        }, follow_redirects=True)
        assert b"valid path is required" in resp.data

    def test_edit_page_get(self, client, admin_user):
        """GET edit page shows the form with current data."""
        import db
        pid = db.create_custom_page("/edit-get", "Edit Me", "html",
                                    admin_user, content="original")
        _login(client, "admin", "admin123")
        resp = client.get(f"/admin/custom-pages/{pid}/edit")
        assert resp.status_code == 200
        assert b"Edit Me" in resp.data

    def test_edit_page_post(self, client, admin_user):
        """POST edit page updates the page."""
        import db
        pid = db.create_custom_page("/edit-post", "Before", "html",
                                    admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(f"/admin/custom-pages/{pid}/edit", data={
            "path": "/edit-post",
            "title": "After",
            "content_type": "html",
            "content": "updated content",
            "is_published": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Custom page updated successfully" in resp.data
        page = db.get_custom_page(pid)
        assert page["title"] == "After"
        assert page["content"] == "updated content"

    def test_edit_nonexistent(self, client, admin_user):
        """Editing a non-existent page returns 404."""
        _login(client, "admin", "admin123")
        resp = client.get("/admin/custom-pages/99999/edit")
        assert resp.status_code == 404

    def test_delete_page_via_form(self, client, admin_user):
        """Deleting a page via POST removes it."""
        import db
        pid = db.create_custom_page("/del-form", "Delete Me", "html",
                                    admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(f"/admin/custom-pages/{pid}/delete",
                           follow_redirects=True)
        assert b"Custom page deleted successfully" in resp.data
        assert db.get_custom_page(pid) is None

    def test_delete_nonexistent_page(self, client, admin_user):
        """Deleting a non-existent page returns 404."""
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/99999/delete")
        assert resp.status_code == 404

    def test_requires_admin(self, client, editor_user):
        """Non-admin users cannot access admin custom pages routes."""
        _enable_custom_pages_plugin()
        _login(client, "editor", "editor123")
        resp = client.get("/admin/custom-pages")
        assert resp.status_code in (302, 403)

    def test_create_form_get(self, client, admin_user):
        """GET create form renders successfully."""
        _login(client, "admin", "admin123")
        resp = client.get("/admin/custom-pages/create")
        assert resp.status_code == 200
        assert b"Create Custom Page" in resp.data

    def test_file_upload_on_create(self, client, admin_user):
        """Uploading a file during creation stores it."""
        import db
        _login(client, "admin", "admin123")
        data = {
            "path": "/upload-test",
            "title": "Upload Test",
            "content_type": "image",
            "is_published": "1",
        }
        data["file"] = (io.BytesIO(b"fake image data"), "test.png")
        resp = client.post("/admin/custom-pages/create",
                           data=data,
                           content_type="multipart/form-data",
                           follow_redirects=True)
        assert b"Custom page created successfully" in resp.data
        page = db.get_custom_page_by_path("/upload-test")
        files = db.list_custom_page_files(page["id"])
        assert len(files) == 1
        assert files[0]["original_name"] == "test.png"


class TestCustomPagesPublicServing:
    """Tests for public custom page serving via 404 handler."""

    @pytest.fixture(autouse=True)
    def setup_plugin(self):
        _enable_custom_pages_plugin()

    def test_serve_plain_text(self, client, admin_user):
        """Plain text pages serve with text/plain content type."""
        import db
        db.create_custom_page("/txt", "Text", "plain_text",
                              admin_user, content="hello text")
        resp = client.get("/txt")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/plain")
        assert b"hello text" in resp.data

    def test_serve_json(self, client, admin_user):
        """JSON pages serve with application/json content type."""
        import db
        db.create_custom_page("/api-data", "JSON", "json_content",
                              admin_user, content='{"key":"value"}')
        resp = client.get("/api-data")
        assert resp.status_code == 200
        assert resp.content_type.startswith("application/json")
        assert json.loads(resp.data) == {"key": "value"}

    def test_serve_xml(self, client, admin_user):
        """XML pages serve with application/xml content type."""
        import db
        db.create_custom_page("/feed", "XML", "xml_content",
                              admin_user, content="<root><item/></root>")
        resp = client.get("/feed")
        assert resp.status_code == 200
        assert resp.content_type.startswith("application/xml")
        assert b"<root>" in resp.data

    def test_serve_html(self, client, admin_user):
        """HTML pages serve raw HTML."""
        import db
        db.create_custom_page("/raw-html", "Raw", "html",
                              admin_user, content="<h1>Hello</h1>")
        resp = client.get("/raw-html")
        assert resp.status_code == 200
        assert b"<h1>Hello</h1>" in resp.data

    def test_serve_html_styled(self, client, admin_user):
        """HTML+CSS pages include a style block."""
        import db
        db.create_custom_page("/styled", "Styled", "html_styled",
                              admin_user, content="<p>styled</p>",
                              css="p { color: red; }")
        resp = client.get("/styled")
        assert resp.status_code == 200
        assert b"color: red" in resp.data
        assert b"<p>styled</p>" in resp.data

    def test_serve_html_full(self, client, admin_user):
        """HTML+CSS+JS pages include style and script blocks."""
        import db
        db.create_custom_page("/full", "Full", "html_full",
                              admin_user, content="<div>full</div>",
                              css="div{margin:0}", js="alert(1)")
        resp = client.get("/full")
        assert resp.status_code == 200
        assert b"<style nonce=" in resp.data
        assert b"<script nonce=" in resp.data
        assert "sandbox allow-scripts" in resp.headers.get("Content-Security-Policy", "")
        assert "X-Custom-Page-Sandbox" not in resp.headers

    def test_serve_markdown(self, client, admin_user):
        """Markdown pages render to HTML."""
        import db
        db.create_custom_page("/md", "Markdown", "markdown",
                              admin_user, content="# Hello\n\nWorld")
        resp = client.get("/md")
        assert resp.status_code == 200
        assert b"<h1>" in resp.data or b"Hello" in resp.data

    def test_serve_wiki_page(self, client, admin_user):
        """Wiki pages render in the wiki layout."""
        import db
        db.create_custom_page("/wiki-cp", "Wiki CP", "wiki_page",
                              admin_user, content="**bold wiki**")
        resp = client.get("/wiki-cp")
        assert resp.status_code == 200
        assert b"Wiki CP" in resp.data

    def test_serve_redirect_302(self, client, admin_user):
        """Redirect pages issue HTTP redirects."""
        import db
        db.create_custom_page("/go", "Go", "redirect",
                              admin_user,
                              redirect_url="https://example.com",
                              redirect_code=302)
        resp = client.get("/go")
        assert resp.status_code == 302
        assert "example.com" in resp.headers.get("Location", "")

    def test_serve_redirect_301(self, client, admin_user):
        """Redirect 301 pages issue permanent redirects."""
        import db
        db.create_custom_page("/perm", "Perm", "redirect",
                              admin_user,
                              redirect_url="https://example.org",
                              redirect_code=301)
        resp = client.get("/perm")
        assert resp.status_code == 301

    def test_redirect_no_url_404(self, client, admin_user):
        """Redirect page without URL falls through to 404."""
        import db
        db.create_custom_page("/bad-redir", "Bad", "redirect",
                              admin_user, redirect_url="")
        resp = client.get("/bad-redir")
        assert resp.status_code == 404

    def test_serve_code_snippet(self, client, admin_user):
        """Code snippet pages display the code."""
        import db
        db.create_custom_page("/snippet", "Snippet", "code_snippet",
                              admin_user, content="print('hello')",
                              code_language="python")
        resp = client.get("/snippet")
        assert resp.status_code == 200
        assert b"print(&#39;hello&#39;)" in resp.data or b"print(" in resp.data
        assert b"python" in resp.data

    def test_serve_iframe(self, client, admin_user):
        """Iframe pages include an iframe element."""
        import db
        db.create_custom_page("/framed", "Framed", "iframe_embed",
                              admin_user,
                              iframe_url="https://example.com")
        resp = client.get("/framed")
        assert resp.status_code == 200
        assert b"iframe" in resp.data
        assert b"example.com" in resp.data

    def test_iframe_embed_csp_adds_specific_origin(self, client, admin_user):
        """iframe_embed pages must extend frame-src with ONLY the page's origin."""
        import db
        db.create_custom_page("/framed-csp", "Framed CSP", "iframe_embed",
                              admin_user,
                              iframe_url="https://example.com/some/path")
        resp = client.get("/framed-csp")
        assert resp.status_code == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        csp_tokens = csp.replace(";", " ").split()
        assert "frame-src" in csp
        # The specific origin (no path) is whitelisted; wildcard must NOT be.
        assert "https://example.com" in csp_tokens
        assert "*" not in csp_tokens
        # The internal override header must not leak to the client
        assert "X-Frame-Src-Override" not in resp.headers

    def test_page_embed_csp_adds_specific_origin(self, client, admin_user):
        """page_embed pages must extend frame-src with ONLY the page's origin."""
        import db
        db.create_custom_page("/page-embed-csp", "Page Embed CSP", "page_embed",
                              admin_user,
                              iframe_url="https://partner.example.com")
        resp = client.get("/page-embed-csp")
        assert resp.status_code == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        csp_tokens = csp.replace(";", " ").split()
        assert "frame-src" in csp
        assert "https://partner.example.com" in csp_tokens
        assert "*" not in csp_tokens
        assert "X-Frame-Src-Override" not in resp.headers

    def test_non_iframe_pages_keep_restricted_frame_src(self, client, admin_user):
        """Non-iframe custom pages must keep the default restricted frame-src."""
        import db
        db.create_custom_page("/plain-csp", "Plain CSP", "plain_text",
                              admin_user, content="hello")
        resp = client.get("/plain-csp")
        assert resp.status_code == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        csp_tokens = csp.replace(";", " ").split()
        assert "https://www.youtube.com" in csp_tokens
        assert "https://player.vimeo.com" in csp_tokens

    def test_serve_link_list(self, client, admin_user):
        """Link list pages display links from JSON."""
        import db
        links = json.dumps([
            {"title": "Example", "url": "https://example.com",
             "description": "A test link"},
        ])
        db.create_custom_page("/links", "Links", "link_list",
                              admin_user, links_json=links)
        resp = client.get("/links")
        assert resp.status_code == 200
        assert b"Example" in resp.data
        assert b"example.com" in resp.data

    def test_serve_link_list_invalid_json(self, client, admin_user):
        """Link list with invalid JSON shows empty list."""
        import db
        db.create_custom_page("/bad-links", "Bad Links", "link_list",
                              admin_user, links_json="not json")
        resp = client.get("/bad-links")
        assert resp.status_code == 200

    def test_unpublished_page_hidden(self, client, admin_user):
        """Unpublished pages return 404 for anonymous users."""
        import db
        db.create_custom_page("/hidden", "Hidden", "html",
                              admin_user, content="secret",
                              is_published=0)
        resp = client.get("/hidden")
        assert resp.status_code == 404

    def test_unpublished_page_visible_to_admin(self, client, admin_user):
        """Admins can see unpublished pages."""
        import db
        db.create_custom_page("/admin-hidden", "AH", "plain_text",
                              admin_user, content="admin only",
                              is_published=0)
        _login(client, "admin", "admin123")
        resp = client.get("/admin-hidden")
        assert resp.status_code == 200
        assert b"admin only" in resp.data

    def test_nested_path(self, client, admin_user):
        """Pages at nested paths are served correctly."""
        import db
        db.create_custom_page("/docs/guide/intro", "Intro", "plain_text",
                              admin_user, content="nested content")
        resp = client.get("/docs/guide/intro")
        assert resp.status_code == 200
        assert b"nested content" in resp.data

    def test_nonexistent_path_still_404(self, client, admin_user):
        """Non-custom paths still return 404."""
        resp = client.get("/this-does-not-exist")
        assert resp.status_code == 404

    def test_post_request_not_served(self, client, admin_user):
        """POST requests to custom page paths return 404 (not 405)."""
        import db
        db.create_custom_page("/post-test", "PT", "html",
                              admin_user, content="hi")
        resp = client.post("/post-test")
        assert resp.status_code in (404, 405)


class TestPathValidation:
    """Tests for path normalisation and reserved path checking."""

    def test_normalise_strips_trailing_slash(self):
        """Trailing slashes are stripped."""
        from routes.custom_pages import _normalise_path
        assert _normalise_path("/example/") == "/example"

    def test_normalise_collapses_slashes(self):
        """Multiple slashes are collapsed."""
        from routes.custom_pages import _normalise_path
        assert _normalise_path("//a///b//") == "/a/b"

    def test_normalise_adds_leading_slash(self):
        """Leading slash is added if missing."""
        from routes.custom_pages import _normalise_path
        assert _normalise_path("example") == "/example"

    def test_normalise_empty_becomes_root(self):
        """Empty string normalises to root."""
        from routes.custom_pages import _normalise_path
        assert _normalise_path("") == "/"

    def test_reserved_admin(self):
        """Admin paths are reserved."""
        from routes.custom_pages import _is_path_reserved
        assert _is_path_reserved("/admin") is True
        assert _is_path_reserved("/admin/anything") is True

    def test_reserved_login(self):
        """Login path is reserved."""
        from routes.custom_pages import _is_path_reserved
        assert _is_path_reserved("/login") is True

    def test_reserved_root(self):
        """Root path is reserved."""
        from routes.custom_pages import _is_path_reserved
        assert _is_path_reserved("/") is True

    def test_unreserved_path(self):
        """Unreserved paths are allowed."""
        from routes.custom_pages import _is_path_reserved
        assert _is_path_reserved("/my-custom-page") is False
        assert _is_path_reserved("/docs/guide") is False

    def test_reserved_static(self):
        """Static paths are reserved."""
        from routes.custom_pages import _is_path_reserved
        assert _is_path_reserved("/static/css/style.css") is True

    def test_reserved_cpf(self):
        """_cpf paths are reserved."""
        from routes.custom_pages import _is_path_reserved
        assert _is_path_reserved("/_cpf/1/test.txt") is True


class TestCustomPageFileServing:
    """Tests for custom page file serving."""

    @pytest.fixture(autouse=True)
    def setup_plugin(self):
        _enable_custom_pages_plugin()

    def test_file_download_route(self, client, admin_user, tmp_path):
        """Files can be downloaded via the _cpf route."""
        import db
        import config
        import os

        # Set up a temp dir for custom page files
        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        test_folder = str(tmp_path / "cpf")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder

        try:
            pid = db.create_custom_page("/file-serve", "FS", "file_listing",
                                        admin_user)
            # Create a physical file
            test_file_path = os.path.join(test_folder, "stored123.txt")
            with open(test_file_path, "w") as f:
                f.write("file content here")

            fid = db.add_custom_page_file(pid, "stored123.txt", "readme.txt",
                                          "text/plain",
                                          os.path.getsize(test_file_path))

            resp = client.get(f"/_cpf/{fid}/readme.txt")
            assert resp.status_code == 200
            assert b"file content here" in resp.data
        finally:
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder

    def test_file_download_nonexistent(self, client, admin_user):
        """Downloading a non-existent file returns 404."""
        resp = client.get("/_cpf/99999/nothing.txt")
        assert resp.status_code == 404

    def test_file_unpublished_hidden(self, client, admin_user, tmp_path):
        """Files from unpublished pages are hidden from anonymous users."""
        import db
        import config
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        test_folder = str(tmp_path / "cpf2")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder

        try:
            pid = db.create_custom_page("/unpub-file", "UF", "file_listing",
                                        admin_user, is_published=0)
            test_file_path = os.path.join(test_folder, "hidden.txt")
            with open(test_file_path, "w") as f:
                f.write("secret")
            fid = db.add_custom_page_file(pid, "hidden.txt", "hidden.txt",
                                          "text/plain", 6)

            # Anonymous user can't see it
            resp = client.get(f"/_cpf/{fid}/hidden.txt")
            assert resp.status_code == 404

            # Admin can see it
            _login(client, "admin", "admin123")
            resp = client.get(f"/_cpf/{fid}/hidden.txt")
            assert resp.status_code == 200
        finally:
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder

    def test_admin_delete_file(self, client, admin_user):
        """Admin can delete a file via POST."""
        import db
        _login(client, "admin", "admin123")
        pid = db.create_custom_page("/admin-del-file", "ADF", "file_listing",
                                    admin_user)
        fid = db.add_custom_page_file(pid, "s.txt", "o.txt", "text/plain", 10)
        resp = client.post(f"/admin/custom-pages/files/{fid}/delete",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_custom_page_file(fid) is None

    def test_admin_delete_file_nonexistent(self, client, admin_user):
        """Deleting a non-existent file returns 404."""
        _login(client, "admin", "admin123")
        resp = client.post("/admin/custom-pages/files/99999/delete")
        assert resp.status_code == 404


class TestMimeTypeSafety:
    """Verify that MIME types are derived server-side and dangerous types are
    neutralised when serving custom page files."""

    @pytest.fixture(autouse=True)
    def setup_plugin(self):
        _enable_custom_pages_plugin()

    def test_mime_derived_from_extension(self, client, admin_user, tmp_path):
        """Uploaded file MIME type is derived from the file extension, not
        from the client-supplied Content-Type header."""
        import config
        import db
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        test_folder = str(tmp_path / "cpf_mime")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder

        try:
            _login(client, "admin", "admin123")
            # Upload a .png file: the server should store image/png regardless
            # of what the client sends as Content-Type.
            data = {
                "path": "/mime-test",
                "title": "Mime Test",
                "content_type": "image",
                "is_published": "1",
                "file": (io.BytesIO(b"fake image"), "photo.png"),
            }
            resp = client.post(
                "/admin/custom-pages/create",
                data=data,
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            assert b"Custom page created successfully" in resp.data

            page = db.get_custom_page_by_path("/mime-test")
            files = db.list_custom_page_files(page["id"])
            assert len(files) == 1
            assert files[0]["mime_type"] == "image/png"
        finally:
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder

    def test_html_file_not_served_as_html(self, client, admin_user, tmp_path):
        """An HTML file stored with text/html MIME is served as
        application/octet-stream to prevent XSS."""
        import config
        import db
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        test_folder = str(tmp_path / "cpf_xss")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder

        try:
            pid = db.create_custom_page(
                "/xss-test", "XSS", "file_listing", admin_user,
            )
            # Simulate a record with a dangerous MIME type already stored
            test_file = os.path.join(test_folder, "evil.html")
            with open(test_file, "w") as f:
                f.write("<script>alert(1)</script>")
            fid = db.add_custom_page_file(
                pid, "evil.html", "evil.html", "text/html",
                os.path.getsize(test_file),
            )

            resp = client.get(f"/_cpf/{fid}/evil.html")
            assert resp.status_code == 200
            # Must NOT be served as text/html
            assert "text/html" not in resp.content_type
            assert resp.content_type.startswith("application/octet-stream")
        finally:
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder

    def test_svg_file_not_served_as_svg(self, client, admin_user, tmp_path):
        """SVG files are served as application/octet-stream to block XSS."""
        import config
        import db
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        test_folder = str(tmp_path / "cpf_svg")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder

        try:
            pid = db.create_custom_page(
                "/svg-test", "SVG", "file_listing", admin_user,
            )
            test_file = os.path.join(test_folder, "bad.svg")
            with open(test_file, "w") as f:
                f.write('<svg onload="alert(1)"/>')
            fid = db.add_custom_page_file(
                pid, "bad.svg", "bad.svg", "image/svg+xml",
                os.path.getsize(test_file),
            )

            resp = client.get(f"/_cpf/{fid}/bad.svg")
            assert resp.status_code == 200
            assert "image/svg+xml" not in resp.content_type
            assert resp.content_type.startswith("application/octet-stream")
        finally:
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder

    def test_safe_mime_types_pass_through(self, client, admin_user, tmp_path):
        """Safe MIME types like text/plain are served normally."""
        import config
        import db
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        test_folder = str(tmp_path / "cpf_safe")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder

        try:
            pid = db.create_custom_page(
                "/safe-test", "Safe", "file_listing", admin_user,
            )
            test_file = os.path.join(test_folder, "notes.txt")
            with open(test_file, "w") as f:
                f.write("just text")
            fid = db.add_custom_page_file(
                pid, "notes.txt", "notes.txt", "text/plain",
                os.path.getsize(test_file),
            )

            resp = client.get(f"/_cpf/{fid}/notes.txt")
            assert resp.status_code == 200
            assert resp.content_type.startswith("text/plain")
        finally:
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder


class TestFileUploadSizeValidation:
    """Verify that oversized files are rejected without filling the disk."""

    @pytest.fixture(autouse=True)
    def setup_plugin(self):
        """Enable the custom_pages plugin."""
        _enable_custom_pages_plugin()

    def test_oversized_file_rejected(self, client, admin_user, tmp_path):
        """A file exceeding the max size is rejected and not stored on disk."""
        import config
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        original_max = config.CUSTOM_PAGE_MAX_FILE_SIZE
        test_folder = str(tmp_path / "cpf_size")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder
        config.CUSTOM_PAGE_MAX_FILE_SIZE = 1024  # 1 KB limit

        try:
            _login(client, "admin", "admin123")
            oversized = b"x" * 2048  # 2 KB: exceeds the 1 KB limit
            data = {
                "path": "/size-reject",
                "title": "Too Big",
                "content_type": "image",
                "is_published": "1",
                "file": (io.BytesIO(oversized), "big.png"),
            }
            resp = client.post("/admin/custom-pages/create",
                               data=data,
                               content_type="multipart/form-data",
                               follow_redirects=True)
            assert b"File too large" in resp.data

            # No file should remain on disk
            remaining = [f for f in os.listdir(test_folder)
                         if not f.startswith(".")]
            assert remaining == []
        finally:
            config.CUSTOM_PAGE_MAX_FILE_SIZE = original_max
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder

    def test_file_within_limit_accepted(self, client, admin_user, tmp_path):
        """A file within the max size is accepted and stored."""
        import config
        import db
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        original_max = config.CUSTOM_PAGE_MAX_FILE_SIZE
        test_folder = str(tmp_path / "cpf_ok")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder
        config.CUSTOM_PAGE_MAX_FILE_SIZE = 4096  # 4 KB limit

        try:
            _login(client, "admin", "admin123")
            small = b"y" * 1024  # 1 KB: within limit
            data = {
                "path": "/size-ok",
                "title": "Small Enough",
                "content_type": "image",
                "is_published": "1",
                "file": (io.BytesIO(small), "small.png"),
            }
            resp = client.post("/admin/custom-pages/create",
                               data=data,
                               content_type="multipart/form-data",
                               follow_redirects=True)
            assert b"Custom page created successfully" in resp.data
            page = db.get_custom_page_by_path("/size-ok")
            files = db.list_custom_page_files(page["id"])
            assert len(files) == 1
            assert files[0]["file_size"] == 1024
        finally:
            config.CUSTOM_PAGE_MAX_FILE_SIZE = original_max
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder

    def test_oversized_file_does_not_write_full_content(self, client, admin_user, tmp_path):
        """The streaming write stops early so at most max_size bytes hit disk."""
        import config
        import os

        original_folder = config.CUSTOM_PAGE_FILES_FOLDER
        original_max = config.CUSTOM_PAGE_MAX_FILE_SIZE
        test_folder = str(tmp_path / "cpf_stream")
        os.makedirs(test_folder, exist_ok=True)
        config.CUSTOM_PAGE_FILES_FOLDER = test_folder
        config.CUSTOM_PAGE_MAX_FILE_SIZE = 512  # 512 B limit

        try:
            _login(client, "admin", "admin123")
            huge = b"z" * (1024 * 100)  # 100 KB: far exceeds 512 B limit
            data = {
                "path": "/no-fill",
                "title": "No Fill",
                "content_type": "file_download",
                "is_published": "1",
                "file": (io.BytesIO(huge), "huge.bin"),
            }
            resp = client.post("/admin/custom-pages/create",
                               data=data,
                               content_type="multipart/form-data",
                               follow_redirects=True)
            assert b"File too large" in resp.data

            # The file must have been cleaned up
            remaining = [f for f in os.listdir(test_folder)
                         if not f.startswith(".")]
            assert remaining == []
        finally:
            config.CUSTOM_PAGE_MAX_FILE_SIZE = original_max
            config.CUSTOM_PAGE_FILES_FOLDER = original_folder


class TestAllContentTypes:
    """Verify all 19 content types can be created and served."""

    @pytest.fixture(autouse=True)
    def setup_plugin(self):
        _enable_custom_pages_plugin()

    def test_all_types_exist(self):
        """All 19 content types are defined."""
        import db
        expected = {
            "redirect", "html", "html_styled", "html_full",
            "markdown", "wiki_page", "plain_text",
            "json_content", "xml_content",
            "image", "image_page", "youtube_video", "video_hosted",
            "file_download", "file_listing",
            "iframe_embed", "page_embed",
            "link_list", "code_snippet",
        }
        assert set(db.CUSTOM_PAGE_CONTENT_TYPES.keys()) == expected

    def test_create_each_type(self, admin_user):
        """Each content type can be created in the database."""
        import db
        for ct in db.CUSTOM_PAGE_CONTENT_TYPES:
            pid = db.create_custom_page(
                f"/type-{ct}", f"Type {ct}", ct, admin_user,
            )
            page = db.get_custom_page(pid)
            assert page["content_type"] == ct


class TestMigrationInclusion:
    """Verify custom page tables are included in migration export."""

    def test_tables_in_export_list(self):
        """custom_pages and custom_page_files are in _EXPORT_TABLES."""
        from db._migration import _EXPORT_TABLES
        assert "custom_pages" in _EXPORT_TABLES
        assert "custom_page_files" in _EXPORT_TABLES
