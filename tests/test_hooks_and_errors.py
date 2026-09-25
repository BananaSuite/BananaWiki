"""
Tests for:
1. Plugin hooks (emit_hook) being called in core routes
2. Template slots (render_slot) being registered as Jinja global
3. HTTP 400 and 405 error handlers
4. Defensive DB error handling in 403/404 handlers
"""

import db
from bananawiki_sdk._hooks import _hook_registry, _hook_lock


class TestEmitHookCalls:
    """Verify that emit_hook() is called at the right lifecycle points."""

    def _register_hook(self, hook_name):
        """Register a test hook subscriber that records calls.

        Returns a list-subclass that also carries a reference to the registered
        subscriber so :meth:`_cleanup_hook` can remove only that subscriber
        instead of dropping the whole registry entry (which would also wipe
        plugin-registered hooks such as the canvas sync handlers).
        """
        class _CallList(list):
            pass

        calls = _CallList()

        def subscriber(**kwargs):
            calls.append(kwargs)

        with _hook_lock:
            _hook_registry[hook_name].append(subscriber)
        calls._subscriber = subscriber
        return calls

    def _cleanup_hook(self, hook_name, calls=None):
        with _hook_lock:
            subscriber = getattr(calls, "_subscriber", None) if calls is not None else None
            if subscriber is not None:
                _hook_registry[hook_name] = [
                    fn for fn in _hook_registry.get(hook_name, [])
                    if fn is not subscriber
                ]
            else:
                _hook_registry.pop(hook_name, None)

    def test_after_page_create_hook(self, logged_in_admin, admin_user):
        """Creating a page should emit after_page_create."""
        cat_id = db.create_category("Test")
        calls = self._register_hook("after_page_create")
        try:
            resp = logged_in_admin.post("/create-page", data={
                "title": "Hook Test Page",
                "content": "Content here",
                "category_id": cat_id,
            })
            assert resp.status_code == 302
            assert len(calls) == 1
            assert calls[0]["page"]["title"] == "Hook Test Page"
            assert calls[0]["user"]["id"] == admin_user
        finally:
            self._cleanup_hook("after_page_create", calls)

    def test_after_page_update_hook(self, logged_in_admin, admin_user):
        """Editing a page should emit after_page_update."""
        cat_id = db.create_category("Test")
        db.create_page("Edit Hook", "edit-hook", "Original", cat_id, admin_user)
        calls = self._register_hook("after_page_update")
        try:
            resp = logged_in_admin.post("/page/edit-hook/edit", data={
                "title": "Edit Hook",
                "content": "Updated content",
                "category_id": cat_id,
            })
            assert resp.status_code == 302
            assert len(calls) == 1
            assert calls[0]["page"]["slug"] == "edit-hook"
        finally:
            self._cleanup_hook("after_page_update", calls)

    def test_after_page_delete_hook(self, logged_in_admin, admin_user):
        """Deleting a page should emit after_page_delete."""
        cat_id = db.create_category("Test")
        db.create_page("Delete Hook", "delete-hook", "Content", cat_id, admin_user)
        calls = self._register_hook("after_page_delete")
        try:
            resp = logged_in_admin.post("/page/delete-hook/delete")
            assert resp.status_code == 302
            assert len(calls) == 1
            assert calls[0]["page"]["slug"] == "delete-hook"
        finally:
            self._cleanup_hook("after_page_delete", calls)

    def test_after_login_hook(self, client, admin_user):
        """Successful login should emit after_login."""
        calls = self._register_hook("after_login")
        try:
            resp = client.post("/login", data={
                "username": "admin",
                "password": "admin123",
            })
            assert resp.status_code == 302
            assert len(calls) == 1
            assert calls[0]["user"]["username"] == "admin"
        finally:
            self._cleanup_hook("after_login", calls)

    def test_after_user_create_hook(self, logged_in_admin):
        """Admin creating a user should emit after_user_create."""
        calls = self._register_hook("after_user_create")
        try:
            resp = logged_in_admin.post("/admin/users/create", data={
                "username": "hooktest",
                "password": "hook12345",
                "confirm_password": "hook12345",
                "role": "user",
            })
            assert resp.status_code == 302
            assert len(calls) == 1
            assert calls[0]["user"]["username"] == "hooktest"
        finally:
            self._cleanup_hook("after_user_create", calls)


class TestRenderSlotJinjaGlobal:
    """Verify that render_slot is available in templates."""

    def test_render_slot_in_page_template(self, logged_in_admin, admin_user):
        """Page template should include render_slot call (no crash even without plugins)."""
        cat_id = db.create_category("Test")
        db.create_page("Slot Test", "slot-test", "Hello world", cat_id, admin_user)
        resp = logged_in_admin.get("/page/slot-test")
        assert resp.status_code == 200
        assert b"Hello world" in resp.data

    def test_render_slot_in_base_sidebar(self, logged_in_admin):
        """Base template sidebar should include render_slot call (no crash)."""
        resp = logged_in_admin.get("/")
        assert resp.status_code == 200


class TestErrorHandlers:
    """Test HTTP 400, 405 error handlers and DB-resilient 403/404."""

    def test_400_template_renders(self, admin_user):
        """The 400 template should render without errors."""
        from app import app
        with app.test_request_context():
            from flask import render_template
            html = render_template("wiki/400.html", categories=[], uncategorized=[])
            assert "400" in html
            assert "Bad Request" in html

    def test_405_template_renders(self, admin_user):
        """The 405 template should render without errors."""
        from app import app
        with app.test_request_context():
            from flask import render_template
            html = render_template("wiki/405.html", categories=[], uncategorized=[])
            assert "405" in html
            assert "Method Not Allowed" in html

    def test_405_on_get_to_post_only_route(self, client, admin_user):
        """GET to a POST-only route should return styled 405, not raw Werkzeug."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.get("/logout")
        assert resp.status_code == 405
        assert b"405" in resp.data
        assert b"Method Not Allowed" in resp.data

    def test_404_renders_styled_page(self, client, admin_user):
        """404 should render a styled page."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.get("/nonexistent-page-xyz-123")
        assert resp.status_code == 404
        assert b"404" in resp.data

    def test_404_handler_survives_db_failure(self, client, admin_user, monkeypatch):
        """404 handler should not crash if DB is unavailable."""
        client.post("/login", data={"username": "admin", "password": "admin123"})

        original_get_category_tree = db.get_category_tree
        call_count = 0

        def conditionally_broken_category_tree(**kwargs):
            nonlocal call_count
            call_count += 1
            # Let the first few calls through (inject_globals), fail on later ones
            if call_count > 2:
                raise RuntimeError("DB gone")
            return original_get_category_tree(**kwargs)

        monkeypatch.setattr(db, "get_category_tree", conditionally_broken_category_tree)
        resp = client.get("/nonexistent-page-xyz-123")
        # Should not crash with 500, should still return 404
        assert resp.status_code in (404, 200)  # 200 if custom page plugin catches it

    def test_405_handler_survives_db_failure(self, client, admin_user, monkeypatch):
        """405 handler should not crash if DB is unavailable."""
        client.post("/login", data={"username": "admin", "password": "admin123"})

        original_get_category_tree = db.get_category_tree
        call_count = 0

        def conditionally_broken_category_tree(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise RuntimeError("DB gone")
            return original_get_category_tree(**kwargs)

        monkeypatch.setattr(db, "get_category_tree", conditionally_broken_category_tree)
        resp = client.get("/logout")
        assert resp.status_code == 405
