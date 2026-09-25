"""Tests for the Canvas plugin feature."""

import io
import json
import pytest


def _login(client, username, password):
    """Log in via the test client."""
    return client.post("/login", data={"username": username, "password": password})


def _enable_canvas_plugin():
    """Enable the canvas plugin in the database."""
    import db
    db.enable_plugin("canvas")


def _set_canvas_access(access="admin", write_access="admin"):
    """Set canvas access level in site settings."""
    import db
    db.update_site_settings(canvas_access=access, canvas_write_access=write_access)


class TestCanvasPluginGating:
    """Canvas routes return 404 when the plugin is disabled."""

    def test_canvas_list_404_when_disabled(self, client, admin_user):
        import db
        db.disable_plugin("canvas")
        _login(client, "admin", "admin123")
        resp = client.get("/canvas")
        assert resp.status_code == 404

    def test_canvas_view_404_when_disabled(self, client, admin_user):
        import db
        db.disable_plugin("canvas")
        _login(client, "admin", "admin123")
        resp = client.get("/canvas/test-slug")
        assert resp.status_code == 404

    def test_canvas_create_404_when_disabled(self, client, admin_user):
        import db
        db.disable_plugin("canvas")
        _login(client, "admin", "admin123")
        resp = client.post("/canvas/create", data={"title": "Test"})
        assert resp.status_code == 404


