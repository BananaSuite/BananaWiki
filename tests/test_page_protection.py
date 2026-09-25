"""Tests for page protection ownership and admin unlock cooldown behavior."""

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

import db


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def _create_editor(username, password="editor123"):
    return db.create_user(username, generate_password_hash(password), role="editor")


def _create_admin(username, password="admin123"):
    return db.create_user(username, generate_password_hash(password), role="admin")


def test_page_protection_disabled_by_default(admin_user):
    settings = db.get_site_settings()
    assert settings["page_protection_enabled"] == 0


def test_only_protector_can_edit_when_enabled(client, admin_user):
    db.update_site_settings(setup_done=1, page_protection_enabled=1)
    _create_editor("owner")
    _create_editor("other")
    _login(client, "owner", "editor123")
    page_id = db.create_page("Protected Page", "protected-page", "Initial", user_id=admin_user)
    db.set_page_protection(page_id, db.get_user_by_username("owner")["id"])

    _login(client, "other", "editor123")
    resp = client.get("/page/protected-page/edit", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Only the user who protected this page can edit it" in resp.data

    _login(client, "owner", "editor123")
    ok = client.post(
        "/page/protected-page/edit",
        data={"title": "Protected Page", "content": "Owner edit"},
        follow_redirects=True,
    )
    assert ok.status_code == 200
    updated = db.get_page_by_slug("protected-page")
    assert updated["content"] == "Owner edit"


def test_page_view_shows_protection_controls_when_enabled(client):
    db.update_site_settings(setup_done=1, page_protection_enabled=1)
    editor_user = _create_editor("editor3")
    _login(client, "editor3", "editor123")
    page_id = db.create_page("Page", "page", "Body", user_id=editor_user)
    db.set_page_protection(page_id, editor_user)
    resp = client.get("/page/page")
    assert resp.status_code == 200
    assert b"Unprotect" in resp.data
    assert b"Only you can edit it" in resp.data


def test_admin_owner_can_remove_protection_immediately(client):
    owner_admin_id = _create_admin("owner_admin")
    db.update_site_settings(setup_done=1, page_protection_enabled=1)
    page_id = db.create_page("Owned", "owned", "Body", user_id=owner_admin_id)
    db.set_page_protection(page_id, owner_admin_id)
    _login(client, "owner_admin", "admin123")
    resp = client.post(
        f"/global-settings/page-protection/{page_id}/request-unlock",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Page protection has been successfully removed." in resp.data
    assert db.get_page(page_id)["protected_by"] is None


def test_non_owner_admin_must_wait_72h_before_force_unlock(client, admin_user):
    db.update_site_settings(setup_done=1, page_protection_enabled=1)
    owner_id = _create_editor("owner2")
    second_admin_id = _create_admin("second_admin")
    page_id = db.create_page("Timed", "timed", "Body", user_id=admin_user)
    db.set_page_protection(page_id, owner_id)

    _login(client, "second_admin", "admin123")
    req = client.post(
        f"/global-settings/page-protection/{page_id}/request-unlock",
        follow_redirects=True,
    )
    assert req.status_code == 200
    page = db.get_page(page_id)
    assert page["protection_unlock_requested_by"] == second_admin_id

    too_soon = client.post(
        f"/global-settings/page-protection/{page_id}/force-unlock",
        follow_redirects=True,
    )
    assert b"Please wait for the 72-hour cooldown" in too_soon.data
    assert db.get_page(page_id)["protected_by"] == owner_id

    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=db.PAGE_PROTECTION_ADMIN_UNLOCK_DELAY_SECONDS + 3600)
    ).isoformat()
    conn = db.get_db()
    conn.execute(
        "UPDATE pages SET protection_unlock_requested_at=?, protection_unlock_requested_by=? WHERE id=?",
        (old, second_admin_id, page_id),
    )
    conn.commit()
    conn.close()

    unlocked = client.post(
        f"/global-settings/page-protection/{page_id}/force-unlock",
        follow_redirects=True,
    )
    assert unlocked.status_code == 200
    assert b"Page protection has been successfully removed." in unlocked.data
    assert db.get_page(page_id)["protected_by"] is None
