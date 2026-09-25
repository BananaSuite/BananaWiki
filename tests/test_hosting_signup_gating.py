"""Regression tests for the signup gating + invite codes feature (task #5).

Contract:

* ``signup_mode='open'`` accepts free signups (existing behaviour).
* ``signup_mode='invite'`` requires a valid invite code.  Codes are
  redeemed atomically and cannot exceed ``max_uses``.
* ``signup_mode='closed'`` rejects all public signups (HTTP 403) but
  admins can still create accounts directly.
* The very-first signup always bypasses the gate so the platform can
  be bootstrapped.
* Admin-created accounts honour the password / username validation
  rules.
"""

from __future__ import annotations

import os
import sys

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
    """Hosting Flask test client with CSRF + bot protection disabled."""
    application = create_hosting_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signup(client, username, password="password123", invite_code=None):
    """Issue a /signup POST with optional invite code.  Returns the response."""
    data = {
        "username": username,
        "password": password,
        "confirm_password": password,
    }
    if invite_code is not None:
        data["invite_code"] = invite_code
    return client.post("/signup", data=data, follow_redirects=False)


def _bootstrap_admin(client, username="bootstrap_admin"):
    """Sign up the very-first account (auto-admin) and log out."""
    rv = _signup(client, username)
    assert rv.status_code in (302, 303), rv.data
    # Drop the session so subsequent signups behave like a fresh visitor.
    with client.session_transaction() as sess:
        sess.clear()


def _login(client, username, password="password123"):
    rv = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert rv.status_code in (302, 303), rv.data


def _set_signup_mode(mode):
    """Bypass HTTP and set signup_mode directly via the DB layer."""
    from hosting.db import update_hosting_settings
    update_hosting_settings(signup_mode=mode)


# ---------------------------------------------------------------------------
# get_signup_mode + first-signup bootstrap
# ---------------------------------------------------------------------------


class TestSignupModeDefault:
    def test_default_signup_mode_is_open(self):
        from hosting.db import get_signup_mode
        assert get_signup_mode() == "open"

    def test_first_signup_bypasses_closed_mode(self, client):
        """Even with signup closed, the platform must allow the first signup."""
        _set_signup_mode("closed")
        rv = _signup(client, "founder")
        assert rv.status_code in (302, 303), rv.data
        # Founder is admin
        from hosting.db import get_account_by_username
        founder = get_account_by_username("founder")
        assert founder is not None
        assert founder["is_admin"] == 1


# ---------------------------------------------------------------------------
# Closed mode
# ---------------------------------------------------------------------------


