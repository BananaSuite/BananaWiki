"""Suspended-user self-deletion policy tests."""

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash


def _create_suspended_user(username="suspended-user", password="password123"):
    import db

    user_id = db.create_user(username, generate_password_hash(password), role="user")
    db.update_user(user_id, suspended=1)
    return user_id


def _login(client, username="suspended-user", password="password123"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def test_suspended_account_deletion_is_disabled_by_default(admin_user):
    import db

    assert db.get_site_settings()["suspended_account_deletion_enabled"] == 0


def test_admin_can_toggle_suspended_account_deletion(logged_in_admin):
    import db

    response = logged_in_admin.post(
        "/global-settings",
        data={
            "site_name": "BananaWiki",
            "suspended_account_deletion_enabled": "1",
        },
    )
    assert response.status_code == 302
    assert db.get_site_settings()["suspended_account_deletion_enabled"] == 1

    response = logged_in_admin.post(
        "/global-settings",
        data={"site_name": "BananaWiki"},
    )
    assert response.status_code == 302
    assert db.get_site_settings()["suspended_account_deletion_enabled"] == 0


def test_disabled_policy_hides_form_and_blocks_direct_post(client, admin_user):
    import db

    user_id = _create_suspended_user()
    response = _login(client)
    assert response.status_code == 403
    assert b"/account-suspended/delete" not in response.data

    response = client.post(
        "/account-suspended/delete",
        data={"password": "password123"},
    )
    assert response.status_code == 403
    assert db.get_user_by_id(user_id) is not None


def test_delete_endpoint_rejects_anonymous_and_active_users(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    response = client.post(
        "/account-suspended/delete",
        data={"password": "password123"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")

    user_id = db.create_user(
        "active-user",
        generate_password_hash("password123"),
        role="user",
    )
    client.post(
        "/login",
        data={"username": "active-user", "password": "password123"},
    )
    response = client.post(
        "/account-suspended/delete",
        data={"password": "password123"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")
    assert db.get_user_by_id(user_id) is not None


def test_suspended_delete_endpoint_requires_csrf(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    user_id = _create_suspended_user()
    _login(client)

    client.application.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            "/account-suspended/delete",
            data={"password": "password123"},
            follow_redirects=True,
        )
    finally:
        client.application.config["WTF_CSRF_ENABLED"] = False

    assert response.status_code == 403
    assert b"session has expired" in response.data
    assert db.get_user_by_id(user_id) is not None


def test_enabled_policy_shows_form_and_rejects_wrong_password(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    user_id = _create_suspended_user()
    response = _login(client)
    assert response.status_code == 403
    assert b"/account-suspended/delete" in response.data

    response = client.post(
        "/account-suspended/delete",
        data={"password": "wrong-password"},
        follow_redirects=True,
    )
    assert response.status_code == 403
    assert b"Incorrect password" in response.data
    assert db.get_user_by_id(user_id) is not None


def test_enabled_policy_allows_suspended_user_to_delete_account(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    user_id = _create_suspended_user()
    _login(client)

    response = client.post(
        "/account-suspended/delete",
        data={"password": "password123"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Your account has been deleted" in response.data
    assert db.get_user_by_id(user_id) is None


def test_suspended_last_admin_cannot_delete_account(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    db.update_user(admin_user, suspended=1)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )
    assert response.status_code == 403

    response = client.post(
        "/account-suspended/delete",
        data={"password": "admin123"},
        follow_redirects=True,
    )
    assert response.status_code == 403
    assert b"Cannot delete the last admin account" in response.data
    assert db.get_user_by_id(admin_user) is not None


def test_suspended_admin_can_delete_when_an_active_admin_remains(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    user_id = db.create_user(
        "suspended-admin",
        generate_password_hash("password123"),
        role="admin",
    )
    db.update_user(user_id, suspended=1)
    _login(client, username="suspended-admin")

    response = client.post(
        "/account-suspended/delete",
        data={"password": "password123"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert db.get_user_by_id(user_id) is None
    assert db.get_user_by_id(admin_user) is not None


def test_suspended_superuser_cannot_delete_account(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    user_id = _create_suspended_user(username="protected-user")
    db.update_user(user_id, is_superuser=1)
    response = _login(client, username="protected-user")
    assert b"/account-suspended/delete" not in response.data

    response = client.post(
        "/account-suspended/delete",
        data={"password": "password123"},
        follow_redirects=True,
    )
    assert response.status_code == 403
    assert b"protected and cannot be deleted" in response.data
    assert db.get_user_by_id(user_id) is not None


def test_expired_suspension_uses_normal_account_flow(client, admin_user):
    import db

    db.update_site_settings(suspended_account_deletion_enabled=1)
    user_id = _create_suspended_user()
    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.update_user(user_id, suspended_until=expired_at)
    _login(client)

    response = client.post(
        "/account-suspended/delete",
        data={"password": "password123"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")
    assert db.get_user_by_id(user_id) is not None
