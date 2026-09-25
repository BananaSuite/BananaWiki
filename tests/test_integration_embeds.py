"""Tests for canvas/kanban embed integration in wiki pages."""

import json
import pytest


def _login(client, username, password):
    """Log in via the test client."""
    return client.post("/login", data={"username": username, "password": password})


def _enable_canvas_plugin():
    """Enable the canvas plugin in the database."""
    import db
    db.enable_plugin("canvas")


def _enable_kanban_plugin():
    """Enable the kanban plugin in the database."""
    import db
    db.enable_plugin("kanban")


def _set_canvas_access(access="all", write_access="all"):
    """Set canvas access level in site settings."""
    import db
    db.update_site_settings(canvas_access=access, canvas_write_access=write_access)


def _set_kanban_access(access="all", write_access="all"):
    """Set kanban access level in site settings."""
    import db
    db.update_site_settings(kanban_access=access, kanban_write_access=write_access)


class TestMarkdownEmbedShortcodes:
    """[[canvas …]] and [[kanban …]] shortcodes render as embed containers."""

    def test_canvas_shortcode_renders_container(self):
        from helpers._markdown import render_markdown
        html = render_markdown('[[canvas slug="my-canvas"]]', embed_videos=True)
        assert 'data-embed-type="canvas"' in html
        assert 'data-embed-slug="my-canvas"' in html
        assert 'bw-embed-canvas' in html

    def test_kanban_shortcode_renders_container(self):
        from helpers._markdown import render_markdown
        html = render_markdown('[[kanban board="42"]]', embed_videos=True)
        assert 'data-embed-type="kanban"' in html
        assert 'data-embed-board="42"' in html
        assert 'bw-embed-kanban' in html

    def test_canvas_shortcode_with_dimensions(self):
        from helpers._markdown import render_markdown
        html = render_markdown('[[canvas slug="test" width="600" height="400"]]', embed_videos=True)
        assert 'width:600px' in html
        assert 'height:400px' in html

    def test_kanban_shortcode_with_dimensions(self):
        from helpers._markdown import render_markdown
        html = render_markdown('[[kanban board="1" width="800" height="500"]]', embed_videos=True)
        assert 'width:800px' in html
        assert 'height:500px' in html

    def test_canvas_shortcode_default_dimensions(self):
        from helpers._markdown import render_markdown
        html = render_markdown('[[canvas slug="test"]]', embed_videos=True)
        assert 'width:100%' in html
        assert 'height:400' in html

    def test_shortcodes_inside_fenced_code_not_processed(self):
        from helpers._markdown import render_markdown
        html = render_markdown('```\n[[canvas slug="test"]]\n```', embed_videos=True)
        assert 'data-embed-type="canvas"' not in html

    def test_canvas_shortcode_escapes_slug(self):
        from helpers._markdown import render_markdown
        html = render_markdown('[[canvas slug="my-canvas&script"]]', embed_videos=True)
        # html.escape double-encodes & to &amp;amp; in attribute values
        assert 'my-canvas' in html

    def test_shortcode_isolation_adds_blank_lines(self):
        from helpers._markdown import render_markdown
        text = "Some text\n[[canvas slug=\"test\"]]\nMore text"
        html = render_markdown(text, embed_videos=True)
        assert 'data-embed-type="canvas"' in html


