"""Focused tests for hosting account approval and denial reasons."""

import os
import sqlite3

import pytest

from helpers._passwords import generate_password_hash
from hosting import config as hosting_config
from hosting.app import create_hosting_app


@pytest.fixture(autouse=True)
def isolated_hosting(tmp_path):
    hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
    hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
    os.makedirs(hosting_config.INSTANCES_DIR, exist_ok=True)
    from hosting.db import init_hosting_db

    init_hosting_db()


@pytest.fixture
def client():
    application = create_hosting_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application.test_client()


def _account(username, *, admin=False, pending=False, email=""):
    from hosting.db import create_account, update_hosting_account

    account_id = create_account(
        username, generate_password_hash("password123"), is_admin=admin
    )
    updates = {}
    if pending:
        updates["approval_status"] = "pending"
    if email:
        updates["email"] = email
    if updates:
        update_hosting_account(account_id, **updates)
    return account_id


def _login(client, username):
    return client.post(
        "/login",
        data={"username": username, "password": "password123"},
        follow_redirects=True,
    )


def _clear_session(client):
    with client.session_transaction() as session:
        session.clear()


def test_existing_accounts_table_migrates_decision_reason(tmp_path):
    from hosting.db import get_account_by_id, init_hosting_db

    legacy_path = tmp_path / "legacy-hosting.db"
    hosting_config.HOSTING_DATABASE_PATH = str(legacy_path)
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, username TEXT NOT NULL "
            "UNIQUE COLLATE NOCASE, password TEXT NOT NULL, is_admin INTEGER NOT "
            "NULL DEFAULT 0, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO accounts (id, username, password, created_at) "
            "VALUES ('legacy-user', 'legacy', 'hash', '2026-01-01T00:00:00+00:00')"
        )

    init_hosting_db()

    account = get_account_by_id("legacy-user")
    assert account["decision_reason"] == ""


def test_account_decision_functions_validate_and_clear_stale_reason():
    from hosting.db import (
        approve_hosting_account,
        deny_hosting_account,
        get_account_by_id,
        update_hosting_account,
    )

    admin_id = _account("decision-admin", admin=True)
    account_id = _account("decision-user", pending=True)

    with pytest.raises(ValueError, match="decision_reason_too_long"):
        deny_hosting_account(account_id, admin_id, "x" * 1001)
    assert get_account_by_id(account_id)["approval_status"] == "pending"

    assert deny_hosting_account(account_id, admin_id, "The requested use is unsupported.")
    denied = get_account_by_id(account_id)
    assert denied["decision_reason"] == "The requested use is unsupported."

    update_hosting_account(account_id, approval_status="pending")
    assert approve_hosting_account(account_id, admin_id)
    approved = get_account_by_id(account_id)
    assert approved["decision_reason"] == ""
    assert approved["denied_at"] is None
    assert approved["denied_by"] is None