class TestClosedMode:
    def test_closed_mode_rejects_public_signup(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("closed")

        rv = _signup(client, "alice")
        assert rv.status_code == 403
        from hosting.db import get_account_by_username
        assert get_account_by_username("alice") is None

    def test_closed_mode_get_shows_notice(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("closed")

        rv = client.get("/signup")
        assert rv.status_code == 200
        assert b"Public signups are disabled" in rv.data
        assert b"Back to BananaWiki" in rv.data


# ---------------------------------------------------------------------------
# Invite mode
# ---------------------------------------------------------------------------


class TestInviteMode:
    def _mint_code(self, max_uses=1):
        from hosting.db import create_invite_code, get_account_by_username
        founder = get_account_by_username("bootstrap_admin")
        return create_invite_code(
            created_by=founder["id"],
            max_uses=max_uses,
        )

    def test_invite_mode_rejects_missing_code(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("invite")

        rv = _signup(client, "alice")
        assert rv.status_code == 400
        from hosting.db import get_account_by_username
        assert get_account_by_username("alice") is None, (
            "Account must be rolled back when invite redemption fails."
        )

    def test_invite_mode_rejects_unknown_code(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("invite")

        rv = _signup(client, "alice", invite_code="DOES-NOT-EXIST")
        assert rv.status_code == 400
        from hosting.db import get_account_by_username
        assert get_account_by_username("alice") is None

    def test_invite_mode_accepts_valid_code(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("invite")
        code = self._mint_code()

        rv = _signup(client, "alice", invite_code=code["code"])
        assert rv.status_code in (302, 303), rv.data
        from hosting.db import get_account_by_username, get_invite_code_by_code
        assert get_account_by_username("alice") is not None
        # Use count incremented
        updated = get_invite_code_by_code(code["code"])
        assert updated["current_uses"] == 1
        assert updated["last_used_by"] is not None

    def test_invite_mode_enforces_max_uses(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("invite")
        code = self._mint_code(max_uses=1)

        rv1 = _signup(client, "alice", invite_code=code["code"])
        assert rv1.status_code in (302, 303), rv1.data

        # Drop the alice session so we're not logged in as her for bob.
        with client.session_transaction() as sess:
            sess.clear()

        rv2 = _signup(client, "bob", invite_code=code["code"])
        assert rv2.status_code == 400, rv2.data
        from hosting.db import get_account_by_username
        assert get_account_by_username("bob") is None

    def test_invite_mode_code_lowercase_is_normalised(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("invite")
        code = self._mint_code()

        rv = _signup(
            client, "alice",
            invite_code=code["code"].lower(),  # COLLATE NOCASE handles this
        )
        assert rv.status_code in (302, 303), rv.data

    def test_custom_invite_code_can_be_redeemed(self, client):
        _bootstrap_admin(client)
        _set_signup_mode("invite")
        from hosting.db import create_invite_code, get_account_by_username
        founder = get_account_by_username("bootstrap_admin")
        code = create_invite_code(
            created_by=founder["id"],
            max_uses=1,
            code="team2026",
        )

        assert code["code"] == "TEAM2026"
        rv = _signup(client, "alice", invite_code="team2026")
        assert rv.status_code in (302, 303), rv.data

    def test_admin_can_create_custom_invite_code(self, client):
        _bootstrap_admin(client)
        _login(client, "bootstrap_admin")

        rv = client.post(
            "/admin/invites/create",
            data={"custom_code": "portal2026", "max_uses": "2"},
            follow_redirects=True,
        )

        assert rv.status_code == 200
        assert b"PORTAL2026" in rv.data
        from hosting.db import get_invite_code_by_code
        row = get_invite_code_by_code("portal2026")
        assert row is not None
        assert row["max_uses"] == 2


# ---------------------------------------------------------------------------
# Admin direct account creation
# ---------------------------------------------------------------------------


class TestAdminCreateAccount:
    def test_admin_can_create_user(self, client):
        _bootstrap_admin(client)
        _login(client, "bootstrap_admin")

        rv = client.post(
            "/admin/accounts/create",
            data={
                "username": "designer",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        assert rv.status_code in (302, 303), rv.data
        from hosting.db import get_account_by_username
        acct = get_account_by_username("designer")
        assert acct is not None
        assert acct["is_admin"] == 0

    def test_admin_can_create_admin(self, client):
        _bootstrap_admin(client)
        _login(client, "bootstrap_admin")

        rv = client.post(
            "/admin/accounts/create",
            data={
                "username": "secondadmin",
                "password": "password123",
                "confirm_password": "password123",
                "is_admin": "1",
            },
            follow_redirects=False,
        )
        assert rv.status_code in (302, 303), rv.data
        from hosting.db import get_account_by_username
        acct = get_account_by_username("secondadmin")
        assert acct is not None
        assert acct["is_admin"] == 1

    def test_admin_create_account_works_even_when_signups_closed(self, client):
        """Even with public signups closed, admins can still create users."""
        _bootstrap_admin(client)
        _login(client, "bootstrap_admin")
        _set_signup_mode("closed")

        rv = client.post(
            "/admin/accounts/create",
            data={
                "username": "designer",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        assert rv.status_code in (302, 303), rv.data
        from hosting.db import get_account_by_username
        assert get_account_by_username("designer") is not None

    def test_admin_create_account_rejects_short_password(self, client):
        _bootstrap_admin(client)
        _login(client, "bootstrap_admin")

        rv = client.post(
            "/admin/accounts/create",
            data={
                "username": "designer",
                "password": "short",
                "confirm_password": "short",
            },
            follow_redirects=False,
        )
        # Redirects to /admin with a flashed error message
        assert rv.status_code in (302, 303), rv.data
        from hosting.db import get_account_by_username
        assert get_account_by_username("designer") is None

    def test_admin_create_account_rejects_duplicate_username(self, client):
        _bootstrap_admin(client)
        _login(client, "bootstrap_admin")

        # First creation succeeds
        client.post(
            "/admin/accounts/create",
            data={
                "username": "designer",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        # Second with same name redirects with error and does not create another row
        client.post(
            "/admin/accounts/create",
            data={
                "username": "designer",
                "password": "different123",
                "confirm_password": "different123",
            },
            follow_redirects=False,
        )
        from hosting.db import get_all_accounts
        designers = [a for a in get_all_accounts() if a["username"] == "designer"]
        assert len(designers) == 1


# ---------------------------------------------------------------------------
# Invite code redemption atomicity (DB-level)
# ---------------------------------------------------------------------------


class TestInviteRedemptionAtomicity:
    def test_redeem_invite_code_consumes_exactly_one_use(self):
        from hosting.db import (
            create_account,
            create_invite_code,
            get_invite_code_by_code,
            redeem_invite_code,
        )

        admin_id = create_account("admin1", "x", is_admin=True)
        alice_id = create_account("alice", "x")

        code = create_invite_code(created_by=admin_id, max_uses=3)
        ok1, _ = redeem_invite_code(code["code"], account_id=alice_id)
        assert ok1

        row = get_invite_code_by_code(code["code"])
        assert row["current_uses"] == 1

    def test_redeem_fails_when_exhausted(self):
        from hosting.db import (
            create_account,
            create_invite_code,
            redeem_invite_code,
        )

        admin_id = create_account("admin1", "x", is_admin=True)
        alice_id = create_account("alice", "x")

        code = create_invite_code(created_by=admin_id, max_uses=1)
        ok1, _ = redeem_invite_code(code["code"], account_id=alice_id)
        assert ok1
        ok2, reason = redeem_invite_code(code["code"], account_id=alice_id)
        assert not ok2
        assert "fully used" in reason.lower()

    def test_redeem_fails_for_unknown_code(self):
        from hosting.db import create_account, redeem_invite_code

        alice_id = create_account("alice", "x")
        ok, reason = redeem_invite_code("ZZZNOPE", account_id=alice_id)
        assert not ok
        assert "invalid" in reason.lower()

    def test_redeem_empty_code_is_rejected(self):
        from hosting.db import create_account, redeem_invite_code

        alice_id = create_account("alice", "x")
        ok, _ = redeem_invite_code("", account_id=alice_id)
        assert not ok
        ok2, _ = redeem_invite_code("   ", account_id=alice_id)
        assert not ok2


def test_failed_invite_signup_leaves_no_account_or_tombstone(client):
    from hosting import db
    _bootstrap_admin(client)
    _set_signup_mode("invite")
    assert _signup(client, "rejected-member", invite_code="invalid").status_code == 400
    with db.get_hosting_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_concurrent_invite_signup_commits_one_account(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hosting import db
    _bootstrap_admin(client)
    _set_signup_mode("invite")
    owner = db.get_account_by_username("bootstrap_admin")
    invite = db.create_invite_code(created_by=owner["id"], max_uses=1)
    ready = Barrier(2)

    def signup(index):
        ready.wait(timeout=10)
        try:
            return db.create_signup_account(f"member-{index}", "test-hash", invite_code=invite["code"])
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(signup, range(2)))
    assert sum(result is not None for result in results) == 1
    with db.get_hosting_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
    assert db.get_invite_code_by_code(invite["code"])["current_uses"] == 1


def test_signup_rechecks_closed_mode_after_password_hashing(client, monkeypatch):
    import hosting.routes.auth as auth
    from hosting import db
    _bootstrap_admin(client)

    def close_signups(_password):
        _set_signup_mode("closed")
        return "test-hash"

    monkeypatch.setattr(auth, "generate_password_hash", close_signups)
    assert _signup(client, "late-member").status_code == 400
    assert db.get_account_by_username("late-member") is None


def test_signup_approval_is_set_at_account_creation(client):
    from hosting import db
    _bootstrap_admin(client)
    _set_signup_mode("approval")
    uid, first = db.create_signup_account("pending-member", "test-hash")
    assert first is False
    assert db.get_account_by_id(uid)["approval_status"] == "pending"
