"""
Tests for maximum password length guards (bcrypt DoS protection).

Every route that calls check_password_hash or generate_password_hash must
reject passwords longer than 1024 characters before invoking any hashing
function, to prevent a CPU-based DoS attack.
"""

import pytest
from werkzeug.security import generate_password_hash

LONG_PASSWORD = "A" * 1025


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------

class TestLoginMaxPasswordLength:
    def test_long_password_rejected_on_login(self, client, admin_user):
        """Submitting a >1024-char password to /login must be rejected without
        calling check_password_hash (returns the generic invalid-credentials
        error, not a 500 or a hang)."""
        resp = client.post("/login", data={
            "username": "admin",
            "password": LONG_PASSWORD,
        })
        assert resp.status_code == 200
        assert b"Invalid username or password" in resp.data

    def test_long_password_rejected_for_nonexistent_user(self, client, admin_user):
        """Same guard applies even when the username does not exist."""
        resp = client.post("/login", data={
            "username": "nobody",
            "password": LONG_PASSWORD,
        })
        assert resp.status_code == 200
        assert b"Invalid username or password" in resp.data

    def test_normal_login_still_works(self, client, admin_user):
        """Normal-length passwords must still be accepted."""
        resp = client.post("/login", data={
            "username": "admin",
            "password": "admin123",
        }, follow_redirects=True)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /session-conflict/force
# ---------------------------------------------------------------------------

class TestSessionConflictForceMaxPasswordLength:
    def test_long_password_rejected(self, client, admin_user):
        """A >1024-char password to /session-conflict/force must be rejected."""
        resp = client.post("/session-conflict/force", data={
            "username": "admin",
            "password": LONG_PASSWORD,
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Invalid username or password" in resp.data


# ---------------------------------------------------------------------------
# /settings (change_username)
# ---------------------------------------------------------------------------

class TestChangeUsernameMaxPasswordLength:
    def test_long_password_rejected_on_change_username(self, client, admin_user):
        """Submitting a >1024-char password when changing username must be
        rejected before any hash comparison."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/settings", data={
            "action": "change_username",
            "new_username": "newname",
            "password": LONG_PASSWORD,
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Incorrect password" in resp.data


# ---------------------------------------------------------------------------
# /settings (change_password: current_password field)
# ---------------------------------------------------------------------------

class TestChangePasswordCurrentMaxPasswordLength:
    def test_long_current_password_rejected(self, client, admin_user):
        """A >1024-char *current* password in the change-password form must be
        rejected before check_password_hash is called."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/settings", data={
            "action": "change_password",
            "current_password": LONG_PASSWORD,
            "new_password": "newpassword123",
            "confirm_password": "newpassword123",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Incorrect current password" in resp.data


# ---------------------------------------------------------------------------
# /settings (delete_account)
# ---------------------------------------------------------------------------

class TestDeleteAccountMaxPasswordLength:
    def test_long_password_rejected_on_delete_account(self, client):
        """A >1024-char password when deleting an account must be rejected."""
        import db
        uid = db.create_user("todelete", generate_password_hash("mypassword"), role="user")
        db.update_site_settings(setup_done=1)
        client.post("/login", data={"username": "todelete", "password": "mypassword"})
        resp = client.post("/settings", data={
            "action": "delete_account",
            "password": LONG_PASSWORD,
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Incorrect password" in resp.data
        # Account must still exist
        assert db.get_user_by_id(uid) is not None


# ---------------------------------------------------------------------------
# /settings (toggle_owner)
# ---------------------------------------------------------------------------

class TestToggleProtectedAdminMaxPasswordLength:
    def test_long_password_rejected_on_toggle_owner(self, client, admin_user):
        """A >1024-char password when toggling protected-admin status must be
        rejected before check_password_hash is called."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/settings", data={
            "action": "toggle_owner",
            "password": LONG_PASSWORD,
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Incorrect password" in resp.data
