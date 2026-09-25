"""Tests for admin notifications about signups awaiting approval."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

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


def _enable_notices(**overrides):
    from hosting.db import update_hosting_settings

    values = {
        "signup_mode": "approval",
        "approval_notify_email": "ops@example.org",
        "approval_notify_mode": "digest",
        "approval_notify_digest_hours": 6,
    }
    values.update(overrides)
    update_hosting_settings(**values)


def test_migration_adds_approval_notify_columns(tmp_path):
    from hosting.db import get_hosting_settings, init_hosting_db

    legacy_path = tmp_path / "legacy-hosting.db"
    hosting_config.HOSTING_DATABASE_PATH = str(legacy_path)
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            "CREATE TABLE hosting_settings (id INTEGER PRIMARY KEY, "
            "signup_mode TEXT NOT NULL DEFAULT 'open')"
        )
        conn.execute("INSERT INTO hosting_settings (id) VALUES (1)")

    init_hosting_db()

    settings = get_hosting_settings()
    assert settings["approval_notify_email"] == ""
    assert settings["approval_notify_mode"] == "digest"
    assert settings["approval_notify_digest_hours"] == 6
    assert settings["approval_notify_last_digest_at"] is None


def test_notify_getters_default_to_digest_off():
    from hosting.db import (
        get_approval_notify_digest_hours,
        get_approval_notify_email,
        get_approval_notify_last_digest_at,
        get_approval_notify_mode,
    )

    assert get_approval_notify_email() == ""
    assert get_approval_notify_mode() == "digest"
    assert get_approval_notify_digest_hours() == 6
    assert get_approval_notify_last_digest_at() is None


def test_unknown_mode_falls_back_to_digest():
    from hosting.db import get_approval_notify_mode, update_hosting_settings

    update_hosting_settings(approval_notify_mode="carrier-pigeon")
    assert get_approval_notify_mode() == "digest"


def test_immediate_mode_emails_admin_on_pending_signup(client, monkeypatch):
    from hosting import notifications

    sent = []
    monkeypatch.setattr(
        notifications, "is_configured", lambda: True
    )
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda **kwargs: sent.append(kwargs) or (True, ""),
    )
    _enable_notices(approval_notify_mode="immediate")
    _account("notify-admin", admin=True)

    response = client.post(
        "/signup",
        data={
            "username": "waiting-user",
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"must approve your account" in response.data
    assert len(sent) == 1
    assert sent[0]["to"] == "ops@example.org"
    assert "waiting-user" in sent[0]["subject"]


def test_digest_mode_sends_nothing_at_signup(client, monkeypatch):
    from hosting import notifications

    sent = []
    monkeypatch.setattr(notifications, "is_configured", lambda: True)
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda **kwargs: sent.append(kwargs) or (True, ""),
    )
    _enable_notices(approval_notify_mode="digest")
    _account("digest-admin", admin=True)

    client.post(
        "/signup",
        data={
            "username": "queued-user",
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=True,
    )

    assert sent == []


def test_no_notice_when_email_unset_or_delivery_down(client, monkeypatch):
    from hosting import notifications

    sent = []
    monkeypatch.setattr(notifications, "is_configured", lambda: True)
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda **kwargs: sent.append(kwargs) or (True, ""),
    )
    _enable_notices(approval_notify_mode="immediate", approval_notify_email="")
    _account("quiet-admin", admin=True)

    client.post(
        "/signup",
        data={
            "username": "silent-user",
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=True,
    )

    assert sent == []


def test_digest_sends_queue_and_stamps_time(monkeypatch):
    from hosting import notifications
    from hosting.db import get_approval_notify_last_digest_at

    sent = []
    monkeypatch.setattr(notifications, "is_configured", lambda: True)
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda **kwargs: sent.append(kwargs) or (True, ""),
    )
    _enable_notices()
    _account("queued-one", pending=True, email="one@example.org")
    _account("queued-two", pending=True, email="two@example.org")

    assert notifications.maybe_send_pending_approval_digest() is True

    assert len(sent) == 1
    assert sent[0]["to"] == "ops@example.org"
    assert "2 signup(s)" in sent[0]["subject"]
    assert "queued-one" in sent[0]["text"]
    assert "queued-two" in sent[0]["text"]
    assert get_approval_notify_last_digest_at() is not None


def test_digest_skips_when_not_due_empty_or_immediate(monkeypatch):
    from datetime import timezone as tz
    from hosting import notifications
    from hosting.db import update_hosting_settings

    sent = []
    monkeypatch.setattr(notifications, "is_configured", lambda: True)
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda **kwargs: sent.append(kwargs) or (True, ""),
    )
    _enable_notices()
    _account("queued-three", pending=True)

    # Fresh digest just went out: not due again yet.
    update_hosting_settings(
        approval_notify_last_digest_at=datetime.now(tz.utc).isoformat()
    )
    assert notifications.maybe_send_pending_approval_digest() is False
    assert sent == []

    # Stale stamp: due again.
    update_hosting_settings(
        approval_notify_last_digest_at=(
            datetime.now(tz.utc) - timedelta(hours=7)
        ).isoformat()
    )
    assert notifications.maybe_send_pending_approval_digest() is True
    assert len(sent) == 1

    # Immediate mode never digests.
    _enable_notices(approval_notify_mode="immediate")
    assert notifications.maybe_send_pending_approval_digest() is False
    assert len(sent) == 1


def test_digest_sends_nothing_for_empty_queue(monkeypatch):
    from hosting import notifications

    sent = []
    monkeypatch.setattr(notifications, "is_configured", lambda: True)
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda **kwargs: sent.append(kwargs) or (True, ""),
    )
    _enable_notices()

    assert notifications.maybe_send_pending_approval_digest() is False
    assert sent == []


def test_admin_settings_reject_bad_notice_values(client):
    _account("notice-admin", admin=True)
    _login(client, "notice-admin")

    bad_email = client.post(
        "/admin/settings",
        data={
            "action": "save_account_communications",
            "email_verification_cooldown_seconds": "60",
            "approval_notify_email": "not-an-email",
            "approval_notify_mode": "digest",
            "approval_notify_digest_hours": "6",
        },
        follow_redirects=True,
    )
    assert bad_email.status_code == 400

    bad_mode = client.post(
        "/admin/settings",
        data={
            "action": "save_account_communications",
            "email_verification_cooldown_seconds": "60",
            "approval_notify_email": "ops@example.org",
            "approval_notify_mode": "smoke-signals",
            "approval_notify_digest_hours": "6",
        },
        follow_redirects=True,
    )
    assert bad_mode.status_code == 400

    bad_hours = client.post(
        "/admin/settings",
        data={
            "action": "save_account_communications",
            "email_verification_cooldown_seconds": "60",
            "approval_notify_email": "ops@example.org",
            "approval_notify_mode": "digest",
            "approval_notify_digest_hours": "99",
        },
        follow_redirects=True,
    )
    assert bad_hours.status_code == 400


def test_admin_settings_save_notice_values(client):
    from hosting.db import get_hosting_settings

    _account("notice-admin-two", admin=True)
    _login(client, "notice-admin-two")

    response = client.post(
        "/admin/settings",
        data={
            "action": "save_account_communications",
            "email_verification_cooldown_seconds": "60",
            "approval_notify_email": "ops@example.org",
            "approval_notify_mode": "immediate",
            "approval_notify_digest_hours": "12",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    settings = get_hosting_settings()
    assert settings["approval_notify_email"] == "ops@example.org"
    assert settings["approval_notify_mode"] == "immediate"
    assert settings["approval_notify_digest_hours"] == 12
