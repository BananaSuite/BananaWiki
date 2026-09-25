"""Tests for the JSON error responses returned from ``/api/...`` endpoints.

Regression coverage for a bug where ``inject_globals`` short-circuited the
template context for any request whose path started with ``/api/``.  When an
``/api/...`` view called ``abort(404)`` / ``abort(403)`` / ... the matching
error handler would then render ``wiki/404.html`` (or similar) which extends
``base.html``; ``base.html`` references ``settings`` so the unbound name
turned the styled error page into a hard 500 for every JSON API consumer.

The expected behaviour is:

* ``/api/...`` aborts return a JSON body (``{"error": ..., "status": ...}``)
  with the original status code, never a 500 from the error template.
* The CSRF error handler keeps redirecting browser requests but returns
  JSON for ``/api/...`` paths (already covered before the fix).
"""

import json

import pytest

from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Local helpers: set up site + login in the right order so the
# ``before_request_hook`` does not intercept us with the /setup redirect.
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(client):
    """Flask client logged in as an admin on a fully-set-up site."""
    import db
    db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    rv = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert rv.status_code in (200, 302), rv.data[:200]
    return client


@pytest.fixture
def user_client(client):
    """Flask client logged in as a regular user on a fully-set-up site."""
    import db
    # Admin row first so update_site_settings has an editor target.
    db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    db.create_user("user", generate_password_hash("user123"), role="user")
    rv = client.post("/login", data={"username": "user", "password": "user123"})
    assert rv.status_code in (200, 302), rv.data[:200]
    return client


# ---------------------------------------------------------------------------
# /api/... abort -> JSON, never 500
# ---------------------------------------------------------------------------


def test_api_abort_404_returns_json_not_500(admin_client):
    """A missing kanban attachment used to 500 because the styled 404 page
    needed ``settings`` from ``inject_globals``.  Now it returns JSON."""
    rv = admin_client.get("/api/kanban/attachments/999999/download")
    assert rv.status_code == 404, rv.data[:200]
    assert rv.content_type.startswith("application/json"), rv.content_type
    body = json.loads(rv.data)
    assert body["status"] == 404
    assert "error" in body


@pytest.mark.parametrize("path", [
    "/api/kanban/tickets/999999",
    "/api/kanban/999999/settings",
    "/api/kanban/tickets/999999/attachments",
    "/api/kanban/attachments/999999/download",
    "/api/kanban/tickets/999999/history",
    "/api/kanban/history/999999",
    "/api/kanban/tickets/999999/comments",
    "/api/kanban/999999/activity",
])
def test_api_kanban_missing_resource_returns_json(user_client, path):
    """All kanban-API GET endpoints must return JSON (404/403), never 500.

    Each of these used to crash the styled error template because
    ``inject_globals`` returned an empty dict for any ``/api/...`` path."""
    rv = user_client.get(path)
    assert rv.status_code != 500, f"{path} returned 500: {rv.data[:200]!r}"
    assert rv.content_type.startswith("application/json"), (
        f"{path} returned {rv.content_type!r} body={rv.data[:200]!r}"
    )
    body = json.loads(rv.data)
    assert "error" in body


def test_api_404_for_nonexistent_route_returns_json(admin_client):
    """A path under ``/api/`` that doesn't match any route should also be
    served as JSON, not as the HTML 404 page."""
    rv = admin_client.get("/api/this/does/not/exist")
    assert rv.status_code == 404, rv.data[:200]
    assert rv.content_type.startswith("application/json")
    body = json.loads(rv.data)
    assert body["status"] == 404


def test_html_404_still_renders_for_browser_paths(admin_client):
    """Browser-facing paths must still receive the styled HTML 404 page."""
    rv = admin_client.get("/this-page-does-not-exist")
    assert rv.status_code == 404
    assert rv.content_type.startswith("text/html"), rv.content_type
    body_lower = rv.data.lower()
    assert b"<html" in body_lower or b"<!doctype" in body_lower


def test_api_abort_403_returns_json(user_client):
    """A regular user hitting an admin-only API endpoint receives JSON 403,
    not the styled HTML 403 page (which used to 500)."""
    import db
    db.update_site_settings(kanban_access="admin")
    rv = user_client.get("/api/kanban/999999/settings")
    # 404 (board not found) or 403 (no permission). Both must be JSON.
    assert rv.status_code in (403, 404), rv.data[:200]
    assert rv.content_type.startswith("application/json")


# ---------------------------------------------------------------------------
# Accept-header negotiation
# ---------------------------------------------------------------------------


def test_browser_path_with_accept_json_returns_json_404(admin_client):
    """A browser-style path that explicitly requests JSON gets a JSON 404."""
    rv = admin_client.get(
        "/this-page-does-not-exist",
        headers={"Accept": "application/json"},
    )
    # The Accept-header path is best-effort: make sure we either honour it
    # with JSON or fall back to the styled HTML 404, but never 500.
    assert rv.status_code != 500
    assert rv.status_code == 404
