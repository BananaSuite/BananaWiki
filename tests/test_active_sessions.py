"""Active login-session management for standalone BananaWiki."""

import hashlib

import db
import sync


def test_removed_sync_hooks_remain_importable():
    """The application imports these compatibility hooks during startup."""
    assert sync.backup_chats_before_cleanup() is None
    assert sync.backup_group_chats_before_cleanup() is None
    assert sync.cleanup_stale_upload_msg_store() == 0


def _login(client, username="admin", password="admin123", user_agent=None):
    headers = {"User-Agent": user_agent} if user_agent else None
    return client.post(
        "/login",
        data={"username": username, "password": password},
        headers=headers,
    )


def _session_token(client):
    with client.session_transaction() as flask_session:
        return flask_session.get("auth_session_token")


def test_login_persists_only_hashed_session_token(client, admin_user):
    _login(
        client,
        user_agent="Mozilla/5.0 Chrome/120.0 Macintosh",
    )
    raw_token = _session_token(client)
    assert raw_token

    row = db.get_user_session(raw_token)
    assert row is not None
    assert row["user_id"] == admin_user
    assert row["user_agent"] == "Mozilla/5.0 Chrome/120.0 Macintosh"

    with db.get_db_context() as conn:
        stored = conn.execute(
            "SELECT token_hash FROM user_sessions WHERE id=?", (row["id"],)
        ).fetchone()["token_hash"]
    assert stored == hashlib.sha256(raw_token.encode()).hexdigest()
    assert stored != raw_token


def test_settings_lists_and_revokes_another_session(client, admin_user):
    from app import app

    _login(client, user_agent="Firefox/120.0 Linux")
    other = app.test_client()
    _login(other, user_agent="Chrome/120.0 Windows")

    other_id = db.get_user_session_id(_session_token(other))
    page = client.get("/settings")
    assert page.status_code == 200
    assert b"Active sessions" in page.data
    assert b"Firefox on Linux" in page.data
    assert b"Chrome on Windows" in page.data

    response = client.post(f"/settings/sessions/{other_id}/revoke")
    assert response.status_code == 302
    assert client.get("/settings").status_code == 200

    rejected = other.get("/settings")
    assert rejected.status_code == 302
    assert "/login" in rejected.headers["Location"]


def test_session_history_can_be_cleared_without_revoking_active_session(
    client, admin_user
):
    _login(client, user_agent="Firefox/120.0 Linux")
    ended_id, _ended_token = db.create_user_session(
        admin_user,
        ip_address="203.0.113.8",
        user_agent="Chrome/120.0 Windows",
    )
    assert db.revoke_user_session(admin_user, ended_id)
    expired_id, _expired_token = db.create_user_session(
        admin_user, user_agent="Safari/17.0 Macintosh"
    )
    with db.get_db_context() as conn:
        conn.execute(
            "UPDATE user_sessions SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", expired_id),
        )
        conn.commit()
    assert len(db.list_user_session_history(admin_user)) == 2

    page = client.get("/settings")
    assert page.status_code == 200
    assert b"Recent session history" in page.data
    assert b"Chrome on Windows" in page.data
    assert b"Ended" in page.data
    assert b"Expired" in page.data

    response = client.post("/settings/sessions/history/clear")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings#session-history")
    assert db.list_user_session_history(admin_user) == []
    assert len(db.list_active_user_sessions(admin_user)) == 1
    assert client.get("/settings").status_code == 200


def test_session_history_clear_is_user_scoped(client, admin_user, regular_user):
    _login(client)
    own_id, _own_token = db.create_user_session(admin_user)
    foreign_id, _foreign_token = db.create_user_session(regular_user)
    db.revoke_user_session(admin_user, own_id)
    db.revoke_user_session(regular_user, foreign_id)

    client.post("/settings/sessions/history/clear")

    assert db.list_user_session_history(admin_user) == []
    assert len(db.list_user_session_history(regular_user)) == 1


def test_revoking_current_session_logs_out(client, admin_user):
    _login(client)
    current_id = db.get_user_session_id(_session_token(client))
    response = client.post(f"/settings/sessions/{current_id}/revoke")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    with client.session_transaction() as flask_session:
        assert "user_id" not in flask_session


def test_logout_everywhere_invalidates_all_clients(client, admin_user):
    from app import app

    _login(client)
    other = app.test_client()
    _login(other)
    assert len(db.list_active_user_sessions(admin_user)) == 2

    response = client.post("/settings/sessions/logout-all")
    assert response.status_code == 302
    assert db.list_active_user_sessions(admin_user) == []

    rejected = other.get("/settings")
    assert rejected.status_code == 302
    assert "/login" in rejected.headers["Location"]


def test_cannot_revoke_another_users_session(client, admin_user, regular_user):
    _login(client)
    foreign_id, _token = db.create_user_session(regular_user)
    response = client.post(f"/settings/sessions/{foreign_id}/revoke")
    assert response.status_code == 404
    assert db.list_active_user_sessions(regular_user)


def test_password_change_preserves_current_and_revokes_other_sessions(
    client, admin_user
):
    from app import app

    _login(client)
    other = app.test_client()
    _login(other)

    response = client.post(
        "/settings",
        data={
            "action": "change_password",
            "current_password": "admin123",
            "new_password": "newpassword123",
            "confirm_password": "newpassword123",
        },
    )
    assert response.status_code == 302
    assert len(db.list_active_user_sessions(admin_user)) == 1
    assert client.get("/settings").status_code == 200
    assert other.get("/settings").status_code == 302


def test_admin_mass_logout_revokes_managed_sessions_but_preserves_actor(
    client, admin_user
):
    from app import app

    _login(client)
    other = app.test_client()
    _login(other)

    response = client.post("/admin/mass-logout")
    assert response.status_code == 302
    assert len(db.list_active_user_sessions(admin_user)) == 1
    assert client.get("/settings").status_code == 200
    assert other.get("/settings").status_code == 302


def test_site_import_clears_authentication_state(client, admin_user):
    from db._migration import _apply_import

    _login(client)
    assert db.list_active_user_sessions(admin_user)
    with db.get_db_context() as conn:
        _apply_import(conn, {}, "keep")
        conn.commit()
    assert db.list_active_user_sessions(admin_user) == []
    assert db.get_user_by_id(admin_user)["session_token"] is None
