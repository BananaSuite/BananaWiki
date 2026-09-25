"""Regression tests for the designated wiki home page."""

import sqlite3

import pytest


def test_set_home_button_uses_translation(logged_in_admin, admin_user):
    import db

    db.create_page("Landing", "landing", "content", None, admin_user)

    resp = logged_in_admin.get("/page/landing")
    assert resp.status_code == 200
    assert b"Set as Home" in resp.data


def test_set_home_switches_root_without_changing_slug(logged_in_admin, admin_user):
    import db

    page_id = db.create_page("Landing", "landing", "# Landing", None, admin_user)

    resp = logged_in_admin.post("/page/landing/set-home", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_home_page()["id"] == page_id
    assert db.get_page_by_slug("landing")["is_home"] == 1

    root = logged_in_admin.get("/")
    assert root.status_code == 200
    assert b"Landing" in root.data


def test_set_home_reindexes_and_restores_selected_page(logged_in_admin, admin_user):
    import db

    page_id = db.create_page("Hidden Landing", "hidden-landing", "content", None, admin_user)
    assert db.set_page_deindexed(page_id, True) is True
    assert db.mark_page_pending_deletion(page_id, admin_user) is True

    resp = logged_in_admin.post("/page/hidden-landing/set-home", follow_redirects=True)
    assert resp.status_code == 200

    page = db.get_home_page()
    assert page["id"] == page_id
    assert page["is_deindexed"] == 0
    assert page["pending_deletion"] == 0


def test_set_home_clears_temporary_schedules(logged_in_admin, admin_user):
    import db

    page_id = db.create_page("Temporary Landing", "temporary-landing", "content", None, admin_user)
    db.set_page_expiry(page_id, "2099-01-01T00:00:00", set_by=admin_user)
    db.set_page_temp_index_state(page_id, 1, 0, "2099-01-02T00:00:00", set_by=admin_user)

    assert db.get_page_expiry(page_id) is not None
    assert db.get_page_temp_index_state(page_id) is not None

    resp = logged_in_admin.post("/page/temporary-landing/set-home", follow_redirects=True)
    assert resp.status_code == 200

    page = db.get_home_page()
    assert page["id"] == page_id
    assert page["is_deindexed"] == 0
    assert db.get_page_expiry(page_id) is None
    assert db.get_page_temp_index_state(page_id) is None


def test_set_home_clears_protection_reservation_and_cooldown(logged_in_admin, admin_user, editor_user):
    import db

    page_id = db.create_page("Governed Landing", "governed-landing", "content", None, admin_user)
    db.update_site_settings(page_reservations_enabled=1)
    assert db.set_page_protection(page_id, editor_user) is True
    assert db.reserve_page(page_id, editor_user)
    with db.get_db_context() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_page_cooldowns (page_id, user_id, cooldown_until) "
            "VALUES (?, ?, ?)",
            (page_id, editor_user, "2099-01-01T00:00:00"),
        )
        conn.commit()

    resp = logged_in_admin.post("/page/governed-landing/set-home", follow_redirects=True)
    assert resp.status_code == 200

    page = db.get_home_page()
    assert page["id"] == page_id
    assert page["protected_by"] is None
    assert db.get_page_reservation_status(page_id, editor_user)["is_reserved"] is False
    with db.get_db_context() as conn:
        reservation_count = conn.execute(
            "SELECT COUNT(*) FROM page_reservations WHERE page_id=?", (page_id,)
        ).fetchone()[0]
        cooldown_count = conn.execute(
            "SELECT COUNT(*) FROM user_page_cooldowns WHERE page_id=?", (page_id,)
        ).fetchone()[0]
    assert reservation_count == 0
    assert cooldown_count == 0


def test_set_home_api_requires_admin_json(client, admin_user, editor_user):
    import db

    page_id = db.create_page("API Landing", "api-landing", "content", None, admin_user)
    client.post("/login", data={"username": "editor", "password": "editor123"})

    resp = client.post(f"/api/pages/{page_id}/home")

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "Only administrators can set the home page"


def test_set_home_api_normalizes_already_home_page(logged_in_admin, admin_user):
    import db

    home = db.get_home_page()
    with db.get_db_context() as conn:
        conn.execute(
            "UPDATE pages SET is_deindexed=1, pending_deletion=1, protected_by=? WHERE id=?",
            (admin_user, home["id"]),
        )
        conn.execute(
            "INSERT INTO temp_pages (page_id, expires_at) VALUES (?, ?)",
            (home["id"], "2099-01-01T00:00:00"),
        )
        conn.commit()

    resp = logged_in_admin.post(f"/api/pages/{home['id']}/home")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["already_home"] is True
    assert data["home_page"]["is_deindexed"] is False
    page = db.get_home_page()
    assert page["is_deindexed"] == 0
    assert page["pending_deletion"] == 0
    assert page["protected_by"] is None
    assert db.get_page_expiry(home["id"]) is None


def test_schema_enforces_single_home_page(admin_user):
    import db

    page_id = db.create_page("Second Home", "second-home", "content", None, admin_user)

    with db.get_db_context() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE pages SET is_home=1 WHERE id=?", (page_id,))


def test_home_page_cannot_be_moved(logged_in_admin):
    import db

    home = db.get_home_page()
    cat_id = db.create_category("Target")

    resp = logged_in_admin.post(
        f"/page/{home['slug']}/move",
        data={"category_id": str(cat_id)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Cannot move the home page" in resp.data
    assert db.get_home_page()["category_id"] == home["category_id"]


def test_db_refuses_to_deindex_home_page():
    import db

    home = db.get_home_page()

    assert db.set_page_deindexed(home["id"], True) is False
    assert db.get_home_page()["is_deindexed"] == 0


def test_db_refuses_home_page_structural_changes(admin_user):
    import db

    home = db.get_home_page()
    cat_id = db.create_category("Blocked Target")

    assert db.update_page_slug(home["id"], "renamed-home") is None
    assert db.update_page_category(home["id"], cat_id) is False
    assert db.set_page_protection(home["id"], admin_user) is False

    unchanged = db.get_home_page()
    assert unchanged["slug"] == home["slug"]
    assert unchanged["category_id"] == home["category_id"]
    assert unchanged["protected_by"] is None


def test_db_refuses_home_page_reservation(admin_user):
    import db

    db.update_site_settings(page_reservations_enabled=1)
    home = db.get_home_page()

    can_reserve, reason = db.can_user_reserve_page(home["id"], admin_user)
    assert can_reserve is False
    assert reason == "The home page cannot be reserved"

    try:
        db.reserve_page(home["id"], admin_user)
    except ValueError as exc:
        assert str(exc) == "The home page cannot be reserved"
    else:
        raise AssertionError("home page reservation should fail")


def test_home_edit_form_hides_category_and_reservation_controls(logged_in_admin):
    import db

    db.update_site_settings(page_reservations_enabled=1)
    home = db.get_home_page()

    resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
    assert resp.status_code == 200
    assert b'name="category_id"' not in resp.data
    assert b'name="reserve_after_commit"' not in resp.data


def test_home_page_reservation_api_blocked(logged_in_admin):
    import db

    db.update_site_settings(page_reservations_enabled=1)
    home = db.get_home_page()

    resp = logged_in_admin.post(f"/api/pages/{home['id']}/reservation")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "The home page cannot be reserved"