def test_admin_denial_reason_is_visible_and_in_email(client, monkeypatch):
    from hosting.db import get_account_by_id

    _account("reason-admin", admin=True)
    account_id = _account(
        "reason-user",
        pending=True,
        email="reason-user@example.com",
    )
    sent = {}
    monkeypatch.setattr("hosting.routes.dashboard_common.email_is_configured", lambda: True)

    def fake_send_email(**kwargs):
        sent.update(kwargs)
        return True, ""

    monkeypatch.setattr("hosting.routes.dashboard_common.send_email", fake_send_email)

    _login(client, "reason-admin")
    dashboard = client.get("/admin")
    assert b'name="decision_reason"' in dashboard.data
    assert b"Denial reason (optional)" in dashboard.data

    too_long = client.post(
        f"/admin/accounts/{account_id}/deny",
        data={"decision_reason": "x" * 1001},
        follow_redirects=True,
    )
    assert b"must not exceed 1000 characters" in too_long.data
    assert get_account_by_id(account_id)["approval_status"] == "pending"

    reason = "The submitted use case does not meet the hosting policy."
    response = client.post(
        f"/admin/accounts/{account_id}/deny",
        data={"decision_reason": reason},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert get_account_by_id(account_id)["decision_reason"] == reason
    assert reason in sent["text"]

    _clear_session(client)
    denied_page = _login(client, "reason-user")
    assert denied_page.status_code == 200
    assert b"Decision reason" in denied_page.data
    assert reason.encode() in denied_page.data


def test_approval_reason_is_visible_on_account_page(client):
    from hosting.db import get_account_by_id

    _account("approval-admin", admin=True)
    account_id = _account("approval-user", pending=True)
    reason = "Approved for the documented community knowledge base."

    _login(client, "approval-admin")
    response = client.post(
        f"/admin/accounts/{account_id}/approve",
        data={"decision_reason": reason},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert get_account_by_id(account_id)["decision_reason"] == reason

    _clear_session(client)
    _login(client, "approval-user")
    account_page = client.get("/account")
    assert account_page.status_code == 200
    assert b"Account approval reason" in account_page.data
    assert reason.encode() in account_page.data


def test_admin_can_undo_a_denial_and_approve_the_account(client, monkeypatch):
    from hosting.db import deny_hosting_account, get_account_by_id

    admin_id = _account("undo-admin", admin=True)
    user_id = _account("undo-user", pending=True, email="undo-user@example.com")

    assert deny_hosting_account(user_id, admin_id, "Denied at first review.")
    assert get_account_by_id(user_id)["approval_status"] == "denied"

    _login(client, "undo-admin")
    admin_page = client.get("/admin")
    assert b"Approve (undo denial)" in admin_page.data

    monkeypatch.setattr("hosting.routes.dashboard_common.email_is_configured", lambda: True)

    def fake_send_email(**kwargs):
        return True, ""

    monkeypatch.setattr("hosting.routes.dashboard_common.send_email", fake_send_email)

    response = client.post(
        f"/admin/accounts/{user_id}/approve",
        data={"decision_reason": "Reconsidered; approved."},
        follow_redirects=True,
    )
    assert response.status_code == 200
    updated = get_account_by_id(user_id)
    assert updated["approval_status"] == "approved"
    assert updated["denied_at"] is None
    assert updated["denied_by"] is None
    assert updated["pending_deletion"] == 0
    assert updated["decision_reason"] == "Reconsidered; approved."

    _clear_session(client)
    login = client.post(
        "/login",
        data={"username": "undo-user", "password": "password123"},
        follow_redirects=True,
    )
    assert login.request.path == "/dashboard"


def test_pending_deletion_countdown_only_matches_when_expired(client):
    """The countdown query must not treat every pending account as expired.

    Regression: the old SQL compared SQLite ``datetime()`` output
    (``YYYY-MM-DD HH:MM:SS``) against ``isoformat()`` values, which is
    lexicographically always "earlier" for the same date, so every
    pending-deletion account matched immediately.
    """
    from datetime import datetime, timedelta, timezone
    from hosting.db import get_account_by_id, get_hosting_db_context, get_pending_deletion_accounts, set_pending_deletion

    future_id = _account("pd-future")
    now_id = _account("pd-now")
    past_id = _account("pd-past")

    set_pending_deletion(future_id, seconds=3600)
    set_pending_deletion(now_id, seconds=3600)
    set_pending_deletion(past_id, seconds=3600)

    # Rewind "now" rows: force pending_deletion_at into the past/future.
    for aid, delta in (
        (future_id, timedelta(days=1)),   # expires tomorrow → not yet
        (past_id, timedelta(days=-1)),    # expired yesterday → eligible
    ):
        with get_hosting_db_context() as conn:
            at = (datetime.now(timezone.utc) + delta).isoformat()
            conn.execute(
                "UPDATE accounts SET pending_deletion_at=? WHERE id=?", (at, aid)
            )
            conn.commit()

    eligible = set(get_pending_deletion_accounts())
    assert now_id not in eligible
    assert future_id not in eligible
    assert past_id in eligible


def test_legal_pages_redirect_to_static_site(client):
    """Terms / Privacy / Compliance must redirect to the static site, not render."""
    hosting_config.BASE_DOMAIN = "bananawiki.com"
    for path in ("/terms", "/privacy", "/compliance"):
        response = client.get(path)
        assert response.status_code in (301, 302)
        assert "https://bananawiki.com" in response.headers.get("Location", "")
