"""Deletion review must retain the viewer's category boundary."""

import db
from helpers import user_can_view_page


def test_pending_page_does_not_bypass_category_read_access(admin_user, editor_user, logged_in_editor):
    allowed = db.create_category("Allowed")
    private = db.create_category("Restricted")
    db.set_user_permissions(
        editor_user, {"page.delete", "page.view_deindexed"},
        read_restricted=True, read_category_ids=[allowed],
        write_restricted=True, write_category_ids=[allowed],
    )
    page_id = db.create_page("Restricted pending page", "restricted-pending", "Private review content", private, admin_user)
    with db.get_db_context() as connection:
        connection.execute("UPDATE pages SET pending_deletion=1 WHERE id=?", (page_id,))
        connection.commit()
    page = db.get_page_by_slug("restricted-pending")
    user = db.get_user_by_id(editor_user)
    assert db.has_permission(user, "page.delete")
    assert not user_can_view_page(user, page)
    response = logged_in_editor.get("/page/restricted-pending")
    assert response.status_code == 403
    assert b"Private review content" not in response.data


def test_pending_page_review_remains_available_in_allowed_category(admin_user, editor_user, logged_in_editor):
    allowed = db.create_category("Allowed")
    db.set_user_permissions(
        editor_user, {"page.delete"},
        read_restricted=True, read_category_ids=[allowed],
        write_restricted=True, write_category_ids=[allowed],
    )
    page_id = db.create_page("Review page", "allowed-pending", "Allowed review content", allowed, admin_user)
    with db.get_db_context() as connection:
        connection.execute("UPDATE pages SET pending_deletion=1 WHERE id=?", (page_id,))
        connection.commit()
    response = logged_in_editor.get("/page/allowed-pending")
    assert response.status_code == 200
    assert b"Allowed review content" in response.data
