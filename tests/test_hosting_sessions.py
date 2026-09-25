"""Active login-session management for the hosting portal."""

import hashlib

import pytest

from hosting import config as hosting_config
from hosting.app import create_hosting_app
from hosting.db import (
    create_account,
    create_hosting_session,
    get_account_by_username,
    get_hosting_db_context,
    get_hosting_session,
    get_hosting_session_id,
    init_hosting_db,
    list_active_hosting_sessions,
    list_hosting_session_history,
    revoke_hosting_session,
)


@pytest.fixture(autouse=True)
def isolated_hosting_db(tmp_path):
    hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
    hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
    init_hosting_db()


@pytest.fixture
def app():
    application = create_hosting_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application


def _signup(client):
    return client.post(
        "/signup",
        data={
            "username": "sessionuser",
            "password": "password123",
            "confirm_password": "password123",
        },
    )


def _login(client, user_agent="Mozilla/5.0 Firefox/120.0 Linux"):
    return client.post(
        "/login",
        data={"username": "sessionuser", "password": "password123"},
        headers={"User-Agent": user_agent},
    )


def _token(client):
    with client.session_transaction() as flask_session:
        return flask_session.get("hosting_auth_session_token")


def test_hosting_login_stores_hash_and_lists_sessions(app):
    first = app.test_client()
    _signup(first)
    account = get_account_by_username("sessionuser")
    raw_token = _token(first)
    row = get_hosting_session(raw_token)
    assert row["account_id"] == account["id"]

    with get_hosting_db_context() as conn:
        stored = conn.execute(
            "SELECT token_hash FROM hosting_account_sessions WHERE id=?",
            (row["id"],),
        ).fetchone()["token_hash"]
    assert stored == hashlib.sha256(raw_token.encode()).hexdigest()
    assert stored != raw_token

    second = app.test_client()
    _login(second, "Mozilla/5.0 Chrome/120.0 Windows")
    page = first.get("/account")
    assert page.status_code == 200
    assert b"Active sessions" in page.data
    assert b"Chrome on Windows" in page.data
    assert len(list_active_hosting_sessions(account["id"])) == 2


def test_hosting_can_revoke_one_session_without_revoking_current(app):
    first = app.test_client()
    _signup(first)
    second = app.test_client()
    _login(second)
    second_id = get_hosting_session_id(_token(second))

    response = first.post(f"/account/sessions/{second_id}/revoke")
    assert response.status_code == 302
    assert first.get("/account").status_code == 200

    rejected = second.get("/dashboard")
    assert rejected.status_code == 302
    assert "/login" in rejected.headers["Location"]


def test_hosting_session_history_can_be_cleared_without_revoking_active(app):
    client = app.test_client()
    _signup(client)
    account = get_account_by_username("sessionuser")
    ended_id, _ended_token = create_hosting_session(
        account["id"],
        account["session_version"],
        ip_address="203.0.113.9",
        user_agent="Mozilla/5.0 Chrome/120.0 Windows",
    )
    assert revoke_hosting_session(account["id"], ended_id)
    expired_id, _expired_token = create_hosting_session(
        account["id"], account["session_version"],
        user_agent="Mozilla/5.0 Safari/17.0 Macintosh",
    )
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE hosting_account_sessions SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", expired_id),
        )
        conn.commit()
    assert len(list_hosting_session_history(account["id"])) == 2

    page = client.get("/account")
    assert page.status_code == 200
    assert b"Recent session history" in page.data
    assert b"Chrome on Windows" in page.data
    assert b"Ended" in page.data
    assert b"Expired" in page.data

    response = client.post("/account/sessions/history/clear")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account#session-history")
    assert list_hosting_session_history(account["id"]) == []
    assert len(list_active_hosting_sessions(account["id"])) == 1
    assert client.get("/account").status_code == 200


def test_hosting_session_history_clear_is_account_scoped(app):
    client = app.test_client()
    _signup(client)
    account = get_account_by_username("sessionuser")
    foreign_id = create_account("foreignhistory", "unused-hash")
    own_session_id, _own_token = create_hosting_session(account["id"], 0)
    foreign_session_id, _foreign_token = create_hosting_session(foreign_id, 0)
    revoke_hosting_session(account["id"], own_session_id)
    revoke_hosting_session(foreign_id, foreign_session_id)

    client.post("/account/sessions/history/clear")

    assert list_hosting_session_history(account["id"]) == []
    assert len(list_hosting_session_history(foreign_id)) == 1


def test_hosting_logout_everywhere_invalidates_all_clients(app):
    first = app.test_client()
    _signup(first)
    second = app.test_client()
    _login(second)
    account = get_account_by_username("sessionuser")

    response = first.post("/account/sessions/logout-all")
    assert response.status_code == 302
    assert list_active_hosting_sessions(account["id"]) == []

    rejected = second.get("/dashboard")
    assert rejected.status_code == 302
    assert "/login" in rejected.headers["Location"]


def test_hosting_revoking_current_session_logs_out(app):
    client = app.test_client()
    _signup(client)
    current_id = get_hosting_session_id(_token(client))

    response = client.post(f"/account/sessions/{current_id}/revoke")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    with client.session_transaction() as flask_session:
        assert "hosting_account_id" not in flask_session


def test_hosting_password_change_preserves_current_and_revokes_others(app):
    first = app.test_client()
    _signup(first)
    second = app.test_client()
    _login(second)
    account = get_account_by_username("sessionuser")

    response = first.post(
        "/account/change-password",
        data={
            "current_password": "password123",
            "new_password": "newpassword123",
            "confirm_new_password": "newpassword123",
        },
    )
    assert response.status_code == 302
    assert len(list_active_hosting_sessions(account["id"])) == 1
    assert first.get("/account").status_code == 200
    assert second.get("/dashboard").status_code == 302


def test_hosting_cannot_revoke_another_accounts_session(app):
    client = app.test_client()
    _signup(client)
    foreign_id = create_account("foreignsession", "unused-hash")
    foreign_session_id, _raw = create_hosting_session(foreign_id, 0)

    response = client.post(f"/account/sessions/{foreign_session_id}/revoke")
    assert response.status_code == 404
    assert list_active_hosting_sessions(foreign_id)
