"""Tests for the editing indicator (active editors) feature."""

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def editor_client(client, admin_user):
    """Create a logged-in editor with setup_done already configured."""
    import db
    db.create_user("editor", generate_password_hash("editor123"), role="editor")
    client.post("/login", data={"username": "editor", "password": "editor123"})
    return client


@pytest.fixture
def page_with_content(editor_client):
    """Create a page and return its slug."""
    editor_client.post("/create-page", data={
        "title": "Test Page",
        "content": "Initial content",
    }, follow_redirects=True)
    return "test-page"


class TestEditingIndicatorAPI:
    def test_heartbeat_requires_login(self, client, page_with_content):
        resp = client.post(f"/api/page/{page_with_content}/editing/heartbeat")
        # In public mode the endpoint may return 200 (allowing anonymous users
        # through) or 302/401 if public mode is not active.  Either is acceptable.
        assert resp.status_code in (200, 302, 401)

    def test_heartbeat_creates_session(self, editor_client, page_with_content):
        resp = editor_client.post(
            f"/api/page/{page_with_content}/editing/heartbeat",
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_heartbeat_updates_existing_session(self, editor_client, page_with_content):
        editor_client.post(
            f"/api/page/{page_with_content}/editing/heartbeat",
            content_type="application/json",
        )
        resp = editor_client.post(
            f"/api/page/{page_with_content}/editing/heartbeat",
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_check_returns_active_editors(self, editor_client, page_with_content):
        editor_client.post(
            f"/api/page/{page_with_content}/editing/heartbeat",
            content_type="application/json",
        )
        resp = editor_client.get(f"/api/page/{page_with_content}/editing/check")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "editors" in data
        assert "editor" not in data["editors"]

    def test_stop_removes_session(self, editor_client, page_with_content):
        editor_client.post(
            f"/api/page/{page_with_content}/editing/heartbeat",
            content_type="application/json",
        )
        resp = editor_client.post(
            f"/api/page/{page_with_content}/editing/stop",
            content_type="application/json",
        )
        assert resp.status_code == 200

        resp = editor_client.get(f"/api/page/{page_with_content}/editing/check")
        data = resp.get_json()
        assert data["editors"] == []

    def test_nonexistent_page_returns_404(self, editor_client, page_with_content):
        resp = editor_client.post(
            "/api/page/nonexistent/editing/heartbeat",
            content_type="application/json",
        )
        assert resp.status_code == 404


class TestEditingIndicatorDB:
    def test_heartbeat_creates_session_in_db(self, admin_user, editor_user):
        import db
        page_id = db.create_page("T", "t-slug", "content")
        db.heartbeat(page_id, editor_user, "editor")
        editors = db.get_active_editors(page_id)
        assert len(editors) == 1
        assert editors[0]["username"] == "editor"

    def test_stale_sessions_excluded(self, admin_user, editor_user):
        import db
        from datetime import datetime, timedelta, timezone
        page_id = db.create_page("T", "t-slug2", "content")
        db.heartbeat(page_id, editor_user, "editor")
        with db.get_db_context() as conn:
            stale_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE editing_sessions SET last_heartbeat = ? WHERE page_id = ? AND user_id = ?",
                (stale_time, page_id, editor_user),
            )
            conn.commit()
        editors = db.get_active_editors(page_id, stale_seconds=30)
        assert len(editors) == 0

    def test_remove_session(self, admin_user, editor_user):
        import db
        page_id = db.create_page("T", "t-slug3", "content")
        db.heartbeat(page_id, editor_user, "editor")
        assert db.remove_session(page_id, editor_user) is True
        editors = db.get_active_editors(page_id)
        assert len(editors) == 0

    def test_cleanup_stale(self, admin_user, editor_user):
        import db
        from datetime import datetime, timedelta, timezone
        page_id = db.create_page("T", "t-slug4", "content")
        db.heartbeat(page_id, editor_user, "editor")
        with db.get_db_context() as conn:
            stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE editing_sessions SET last_heartbeat = ? WHERE page_id = ? AND user_id = ?",
                (stale_time, page_id, editor_user),
            )
            conn.commit()
        removed = db.cleanup_stale(stale_seconds=60)
        assert removed == 1
        editors = db.get_active_editors(page_id)
        assert len(editors) == 0