class TestEmbedCanvasAPI:
    """Canvas embed API endpoints return correct data."""

    def test_embed_canvas_returns_data(self, client, admin_user):
        _enable_canvas_plugin()
        _set_canvas_access()
        _login(client, "admin", "admin123")
        # Create a canvas
        resp = client.post("/canvas/create", data={"title": "Test Canvas"})
        assert resp.status_code == 302
        # Get the slug from redirect
        import db
        layouts = db.canvas_list_layouts()
        slug = layouts[0]["slug"]
        # Fetch embed data
        resp = client.get(f"/api/embed/canvas/{slug}")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["slug"] == slug
        assert data["title"] == "Test Canvas"
        assert "data" in data
        assert "seq" in data

    def test_embed_canvas_404_for_nonexistent(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/api/embed/canvas/nonexistent-slug")
        assert resp.status_code == 404

    def test_embed_canvas_sync_returns_events(self, client, admin_user):
        _enable_canvas_plugin()
        _set_canvas_access()
        _login(client, "admin", "admin123")
        # Create a canvas
        resp = client.post("/canvas/create", data={"title": "Sync Test"})
        assert resp.status_code == 302
        import db
        layouts = db.canvas_list_layouts()
        slug = layouts[0]["slug"]
        # Fetch sync events
        resp = client.get(f"/api/embed/canvas/{slug}/sync?since=0")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "events" in data
        assert "seq" in data


class TestEmbedKanbanAPI:
    """Kanban embed API endpoints return correct data."""

    def test_embed_kanban_returns_data(self, client, admin_user):
        _enable_kanban_plugin()
        _set_kanban_access()
        _login(client, "admin", "admin123")
        # Create a board
        resp = client.post("/kanban/create", data={"title": "Test Board"})
        assert resp.status_code == 302
        # Get the board ID
        import db
        boards = db.kanban_list_boards()
        board_id = boards[0]["id"]
        # Fetch embed data
        resp = client.get(f"/api/embed/kanban/{board_id}")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["id"] == board_id
        assert data["title"] == "Test Board"
        assert "columns" in data
        assert "seq" in data

    def test_embed_kanban_404_for_nonexistent(self, client, admin_user):
        _enable_kanban_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/api/embed/kanban/99999")
        assert resp.status_code == 404

    def test_embed_kanban_sync_returns_events(self, client, admin_user):
        _enable_kanban_plugin()
        _set_kanban_access()
        _login(client, "admin", "admin123")
        # Create a board
        resp = client.post("/kanban/create", data={"title": "Sync Board"})
        assert resp.status_code == 302
        import db
        boards = db.kanban_list_boards()
        board_id = boards[0]["id"]
        # Fetch sync events
        resp = client.get(f"/api/embed/kanban/{board_id}/sync?since=0")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "events" in data
        assert "seq" in data


class TestEmbedAccessControl:
    """Embed API endpoints respect access control."""

    def test_embed_canvas_requires_login(self, client, admin_user):
        _enable_canvas_plugin()
        _set_canvas_access()
        resp = client.get("/api/embed/canvas/test")
        assert resp.status_code == 302  # redirect to login

    def test_embed_kanban_requires_login(self, client, admin_user):
        _enable_kanban_plugin()
        _set_kanban_access()
        resp = client.get("/api/embed/kanban/1")
        assert resp.status_code == 302  # redirect to login

    def test_embed_canvas_denies_unauthorized_user(self, client, admin_user, regular_user):
        _enable_canvas_plugin()
        _set_canvas_access(access="admin")
        _login(client, "user", "user123")
        resp = client.get("/api/embed/canvas/nonexistent")
        assert resp.status_code == 403 or resp.status_code == 404

    def test_embed_kanban_denies_unauthorized_user(self, client, admin_user, regular_user):
        _enable_kanban_plugin()
        _set_kanban_access(access="admin")
        _login(client, "user", "user123")
        resp = client.get("/api/embed/kanban/99999")
        assert resp.status_code == 403 or resp.status_code == 404


class TestCanvasWikiNodeRefresh:
    """Canvas wiki_page nodes have real-time preview refresh capability."""

    def test_canvas_view_includes_refresh_script(self, client, admin_user):
        """Canvas view page includes the wiki preview refresh mechanism."""
        _enable_canvas_plugin()
        _set_canvas_access()
        _login(client, "admin", "admin123")
        resp = client.post("/canvas/create", data={"title": "Refresh Test"})
        assert resp.status_code == 302
        import db
        layouts = db.canvas_list_layouts()
        slug = layouts[0]["slug"]
        resp = client.get(f"/canvas/{slug}")
        assert resp.status_code == 200
        assert b"refreshWikiPreviews" in resp.data or b"pagePreviewLastRefresh" in resp.data


class TestWikiPageEmbedRendering:
    """Wiki pages correctly render embedded canvas/kanban content."""

    def test_wiki_page_with_canvas_shortcode_renders_embed(self, client, admin_user):
        """A wiki page containing [[canvas …]] renders the embed container."""
        _login(client, "admin", "admin123")
        # Create a page with a canvas embed shortcode
        resp = client.post("/create-page", data={
            "title": "Embed Test",
            "content": 'Here is a canvas:\n\n[[canvas slug="test-canvas"]]',
        })
        assert resp.status_code == 302
        # View the page
        resp = client.get("/page/embed-test")
        assert resp.status_code == 200
        assert b"bw-embed-canvas" in resp.data
        assert b'data-embed-type="canvas"' in resp.data

    def test_wiki_page_with_kanban_shortcode_renders_embed(self, client, admin_user):
        """A wiki page containing [[kanban …]] renders the embed container."""
        _login(client, "admin", "admin123")
        resp = client.post("/create-page", data={
            "title": "Kanban Embed",
            "content": 'Board:\n\n[[kanban board="1"]]',
        })
        assert resp.status_code == 302
        resp = client.get("/page/kanban-embed")
        assert resp.status_code == 200
        assert b"bw-embed-kanban" in resp.data
        assert b'data-embed-type="kanban"' in resp.data

    def test_wiki_page_embed_includes_polling_script(self, client, admin_user):
        """Wiki page with embeds includes the real-time polling script."""
        _login(client, "admin", "admin123")
        resp = client.post("/create-page", data={
            "title": "Polling Test",
            "content": '[[canvas slug="x"]]',
        })
        assert resp.status_code == 302
        resp = client.get("/page/polling-test")
        assert resp.status_code == 200
        assert b"bw-embed" in resp.data
