"""Suspension-lockdown regressions (task #7).

The contract for a suspended instance is:

* the user cannot perform ANY mutating action on it (stop / restart /
  terminate / reset-password): each request flashes a clear lockout
  message and redirects;
* the expiration timer freezes for the duration of the suspension and is
  shifted forward by the time spent suspended when the admin unsuspends;
* the expiry sweeper must NOT auto-expire a suspended row;
* unsuspending restores the instance and restarts its runtime.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hosting import config as hosting_config  # noqa: E402
from hosting.app import create_hosting_app  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_hosting_db(tmp_path):
    """Point hosting DB + instances dir at a fresh tmp dir per test."""
    hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
    hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
    os.makedirs(hosting_config.INSTANCES_DIR, exist_ok=True)
    from hosting.db import init_hosting_db
    init_hosting_db()
    yield


@pytest.fixture
def client():
    """Hosting Flask test client with CSRF disabled."""
    application = create_hosting_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application.test_client()


def _login_owner(client, username="lockowner", password="password123"):
    """Create + log in a non-admin owner account and return its id.

    The first hosting signup becomes the platform admin (via
    ``create_account_atomic_first_admin``).  We sign up a sacrificial
    admin first so the owner under test is a normal user.
    """
    # First signup grabs the admin slot; throw it away.
    client.post(
        "/signup",
        data={
            "username": "platformadmin",
            "password": password,
            "confirm_password": password,
        },
        follow_redirects=False,
    )
    # Log the admin out so the next signup creates an independent session.
    client.post("/logout", follow_redirects=False)
    rv = client.post(
        "/signup",
        data={
            "username": username,
            "password": password,
            "confirm_password": password,
        },
        follow_redirects=False,
    )
    assert rv.status_code in (302, 303), rv.data
    from hosting.db._accounts import get_account_by_username
    acct = get_account_by_username(username)
    assert acct is not None
    assert not acct["is_admin"], "test owner must not be admin"
    return acct["id"]


def _make_instance(*, subdomain="lockwiki", domain_mode="hosting", admin=False):
    """Create an account + instance row directly via the DB layer."""
    from hosting.db import create_instance
    from hosting.db._accounts import create_account

    account_id = create_account("lockuser", "password123", is_admin=admin)
    inst = create_instance(
        account_id,
        subdomain,
        admin_username="admin",
        admin_password="password123",
        domain_mode=domain_mode,
    )
    return account_id, inst


def _force_status(instance_id, status, *, suspended_at=None, expires_at=None):
    """Drop a row directly to a desired status without going through the
    Gunicorn-aware ``suspend_instance``/``stop_instance`` helpers."""
    from hosting.db import get_hosting_db_context

    with get_hosting_db_context() as conn:
        if suspended_at is not None:
            conn.execute(
                "UPDATE instances SET status=?, suspended_at=? WHERE id=?",
                (status, suspended_at, instance_id),
            )
        else:
            conn.execute(
                "UPDATE instances SET status=? WHERE id=?",
                (status, instance_id),
            )
        if expires_at is not None:
            conn.execute(
                "UPDATE instances SET expires_at=? WHERE id=?",
                (expires_at, instance_id),
            )
        conn.commit()


class TestSuspensionWindowAccounting:
    """``suspend_instance`` + ``_consume_suspension_window`` freeze the clock."""

    def test_suspend_records_suspended_at(self, monkeypatch):
        from hosting.db import get_instance
        from hosting.instance_manager import suspend_instance

        _, inst = _make_instance()
        # ``suspend_instance`` shells out to stop the process; the row
        # doesn't have a live Gunicorn so monkeypatch the helper.
        monkeypatch.setattr(
            "hosting.instance_manager._stop_process",
            lambda *a, **kw: None,
        )
        # Force the row to "running" so ``suspend_instance`` exercises the
        # ``_stop_process`` branch.
        _force_status(inst["id"], "running")

        ok, _ = suspend_instance(inst["id"])
        assert ok is True
        after = get_instance(inst["id"])
        assert after["status"] == "suspended"
        assert after["suspended_at"] is not None

    def test_resuspend_does_not_overwrite_window(self, monkeypatch):
        """A second suspend on an already-suspended row keeps the original
        timestamp so re-suspending doesn't silently extend the trial."""
        from hosting.db import get_instance
        from hosting.instance_manager import suspend_instance

        _, inst = _make_instance()
        monkeypatch.setattr(
            "hosting.instance_manager._stop_process",
            lambda *a, **kw: None,
        )
        _force_status(inst["id"], "running")

        suspend_instance(inst["id"])
        first = get_instance(inst["id"])["suspended_at"]
        # Idempotent re-suspend.
        suspend_instance(inst["id"])
        second = get_instance(inst["id"])["suspended_at"]
        assert first == second

    def test_unsuspend_shifts_expires_at_forward(self, monkeypatch):
        """Time spent suspended is credited back onto ``expires_at``."""
        from hosting.db import get_instance
        from hosting.instance_manager import unsuspend_instance

        _, inst = _make_instance()
        # Pretend the instance was suspended exactly one hour ago and
        # expires in 24 h.
        now = datetime.now(timezone.utc)
        original_expiry = (now + timedelta(hours=24)).isoformat()
        suspended_at = (now - timedelta(hours=1)).isoformat()
        _force_status(
            inst["id"], "suspended",
            suspended_at=suspended_at, expires_at=original_expiry,
        )

        # ``unsuspend_instance`` ends in ``restart_instance``.  We don't
        # have a real Gunicorn so monkeypatch the process side and accept
        # that ``restart_instance`` may return False. The expiry
        # shift runs FIRST and must persist regardless.
        monkeypatch.setattr(
            "hosting.instance_manager._stop_process",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "hosting.instance_manager._start_process",
            lambda *a, **kw: False,
        )
        monkeypatch.setattr(
            "hosting.instance_manager._clean_stale_pid",
            lambda *a, **kw: None,
        )

        unsuspend_instance(inst["id"])

        after = get_instance(inst["id"])
        assert after["suspended_at"] in (None, "")
        # The expiry should have moved forward by ~3600 s.
        new_exp = datetime.fromisoformat(after["expires_at"])
        if new_exp.tzinfo is None:
            new_exp = new_exp.replace(tzinfo=timezone.utc)
        original = datetime.fromisoformat(original_expiry)
        diff_seconds = (new_exp - original).total_seconds()
        # Allow a generous +/-10s slack for the clock movement during
        # the test itself.
        assert 3600 - 10 <= diff_seconds <= 3600 + 10
        assert (after["suspended_accumulated_seconds"] or 0) >= 3590

    def test_unsuspend_apex_leaves_expires_at_null(self, monkeypatch):
        """Apex / perpetual instances don't have a timer to shift."""
        from hosting.db import get_instance
        from hosting.instance_manager import unsuspend_instance

        _, inst = _make_instance(domain_mode="apex", admin=True)
        now = datetime.now(timezone.utc)
        _force_status(
            inst["id"], "suspended",
            suspended_at=(now - timedelta(hours=2)).isoformat(),
            expires_at=None,
        )

        monkeypatch.setattr(
            "hosting.instance_manager._stop_process",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "hosting.instance_manager._start_process",
            lambda *a, **kw: False,
        )
        monkeypatch.setattr(
            "hosting.instance_manager._clean_stale_pid",
            lambda *a, **kw: None,
        )

        unsuspend_instance(inst["id"])
        after = get_instance(inst["id"])
        assert after["expires_at"] is None
        assert after["suspended_at"] in (None, "")


