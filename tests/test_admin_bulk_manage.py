"""Smoke tests for the unified Admin → Bulk Manage page (``/admin/bulk``)."""

import db


def _login_admin(client):
    client.post("/login", data={"username": "admin", "password": "admin123"})


def test_admin_bulk_manage_renders_for_admin(client, admin_user):
    _login_admin(client)
    response = client.get("/admin/bulk")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # Each of the four bulk sections renders by section anchor id.
    for anchor in ("bulk-categories", "bulk-pages", "bulk-canvases", "bulk-kanban"):
        assert anchor in body


def test_admin_bulk_manage_blocks_non_admin(client, admin_user, regular_user):
    # admin_user marks setup done; without that fixture this request would
    # bounce at the global /setup redirect and never reach admin_required.
    client.post("/login", data={"username": "user", "password": "user123"})
    response = client.get("/admin/bulk", follow_redirects=False)
    # admin_required flashes an error and sends non-admins home.
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_admin_bulk_delete_categories_removes_only_selected(client, admin_user):
    keep = db.create_category("keep")
    drop = db.create_category("drop")
    _login_admin(client)

    response = client.post(
        "/admin/bulk/categories/delete",
        data={"ids": [str(drop)]},
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    assert db.get_category(keep) is not None
    assert db.get_category(drop) is None


def test_admin_bulk_delete_pages_skips_home_page(client, admin_user):
    home_page = db.get_home_page()
    home_id = home_page["id"] if home_page else None
    target = db.create_page("Target", "target-page", "to delete")
    _login_admin(client)

    ids = [str(target)]
    if home_id is not None:
        ids.append(str(home_id))
    response = client.post(
        "/admin/bulk/pages/delete",
        data={"ids": ids},
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    if home_id is not None:
        # Home is protected: must still exist after a bulk-delete attempt.
        assert db.get_page(home_id) is not None
    # When the deletion_slowdown plugin is on (auto-enabled in test fixtures),
    # the page is queued rather than hard-deleted: either is acceptable.
    page = db.get_page(target)
    if page is not None:
        assert page["pending_deletion"] == 1


def test_admin_bulk_delete_canvases_removes_selected(client, admin_user):
    keep_id = db.canvas_create_layout(title="keep", creator_id=admin_user)
    drop_id = db.canvas_create_layout(title="drop", creator_id=admin_user)
    _login_admin(client)

    response = client.post(
        "/admin/bulk/canvases/delete",
        data={"ids": [str(drop_id)]},
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    assert db.canvas_get_layout(keep_id) is not None
    assert db.canvas_get_layout(drop_id) is None


def test_admin_bulk_delete_kanban_boards_removes_selected(client, admin_user):
    keep_id = db.kanban_create_board("keep", "", admin_user)
    drop_id = db.kanban_create_board("drop", "", admin_user)
    _login_admin(client)

    response = client.post(
        "/admin/bulk/kanban-boards/delete",
        data={"ids": [str(drop_id)]},
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    assert db.kanban_get_board(keep_id) is not None
    assert db.kanban_get_board(drop_id) is None