class TestCanvasAccessControl:
    """Canvas access respects the canvas_access site setting."""

    def test_unauthenticated_user_redirected(self, client, admin_user):
        _enable_canvas_plugin()
        resp = client.get("/canvas")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_admin_can_access_by_default(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/canvas")
        assert resp.status_code == 200
        assert b"Canvases" in resp.data

    def test_editor_blocked_by_default(self, client, admin_user, editor_user):
        _enable_canvas_plugin()
        _set_canvas_access("admin")
        _login(client, "editor", "editor123")
        resp = client.get("/canvas")
        assert resp.status_code == 403

    def test_editor_can_access_when_allowed(self, client, admin_user, editor_user):
        _enable_canvas_plugin()
        _set_canvas_access("editor")
        _login(client, "editor", "editor123")
        resp = client.get("/canvas")
        assert resp.status_code == 200

    def test_user_blocked_by_default(self, client, admin_user, regular_user):
        _enable_canvas_plugin()
        _set_canvas_access("admin")
        _login(client, "user", "user123")
        resp = client.get("/canvas")
        assert resp.status_code == 403

    def test_user_can_access_when_all(self, client, admin_user, regular_user):
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        _login(client, "user", "user123")
        resp = client.get("/canvas")
        assert resp.status_code == 200


class TestCanvasCRUD:
    """Canvas create, read, update, delete operations."""

    def test_create_canvas(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/canvas/create", data={"title": "My Canvas", "description": "A test canvas"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"My Canvas" in resp.data
        assert b"Canvas has been successfully created." in resp.data

    def test_create_canvas_missing_title(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/canvas/create", data={"title": ""}, follow_redirects=True)
        assert b"Canvas title is required" in resp.data

    def test_create_canvas_title_too_long(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/canvas/create", data={"title": "x" * 201}, follow_redirects=True)
        assert b"cannot exceed 200 characters" in resp.data

    def test_view_canvas(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("Test View", admin_user)
        layout = db.canvas_get_layout(layout_id)
        resp = client.get(f"/canvas/{layout['slug']}")
        assert resp.status_code == 200
        assert b"Test View" in resp.data

    def test_view_nonexistent_canvas_404(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/canvas/nonexistent-slug")
        assert resp.status_code == 404

    def test_edit_canvas_metadata(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("Old Title", admin_user)
        layout = db.canvas_get_layout(layout_id)
        resp = client.post(f"/canvas/{layout['slug']}/edit", data={
            "title": "New Title",
            "description": "Updated desc"
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Canvas has been successfully updated." in resp.data
        assert b"New Title" in resp.data

    def test_delete_canvas(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("To Delete", admin_user)
        layout = db.canvas_get_layout(layout_id)
        resp = client.post(f"/canvas/{layout['slug']}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Canvas has been successfully deleted." in resp.data
        assert db.canvas_get_layout(layout_id) is None


class TestCanvasDataAPI:
    """Canvas JSON data load/save API."""

    def test_get_canvas_data(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("Data Test", admin_user)
        layout = db.canvas_get_layout(layout_id)
        resp = client.get(f"/canvas/{layout['slug']}/data")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "data" in data
        assert "nodes" in data["data"]
        assert "edges" in data["data"]

    def test_save_canvas_data(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("Save Test", admin_user)
        layout = db.canvas_get_layout(layout_id)
        new_data = {
            "nodes": [{"id": "n1", "type": "text", "x": 100, "y": 100, "content": "Hello"}],
            "edges": [],
            "viewport": {"x": 0, "y": 0, "zoom": 1}
        }
        resp = client.post(
            f"/canvas/{layout['slug']}/data",
            data=json.dumps({"data": new_data}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        result = resp.get_json()
        assert result["ok"] is True
        assert result["version"] == 2

    def test_save_canvas_data_invalid_json(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("Invalid", admin_user)
        layout = db.canvas_get_layout(layout_id)
        resp = client.post(
            f"/canvas/{layout['slug']}/data",
            data="not json",
            content_type="text/plain",
        )
        assert resp.status_code == 400

    def test_save_canvas_data_missing_fields(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("Missing", admin_user)
        layout = db.canvas_get_layout(layout_id)
        resp = client.post(
            f"/canvas/{layout['slug']}/data",
            data=json.dumps({"data": {"nodes": []}}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_save_canvas_data_read_only_user(self, client, admin_user, editor_user):
        _enable_canvas_plugin()
        _set_canvas_access("all", "admin")
        import db
        layout_id = db.canvas_create_layout("ReadOnly", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "view", role="editor")
        _login(client, "editor", "editor123")
        resp = client.post(
            f"/canvas/{layout['slug']}/data",
            data=json.dumps({"data": {"nodes": [], "edges": []}}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_pages_search_can_include_home(self, client, logged_in_admin):
        import db
        _enable_canvas_plugin()
        home = db.get_home_page()
        assert home is not None
        query = (home["title"] or "")[:4] or "Home"
        resp = logged_in_admin.get(f"/api/pages/search?include_home=1&q={query}")
        assert resp.status_code == 200
        rows = resp.get_json()
        assert any(r.get("is_home") and r.get("slug") == home["slug"] for r in rows)

    def test_page_preview_by_slug_returns_html(self, client, logged_in_admin, admin_user):
        import db
        _enable_canvas_plugin()
        page_id = db.create_page("Canvas Preview", "canvas-preview", "# Heading\n\nBody", user_id=admin_user)
        page = db.get_page(page_id)
        resp = logged_in_admin.get(f"/api/pages/preview-by-slug?slug={page['slug']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["slug"] == page["slug"]
        assert "Heading" in data["html"]

    def test_code_highlight_api_returns_highlighted_html(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/code/highlight",
            data=json.dumps({"content": "print('hi')", "language": "python"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "codehilite" in data["html"]
        assert "span" in data["html"]


class TestCanvasPermissions:
    """Canvas permission model enforcement."""

    def test_admin_always_has_edit(self, client, admin_user):
        import db
        _enable_canvas_plugin()
        layout_id = db.canvas_create_layout("Admin Perm", admin_user)
        user = db.get_user_by_id(admin_user)
        assert db.canvas_get_user_permission(layout_id, user) == "edit"

    def test_creator_always_has_edit(self, client, admin_user, editor_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        layout_id = db.canvas_create_layout("Creator Perm", editor_user)
        user = db.get_user_by_id(editor_user)
        assert db.canvas_get_user_permission(layout_id, user) == "edit"

    def test_user_specific_permission_overrides_role(self, client, admin_user, regular_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        layout_id = db.canvas_create_layout("Override", admin_user)
        # Role gives view, user-specific gives edit
        db.canvas_set_permission(layout_id, "view", role="user")
        db.canvas_set_permission(layout_id, "edit", user_id=regular_user)
        user = db.get_user_by_id(regular_user)
        assert db.canvas_get_user_permission(layout_id, user) == "edit"

    def test_role_permission_fallback(self, client, admin_user, editor_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        layout_id = db.canvas_create_layout("Role Fallback", admin_user)
        db.canvas_set_permission(layout_id, "view", role="editor")
        user = db.get_user_by_id(editor_user)
        assert db.canvas_get_user_permission(layout_id, user) == "view"

    def test_no_permission_returns_none(self, client, admin_user, regular_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        layout_id = db.canvas_create_layout("No Perm", admin_user)
        user = db.get_user_by_id(regular_user)
        assert db.canvas_get_user_permission(layout_id, user) == "none"

    def test_user_cannot_view_without_permission(self, client, admin_user, regular_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        layout_id = db.canvas_create_layout("Restricted", admin_user)
        layout = db.canvas_get_layout(layout_id)
        _login(client, "user", "user123")
        resp = client.get(f"/canvas/{layout['slug']}")
        assert resp.status_code == 403

    def test_user_can_view_with_permission(self, client, admin_user, regular_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        layout_id = db.canvas_create_layout("Viewable", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "view", user_id=regular_user)
        _login(client, "user", "user123")
        resp = client.get(f"/canvas/{layout['slug']}")
        assert resp.status_code == 200

    def test_none_permission_denies_access(self, client, admin_user, regular_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        layout_id = db.canvas_create_layout("Denied", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "none", user_id=regular_user)
        _login(client, "user", "user123")
        resp = client.get(f"/canvas/{layout['slug']}")
        assert resp.status_code == 403


class TestCanvasSharing:
    """Canvas sharing permission management."""

    def test_add_user_permission(self, client, admin_user, regular_user):
        import db
        _enable_canvas_plugin()
        layout_id = db.canvas_create_layout("Share Test", admin_user)
        layout = db.canvas_get_layout(layout_id)
        _login(client, "admin", "admin123")
        resp = client.post(f"/canvas/{layout['slug']}/share", data={
            "action": "add_user",
            "user_id": regular_user,
            "permission": "view",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Permission for user has been successfully set." in resp.data

    def test_add_role_permission(self, client, admin_user):
        import db
        _enable_canvas_plugin()
        layout_id = db.canvas_create_layout("Role Share", admin_user)
        layout = db.canvas_get_layout(layout_id)
        _login(client, "admin", "admin123")
        resp = client.post(f"/canvas/{layout['slug']}/share", data={
            "action": "add_role",
            "role": "editor",
            "permission": "edit",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Permission for role" in resp.data

    def test_remove_user_permission(self, client, admin_user, regular_user):
        import db
        _enable_canvas_plugin()
        layout_id = db.canvas_create_layout("Remove User", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "view", user_id=regular_user)
        _login(client, "admin", "admin123")
        resp = client.post(f"/canvas/{layout['slug']}/share", data={
            "action": "remove_user",
            "user_id": regular_user,
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"User permission has been successfully removed." in resp.data

    def test_remove_role_permission(self, client, admin_user):
        import db
        _enable_canvas_plugin()
        layout_id = db.canvas_create_layout("Remove Role", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "view", role="editor")
        _login(client, "admin", "admin123")
        resp = client.post(f"/canvas/{layout['slug']}/share", data={
            "action": "remove_role",
            "role": "editor",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Role permission has been successfully removed." in resp.data

    def test_non_creator_cannot_share(self, client, admin_user, editor_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("editor", "editor")
        layout_id = db.canvas_create_layout("No Share", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "edit", role="editor")
        _login(client, "editor", "editor123")
        resp = client.post(f"/canvas/{layout['slug']}/share", data={
            "action": "add_role",
            "role": "user",
            "permission": "view",
        })
        assert resp.status_code == 403


class TestCanvasExport:
    """Canvas export functionality."""

    def test_export_as_creator(self, client, admin_user):
        import db
        _enable_canvas_plugin()
        layout_id = db.canvas_create_layout("Export Me", admin_user)
        layout = db.canvas_get_layout(layout_id)
        _login(client, "admin", "admin123")
        resp = client.get(f"/canvas/{layout['slug']}/export")
        assert resp.status_code == 200
        assert resp.content_type == "application/json"
        export_data = json.loads(resp.data)
        assert export_data["title"] == "Export Me"
        assert "data" in export_data

    def test_export_with_edit_permission(self, client, admin_user, editor_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("editor", "editor")
        layout_id = db.canvas_create_layout("Export Edit", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "edit", user_id=editor_user)
        _login(client, "editor", "editor123")
        resp = client.get(f"/canvas/{layout['slug']}/export")
        assert resp.status_code == 200

    def test_export_denied_without_edit_permission(self, client, admin_user, editor_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("editor", "editor")
        layout_id = db.canvas_create_layout("No Export", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "view", user_id=editor_user)
        _login(client, "editor", "editor123")
        resp = client.get(f"/canvas/{layout['slug']}/export")
        assert resp.status_code == 403


class TestCanvasWikiSync:
    """Wiki page nodes sync when pages update or are deleted."""

    def test_wiki_node_updates_on_page_change(self):
        import db
        _enable_canvas_plugin()
        from werkzeug.security import generate_password_hash
        uid = db.create_user("syncuser", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)

        page_id = db.create_page("Old Title", "old-title", content="content", user_id=uid)
        data = {
            "nodes": [
                {"id": "n1", "type": "wiki_page", "page_id": page_id, "label": "Old Title", "page_slug": "old-title", "x": 0, "y": 0}
            ],
            "edges": [],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        layout_id = db.canvas_create_layout("Sync Test", uid, data=data)

        db.canvas_update_wiki_nodes_for_page(page_id, new_title="New Title", new_slug="new-title")

        layout = db.canvas_get_layout(layout_id)
        import json
        canvas_data = json.loads(layout["data"])
        node = canvas_data["nodes"][0]
        assert node["label"] == "New Title"
        assert node["page_slug"] == "new-title"

    def test_wiki_node_marked_deleted_on_page_delete(self):
        import db
        _enable_canvas_plugin()
        from werkzeug.security import generate_password_hash
        uid = db.create_user("deluser", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)

        page_id = db.create_page("Delete Me", "delete-me", content="content", user_id=uid)
        data = {
            "nodes": [
                {"id": "n1", "type": "wiki_page", "page_id": page_id, "label": "Delete Me", "page_slug": "delete-me", "x": 0, "y": 0}
            ],
            "edges": [],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        layout_id = db.canvas_create_layout("Del Test", uid, data=data)

        db.canvas_mark_deleted_wiki_nodes(page_id)

        layout = db.canvas_get_layout(layout_id)
        import json
        canvas_data = json.loads(layout["data"])
        node = canvas_data["nodes"][0]
        assert node.get("deleted") is True

    def _seed_wiki_node_layout(self, page_id, title, slug, creator_id, layout_title="Sync HTTP Test"):
        """Helper: create a canvas layout with a single wiki_page node referencing *page_id*."""
        import db
        data = {
            "nodes": [
                {
                    "id": "n1",
                    "type": "wiki_page",
                    "page_id": page_id,
                    "label": title,
                    "page_slug": slug,
                    "x": 0,
                    "y": 0,
                }
            ],
            "edges": [],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        return db.canvas_create_layout(layout_title, creator_id, data=data)

    def test_wiki_node_syncs_on_inline_title_edit(self, client, admin_user):
        """Inline title edit (POST /page/<slug>/edit/title) must sync canvas wiki nodes."""
        import db
        _enable_canvas_plugin()
        page_id = db.create_page("Inline Old", "inline-old", content="x", user_id=admin_user)
        layout_id = self._seed_wiki_node_layout(page_id, "Inline Old", "inline-old", admin_user)
        _login(client, "admin", "admin123")
        resp = client.post("/page/inline-old/edit/title", data={"title": "Inline New"})
        assert resp.status_code == 302
        layout = db.canvas_get_layout(layout_id)
        node = json.loads(layout["data"])["nodes"][0]
        assert node["label"] == "Inline New"
        assert node["page_slug"] == "inline-old"

    def test_wiki_node_syncs_on_slug_rename(self, client, admin_user):
        """Slug rename (POST /page/<slug>/rename) must sync canvas wiki nodes."""
        import db
        _enable_canvas_plugin()
        page_id = db.create_page("Slug Page", "slug-old", content="x", user_id=admin_user)
        layout_id = self._seed_wiki_node_layout(page_id, "Slug Page", "slug-old", admin_user)
        _login(client, "admin", "admin123")
        resp = client.post("/page/slug-old/rename", data={"new_slug": "slug-new"})
        assert resp.status_code == 302
        layout = db.canvas_get_layout(layout_id)
        node = json.loads(layout["data"])["nodes"][0]
        assert node["page_slug"] == "slug-new"
        assert node["label"] == "Slug Page"

    def test_wiki_node_syncs_on_revert(self, client, admin_user):
        """Reverting a page (POST /page/<slug>/revert/<entry_id>) must sync canvas wiki nodes."""
        import db
        _enable_canvas_plugin()
        page_id = db.create_page("Revert V1", "revert-page", content="v1", user_id=admin_user)
        history = db.get_page_history(page_id)
        first_entry_id = history[-1]["id"]
        db.update_page(page_id, "Revert V2", "v2", admin_user, "second version")
        layout_id = self._seed_wiki_node_layout(page_id, "Revert V2", "revert-page", admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(f"/page/revert-page/revert/{first_entry_id}")
        assert resp.status_code == 302
        layout = db.canvas_get_layout(layout_id)
        node = json.loads(layout["data"])["nodes"][0]
        assert node["label"] == "Revert V1"

    def test_wiki_node_syncs_on_full_edit(self, client, admin_user):
        """Full editor (POST /page/<slug>/edit) renaming the title must sync canvas wiki nodes."""
        import db
        _enable_canvas_plugin()
        page_id = db.create_page("Full Old", "full-old", content="x", user_id=admin_user)
        layout_id = self._seed_wiki_node_layout(page_id, "Full Old", "full-old", admin_user)
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/full-old/edit",
            data={"title": "Full New", "content": "updated", "edit_message": "rename"},
        )
        assert resp.status_code == 302
        layout = db.canvas_get_layout(layout_id)
        node = json.loads(layout["data"])["nodes"][0]
        assert node["label"] == "Full New"

    def test_wiki_node_syncs_on_page_delete_route(self, client, admin_user):
        """Deleting a page (POST /page/<slug>/delete) must mark canvas wiki nodes as deleted."""
        import db
        _enable_canvas_plugin()
        # Disable deletion_slowdown so the page is deleted immediately rather than
        # being queued behind a 48h grace period (which would leave the node alive).
        db.disable_plugin("deletion_slowdown")
        page_id = db.create_page("Bye Page", "bye-page", content="x", user_id=admin_user)
        layout_id = self._seed_wiki_node_layout(page_id, "Bye Page", "bye-page", admin_user)
        _login(client, "admin", "admin123")
        resp = client.post("/page/bye-page/delete")
        assert resp.status_code == 302
        layout = db.canvas_get_layout(layout_id)
        node = json.loads(layout["data"])["nodes"][0]
        assert node.get("deleted") is True


class TestCanvasDB:
    """Direct database function tests."""

    def test_create_and_get_layout(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("dbtest", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("DB Test", uid, description="desc")
        layout = db.canvas_get_layout(layout_id)
        assert layout is not None
        assert layout["title"] == "DB Test"
        assert layout["description"] == "desc"
        assert layout["version"] == 1

    def test_slug_uniqueness(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("slugtest", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        id1 = db.canvas_create_layout("Same Name", uid)
        id2 = db.canvas_create_layout("Same Name", uid)
        l1 = db.canvas_get_layout(id1)
        l2 = db.canvas_get_layout(id2)
        assert l1["slug"] != l2["slug"]

    def test_update_increments_version(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("vertest", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("Version", uid)
        db.canvas_save_layout_data(layout_id, {"nodes": [{"id": "n1"}], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}})
        layout = db.canvas_get_layout(layout_id)
        assert layout["version"] == 2

    def test_delete_layout_cascades_permissions(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("cascade", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("Cascade", uid)
        db.canvas_set_permission(layout_id, "view", role="editor")
        db.canvas_delete_layout(layout_id)
        perms = db.canvas_get_permissions(layout_id)
        assert len(perms) == 0

    def test_list_layouts_for_admin_shows_all(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("listadmin", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        db.canvas_create_layout("A", uid)
        db.canvas_create_layout("B", uid)
        user = db.get_user_by_id(uid)
        layouts = db.canvas_list_layouts_for_user(user)
        assert len(layouts) >= 2

    def test_list_layouts_for_user_filters(self):
        import db
        from werkzeug.security import generate_password_hash
        admin_uid = db.create_user("filteradm", generate_password_hash("pass123"), role="admin")
        user_uid = db.create_user("filterusr", generate_password_hash("pass123"), role="user")
        db.update_site_settings(setup_done=1)
        id1 = db.canvas_create_layout("Visible", admin_uid)
        db.canvas_create_layout("Hidden", admin_uid)
        db.canvas_set_permission(id1, "view", user_id=user_uid)
        user = db.get_user_by_id(user_uid)
        layouts = db.canvas_list_layouts_for_user(user)
        titles = [l["title"] for l in layouts]
        assert "Visible" in titles
        assert "Hidden" not in titles

    def test_update_layout_changes_slug_on_title_change(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("slugchange", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("Original Title", uid)
        db.canvas_update_layout(layout_id, title="New Title Here")
        layout = db.canvas_get_layout(layout_id)
        assert "new-title-here" in layout["slug"]

    def test_clear_permissions(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("clearperms", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("Clear", uid)
        db.canvas_set_permission(layout_id, "view", role="editor")
        db.canvas_set_permission(layout_id, "edit", role="user")
        db.canvas_clear_permissions(layout_id)
        perms = db.canvas_get_permissions(layout_id)
        assert len(perms) == 0

    def test_get_layout_by_slug(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("slugget", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("By Slug", uid)
        layout = db.canvas_get_layout(layout_id)
        found = db.canvas_get_layout_by_slug(layout["slug"])
        assert found is not None
        assert found["id"] == layout_id

    def test_set_permission_update_existing(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("permupd", generate_password_hash("pass123"), role="admin")
        user_uid = db.create_user("permupduser", generate_password_hash("pass123"), role="user")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("Perm Update", uid)
        db.canvas_set_permission(layout_id, "view", user_id=user_uid)
        db.canvas_set_permission(layout_id, "edit", user_id=user_uid)
        user = db.get_user_by_id(user_uid)
        assert db.canvas_get_user_permission(layout_id, user) == "edit"

    def test_nonexistent_layout_returns_none(self):
        import db
        assert db.canvas_get_layout(99999) is None
        assert db.canvas_get_layout_by_slug("nonexistent") is None


class TestCanvasWriteAccess:
    """Write access enforcement for creating canvases."""

    def test_editor_cannot_create_when_write_access_admin(self, client, admin_user, editor_user):
        _enable_canvas_plugin()
        _set_canvas_access("editor", "admin")
        _login(client, "editor", "editor123")
        resp = client.post("/canvas/create", data={"title": "Denied"})
        assert resp.status_code == 403

    def test_editor_can_create_when_write_access_editor(self, client, admin_user, editor_user):
        _enable_canvas_plugin()
        _set_canvas_access("editor", "editor")
        _login(client, "editor", "editor123")
        resp = client.post("/canvas/create", data={"title": "Allowed"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Canvas has been successfully created." in resp.data

    def test_user_can_create_when_write_access_all(self, client, admin_user, regular_user):
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        _login(client, "user", "user123")
        resp = client.post("/canvas/create", data={"title": "User Canvas"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Canvas has been successfully created." in resp.data
        assert b"User Canvas" in resp.data

    def test_non_creator_cannot_delete(self, client, admin_user, editor_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("editor", "editor")
        layout_id = db.canvas_create_layout("NoDelete", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "edit", role="editor")
        _login(client, "editor", "editor123")
        resp = client.post(f"/canvas/{layout['slug']}/delete")
        assert resp.status_code == 403

    def test_non_creator_cannot_edit_metadata(self, client, admin_user, editor_user):
        import db
        _enable_canvas_plugin()
        _set_canvas_access("editor", "editor")
        layout_id = db.canvas_create_layout("NoMeta", admin_user)
        layout = db.canvas_get_layout(layout_id)
        db.canvas_set_permission(layout_id, "edit", role="editor")
        _login(client, "editor", "editor123")
        resp = client.post(f"/canvas/{layout['slug']}/edit", data={"title": "Hacked"})
        assert resp.status_code == 403


class TestCanvasPluginSettings:
    """Plugin settings are reset when canvas plugin is disabled."""

    def test_settings_reset_on_disable(self, client, admin_user):
        import db
        _enable_canvas_plugin()
        db.update_site_settings(canvas_access="all", canvas_write_access="all")
        _login(client, "admin", "admin123")
        resp = client.post("/admin/plugins/canvas/disable", follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["canvas_access"] == "admin"
        assert settings["canvas_write_access"] == "admin"


class TestCanvasEdgeCases:
    """Edge cases and boundary conditions."""

    def test_save_data_with_many_nodes(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        import db
        layout_id = db.canvas_create_layout("Many Nodes", admin_user)
        layout = db.canvas_get_layout(layout_id)
        nodes = [{"id": f"n{i}", "type": "text", "x": i * 10, "y": i * 10, "content": f"Node {i}"} for i in range(50)]
        edges = [{"id": f"e{i}", "from": f"n{i}", "to": f"n{i+1}"} for i in range(49)]
        data = {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 1}}
        resp = client.post(
            f"/canvas/{layout['slug']}/data",
            data=json.dumps({"data": data}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_export_nonexistent_canvas(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/canvas/nonexistent/export")
        assert resp.status_code == 404

    def test_delete_nonexistent_canvas(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/canvas/nonexistent/delete")
        assert resp.status_code == 404

    def test_wiki_sync_handles_bad_json(self):
        """Wiki sync should not crash on layouts with malformed JSON."""
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("badjson", generate_password_hash("pass123"), role="admin")
        db.update_site_settings(setup_done=1)
        layout_id = db.canvas_create_layout("Bad JSON", uid)
        # Written with SQL because the db helpers now only store sanitised
        # JSON.  The text matches the page_id pre-filter so the sync code
        # really has to parse it.
        with db.get_db_context() as conn:
            conn.execute(
                "UPDATE canvas__layouts SET data = ? WHERE id = ?",
                ('{"page_id":1, not valid json', layout_id),
            )
            conn.commit()
        # Should not raise
        db.canvas_update_wiki_nodes_for_page(1, new_title="Test", new_slug="test")
        db.canvas_mark_deleted_wiki_nodes(1)


class TestCanvasImport:
    """Tests for the canvas import route."""

    def _make_canvas_file(self, title="Imported Canvas", description="A description", nodes=None, edges=None):
        """Build a minimal canvas export JSON file-like payload."""
        content = json.dumps({
            "title": title,
            "description": description,
            "data": {
                "nodes": nodes or [{"id": "n1", "type": "text", "x": 10, "y": 20, "content": "Hello"}],
                "edges": edges or [],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        }).encode()
        return io.BytesIO(content)

    def test_import_canvas_success(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(), "test.canvas.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"successfully imported" in resp.data

    def test_import_canvas_appears_in_list(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(title="My Import"), "my.canvas.json")},
            content_type="multipart/form-data",
        )
        resp = client.get("/canvas")
        assert resp.status_code == 200
        assert b"My Import" in resp.data

    def test_import_canvas_preserves_nodes(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        nodes = [{"id": "n1", "type": "text", "x": 5, "y": 10, "content": "Preserved"}]
        client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(title="Node Canvas", nodes=nodes), "nodes.canvas.json")},
            content_type="multipart/form-data",
        )
        import db
        layouts = db.canvas_list_layouts_for_user(db.get_user_by_username("admin"))
        target = next((l for l in layouts if l["title"] == "Node Canvas"), None)
        assert target is not None
        saved_data = json.loads(target["data"])
        assert saved_data["nodes"][0]["content"] == "Preserved"

    def test_import_canvas_invalid_json(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/canvas/import",
            data={"import_file": (io.BytesIO(b"not valid json"), "bad.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Invalid JSON" in resp.data

    def test_import_canvas_no_file(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/canvas/import",
            data={},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"No file part" in resp.data

    def test_import_canvas_forbidden_for_non_writer(self, client, admin_user, regular_user):
        _enable_canvas_plugin()
        _set_canvas_access("all", "admin")
        _login(client, "user", "user123")
        resp = client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(), "test.canvas.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"do not have the required permissions" in resp.data

    def test_import_canvas_allowed_for_editor_writer(self, client, admin_user, editor_user):
        _enable_canvas_plugin()
        _set_canvas_access("editor", "editor")
        _login(client, "editor", "editor123")
        resp = client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(title="Editor Import"), "editor.canvas.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"successfully imported" in resp.data
        assert b"Editor Import" in resp.data

    def test_import_canvas_allowed_for_user_writer(self, client, admin_user, regular_user):
        _enable_canvas_plugin()
        _set_canvas_access("all", "all")
        _login(client, "user", "user123")
        resp = client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(title="User Import"), "user.canvas.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"successfully imported" in resp.data
        assert b"User Import" in resp.data

    def test_import_canvas_requires_login(self, client, admin_user):
        _enable_canvas_plugin()
        resp = client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(), "test.canvas.json")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_import_canvas_fallback_title(self, client, admin_user):
        """Missing title in JSON falls back to 'Imported Canvas'."""
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        payload = io.BytesIO(json.dumps({"data": {"nodes": [], "edges": []}}).encode())
        client.post(
            "/canvas/import",
            data={"import_file": (payload, "no_title.json")},
            content_type="multipart/form-data",
        )
        import db
        layouts = db.canvas_list_layouts_for_user(db.get_user_by_username("admin"))
        assert any(l["title"] == "Imported Canvas" for l in layouts)

    def test_import_canvas_malformed_data_fallback(self, client, admin_user):
        """Invalid 'data' field in JSON falls back to empty canvas structure."""
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        payload = io.BytesIO(json.dumps({"title": "Bad Data", "data": "not-a-dict"}).encode())
        resp = client.post(
            "/canvas/import",
            data={"import_file": (payload, "bad_data.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"successfully imported" in resp.data
        import db
        layouts = db.canvas_list_layouts_for_user(db.get_user_by_username("admin"))
        target = next((l for l in layouts if l["title"] == "Bad Data"), None)
        assert target is not None
        saved_data = json.loads(target["data"])
        assert saved_data["nodes"] == []
        assert saved_data["edges"] == []

    def test_import_canvas_plugin_disabled_returns_404(self, client, admin_user):
        import db
        db.disable_plugin("canvas")
        _login(client, "admin", "admin123")
        resp = client.post(
            "/canvas/import",
            data={"import_file": (self._make_canvas_file(), "test.canvas.json")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404


class TestCanvasRealtimeSync:
    """Verify the operational sync endpoints used by collaborative editing."""

    def _make_canvas(self, title="Sync Canvas"):
        import db
        admin = db.get_user_by_username("admin")
        layout_id = db.canvas_create_layout(title, admin["id"])
        return db.canvas_get_layout(layout_id)

    def test_data_endpoint_returns_seq(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        layout = self._make_canvas("Seq Smoke")
        resp = client.get(f"/canvas/{layout['slug']}/data")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "seq" in body
        # No events yet means the head pointer is zero.
        assert body["seq"] == 0

    def test_ops_upsert_and_delete_node_round_trip(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        layout = self._make_canvas()
        # Upsert a node, then verify the canonical blob carries it.
        resp = client.post(
            f"/canvas/{layout['slug']}/ops",
            data=json.dumps({"ops": [{
                "type": "upsert_node",
                "node": {"id": "n1", "type": "text", "x": 0, "y": 0,
                         "label": "hi", "display_text": "hi", "layer": 0},
            }]}),
            content_type="application/json",
            headers={"X-Canvas-Session": "alpha"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["seq"] > 0
        assert body["applied"] == 1

        # The blob now reflects the op.
        data_resp = client.get(f"/canvas/{layout['slug']}/data").get_json()
        node_ids = [n["id"] for n in data_resp["data"]["nodes"]]
        assert "n1" in node_ids

        # Delete it again via ops.
        del_resp = client.post(
            f"/canvas/{layout['slug']}/ops",
            data=json.dumps({"ops": [{"type": "delete_node", "id": "n1"}]}),
            content_type="application/json",
            headers={"X-Canvas-Session": "alpha"},
        )
        assert del_resp.status_code == 200
        data_after = client.get(f"/canvas/{layout['slug']}/data").get_json()
        assert all(n["id"] != "n1" for n in data_after["data"]["nodes"])

    def test_sync_excludes_caller_session(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        layout = self._make_canvas()
        client.post(
            f"/canvas/{layout['slug']}/ops",
            data=json.dumps({"ops": [{
                "type": "upsert_node",
                "node": {"id": "n2", "type": "text", "x": 0, "y": 0,
                         "label": "hi", "display_text": "hi", "layer": 0},
            }]}),
            content_type="application/json",
            headers={"X-Canvas-Session": "alpha"},
        )
        # The session that produced the op should NOT see it back.
        own = client.get(
            f"/canvas/{layout['slug']}/sync?since=0",
            headers={"X-Canvas-Session": "alpha"},
        ).get_json()
        assert own["events"] == []
        # A different session SHOULD see it.
        other = client.get(
            f"/canvas/{layout['slug']}/sync?since=0",
            headers={"X-Canvas-Session": "beta"},
        ).get_json()
        assert len(other["events"]) == 1
        assert other["events"][0]["op_type"] == "upsert_node"

    def test_data_post_emits_snapshot_event(self, client, admin_user):
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        layout = self._make_canvas()
        resp = client.post(
            f"/canvas/{layout['slug']}/data",
            data=json.dumps({"data": {
                "nodes": [{"id": "x", "type": "text", "x": 0, "y": 0,
                           "label": "x", "display_text": "x", "layer": 0}],
                "edges": [],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }}),
            content_type="application/json",
            headers={"X-Canvas-Session": "writer"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["seq"] > 0
        # Other session sees a snapshot event so they know to refetch.
        peek = client.get(
            f"/canvas/{layout['slug']}/sync?since=0",
            headers={"X-Canvas-Session": "reader"},
        ).get_json()
        kinds = [e["op_type"] for e in peek["events"]]
        assert "snapshot" in kinds

    def test_ops_silently_ignores_unknown_op_type(self, client, admin_user):
        """Unknown op types are dropped on the server.  This protects older
        clients from breaking when newer ones learn new op vocabulary."""
        _enable_canvas_plugin()
        _login(client, "admin", "admin123")
        layout = self._make_canvas()
        resp = client.post(
            f"/canvas/{layout['slug']}/ops",
            data=json.dumps({"ops": [{"type": "drop_database"}]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["applied"] == 0