class TestExpirySweeperSkipsSuspended:
    """Suspended rows must never be picked up by the expiry sweeper."""

    def test_get_expired_instances_excludes_suspended(self):
        from hosting.db import get_expired_instances

        _, inst = _make_instance()
        long_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        _force_status(inst["id"], "suspended", expires_at=long_ago)

        rows = get_expired_instances()
        assert all(r["id"] != inst["id"] for r in rows)

    def test_get_expired_instances_includes_running_expired(self):
        from hosting.db import get_expired_instances

        _, inst = _make_instance(subdomain="expiredwiki")
        long_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        _force_status(inst["id"], "running", expires_at=long_ago)

        rows = get_expired_instances()
        assert any(r["id"] == inst["id"] for r in rows)

    def test_get_expired_instances_ignores_null_expiry(self):
        from hosting.db import get_expired_instances

        _, inst = _make_instance(domain_mode="apex", admin=True)
        _force_status(inst["id"], "running", expires_at=None)

        rows = get_expired_instances()
        assert all(r["id"] != inst["id"] for r in rows)


class TestOwnerLockoutHTTP:
    """End-to-end: non-admin owner of a suspended instance cannot mutate it."""

    def _setup_suspended_owned_by(self, client, account_id, subdomain="ownedwiki"):
        """Create an instance owned by ``account_id`` and force-suspend it."""
        from hosting.db import create_instance

        inst = create_instance(
            account_id,
            subdomain,
            admin_username="admin",
            admin_password="password123",
            domain_mode="hosting",
        )
        _force_status(inst["id"], "suspended")
        return inst

    def test_owner_stop_blocked(self, client):
        account_id = _login_owner(client)
        inst = self._setup_suspended_owned_by(client, account_id)

        rv = client.post(
            f"/instances/{inst['id']}/stop", follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"suspended by an administrator" in rv.data

    def test_owner_restart_blocked(self, client):
        account_id = _login_owner(client)
        inst = self._setup_suspended_owned_by(client, account_id, subdomain="own2")

        rv = client.post(
            f"/instances/{inst['id']}/restart", follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"suspended by an administrator" in rv.data

    def test_owner_terminate_blocked(self, client):
        from hosting.db import get_instance

        account_id = _login_owner(client)
        inst = self._setup_suspended_owned_by(client, account_id, subdomain="own3")

        rv = client.post(
            f"/instances/{inst['id']}/terminate", follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"suspended by an administrator" in rv.data
        # Row must still exist and remain suspended.
        after = get_instance(inst["id"])
        assert after["status"] == "suspended"

    def test_owner_reset_password_blocked(self, client):
        account_id = _login_owner(client)
        inst = self._setup_suspended_owned_by(client, account_id, subdomain="own4")

        rv = client.post(
            f"/instances/{inst['id']}/reset-password", follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"suspended by an administrator" in rv.data

    def test_owner_bulk_delete_skips_suspended(self, client, monkeypatch):
        from hosting.db import get_instance
        monkeypatch.setattr("hosting.container_runtime.stop_container", lambda *_a, **_k: True)

        account_id = _login_owner(client)
        inst_suspended = self._setup_suspended_owned_by(
            client, account_id, subdomain="bulk-suspended"
        )
        # An ordinary stopped row that SHOULD be terminated.
        from hosting.db import create_instance
        inst_stopped = create_instance(
            account_id,
            "bulk-stopped",
            admin_username="admin",
            admin_password="password123",
            domain_mode="hosting",
        )
        _force_status(inst_stopped["id"], "stopped")

        rv = client.post(
            "/instances/bulk-delete",
            data={"instance_ids": [inst_suspended["id"], inst_stopped["id"]]},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        # The suspended one is still alive.
        assert get_instance(inst_suspended["id"])["status"] == "suspended"
        # The stopped one is terminated.
        assert get_instance(inst_stopped["id"])["status"] == "terminated"
