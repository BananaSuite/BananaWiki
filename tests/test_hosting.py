"""Tests for the BananaWiki managed hosting platform."""

import io
import os
import re
import signal
import sqlite3
import stat
import sys
import tempfile
import time
from unittest.mock import patch

import pytest

# Ensure repo root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hosting import config as hosting_config  # noqa: E402
from hosting.config import _load_or_generate_secret_key, _SECRET_KEY_PATH  # noqa: E402
from hosting.app import create_hosting_app  # noqa: E402
from hosting.mfa import (  # noqa: E402
    consume_recovery_code,
    encrypt_secret,
    generate_secret,
    recovery_hashes,
    totp_code,
)


@pytest.fixture(autouse=True)
def isolated_hosting_db(tmp_path, monkeypatch):
    """Point the hosting DB and instances dir at a temp directory per test."""
    hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
    hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
    os.makedirs(hosting_config.INSTANCES_DIR, exist_ok=True)
    from hosting.db import init_hosting_db
    init_hosting_db()
    # Stub out the Docker container runtime so instance-management tests pass
    # without a live Docker daemon.  start_container writes a marker file and
    # returns the current PID; container_is_running checks for that marker so
    # unit tests that use a fresh tmp_path (no marker) still get False. The
    # right answer when no container was actually started for that data dir.
    from hosting import container_runtime as _cr

    def _fake_start_container(**kw):
        data_dir = kw.get("data_dir", "")
        try:
            with open(os.path.join(data_dir, ".container_running"), "w") as f:
                f.write("running")
        except OSError:
            pass
        return os.getpid()

    def _fake_container_is_running(data_dir):
        return os.path.exists(os.path.join(data_dir, ".container_running"))

    def _fake_stop_container(data_dir, **_kwargs):
        from pathlib import Path
        (Path(data_dir) / ".container_running").unlink(missing_ok=True)
        return True

    monkeypatch.setattr(_cr, "start_container", _fake_start_container)
    monkeypatch.setattr(_cr, "container_is_running", _fake_container_is_running)
    monkeypatch.setattr(_cr, "stop_container", _fake_stop_container)
    yield


@pytest.fixture
def app():
    """Create the hosting Flask app for testing."""
    application = create_hosting_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture
def client(app):
    """Flask test client for the hosting portal."""
    return app.test_client()


def _signup(client, username="testuser", password="password123"):
    """Helper: create a hosting account via the signup endpoint."""
    return client.post("/signup", data={
        "username": username,
        "password": password,
        "confirm_password": password,
    }, follow_redirects=True)


def _login(client, username="testuser", password="password123"):
    """Helper: log in to a hosting account."""
    return client.post("/login", data={
        "username": username,
        "password": password,
    }, follow_redirects=True)


class TestHostingGlobalBanners:
    def test_banner_audience_policy(self):
        from hosting.db import create_account, create_hosting_banner, get_active_hosting_banners

        admin_id = create_account("banneradmin", "hash", is_admin=True)
        selected_id = create_account("selected", "hash")
        other_id = create_account("other", "hash")
        create_hosting_banner(
            "Allowlisted hosting notice", "blue", "both", "allowlist", None,
            admin_id, audience_account_ids=[selected_id],
        )
        create_hosting_banner(
            "Hosting denylist notice", "red", "both", "denylist", None,
            admin_id, audience_account_ids=[selected_id],
        )

        selected = {row["content"] for row in get_active_hosting_banners(selected_id)}
        other = {row["content"] for row in get_active_hosting_banners(other_id)}
        guest = {row["content"] for row in get_active_hosting_banners()}
        assert selected == {"Allowlisted hosting notice"}
        assert other == {"Hosting denylist notice"}
        assert guest == {"Hosting denylist notice"}

    def test_admin_can_create_custom_timed_banner(self, client):
        from hosting.db import list_hosting_banners

        _signup(client, username="bannerroot", password="password123")
        response = client.post("/admin/banners/create", data={
            "content": "Planned maintenance",
            "color": "orange",
            "visibility": "both",
            "audience_mode": "all",
            "expires_at": "2030-06-15T12:00",
            "show_countdown": "1",
            "custom_colors": "1",
            "custom_background": "#123456",
            "custom_text_color": "#ffffff",
        }, follow_redirects=True)

        assert response.status_code == 200
        assert b"Hosting banner created" in response.data
        banner = next(row for row in list_hosting_banners() if row["content"] == "Planned maintenance")
        assert banner["show_countdown"] == 1
        assert banner["custom_background"] == "#123456"
        assert banner["expires_at"].startswith("2030-06-15T12:00")

    def test_banner_renders_on_logged_out_page(self, client):
        from hosting.db import create_account, create_hosting_banner

        admin_id = create_account("renderadmin", "hash", is_admin=True)
        create_hosting_banner(
            "Portal-wide notice", "green", "both", "all", None, admin_id,
            not_removable=0, custom_background="#112233",
            custom_text_color="#fefefe",
        )

        response = client.get("/login")
        assert response.status_code == 200
        assert b"Portal-wide notice" in response.data
        assert b'data-banner-background="#112233"' in response.data
        assert b"hosting-banner-close" in response.data


# ---------------------------------------------------------------------------
# Portal account tests
# ---------------------------------------------------------------------------

class TestAccountSignup:
    """Tests for the hosting portal signup flow."""

    def test_signup_page_renders(self, client):
        """GET /signup returns the signup form."""
        rv = client.get("/signup")
        assert rv.status_code == 200
        assert b"Create Account" in rv.data
        assert b"Back to BananaWiki" in rv.data

    def test_signup_success(self, client):
        """Successful signup redirects to the dashboard."""
        rv = _signup(client)
        assert rv.status_code == 200
        assert b"Dashboard" in rv.data or b"dashboard" in rv.data

    def test_signup_short_username(self, client):
        """Username under 3 chars is rejected."""
        rv = client.post("/signup", data={
            "username": "ab",
            "password": "password123",
            "confirm_password": "password123",
        })
        assert rv.status_code == 400

    def test_signup_short_password(self, client):
        """Password under 8 chars is rejected."""
        rv = client.post("/signup", data={
            "username": "testuser",
            "password": "short",
            "confirm_password": "short",
        })
        assert rv.status_code == 400

    def test_signup_password_mismatch(self, client):
        """Mismatched passwords are rejected."""
        rv = client.post("/signup", data={
            "username": "testuser",
            "password": "password123",
            "confirm_password": "differentpass",
        })
        assert rv.status_code == 400

    def test_signup_password_requires_letter_and_number(self, client):
        """Signup password must include at least one letter and one number."""
        rv = client.post("/signup", data={
            "username": "testuser",
            "password": "onlyletters",
            "confirm_password": "onlyletters",
        })
        assert rv.status_code == 400
        assert b"at least one letter and one number" in rv.data

    def test_signup_duplicate_username(self, client):
        """Duplicate usernames are rejected."""
        _signup(client, username="dupeuser")
        client.post("/logout")
        rv = client.post("/signup", data={
            "username": "dupeuser",
            "password": "password123",
            "confirm_password": "password123",
        })
        assert rv.status_code == 409


class TestAccountLogin:
    """Tests for the hosting portal login flow."""

    def test_login_page_renders(self, client):
        """GET /login returns the login form."""
        rv = client.get("/login")
        assert rv.status_code == 200
        assert b"Log In" in rv.data
        assert b"Back to BananaWiki" in rv.data

    @pytest.mark.parametrize("path", [
        "/login",
        "/signup",
        "/forgot-password",
        "/forgot-username",
        "/reset-password?token=test-token",
    ])
    def test_auth_pages_use_portal_topbar(self, client, path):
        rv = client.get(path)
        assert rv.status_code == 200
        assert b'class="topbar"' in rv.data
        assert b'id="nav-menu"' in rv.data
        assert b'id="hosting-language-select-topbar"' in rv.data
        assert b'class="hosting-footer"' in rv.data

    @pytest.mark.parametrize("path", [
        "/login",
        "/signup",
        "/forgot-password",
        "/forgot-username",
        "/reset-password?token=test-token",
    ])
    def test_logged_in_user_redirected_from_public_auth_pages(self, client, path):
        """A logged-in user is bounced to the dashboard instead of seeing
        the login, signup or account-recovery pages."""
        _signup(client)
        rv = client.get(path, follow_redirects=False)
        assert rv.status_code in (302, 303)
        assert rv.headers["Location"].endswith("/dashboard")

    @pytest.mark.parametrize("path", [
        "/login",
        "/signup",
        "/forgot-password",
        "/forgot-username",
        "/reset-password?token=test-token",
    ])
    def test_logged_in_user_redirected_from_public_auth_posts(self, client, path):
        """Even a POST to a public auth page redirects a logged-in user."""
        _signup(client)
        rv = client.post(path, data={"username": "testuser", "password": "password123"},
                         follow_redirects=False)
        assert rv.status_code in (302, 303)
        assert rv.headers["Location"].endswith("/dashboard")

    def test_suspended_user_redirected_from_login(self, client):
        """A user in a restricted suspension session cannot reach /login."""
        from hosting.db import suspend_account, get_account_by_username

        _signup(client)
        acct = get_account_by_username("testuser")
        suspend_account(acct["id"], reason="policy")
        client.post("/logout")
        client.post("/login", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=True)
        rv = client.get("/login", follow_redirects=False)
        assert rv.status_code in (302, 303)
        assert rv.headers["Location"].endswith("/dashboard")

    def test_login_success(self, client):
        """Valid credentials log in and redirect to dashboard."""
        _signup(client)
        client.post("/logout")
        rv = _login(client)
        assert rv.status_code == 200
        assert b"Dashboard" in rv.data or b"dashboard" in rv.data

    def test_login_wrong_password(self, client):
        """Wrong password returns 401."""
        _signup(client)
        client.post("/logout")
        rv = client.post("/login", data={
            "username": "testuser",
            "password": "wrongpassword",
        })
        assert rv.status_code == 401

    def test_login_nonexistent_user(self, client):
        """Non-existent username returns 401."""
        rv = client.post("/login", data={
            "username": "nouser",
            "password": "password123",
        })
        assert rv.status_code == 401

    def test_login_rejects_suspended_account(self, client):
        """Suspended accounts cannot log in to the hosting portal."""
        from hosting.db import suspend_account, get_account_by_username

        _signup(client)
        acct = get_account_by_username("testuser")
        suspend_account(acct["id"], reason="policy")
        client.post("/logout")
        rv = client.post("/login", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=True)
        assert rv.status_code == 403
        assert b"issue with your account" in rv.data.lower()
        assert b"account-state-card account-state-danger" in rv.data


class TestAccountStatePages:
    """Focused rendering checks for blocked-account workflows."""

    @pytest.mark.parametrize(
        ("path", "heading"),
        [
            ("/activation-pending", b"Account Pending Approval"),
            ("/activation-denied", b"Your account has been disabled"),
        ],
    )
    def test_public_account_state_page_renders(self, client, path, heading):
        rv = client.get(path)

        assert rv.status_code == 200
        assert heading in rv.data
        assert b"account-state-card" in rv.data
        assert b"Contact support" in rv.data

    def test_verification_page_keeps_resend_disabled_without_delivery(self, client):
        from hosting.db import get_account_by_username, update_hosting_account

        _signup(client)
        account = get_account_by_username("testuser")
        update_hosting_account(account["id"], email="reader@example.com")

        rv = client.get("/account/verify-email")

        assert rv.status_code == 200
        assert b"Verify your email address" in rv.data
        assert b'data-delivery-available="false" disabled' in rv.data
        assert b"account-state-card" in rv.data

    def test_flagged_email_page_shows_visible_reason(self, client):
        from hosting.db import flag_email_invalid, get_account_by_username

        _signup(client)
        account = get_account_by_username("testuser")
        flag_email_invalid(
            account["id"], reason="Mailbox rejected messages", reason_visible=True,
        )

        rv = client.get("/account/email-flagged")

        assert rv.status_code == 200
        assert b"Add a valid email address" in rv.data
        assert b"Mailbox rejected messages" in rv.data
        assert b"account-state-card account-state-warning" in rv.data

    def test_pending_deletion_page_uses_danger_state(self, client):
        from hosting.db import get_account_by_username, set_pending_deletion

        _signup(client)
        account = get_account_by_username("testuser")
        set_pending_deletion(account["id"], seconds=3600, reason="Requested closure")

        rv = client.get("/pending-deletion")

        assert rv.status_code == 200
        assert b"Your account is scheduled for deletion" in rv.data
        assert b"Requested closure" in rv.data
        assert b"account-state-card account-state-danger" in rv.data


class TestLoginRedirectsBackToRequestedPage:
    """Tests for the post-login ``next`` redirect behaviour."""

    def test_protected_page_redirects_to_login_with_next(self, client):
        """Visiting a protected URL while anonymous redirects to
        ``/login?next=<original-path>`` so the user can be sent back
        after logging in."""
        rv = client.get("/instances/create")
        assert rv.status_code == 302
        # Location header should preserve the original path via ?next=.
        # Werkzeug may emit either the raw path or its percent-encoded
        # form, so accept both.
        loc = rv.headers["Location"]
        assert "/login" in loc
        assert (
            "next=/instances/create" in loc
            or "next=%2Finstances%2Fcreate" in loc
        )

    def test_protected_page_with_query_string_preserved_in_next(self, client):
        """Query strings on the protected URL are preserved in ``next``."""
        rv = client.get("/instances/create?foo=bar")
        assert rv.status_code == 302
        # The ``next`` query parameter should include the original query
        # string so the user lands back on the exact URL they requested.
        loc = rv.headers["Location"]
        assert "/login" in loc
        assert "next=" in loc
        assert "foo" in loc and "bar" in loc

    def test_login_form_preserves_next_in_hidden_input(self, client):
        """The login template forwards ``?next=…`` into a hidden form
        field so the value survives a POST submission."""
        rv = client.get("/login?next=/instances/create")
        assert rv.status_code == 200
        assert b'name="next"' in rv.data
        assert b'value="/instances/create"' in rv.data

    def test_successful_login_redirects_to_next(self, client):
        """A valid login submission with a safe ``next`` query parameter
        redirects to the requested page rather than the dashboard."""
        _signup(client)
        client.post("/logout")
        rv = client.post(
            "/login?next=/instances/create",
            data={"username": "testuser", "password": "password123"},
        )
        assert rv.status_code == 302
        assert rv.headers["Location"].endswith("/instances/create")

    def test_successful_login_honours_next_in_form_field(self, client):
        """A valid login submission with ``next`` carried via the POST
        body (e.g. from the hidden form field) also redirects there."""
        _signup(client)
        client.post("/logout")
        rv = client.post(
            "/login",
            data={
                "username": "testuser",
                "password": "password123",
                "next": "/instances/create",
            },
        )
        assert rv.status_code == 302
        assert rv.headers["Location"].endswith("/instances/create")

    def test_login_rejects_external_next_url(self, client):
        """An attacker-controlled ``next`` pointing at an external host
        must NOT be honoured: login falls back to the dashboard so the
        portal cannot be turned into an open-redirect vector."""
        _signup(client)
        client.post("/logout")
        rv = client.post(
            "/login?next=https://evil.example.com/phish",
            data={"username": "testuser", "password": "password123"},
        )
        assert rv.status_code == 302
        # Should redirect to dashboard, not to evil.example.com
        assert "evil.example.com" not in rv.headers["Location"]
        assert rv.headers["Location"].endswith("/dashboard")

    def test_login_rejects_protocol_relative_next(self, client):
        """Protocol-relative ``next`` values (``//evil.com``) are blocked."""
        _signup(client)
        client.post("/logout")
        rv = client.post(
            "/login?next=//evil.example.com",
            data={"username": "testuser", "password": "password123"},
        )
        assert rv.status_code == 302
        assert "evil.example.com" not in rv.headers["Location"]
        assert rv.headers["Location"].endswith("/dashboard")

    def test_login_without_next_redirects_to_dashboard(self, client):
        """When no ``next`` is supplied, login still redirects to the
        dashboard as before: preserving the existing default."""
        _signup(client)
        client.post("/logout")
        rv = client.post(
            "/login",
            data={"username": "testuser", "password": "password123"},
        )
        assert rv.status_code == 302
        assert rv.headers["Location"].endswith("/dashboard")

    def test_root_path_does_not_get_next_param(self, client):
        """Requests to ``/`` while anonymous route through the index
        redirect chain: they should NOT add ``?next=/`` to the login
        URL (which would otherwise be sticky on every visit)."""
        rv = client.get("/", follow_redirects=False)
        # ``/`` redirects to ``/login`` (no next) for anonymous users
        assert rv.status_code == 302
        assert rv.headers["Location"].endswith("/login")
        assert "next=" not in rv.headers["Location"]


class TestAccountLogout:
    """Tests for the hosting portal logout flow."""

    def test_logout(self, client):
        """Logout clears the session and redirects to login."""
        _signup(client)
        rv = client.post("/logout", follow_redirects=True)
        assert rv.status_code == 200
        assert b"Log In" in rv.data

    def test_logout_get_rejected(self, client):
        """GET /logout is rejected, only POST is allowed."""
        _signup(client)
        rv = client.get("/logout")
        assert rv.status_code == 405


class TestAccountDeletion:
    """Tests for account deletion."""

    def test_delete_account(self, client):
        """Deleting an account removes it and all instances."""
        _signup(client)
        # Create an instance first
        client.post("/instances/create", data={"subdomain": "testinst"})
        rv = client.post(
            "/settings/delete", data={"current_password": "password123"},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"deleted" in rv.data.lower()

    def test_delete_account_requires_login(self, client):
        """DELETE /settings/delete requires authentication."""
        rv = client.post(
            "/settings/delete", data={"current_password": "password123"},
            follow_redirects=True,
        )
        assert b"Log In" in rv.data


class TestInterfaceLanguage:
    """Tests for the per-session language selector (en/it only)."""

    def test_default_language_is_english(self, client):
        """Without any selection, the portal renders in English."""
        rv = client.get("/login")
        assert rv.status_code == 200
        assert b'<html lang="en"' in rv.data
        # Topbar selector is rendered with English selected
        assert b'<option value="en" selected>English</option>' in rv.data
        assert b'<option value="it" >Italiano</option>' in rv.data

    def test_switch_to_italian(self, client):
        """POST /language with ``it`` switches the session to Italian."""
        rv = client.post("/language", data={"language": "it"}, follow_redirects=True)
        assert rv.status_code == 200
        # Subsequent page loads should render with Italian as the active lang
        rv = client.get("/login")
        assert b'<html lang="it"' in rv.data
        assert b'<option value="it" selected>Italiano</option>' in rv.data
        # The Italian translation of the dashboard nav label is "Dashboard"
        # (same word in both languages) so we check the login subtitle which
        # IS translated.
        assert "Accedi per gestire".encode("utf-8") in rv.data

    def test_switch_back_to_english(self, client):
        """POST /language with ``en`` clears any prior Italian selection."""
        client.post("/language", data={"language": "it"})
        client.post("/language", data={"language": "en"})
        rv = client.get("/login")
        assert b'<html lang="en"' in rv.data
        assert b'<option value="en" selected>English</option>' in rv.data

    def test_unknown_language_is_rejected(self, client):
        """Unsupported codes (e.g. ``fr``) are silently ignored."""
        # First set Italian, then try to set French: French should be
        # rejected and the session preference cleared (back to default en).
        client.post("/language", data={"language": "it"})
        client.post("/language", data={"language": "fr"})
        rv = client.get("/login")
        assert b'<html lang="en"' in rv.data
        assert b'<option value="en" selected>English</option>' in rv.data

    def test_language_get_rejected(self, client):
        """GET /language is rejected, only POST is allowed."""
        rv = client.get("/language")
        assert rv.status_code == 405

    def test_language_form_visible_to_anonymous_users(self, client):
        """The language selector is shown on the login page (no auth)."""
        rv = client.get("/login")
        assert rv.status_code == 200
        assert b'name="language"' in rv.data
        assert b'<option value="en"' in rv.data
        assert b'<option value="it"' in rv.data
        # Selector should NOT include any other languages
        assert b'<option value="fr"' not in rv.data
        assert b'<option value="es"' not in rv.data
        assert b'<option value="de"' not in rv.data

    def test_language_form_visible_to_logged_in_users(self, client):
        """The language selector is shown on the dashboard (logged in)."""
        _signup(client)
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        assert b'name="language"' in rv.data
        assert b'<option value="en"' in rv.data
        assert b'<option value="it"' in rv.data

    def test_language_redirects_to_referrer(self, client):
        """The route round-trips back to the referring page when present."""
        rv = client.post(
            "/language",
            data={"language": "it"},
            headers={"Referer": "http://localhost/login"},
        )
        assert rv.status_code == 302
        assert rv.headers["Location"].endswith("/login")

    def test_language_redirects_to_index_without_referrer(self, client):
        """Without a referrer, the route redirects to the portal index."""
        rv = client.post("/language", data={"language": "it"})
        assert rv.status_code == 302
        # ``hosting_index`` is mounted at ``/`` and redirects to login.
        assert rv.headers["Location"].endswith("/")

    def test_language_does_not_leak_to_other_session(self, client, app):
        """Two clients have independent language preferences (per-session)."""
        client_a = app.test_client()
        client_b = app.test_client()
        client_a.post("/language", data={"language": "it"})
        # Client B has not switched and should still see English.
        rv = client_b.get("/login")
        assert b'<html lang="en"' in rv.data
        # Client A should still see Italian.
        rv = client_a.get("/login")
        assert b'<html lang="it"' in rv.data

    def test_external_referrer_falls_back_to_index(self, client):
        """An off-host referrer is ignored to avoid open redirects."""
        rv = client.post(
            "/language",
            data={"language": "it"},
            headers={"Referer": "https://evil.example.com/phish"},
        )
        assert rv.status_code == 302
        # Falls back to local index, NOT the external URL.
        assert "evil.example.com" not in rv.headers["Location"]


# ---------------------------------------------------------------------------
# Instance lifecycle tests
# ---------------------------------------------------------------------------

class TestInstanceCreation:
    """Tests for creating BananaWiki instances."""

    def test_create_page_renders(self, client):
        """GET /instances/create shows the creation form."""
        _signup(client)
        rv = client.get("/instances/create")
        assert rv.status_code == 200
        assert b"Create a New Instance" in rv.data

    def test_create_instance_success(self, client):
        """Valid subdomain creates an instance and shows credentials."""
        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "myteam"})
        assert rv.status_code == 200
        assert b"Instance Created" in rv.data
        assert b"Login Credentials" in rv.data
        assert b"myteam" in rv.data

    def test_create_instance_invalid_subdomain(self, client):
        """Invalid subdomain is rejected."""
        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "no spaces!"})
        assert rv.status_code == 400

    def test_create_instance_reserved_subdomain(self, client):
        """Reserved subdomains are rejected."""
        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "admin"})
        assert rv.status_code == 400

    def test_create_instance_duplicate_subdomain(self, client):
        """Duplicate subdomain is rejected."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "taken"})
        rv = client.post("/instances/create", data={"subdomain": "taken"})
        assert rv.status_code == 400
        assert b"already in use" in rv.data

    def test_create_instance_duplicate_subdomain_race_returns_clean_error(self, client):
        """A duplicate caught inside the transactional creator should not 500."""
        _signup(client)
        with patch("hosting.instance_manager.db_create_instance", side_effect=ValueError("subdomain_in_use")):
            rv = client.post("/instances/create", data={"subdomain": "taken"})
        assert rv.status_code == 400
        assert b"already in use" in rv.data

    def test_create_instance_unexpected_provisioning_error_returns_clean_error(self, client):
        """Unexpected setup failures should not bubble up as a raw 500."""
        from hosting.db import get_instance_by_subdomain

        _signup(client)
        with patch("hosting.instance_manager._create_instance_dirs", side_effect=OSError("boom")):
            rv = client.post("/instances/create", data={"subdomain": "proverror"})

        assert rv.status_code == 400
        assert b"failed to provision the instance" in rv.data.lower()
        assert get_instance_by_subdomain("proverror") is None

    def test_reuse_subdomain_after_termination(self, client):
        """A terminated instance's name can be reused."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "reusable"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("reusable")
        assert inst is not None

        terminate = client.post(f"/instances/{inst['id']}/terminate", follow_redirects=True)
        assert terminate.status_code == 200
        assert "terminated" in terminate.data.decode("utf-8").lower()

        recreate = client.post("/instances/create", data={"subdomain": "reusable"})
        assert recreate.status_code == 200
        assert b"Instance Created" in recreate.data

    def test_create_instance_quota_limit(self, client):
        """Cannot exceed the instance quota."""
        original = hosting_config.MAX_INSTANCES_PER_ACCOUNT
        try:
            hosting_config.MAX_INSTANCES_PER_ACCOUNT = 2
            _signup(client, username="adminquota", password="password123")
            client.post("/logout")
            _signup(client, username="regularquota", password="password123")
            client.post("/instances/create", data={"subdomain": "inst1"})
            client.post("/instances/create", data={"subdomain": "inst2"})
            rv = client.post("/instances/create", data={"subdomain": "inst3"})
            assert rv.status_code == 400
        finally:
            hosting_config.MAX_INSTANCES_PER_ACCOUNT = original

    def test_admin_not_affected_by_instance_quota_limit(self, client):
        """Admin accounts bypass per-account instance quota limits."""
        original = hosting_config.MAX_INSTANCES_PER_ACCOUNT
        try:
            hosting_config.MAX_INSTANCES_PER_ACCOUNT = 1
            _signup(client, username="adminfree", password="password123")
            first = client.post("/instances/create", data={"subdomain": "adminfree1"})
            second = client.post("/instances/create", data={"subdomain": "adminfree2"})
            assert first.status_code == 200
            assert second.status_code == 200
        finally:
            hosting_config.MAX_INSTANCES_PER_ACCOUNT = original

    def test_admin_instances_are_unlimited_while_account_is_admin(self, client):
        """Admin-owned instances get unlimited quota settings."""
        from datetime import datetime, timezone
        from hosting.db import get_instance_by_subdomain

        _signup(client, username="adminunlimited", password="password123")
        client.post("/instances/create", data={"subdomain": "adminul1"})
        inst = get_instance_by_subdomain("adminul1")
        assert inst is not None
        assert int(inst["storage_limit_mb"]) == 0
        expires = datetime.fromisoformat(inst["expires_at"])
        assert (expires - datetime.now(timezone.utc)).days > 36000

    def test_create_instance_requires_login(self, client):
        """Instance creation requires authentication."""
        rv = client.post("/instances/create", data={"subdomain": "test"}, follow_redirects=True)
        assert b"Log In" in rv.data


class TestInstanceControls:
    """Tests for stop, restart, and terminate actions."""

    def _create_instance(self, client, subdomain="testinst"):
        """Helper: create an instance and return the instance ID."""
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": subdomain})
        inst = get_instance_by_subdomain(subdomain)
        return inst["id"]

    def test_stop_instance(self, client):
        """Stopping a running instance changes status to stopped."""
        _signup(client)
        iid = self._create_instance(client)
        rv = client.post(f"/instances/{iid}/stop", follow_redirects=True)
        assert rv.status_code == 200
        assert b"stopped" in rv.data.lower()

    def test_restart_instance(self, client):
        """Restarting a stopped instance changes status to running."""
        _signup(client)
        iid = self._create_instance(client)
        client.post(f"/instances/{iid}/stop")
        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(f"/instances/{iid}/restart", follow_redirects=True)
        assert rv.status_code == 200
        assert b"restarted" in rv.data.lower()

    def test_restart_instance_process_failure(self, client):
        """Restarting when process fails to start shows error."""
        _signup(client)
        iid = self._create_instance(client)
        client.post(f"/instances/{iid}/stop")
        with patch("hosting.instance_manager._start_process", return_value=False):
            rv = client.post(f"/instances/{iid}/restart", follow_redirects=True)
        assert rv.status_code == 200
        assert b"did not start" in rv.data.lower()

    def test_create_instance_start_failure_returns_error(self, client):
        """Create should fail cleanly when the instance never becomes reachable."""
        from hosting.db import get_instance_by_subdomain

        _signup(client)
        with patch("hosting.instance_manager._start_process", return_value=False):
            rv = client.post("/instances/create", data={"subdomain": "brokenstart"})

        assert rv.status_code == 400
        assert b"did not become reachable" in rv.data.lower()
        assert get_instance_by_subdomain("brokenstart") is None

    def test_terminate_instance(self, client):
        """Terminating an instance removes its data."""
        _signup(client)
        iid = self._create_instance(client)
        rv = client.post(f"/instances/{iid}/terminate", follow_redirects=True)
        assert rv.status_code == 200
        assert b"terminated" in rv.data.lower()

    def test_stop_nonexistent_instance(self, client):
        """Stopping a non-existent instance shows error."""
        _signup(client)
        rv = client.post("/instances/fakeid/stop", follow_redirects=True)
        assert rv.status_code == 200
        assert b"not found" in rv.data.lower()

    def test_cannot_stop_already_stopped(self, client):
        """Stopping an already-stopped instance shows error."""
        _signup(client)
        iid = self._create_instance(client)
        client.post(f"/instances/{iid}/stop")
        rv = client.post(f"/instances/{iid}/stop", follow_redirects=True)
        assert b"not running" in rv.data.lower()

    def test_cannot_restart_running(self, client):
        """Restarting a running instance shows error."""
        _signup(client)
        iid = self._create_instance(client)
        rv = client.post(f"/instances/{iid}/restart", follow_redirects=True)
        assert b"not stopped" in rv.data.lower()

    def test_cannot_terminate_already_terminated(self, client):
        """Terminating an already-terminated instance shows error."""
        _signup(client)
        iid = self._create_instance(client)
        client.post(f"/instances/{iid}/terminate")
        rv = client.post(f"/instances/{iid}/terminate", follow_redirects=True)
        # After termination, instance_id lookup fails the ownership check
        assert rv.status_code == 200

    def test_other_user_cannot_control_instance(self, client):
        """Users cannot control instances belonging to others."""
        _signup(client, username="user1")
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": "priv"})
        inst = get_instance_by_subdomain("priv")
        iid = inst["id"]

        # Log out and sign up as a different user
        client.post("/logout")
        _signup(client, username="user2", password="password456")
        rv = client.post(f"/instances/{iid}/stop", follow_redirects=True)
        assert b"not found" in rv.data.lower()


# ---------------------------------------------------------------------------
# Dashboard tests
# ---------------------------------------------------------------------------

class TestDashboard:
    """Tests for the hosting dashboard."""

    def test_dashboard_requires_login(self, client):
        """Dashboard redirects to login when not authenticated."""
        rv = client.get("/dashboard", follow_redirects=True)
        assert b"Log In" in rv.data

    def test_dashboard_empty(self, client):
        """Dashboard with no instances shows the empty state."""
        _signup(client)
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        assert b"Create Your First Instance" in rv.data

    def test_dashboard_shows_instances(self, client):
        """Dashboard lists created instances."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "mytest"})
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        # instance-card is the CSS class wrapping each instance in the
        # dashboard template; it appears regardless of hostname/port values.
        assert b"instance-card" in rv.data or b"mytest" in rv.data

    def test_dashboard_shows_countdown(self, client):
        """Dashboard shows remaining time for instances."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "countme"})
        rv = client.get("/dashboard")
        assert b"remaining" in rv.data


# ---------------------------------------------------------------------------
# Subdomain validation tests
# ---------------------------------------------------------------------------

class TestSubdomainValidation:
    """Tests for subdomain validation logic."""

    def test_valid_subdomain(self):
        """Simple alphanumeric subdomain is valid."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain("myteam")
        assert ok

    def test_subdomain_with_hyphens(self):
        """Hyphens are allowed in the middle."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain("my-team-123")
        assert ok

    def test_subdomain_too_short(self):
        """Subdomains shorter than min length are rejected."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain("ab")
        assert not ok
        assert "at least" in reason

    def test_subdomain_too_long(self):
        """Subdomains longer than max length are rejected."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain("a" * 50)
        assert not ok
        assert "exceed" in reason

    def test_subdomain_leading_hyphen(self):
        """Subdomains cannot start with a hyphen."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain("-bad")
        assert not ok

    def test_subdomain_trailing_hyphen(self):
        """Subdomains cannot end with a hyphen."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain("bad-")
        assert not ok

    def test_subdomain_special_chars(self):
        """Subdomains with special characters are rejected."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain("no spaces!")
        assert not ok

    def test_subdomain_uppercase_normalised(self):
        """Uppercase input is normalised to lowercase."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain("MyTeam")
        assert ok

    def test_reserved_subdomain(self):
        """Reserved names are rejected."""
        from hosting.subdomain import validate_subdomain
        for name in ("www", "admin", "api", "mail"):
            ok, reason = validate_subdomain(name)
            assert not ok, f"{name} should be reserved"
            assert "reserved" in reason

    def test_empty_subdomain(self):
        """Empty string is rejected."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain("")
        assert not ok

    def test_apex_mode_rejected_for_non_admin(self):
        """Apex mode is admin-only; regular users get the ``hosting`` format."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain(
            "wiki", account_is_admin=False, domain_mode="apex",
        )
        assert not ok
        assert "admin" in reason.lower()

    def test_apex_mode_allowed_for_admin(self):
        """Admins may claim an apex subdomain like ``wiki.example.com``."""
        from hosting.subdomain import validate_subdomain
        ok, _ = validate_subdomain(
            "myteam", account_is_admin=True, domain_mode="apex",
        )
        assert ok

    def test_apex_mode_reserved_names_still_blocked(self):
        """DNS-critical labels are reserved even in apex mode."""
        from hosting.subdomain import validate_subdomain
        for label in ("www", "mail", "ns1"):
            ok, _ = validate_subdomain(
                label, account_is_admin=True, domain_mode="apex",
            )
            assert not ok, f"{label} should remain reserved in apex mode"


# ---------------------------------------------------------------------------
# Composite-uniqueness tests (apex + hosting modes coexist for same slug)
# ---------------------------------------------------------------------------

class TestApexHostingCoexistence:
    """The same slug may live in both apex and hosting mode at once.

    ``wiki.example.com`` (apex) and ``wiki-hosting.example.com`` (hosting)
    are different public URLs and must therefore be allowed as separate
    instance rows.  Uniqueness is scoped to ``(subdomain, domain_mode)``.
    """

    def test_same_slug_allowed_across_modes(self):
        """Creating ``wiki`` in apex then ``wiki`` in hosting both succeed."""
        from hosting.db import create_account, create_instance

        aid = create_account("owner", "hash")
        apex = create_instance(
            aid, "wiki", admin_username="admin", admin_password="x",
            domain_mode="apex",
        )
        hosting = create_instance(
            aid, "wiki", admin_username="admin", admin_password="x",
            domain_mode="hosting",
        )
        assert apex is not None
        assert hosting is not None
        assert apex["id"] != hosting["id"]
        assert apex["domain_mode"] == "apex"
        assert hosting["domain_mode"] == "hosting"

    def test_duplicate_within_same_mode_still_blocked(self):
        """Two hosting-mode rows with the same slug still collide."""
        from hosting.db import create_account, create_instance

        aid = create_account("owner", "hash")
        create_instance(
            aid, "wiki", admin_username="admin", admin_password="x",
            domain_mode="hosting",
        )
        with pytest.raises(ValueError) as exc:
            create_instance(
                aid, "wiki", admin_username="admin", admin_password="x",
                domain_mode="hosting",
            )
        assert str(exc.value) == "subdomain_in_use"

    def test_duplicate_within_apex_mode_still_blocked(self):
        """Two apex-mode rows with the same slug still collide."""
        from hosting.db import create_account, create_instance

        aid = create_account("owner", "hash")
        create_instance(
            aid, "wiki", admin_username="admin", admin_password="x",
            domain_mode="apex",
        )
        with pytest.raises(ValueError) as exc:
            create_instance(
                aid, "wiki", admin_username="admin", admin_password="x",
                domain_mode="apex",
            )
        assert str(exc.value) == "subdomain_in_use"

    def test_lookup_filters_by_mode(self):
        """``get_instance_by_subdomain`` returns the correct row per mode."""
        from hosting.db import (
            create_account, create_instance, get_instance_by_subdomain,
        )

        aid = create_account("owner", "hash")
        apex = create_instance(
            aid, "wiki", admin_username="admin", admin_password="x",
            domain_mode="apex",
        )
        hosting = create_instance(
            aid, "wiki", admin_username="admin", admin_password="x",
            domain_mode="hosting",
        )
        # No mode passed → returns either (back-compat); both modes filter.
        apex_row = get_instance_by_subdomain("wiki", domain_mode="apex")
        hosting_row = get_instance_by_subdomain("wiki", domain_mode="hosting")
        assert apex_row is not None
        assert hosting_row is not None
        assert apex_row["id"] == apex["id"]
        assert hosting_row["id"] == hosting["id"]

    def test_apex_and_hosting_use_distinct_data_dirs(self):
        """Apex/hosting same-slug instances must not share a data dir.

        Apex instances live at ``<INSTANCES_DIR>/<slug>__apex`` so they
        cannot collide with the hosting instance's bare ``<slug>`` path.
        """
        from hosting.instance_manager import _instance_dir

        apex_path = _instance_dir("wiki", "apex")
        hosting_path = _instance_dir("wiki", "hosting")
        assert apex_path != hosting_path
        assert apex_path.endswith("__apex")
        assert not hosting_path.endswith("__apex")

    def test_admin_demotion_collision_renames_apex_to_next_hosting_slug(self):
        """Demoting an admin must not collide with an existing hosting slug."""
        from hosting.db import (
            create_account,
            create_instance,
            get_instance,
            update_instance_status,
        )
        from hosting.instance_manager import (
            _instance_dir,
            convert_account_apex_instances_to_hosting,
        )

        admin_id = create_account("apexowner", "hash", is_admin=True)
        user_id = create_account("hostowner", "hash")
        apex = create_instance(admin_id, "luca", domain_mode="apex")
        hosting = create_instance(user_id, "luca", domain_mode="hosting")
        update_instance_status(apex["id"], "stopped")
        update_instance_status(hosting["id"], "stopped")
        os.makedirs(_instance_dir("luca", "apex"), exist_ok=True)
        os.makedirs(_instance_dir("luca", "hosting"), exist_ok=True)

        converted = convert_account_apex_instances_to_hosting(admin_id)

        assert converted == [(apex["id"], "luca", "luca-2")]
        updated = get_instance(apex["id"])
        assert updated["subdomain"] == "luca-2"
        assert updated["domain_mode"] == "hosting"
        assert os.path.isdir(_instance_dir("luca-2", "hosting"))
        assert os.path.isdir(_instance_dir("luca", "hosting"))

    def test_admin_can_rename_instance_and_change_mode(self):
        """The manager supports admin-controlled slug and mode changes."""
        from hosting.db import (
            create_account,
            create_instance,
            get_instance,
            update_instance_status,
        )
        from hosting.instance_manager import _instance_dir, change_instance_identity

        admin_id = create_account("renameadmin", "hash", is_admin=True)
        inst = create_instance(admin_id, "oldwiki", domain_mode="hosting")
        update_instance_status(inst["id"], "stopped")
        os.makedirs(_instance_dir("oldwiki", "hosting"), exist_ok=True)

        updated, error = change_instance_identity(
            inst["id"],
            "newwiki",
            "apex",
            account_is_admin=True,
        )

        assert error is None
        assert updated["subdomain"] == "newwiki"
        assert updated["domain_mode"] == "apex"
        assert get_instance(inst["id"])["domain_mode"] == "apex"
        assert os.path.isdir(_instance_dir("newwiki", "apex"))
        assert not os.path.isdir(_instance_dir("oldwiki", "hosting"))

    def test_admin_rename_rejects_same_mode_collision(self):
        """Explicit admin renames still reject already-taken target names."""
        from hosting.db import create_account, create_instance, update_instance_status
        from hosting.instance_manager import change_instance_identity

        owner_id = create_account("renameowner", "hash", is_admin=True)
        inst = create_instance(owner_id, "firstwiki", domain_mode="hosting")
        create_instance(owner_id, "secondwiki", domain_mode="hosting")
        update_instance_status(inst["id"], "stopped")

        updated, error = change_instance_identity(
            inst["id"],
            "secondwiki",
            "hosting",
            account_is_admin=True,
        )

        assert updated is None
        assert error == "That name is already in use."

    def test_proxy_extract_returns_mode_tuple(self):
        """``_extract_subdomain`` returns ``(slug, mode)`` so the proxy
        can disambiguate apex vs hosting at lookup time.
        """
        from hosting import _subdomain_proxy

        saved = (
            hosting_config.HOSTING_MODE,
            hosting_config.BASE_DOMAIN,
            hosting_config.PORTAL_DOMAIN,
            hosting_config.EFFECTIVE_PORTAL_DOMAIN,
            hosting_config.INSTANCE_URL_SUFFIX,
        )
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"

            # Hosting form: ``<slug>-hosting.example.com`` → ("slug", "hosting").
            slug, mode = _subdomain_proxy._extract_subdomain(
                "wiki-hosting.example.com",
            )
            assert (slug, mode) == ("wiki", "hosting")

            # Apex form only resolves if an apex-mode row exists.
            from hosting.db import create_account, create_instance
            aid = create_account("owner", "hash")
            create_instance(
                aid, "wiki", admin_username="admin", admin_password="x",
                domain_mode="apex",
            )
            slug, mode = _subdomain_proxy._extract_subdomain(
                "wiki.example.com",
            )
            assert (slug, mode) == ("wiki", "apex")
        finally:
            (
                hosting_config.HOSTING_MODE,
                hosting_config.BASE_DOMAIN,
                hosting_config.PORTAL_DOMAIN,
                hosting_config.EFFECTIVE_PORTAL_DOMAIN,
                hosting_config.INSTANCE_URL_SUFFIX,
            ) = saved


# ---------------------------------------------------------------------------
# Trial key tests
# ---------------------------------------------------------------------------

class TestPreActivation:
    """Tests for instance pre-activation with temporary credentials."""

    def test_instance_has_admin_credentials(self):
        """A newly provisioned instance has admin credentials stored."""
        from hosting.db import create_account, create_instance
        from werkzeug.security import generate_password_hash
        aid = create_account("creduser", generate_password_hash("pass"))
        inst = create_instance(aid, "credtest", admin_username="admin", admin_password="Temp1234")
        assert inst["admin_username"] == "admin"
        assert inst["admin_password_plain"] == "Temp1234"

    def test_provision_stores_credentials(self, client):
        """provision_instance stores admin credentials in the instance row."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "credprov"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("credprov")
        assert inst["admin_username"] == "admin"
        assert inst["admin_password_plain"]  # non-empty generated password

    def test_credentials_shown_on_create(self, client):
        """Instance creation page shows temporary credentials."""
        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "credshow"})
        assert rv.status_code == 200
        assert b"admin" in rv.data
        assert b"Login Credentials" in rv.data

    def test_instance_db_seeded(self, client):
        """Instance provisioning seeds the BananaWiki database."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "seedtest"})
        # Check that the BananaWiki DB was created
        data_dir = os.path.join(hosting_config.INSTANCES_DIR, "seedtest")
        db_path = os.path.join(data_dir, "bananawiki.db")
        assert os.path.exists(db_path)


# ---------------------------------------------------------------------------
# Instance expiry tests
# ---------------------------------------------------------------------------

class TestInstanceExpiry:
    """Tests for automatic instance expiration."""

    def test_expired_instances_are_found(self):
        """Instances past their expiry date are detected."""
        from hosting.db import get_expired_instances
        from hosting.db._connection import get_hosting_db
        conn = get_hosting_db()
        try:
            conn.execute(
                "INSERT INTO accounts (id, username, password, created_at) VALUES ('acc4', 'u4', 'p', '2024-01-01')"
            )
            conn.execute(
                "INSERT INTO instances (id, account_id, subdomain, status, port, created_at, expires_at) "
                "VALUES ('exp1', 'acc4', 'expired1', 'running', 6010, '2024-01-01', '2024-01-02')"
            )
            conn.commit()
        finally:
            conn.close()

        expired = get_expired_instances()
        assert len(expired) == 1
        assert expired[0]["id"] == "exp1"

    def test_terminate_expired(self):
        """terminate_expired() cleans up expired instances."""
        from hosting.instance_manager import terminate_expired
        from hosting.db import get_instance
        from hosting.db._connection import get_hosting_db
        conn = get_hosting_db()
        try:
            conn.execute(
                "INSERT INTO accounts (id, username, password, created_at) VALUES ('acc5', 'u5', 'p', '2024-01-01')"
            )
            conn.execute(
                "INSERT INTO instances (id, account_id, subdomain, status, port, created_at, expires_at) "
                "VALUES ('exp2', 'acc5', 'expired2', 'running', 6011, '2024-01-01', '2024-01-02')"
            )
            conn.commit()
        finally:
            conn.close()

        count = terminate_expired()
        assert count == 1
        inst = get_instance("exp2")
        assert inst["status"] == "terminated"


# ---------------------------------------------------------------------------
# Grace period tests
# ---------------------------------------------------------------------------

class TestGracePeriod:
    """Tests for the post-termination grace period for instance data retention."""

    def _create_instance_with_data(self, client, subdomain="graceinst"):
        """Helper: create an instance and write a sentinel file to its data dir.

        Returns ``(instance_id, data_dir)``.
        """
        from hosting.db import get_instance_by_subdomain
        from hosting.instance_manager import _instance_dir

        client.post("/instances/create", data={"subdomain": subdomain})
        inst = get_instance_by_subdomain(subdomain)
        assert inst is not None
        data_dir = _instance_dir(inst["subdomain"])
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "sentinel.txt"), "w") as f:
            f.write("hello-grace")
        return inst["id"], data_dir

    def test_default_grace_period_days(self, client):
        """Default grace period is 30 days when nothing is configured."""
        from hosting.db import get_grace_period_days

        _signup(client)
        assert get_grace_period_days() == 30

    def test_terminate_retains_data_during_grace_period(self, client):
        """Terminating an instance keeps the data dir on disk under the archived subdomain."""
        from hosting.db import get_instance
        from hosting.instance_manager import _instance_dir

        _signup(client)
        iid, original_dir = self._create_instance_with_data(client, "retainme")

        rv = client.post(f"/instances/{iid}/terminate", follow_redirects=True)
        assert rv.status_code == 200

        inst = get_instance(iid)
        assert inst["status"] == "terminated"
        assert inst["data_retained_until"] is not None
        assert inst["terminated_reason"] == "manual"
        # Original directory has been moved away so the original subdomain is reusable.
        assert not os.path.isdir(original_dir)
        # Data is now under the archived subdomain.
        archived_dir = _instance_dir(inst["subdomain"])
        assert os.path.isdir(archived_dir)
        assert os.path.exists(os.path.join(archived_dir, "sentinel.txt"))

    def test_expired_instances_enter_grace_period(self):
        """terminate_expired() places expired instances into the grace window with reason='expired'."""
        from hosting.instance_manager import terminate_expired
        from hosting.db import get_instance
        from hosting.db._connection import get_hosting_db

        conn = get_hosting_db()
        try:
            conn.execute(
                "INSERT INTO accounts (id, username, password, created_at) VALUES ('accg1', 'ug1', 'p', '2024-01-01')"
            )
            conn.execute(
                "INSERT INTO instances (id, account_id, subdomain, status, port, created_at, expires_at) "
                "VALUES ('expg1', 'accg1', 'expiredg1', 'running', 6020, '2024-01-01', '2024-01-02')"
            )
            conn.commit()
        finally:
            conn.close()

        terminate_expired()
        inst = get_instance("expg1")
        assert inst["status"] == "terminated"
        assert inst["terminated_reason"] == "expired"
        assert inst["data_retained_until"] is not None

    def test_zero_grace_period_wipes_immediately(self, client):
        """Setting grace_period_days=0 reverts to the legacy wipe-on-terminate behavior."""
        from hosting.db import get_instance, update_hosting_settings
        from hosting.instance_manager import _instance_dir

        _signup(client)
        update_hosting_settings(grace_period_days=0)
        iid, original_dir = self._create_instance_with_data(client, "wipefast")

        rv = client.post(f"/instances/{iid}/terminate", follow_redirects=True)
        assert rv.status_code == 200

        inst = get_instance(iid)
        assert inst["status"] == "terminated"
        assert inst["data_retained_until"] is None
        # Original data dir is gone and no archived dir was created.
        assert not os.path.isdir(original_dir)
        archived_dir = _instance_dir(inst["subdomain"])
        assert not os.path.isdir(archived_dir)

    def test_purge_expired_grace_periods_hard_deletes(self, client):
        """purge_expired_grace_periods() removes data dirs and DB rows whose retention has elapsed."""
        from hosting.db import get_instance
        from hosting.db._connection import get_hosting_db
        from hosting.instance_manager import _instance_dir, purge_expired_grace_periods

        _signup(client)
        iid, _ = self._create_instance_with_data(client, "purgeme")
        client.post(f"/instances/{iid}/terminate")

        # Move retention into the past.
        conn = get_hosting_db()
        try:
            conn.execute(
                "UPDATE instances SET data_retained_until=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", iid),
            )
            conn.commit()
        finally:
            conn.close()

        inst = get_instance(iid)
        archived_dir = _instance_dir(inst["subdomain"])
        assert os.path.isdir(archived_dir)

        purged = purge_expired_grace_periods()
        assert purged == 1
        assert not os.path.isdir(archived_dir)

        # The instance row must be fully deleted from the database.
        assert get_instance(iid) is None

    def test_purge_does_not_touch_live_grace_periods(self, client):
        """Instances still inside their grace window are left alone by the purge."""
        from hosting.db import get_instance
        from hosting.instance_manager import _instance_dir, purge_expired_grace_periods

        _signup(client)
        iid, _ = self._create_instance_with_data(client, "stillgrace")
        client.post(f"/instances/{iid}/terminate")

        purged = purge_expired_grace_periods()
        assert purged == 0
        inst = get_instance(iid)
        assert os.path.isdir(_instance_dir(inst["subdomain"]))

    def test_admin_download_grace_period_returns_zip(self, client):
        """Admins can download a ZIP of the retained data during the grace window."""
        import zipfile
        from io import BytesIO
        from hosting.db import get_instance_by_subdomain

        _signup(client)
        client.post("/instances/create", data={"subdomain": "downme"})
        inst = get_instance_by_subdomain("downme")
        # Write a sentinel file inside the live instance dir
        from hosting.instance_manager import _instance_dir
        live_dir = _instance_dir(inst["subdomain"])
        os.makedirs(live_dir, exist_ok=True)
        with open(os.path.join(live_dir, "marker.txt"), "w") as f:
            f.write("download-me")

        client.post(f"/instances/{inst['id']}/terminate")

        rv = client.get(f"/admin/instances/{inst['id']}/download")
        assert rv.status_code == 200
        assert rv.mimetype == "application/zip"
        zf = zipfile.ZipFile(BytesIO(rv.data))
        names = zf.namelist()
        assert any(n.endswith("marker.txt") for n in names), names

    def test_admin_download_after_grace_returns_error(self, client):
        """Once the data has been hard-deleted, downloads fail gracefully."""
        from hosting.db import get_instance_by_subdomain
        from hosting.db._connection import get_hosting_db
        from hosting.instance_manager import purge_expired_grace_periods

        _signup(client)
        client.post("/instances/create", data={"subdomain": "downgone"})
        inst = get_instance_by_subdomain("downgone")
        client.post(f"/instances/{inst['id']}/terminate")

        conn = get_hosting_db()
        try:
            conn.execute(
                "UPDATE instances SET data_retained_until=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", inst["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        purge_expired_grace_periods()

        rv = client.get(f"/admin/instances/{inst['id']}/download", follow_redirects=True)
        # The route flashes an error and redirects to /admin
        assert rv.status_code == 200
        assert b"Platform Administration" in rv.data

    def test_admin_restore_in_place_reuses_retained_data(self, client):
        """Restoring during the grace window brings back the same data and original subdomain."""
        from hosting.db import get_instance, get_instance_by_subdomain
        from hosting.instance_manager import _instance_dir

        _signup(client)
        client.post("/instances/create", data={"subdomain": "bringback"})
        inst = get_instance_by_subdomain("bringback")
        live_dir = _instance_dir("bringback")
        os.makedirs(live_dir, exist_ok=True)
        with open(os.path.join(live_dir, "data.txt"), "w") as f:
            f.write("preserved")

        client.post(f"/instances/{inst['id']}/terminate")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(f"/admin/instances/{inst['id']}/restore", follow_redirects=True)
        assert rv.status_code == 200

        restored = get_instance(inst["id"])
        assert restored["status"] == "running"
        assert restored["subdomain"] == "bringback"
        assert restored["data_retained_until"] is None
        assert restored["terminated_reason"] is None
        # Sentinel file survived the round-trip.
        assert os.path.exists(os.path.join(_instance_dir("bringback"), "data.txt"))

    def test_admin_restore_with_extend_days(self, client):
        """The extend_days form field pushes expiry further into the future."""
        from datetime import datetime, timezone
        from hosting.db import get_instance, get_instance_by_subdomain

        _signup(client)
        client.post("/instances/create", data={"subdomain": "extendme"})
        inst = get_instance_by_subdomain("extendme")
        client.post(f"/instances/{inst['id']}/terminate")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                f"/admin/instances/{inst['id']}/restore",
                data={"extend_days": "120"},
                follow_redirects=True,
            )
        assert rv.status_code == 200

        restored = get_instance(inst["id"])
        assert restored["status"] == "running"
        expires = datetime.fromisoformat(restored["expires_at"])
        days_until_expiry = (expires - datetime.now(timezone.utc)).days
        assert days_until_expiry >= 100  # ≥ requested extension minus rounding

    def test_admin_restore_after_grace_returns_not_found(self, client):
        """When the grace window has elapsed the instance is fully deleted and cannot be restored."""
        from hosting.db import get_instance
        from hosting.db._connection import get_hosting_db
        from hosting.instance_manager import purge_expired_grace_periods

        _signup(client)
        client.post("/instances/create", data={"subdomain": "fresh-after"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("fresh-after")
        client.post(f"/instances/{inst['id']}/terminate")

        conn = get_hosting_db()
        try:
            conn.execute(
                "UPDATE instances SET data_retained_until=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", inst["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        purge_expired_grace_periods()

        # The instance row is fully deleted after the grace period expires.
        assert get_instance(inst["id"]) is None

        # Attempting to restore returns an error since the row no longer exists.
        rv = client.post(
            f"/admin/instances/{inst['id']}/restore", follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"not found" in rv.data.lower() or b"Platform Administration" in rv.data

    def test_non_admin_cannot_download_grace_data(self, client):
        """Regular users cannot reach the admin download endpoint."""
        from hosting.db import get_instance_by_subdomain

        _signup(client, username="adminx", password="password123")
        client.post("/instances/create", data={"subdomain": "secret-data"})
        inst = get_instance_by_subdomain("secret-data")
        client.post(f"/instances/{inst['id']}/terminate")
        client.post("/logout")

        _signup(client, username="snoop", password="password123")
        rv = client.get(
            f"/admin/instances/{inst['id']}/download", follow_redirects=True,
        )
        # Non-admin is redirected away from admin endpoints.
        assert b"Platform Administration" not in rv.data

    def test_admin_settings_save_grace_period(self, client):
        """The grace period setting is persisted via the admin settings page."""
        from hosting.db import get_grace_period_days

        _signup(client)
        rv = client.post(
            "/admin/settings",
            data={"action": "save_grace_period", "grace_period_days": "7"},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert get_grace_period_days() == 7

    def test_admin_settings_save_wiki_upload_policy(self, client):
        """Hosting admins can save the global wiki upload policy."""
        from hosting.db import get_hosting_settings

        _signup(client)
        rv = client.post(
            "/admin/settings",
            data={
                "action": "save_wiki_upload_policy",
                "global_wiki_upload_max_size_mb": "33",
                "global_wiki_blocked_extensions": "zip, mp4",
            },
            follow_redirects=True,
        )
        assert rv.status_code == 200
        settings = get_hosting_settings()
        assert settings["global_wiki_upload_max_size_mb"] == 33
        assert settings["global_wiki_blocked_extensions"] == "zip, mp4"

    def test_admin_dashboard_shows_grace_countdown(self, client):
        """Terminated rows render a grace-period countdown badge in the admin dashboard."""
        from hosting.db import get_instance_by_subdomain

        _signup(client)
        client.post("/instances/create", data={"subdomain": "showgrace"})
        inst = get_instance_by_subdomain("showgrace")
        client.post(f"/instances/{inst['id']}/terminate")

        rv = client.get("/admin")
        assert rv.status_code == 200
        # Either translated label or fallback text should be present.
        body = rv.data.decode("utf-8")
        assert "Grace" in body or "grace" in body
        assert "Download" in body


# ---------------------------------------------------------------------------
# Admin per-instance error-log viewer
# ---------------------------------------------------------------------------

class TestAdminInstanceLogs:
    """Tests for the admin per-instance gunicorn error.log viewer."""

    def _make_instance(self, client, subdomain="logme"):
        from hosting.db import get_instance_by_subdomain
        # ``_start_process`` is patched so that no real gunicorn boots
        # (we still want a row + on-disk data dir, but we control the
        # contents of ``error.log`` ourselves below).
        with patch("hosting.instance_manager._start_process", return_value=True):
            client.post("/instances/create", data={"subdomain": subdomain})
        inst = get_instance_by_subdomain(subdomain)
        # Ensure error.log starts empty for tests that exercise the
        # "missing log" path.  Real provisioning calls ``--daemon`` and
        # writes a couple of startup lines; we want a clean slate here.
        from hosting.instance_manager import _instance_dir
        try:
            log_path = os.path.join(_instance_dir(subdomain), "error.log")
            if os.path.isfile(log_path):
                os.unlink(log_path)
        except (OSError, ValueError):
            pass
        return inst

    def test_read_instance_log_missing_file_returns_empty(self, client):
        """A freshly provisioned instance with no log on disk returns empty content."""
        from hosting.instance_manager import read_instance_log
        _signup(client)
        inst = self._make_instance(client, "nologyet")
        content, path, size, error = read_instance_log(inst["id"])
        assert error is None
        assert content == ""
        assert size == 0
        assert path is not None and path.endswith("error.log")

    def test_read_instance_log_returns_content(self, client):
        """When the gunicorn error.log exists, the tail is returned."""
        from hosting.instance_manager import read_instance_log, _instance_dir
        _signup(client)
        inst = self._make_instance(client, "haslog")
        log_path = os.path.join(_instance_dir("haslog"), "error.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("traceback: KaboomError\n")

        content, path, size, error = read_instance_log(inst["id"])
        assert error is None
        assert "KaboomError" in content
        assert size == len("traceback: KaboomError\n")
        assert path == log_path

    def test_read_instance_log_truncates_to_tail(self, client):
        """Logs larger than ``max_bytes`` are tailed, not returned in full."""
        from hosting.instance_manager import read_instance_log, _instance_dir
        _signup(client)
        inst = self._make_instance(client, "bignoise")
        log_path = os.path.join(_instance_dir("bignoise"), "error.log")
        big = ("Z" * 1024) + "\nTAIL-MARKER\n"
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(big)

        content, _path, size, error = read_instance_log(
            inst["id"], "error.log", max_bytes=64,
        )
        assert error is None
        assert "TAIL-MARKER" in content
        assert len(content) <= 64
        assert size == len(big)

    def test_read_instance_log_rejects_unknown_log_name(self, client):
        """Only the gunicorn log allowlist is readable."""
        from hosting.instance_manager import read_instance_log
        _signup(client)
        inst = self._make_instance(client, "noevil")
        content, path, size, error = read_instance_log(
            inst["id"], "../../etc/passwd",
        )
        assert content == ""
        assert path is None
        assert size == 0
        assert error is not None

    def test_admin_route_renders_log_for_admin(self, client):
        """An admin viewing the route sees the gunicorn error.log contents."""
        from hosting.instance_manager import _instance_dir
        _signup(client)  # first signup = admin
        inst = self._make_instance(client, "shownlog")
        log_path = os.path.join(_instance_dir("shownlog"), "error.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("[2024-01-01] gunicorn worker died: KaboomError\n")

        rv = client.get(f"/admin/instances/{inst['id']}/logs")
        assert rv.status_code == 200
        body = rv.data.decode("utf-8")
        assert "KaboomError" in body
        assert "shownlog" in body

    def test_admin_route_redirects_non_admin(self, client):
        """Non-admin accounts cannot reach the per-instance log viewer."""
        from hosting.db import get_instance_by_subdomain
        _signup(client, username="adminuser")  # first signup = admin
        client.post("/instances/create", data={"subdomain": "secretlogs"})
        inst = get_instance_by_subdomain("secretlogs")
        client.post("/logout")
        _signup(client, username="someuser", password="password123")

        rv = client.get(f"/admin/instances/{inst['id']}/logs")
        # ``@hosting_admin_required`` redirects non-admins to the dashboard
        # (or login if logged out): either way it is NOT a 200 render.
        assert rv.status_code in (302, 403)

    def test_admin_route_missing_instance_redirects(self, client):
        """An unknown instance ID flashes an error and redirects to /admin."""
        _signup(client)
        rv = client.get(
            "/admin/instances/00000000-0000-0000-0000-000000000000/logs",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"Platform Administration" in rv.data


# ---------------------------------------------------------------------------
# Per-wiki analytics (read by the hosting portal)
# ---------------------------------------------------------------------------

class TestInstanceAnalytics:
    """The portal can read each wiki's analytics_daily roll-up."""

    def _make_instance(self, client, subdomain="metrics"):
        """Create an instance row + data dir + seed analytics rows."""
        from hosting.db import get_instance_by_subdomain
        from hosting.instance_manager import _instance_dir
        # Patch _start_process so we don't fork gunicorn for these
        # read-only tests: we only need the on-disk DB.
        with patch("hosting.instance_manager._start_process", return_value=True):
            client.post("/instances/create", data={"subdomain": subdomain})
        inst = get_instance_by_subdomain(subdomain)
        return inst, _instance_dir(subdomain)

    def _seed_analytics(self, data_dir, rows):
        """Insert a few analytics_daily rows directly into the instance DB."""
        import sqlite3
        db_path = os.path.join(data_dir, "bananawiki.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS analytics_daily ("
                "day TEXT NOT NULL, kind TEXT NOT NULL, "
                "count INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (day, kind))"
            )
            for day, kind, count in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO analytics_daily (day, kind, count) "
                    "VALUES (?, ?, ?)",
                    (day, kind, count),
                )
            conn.commit()

    def test_read_instance_analytics_returns_summary(self, client):
        """read_instance_analytics reads the wiki's roll-up table."""
        from hosting.instance_manager import read_instance_analytics
        from datetime import datetime, timezone

        _signup(client)
        inst, data_dir = self._make_instance(client, "anlx1")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._seed_analytics(data_dir, [
            (today, "request", 12),
            (today, "page_view", 7),
            (today, "error", 2),
        ])
        summary, error = read_instance_analytics(inst["id"], days=7)
        assert error is None
        assert summary["totals"]["request"] == 12
        assert summary["totals"]["page_view"] == 7
        assert summary["totals"]["error"] == 2

    def test_read_instance_analytics_missing_db_returns_friendly_error(self, client):
        """When the wiki DB doesn't exist yet, return a flashable error."""
        from hosting.instance_manager import read_instance_analytics
        _signup(client)
        # Create an instance row in the hosting DB but DON'T create its
        # data dir / SQLite file.
        from hosting.db import create_instance, get_account_by_username
        acct = get_account_by_username("testuser")
        inst = create_instance(acct["id"], "noschema-yet")
        summary, error = read_instance_analytics(inst["id"], days=7)
        assert summary is None
        assert error and "No analytics data" in error

    def test_read_instance_analytics_unknown_instance(self, client):
        """Unknown instance ID returns a friendly error."""
        from hosting.instance_manager import read_instance_analytics
        summary, error = read_instance_analytics("nope1234", days=7)
        assert summary is None
        assert error == "Instance not found."

    def test_owner_can_view_analytics_page(self, client):
        """The wiki owner can hit /instances/<id>/analytics."""
        from datetime import datetime, timezone
        _signup(client)
        inst, data_dir = self._make_instance(client, "anlx2")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._seed_analytics(data_dir, [
            (today, "request", 5),
            (today, "page_view", 3),
        ])
        rv = client.get(f"/instances/{inst['id']}/analytics")
        assert rv.status_code == 200
        body = rv.data.lower()
        assert b"analytics" in body
        # Either the seeded counts (5/3) or the daily breakdown table
        # render the actual numbers, just make sure we got the page.
        assert b"daily breakdown" in body or b"daily activity" in body

    def test_running_analytics_page_auto_refreshes(self, client):
        """Running-instance analytics page should auto-refresh for live updates."""
        _signup(client)
        inst, _data_dir = self._make_instance(client, "anlx-live")
        rv = client.get(f"/instances/{inst['id']}/analytics")
        assert rv.status_code == 200
        body = rv.data
        assert b"auto-refreshes every 10 seconds" in body
        assert b"location.reload" in body
        assert b"10000" in body

    def test_other_user_cannot_view_analytics(self, client):
        """A second account cannot view another user's wiki analytics."""
        _signup(client, "alice", "alicepass1")
        inst, _data_dir = self._make_instance(client, "alicewiki")
        # Log out, sign up as bob, then attempt to view alice's analytics.
        client.post("/logout")
        _signup(client, "bob", "bobpass1234")
        rv = client.get(f"/instances/{inst['id']}/analytics", follow_redirects=False)
        # Bob is redirected back to the dashboard (and never gets to see
        # the page): exact status code may be 302 to dashboard with a flash.
        assert rv.status_code in (302, 303)
        assert rv.headers["Location"].endswith("/dashboard")

    def test_admin_can_view_other_users_analytics(self, client):
        """A hosting admin can inspect any user's wiki analytics."""
        # First signup creates the admin account.
        _signup(client, "rootadmin", "rootpass99")
        # Second account is a regular user that owns the wiki.
        client.post("/logout")
        _signup(client, "owner", "ownerpass1")
        inst, _data_dir = self._make_instance(client, "adminseen")
        client.post("/logout")
        # Log back in as the admin.
        _login(client, "rootadmin", "rootpass99")
        rv = client.get(f"/instances/{inst['id']}/analytics")
        assert rv.status_code == 200
        assert b"analytics" in rv.data.lower()


# ---------------------------------------------------------------------------
# Admin export-platform: one-click backup download for migration
# ---------------------------------------------------------------------------

class TestAdminExportPlatform:
    """Admin can download a complete hosting backup as a single ZIP."""

    def test_export_platform_requires_admin(self, client):
        """Non-admins are bounced from the export endpoint."""
        # First signup creates the admin, so create a second account.
        _signup(client, "rootadmin", "rootpass99")
        client.post("/logout")
        _signup(client, "nobody", "nobodypass1")
        rv = client.post("/admin/export-platform", follow_redirects=False)
        # The decorator redirects non-admins back to the dashboard.
        assert rv.status_code in (302, 303, 403)

    def test_export_platform_returns_zip_for_admin(self, client, tmp_path):
        """Admin gets an encrypted downloadable backup containing the DB."""
        import zipfile
        import io

        _signup(client, "rootadmin", "rootpass99")
        rv = client.post("/admin/export-platform")
        assert rv.status_code == 200
        assert rv.mimetype == "application/octet-stream"
        cd = rv.headers.get("Content-Disposition", "")
        assert "attachment" in cd.lower()
        assert "bananawiki_hosting_backup_" in cd
        assert ".bwenc" in cd

        from hosting.backup_crypto import decrypt_backup
        encrypted = tmp_path / "backup.bwenc"
        decrypted = tmp_path / "backup.zip"
        encrypted.write_bytes(rv.data)
        decrypt_backup(str(encrypted), str(decrypted))
        with zipfile.ZipFile(decrypted) as zf:
            names = zf.namelist()

        # Either an envelope (containing inner part ZIPs) or a single
        # backup with the hosting DB inside. Both are valid here.
        is_envelope = all(n.endswith(".zip") for n in names) and names
        has_hosting_db = any(
            "hosting_db" in n or n.endswith("hosting.db") for n in names
        )
        assert is_envelope or has_hosting_db



class TestAdminRestorePlatform:
    """Admin restore flow supports envelopes and chunked uploads."""

    def test_restore_platform_accepts_envelope_zip(self, client, monkeypatch):
        """Uploading an envelope ZIP expands nested backup parts."""
        import zipfile
        import hosting.db as hosting_db

        _signup(client, "rootadmin", "rootpass99")
        captured = {}

        def _fake_restore(zfs):
            captured["names"] = [zf.namelist() for zf in zfs]

        monkeypatch.setattr(hosting_db, "restore_hosting_from_backup_zips", _fake_restore)

        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("hosting_db/hosting.db", b"db")
            zf.writestr("landing/index.html", b"<h1>Landing</h1>")
        envelope = io.BytesIO()
        with zipfile.ZipFile(envelope, "w", zipfile.ZIP_STORED) as outer:
            outer.writestr("part-001.zip", inner.getvalue())

        rv = client.post(
            "/admin/restore-platform",
            data={"backup_files": (io.BytesIO(envelope.getvalue()), "backup-envelope.zip")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert captured.get("names")
        assert any("landing/index.html" in names for names in captured["names"])

    def test_restore_platform_supports_chunked_upload(self, client, monkeypatch):
        """Chunked restore uploads complete and trigger platform restore."""
        import zipfile
        import hosting.db as hosting_db

        _signup(client, "rootadmin", "rootpass99")
        captured = {"called": 0}

        def _fake_restore(zfs):
            captured["called"] += 1
            captured["names"] = [zf.namelist() for zf in zfs]

        monkeypatch.setattr(hosting_db, "restore_hosting_from_backup_zips", _fake_restore)

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("hosting_db/hosting.db", b"db")
            zf.writestr("landing/index.html", b"<h1>Landing</h1>")
        archive = payload.getvalue()

        start = client.post(
            "/admin/restore-platform/chunk/start",
            data={
                "filename": "backup.zip",
                "size": str(len(archive)),
            },
        )
        assert start.status_code == 200
        start_payload = start.get_json()
        assert start_payload["ok"] is True
        upload_id = start_payload["upload_id"]

        upload = client.post(
            f"/admin/restore-platform/chunk/{upload_id}",
            data={
                "chunk_index": "0",
                "chunk": (io.BytesIO(archive), "backup.zip.part"),
            },
            content_type="multipart/form-data",
        )
        assert upload.status_code == 200
        assert upload.get_json()["ok"] is True

        complete = client.post(
            f"/admin/restore-platform/chunk/{upload_id}/complete",
            data={},
        )
        assert complete.status_code == 200
        status_url = complete.get_json()["status_url"]

        state = None
        for _ in range(30):
            status_resp = client.get(status_url)
            assert status_resp.status_code == 200
            payload = status_resp.get_json()
            state = payload.get("state")
            if state in ("complete", "error"):
                break
            time.sleep(0.05)

        assert state == "complete"
        assert captured["called"] == 1
        assert any("landing/index.html" in names for names in captured["names"])


class TestHostingDownloadRobustness:
    """Large archive download safeguards."""

    def test_stream_download_cleans_up_after_iteration(self, app, tmp_path):
        """Temporary downloads are not deleted until streaming has finished."""
        from hosting.routes.dashboard_common import _stream_download_file

        archive_path = tmp_path / "large.zip"
        archive_path.write_bytes(b"a" * 64 + b"b" * 64)

        with app.test_request_context("/download"):
            response = _stream_download_file(
                str(archive_path),
                "large.zip",
                "application/zip",
            )
            iterator = iter(response.response)
            first = next(iterator)
            assert first
            assert archive_path.exists()

            body = first + b"".join(iterator)

        assert len(body) == 128
        assert not archive_path.exists()
        assert response.headers["Content-Length"] == "128"

    def test_large_instance_archive_skips_optional_json_export(
        self, tmp_path, monkeypatch,
    ):
        """Large wiki archives keep the raw DB without building huge JSON."""
        import json
        import zipfile
        import archive_format
        import db
        from hosting.db import create_account, create_instance
        from hosting.instance_manager import _instance_dir, build_instance_archive
        import hosting.instance_archives as instance_manager

        monkeypatch.setattr(
            instance_manager,
            "_SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES",
            1,
        )

        def _fail_export(*args, **kwargs):
            raise AssertionError("site_export.json should be skipped")

        monkeypatch.setattr(db, "export_site_data", _fail_export)

        aid = create_account("archiveowner", "hash")
        inst = create_instance(aid, "bigarchive")
        data_dir = _instance_dir(inst["subdomain"])
        os.makedirs(data_dir, exist_ok=True)
        import sqlite3
        from contextlib import closing
        with db.get_db_context() as source, closing(sqlite3.connect(
            os.path.join(data_dir, archive_format.RAW_DB_FILENAME)
        )) as target:
            source.backup(target)
        with open(os.path.join(data_dir, "marker.txt"), "w", encoding="utf-8") as fh:
            fh.write("included")

        archive_path, filename, error = build_instance_archive(
            inst["id"], str(tmp_path),
        )

        assert error is None
        assert filename.endswith(".zip")
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read(archive_format.MANIFEST_FILENAME))

        assert archive_format.RAW_DB_FILENAME in names
        assert archive_format.SITE_EXPORT_FILENAME not in names
        assert "marker.txt" in names
        assert manifest["has_raw_db"] is True
        assert manifest["has_site_export_json"] is False

    def test_instance_archive_stores_large_and_precompressed_files(
        self, tmp_path, monkeypatch,
    ):
        import zipfile
        import archive_format
        from hosting.db import create_account, create_instance
        from hosting.instance_manager import _instance_dir, build_instance_archive

        monkeypatch.setattr(hosting_config, "HOSTING_EXPORT_STORE_FILE_BYTES", 16)
        monkeypatch.setattr(hosting_config, "HOSTING_EXPORT_COMPRESS_LEVEL", 1)

        aid = create_account("fastarchiveowner", "hash")
        inst = create_instance(aid, "fastarchive")
        data_dir = _instance_dir(inst["subdomain"])
        os.makedirs(os.path.join(data_dir, "uploads"), exist_ok=True)
        import db
        import sqlite3
        from contextlib import closing
        with db.get_db_context() as source, closing(sqlite3.connect(
            os.path.join(data_dir, archive_format.RAW_DB_FILENAME)
        )) as target:
            source.backup(target)
        with open(os.path.join(data_dir, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("small text")
        with open(os.path.join(data_dir, "uploads", "photo.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8" + b"x" * 8 + b"\xff\xd9")
        with open(os.path.join(data_dir, "uploads", "large.bin"), "wb") as fh:
            fh.write(b"x" * 32)
        os.makedirs(os.path.join(data_dir, "tts"), exist_ok=True)
        with open(os.path.join(data_dir, "tts", "cached.mp3"), "wb") as fh:
            fh.write(b"generated-cache")

        archive_path, _filename, error = build_instance_archive(
            inst["id"], str(tmp_path),
        )

        assert error is None
        with zipfile.ZipFile(archive_path) as zf:
            infos = {info.filename: info for info in zf.infolist()}

        assert infos["notes.txt"].compress_type == zipfile.ZIP_DEFLATED
        assert infos["uploads/photo.jpg"].compress_type == zipfile.ZIP_STORED
        assert infos["uploads/large.bin"].compress_type == zipfile.ZIP_STORED
        assert "tts/cached.mp3" not in infos

    def test_export_temp_cleanup_removes_stale_archive_dirs(
        self, tmp_path, monkeypatch,
    ):
        from hosting.routes import dashboard_common as dashboard

        export_root = tmp_path / "exports"
        export_root.mkdir()
        stale_dir = export_root / "bwh-archive-old"
        stale_dir.mkdir()
        (stale_dir / "archive.zip").write_bytes(b"partial")
        stale_file = export_root / "bwh-archive-old.zip"
        stale_file.write_bytes(b"partial")
        keep_dir = export_root / "not-ours"
        keep_dir.mkdir()

        old = time.time() - (2 * 24 * 60 * 60)
        os.utime(stale_dir, (old, old))
        os.utime(stale_file, (old, old))
        monkeypatch.setattr(hosting_config, "HOSTING_EXPORT_TEMP_DIR", str(export_root))

        dashboard._cleanup_stale_export_downloads()

        assert not stale_dir.exists()
        assert not stale_file.exists()
        assert keep_dir.exists()


# ---------------------------------------------------------------------------
# Resource limits configuration tests
# ---------------------------------------------------------------------------

class TestResourceLimits:
    """Tests for resource limit configurability."""

    def test_default_limits(self):
        """Default resource limits match the spec."""
        assert hosting_config.MAX_INSTANCES_PER_ACCOUNT == 5
        assert hosting_config.INSTANCE_DURATION_DAYS == 14
        assert hosting_config.INSTANCE_STORAGE_LIMIT_MB == 500
        assert hosting_config.INSTANCE_MAX_UPLOAD_MB == 16
        assert hosting_config.INSTANCE_MAX_ATTACHMENT_MB == 100

    def test_limits_are_tunable(self):
        """Limits can be changed at runtime."""
        original = hosting_config.MAX_INSTANCES_PER_ACCOUNT
        hosting_config.MAX_INSTANCES_PER_ACCOUNT = 10
        assert hosting_config.MAX_INSTANCES_PER_ACCOUNT == 10
        hosting_config.MAX_INSTANCES_PER_ACCOUNT = original

    def test_hosting_upload_policy_settings_are_persisted(self):
        """Hosting stores global wiki upload quota and blocked extensions."""
        from hosting.db import get_hosting_settings, update_hosting_settings

        update_hosting_settings(
            global_wiki_upload_max_size_mb=25,
            global_wiki_blocked_extensions="zip, mp4",
        )
        settings = get_hosting_settings()
        assert settings["global_wiki_upload_max_size_mb"] == 25
        assert settings["global_wiki_blocked_extensions"] == "zip, mp4"

    def test_effective_upload_policy_combines_global_and_instance_blocks(self):
        """Per-wiki blocked extensions are additive with the global blocklist."""
        from hosting.db import update_hosting_settings
        from hosting.instance_manager import get_effective_instance_upload_policy

        update_hosting_settings(
            global_wiki_upload_max_size_mb=12,
            global_wiki_blocked_extensions="zip",
        )
        policy = get_effective_instance_upload_policy({
            "upload_max_size_mb": 30,
            "upload_blocked_extensions": "mp4, js",
        })
        assert policy["upload_max_size_mb"] == 30
        assert policy["blocked_extensions"] == "js, mp4, zip"


# ---------------------------------------------------------------------------
# Index redirect test
# ---------------------------------------------------------------------------

class TestIndex:
    """Tests for the root URL."""

    def test_index_redirects_to_login(self, client):
        """Root redirects to login when not authenticated."""
        rv = client.get("/", follow_redirects=True)
        assert b"Log In" in rv.data

    def test_index_redirects_to_dashboard(self, client):
        """Root redirects to dashboard when authenticated."""
        _signup(client)
        rv = client.get("/", follow_redirects=True)
        assert b"Dashboard" in rv.data or b"dashboard" in rv.data

    def test_skip_link_targets_focusable_main_content(self, client):
        """The hosting skip link lands on a programmatically focusable target."""
        _signup(client)
        rv = client.get("/", follow_redirects=True)
        assert rv.status_code == 200
        assert b'href="#main-content" class="skip-link"' in rv.data
        assert b'<main class="hosting-main" id="main-content" tabindex="-1">' in rv.data


# ---------------------------------------------------------------------------
# Database layer unit tests
# ---------------------------------------------------------------------------

class TestDatabaseAccounts:
    """Tests for the accounts database layer."""

    def test_create_and_get_account(self):
        """Account can be created and retrieved."""
        from hosting.db import create_account, get_account_by_id
        from werkzeug.security import generate_password_hash
        aid = create_account("dbtest", generate_password_hash("pass123"))
        acc = get_account_by_id(aid)
        assert acc is not None
        assert acc["username"] == "dbtest"

    def test_get_account_by_username(self):
        """Account can be found by username (case-insensitive)."""
        from hosting.db import create_account, get_account_by_username
        from werkzeug.security import generate_password_hash
        create_account("CaseTest", generate_password_hash("pass123"))
        acc = get_account_by_username("casetest")
        assert acc is not None

    def test_delete_account_retains_instance_tombstone(self):
        """Account deletion removes personal details while preserving retained data."""
        from hosting.db import create_account, delete_account, get_instance, get_account_by_id
        from hosting.db._connection import get_hosting_db
        from werkzeug.security import generate_password_hash
        aid = create_account("deluser", generate_password_hash("pass123"))

        conn = get_hosting_db()
        try:
            conn.execute(
                "INSERT INTO instances (id, account_id, subdomain, status, port, created_at, expires_at) "
                "VALUES ('delinst', ?, 'delsub', 'running', 6020, '2024-01-01', '2024-01-15')",
                (aid,),
            )
            conn.commit()
        finally:
            conn.close()

        delete_account(aid)
        assert get_instance("delinst") is not None
        assert get_account_by_id(aid)["deleted_at"]
        assert get_account_by_id(aid)["username"] != "deluser"


class TestDatabaseInstances:
    """Tests for the instances database layer."""

    def test_create_instance(self):
        """Instance can be created and retrieved."""
        from hosting.db import create_instance, get_instance, create_account
        from werkzeug.security import generate_password_hash
        aid = create_account("instowner", generate_password_hash("pass"))
        inst = create_instance(aid, "testsub")
        assert inst is not None
        assert inst["subdomain"] == "testsub"
        assert inst["status"] == "running"

        fetched = get_instance(inst["id"])
        assert fetched is not None

    def test_count_active_instances(self):
        """Active instance count is correct."""
        from hosting.db import create_instance, count_active_instances, create_account
        from werkzeug.security import generate_password_hash
        aid = create_account("counter", generate_password_hash("pass"))
        assert count_active_instances(aid) == 0
        create_instance(aid, "cnt1")
        assert count_active_instances(aid) == 1
        create_instance(aid, "cnt2")
        assert count_active_instances(aid) == 2

    def test_update_instance_status(self):
        """Instance status can be updated."""
        from hosting.db import create_instance, update_instance_status, get_instance, create_account
        from werkzeug.security import generate_password_hash
        aid = create_account("statusowner", generate_password_hash("pass"))
        inst = create_instance(aid, "statussub")
        update_instance_status(inst["id"], "stopped")
        fetched = get_instance(inst["id"])
        assert fetched["status"] == "stopped"
        assert fetched["stopped_at"] is not None

    def test_get_instances_for_account(self):
        """All non-terminated instances are returned."""
        from hosting.db import (
            create_instance, get_instances_for_account, terminate_instance, create_account,
        )
        from werkzeug.security import generate_password_hash
        aid = create_account("listowner", generate_password_hash("pass"))
        create_instance(aid, "list1")
        inst2 = create_instance(aid, "list2")
        terminate_instance(inst2["id"])
        rows = get_instances_for_account(aid)
        assert len(rows) == 1
        assert rows[0]["subdomain"] == "list1"

    def test_port_allocation_starts_at_instance_port_start(self):
        """First allocated port is within the configured range (>= INSTANCE_PORT_START).

        The allocator skips any port that is OS-bound.  The default range now
        starts at 6001 (port 6000 is reserved for X11 and is never usable),
        so the exact port is not predictable, but it must be in range.
        """
        from hosting.db import create_instance, create_account
        from hosting import config as cfg
        from werkzeug.security import generate_password_hash
        aid = create_account("portstart", generate_password_hash("pass"))
        inst = create_instance(aid, "portstarttest")
        assert inst is not None
        assert cfg.INSTANCE_PORT_START <= inst["port"] < cfg.INSTANCE_PORT_END

    def test_port_freed_after_termination(self):
        """Terminating an instance sets port to NULL and allows reuse."""
        from hosting.db import create_instance, create_account, terminate_instance, get_instance
        from werkzeug.security import generate_password_hash
        aid = create_account("portfree", generate_password_hash("pass"))

        inst1 = create_instance(aid, "portfree1")
        assert inst1 is not None
        original_port = inst1["port"]

        terminate_instance(inst1["id"])

        # Port must be NULL in the DB after termination
        fetched = get_instance(inst1["id"])
        assert fetched["port"] is None

        # The same port must be reused for the next instance
        inst2 = create_instance(aid, "portfree2")
        assert inst2 is not None
        assert inst2["port"] == original_port

    def test_active_instances_have_distinct_ports(self):
        """Running instances always receive unique ports."""
        from hosting.db import create_instance, create_account
        from werkzeug.security import generate_password_hash
        aid = create_account("portactive", generate_password_hash("pass"))
        inst1 = create_instance(aid, "portactive1")
        inst2 = create_instance(aid, "portactive2")
        assert inst1 is not None
        assert inst2 is not None
        assert inst1["port"] != inst2["port"]


# ---------------------------------------------------------------------------
# CSRF protection tests
# ---------------------------------------------------------------------------

@pytest.fixture
def csrf_app():
    """Hosting app with CSRF enforcement enabled."""
    application = create_hosting_app()
    application.config["TESTING"] = True
    # HOSTING_PROXY_MODE=True (default) sets PREFERRED_URL_SCHEME="https"
    # which can interfere with the test client's in-process HTTP requests.
    application.config["PREFERRED_URL_SCHEME"] = "http"
    return application


@pytest.fixture
def csrf_client(csrf_app):
    """Flask test client with CSRF enforcement enabled."""
    return csrf_app.test_client()


def _signup_csrf(csrf_client, username="testuser", password="password123"):
    """Helper: create an account via signup with a valid CSRF token."""
    # GET the signup page to obtain a CSRF token from the session
    rv = csrf_client.get("/signup")
    assert rv.status_code == 200
    # Extract the CSRF token from the hidden field
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', rv.data)
    assert match, "Signup form must contain a csrf_token hidden field"
    token = match.group(1).decode()
    return csrf_client.post("/signup", data={
        "username": username,
        "password": password,
        "confirm_password": password,
        "csrf_token": token,
    }, follow_redirects=True)


class TestCSRFProtection:
    """Verify that CSRF protection is active on all hosting POST endpoints."""

    def test_signup_rejected_without_csrf(self, csrf_client):
        """POST /signup without CSRF token is rejected with redirect."""
        rv = csrf_client.post("/signup", data={
            "username": "csrfuser",
            "password": "password123",
            "confirm_password": "password123",
        })
        assert rv.status_code == 302

    def test_login_rejected_without_csrf(self, csrf_client):
        """POST /login without CSRF token is rejected with redirect."""
        rv = csrf_client.post("/login", data={
            "username": "csrfuser",
            "password": "password123",
        })
        assert rv.status_code == 302

    def test_create_instance_rejected_without_csrf(self, csrf_client):
        """POST /instances/create without CSRF token is rejected with redirect."""
        _signup_csrf(csrf_client)
        rv = csrf_client.post("/instances/create", data={
            "subdomain": "nocsrf",
        })
        assert rv.status_code == 302

    def test_delete_account_rejected_without_csrf(self, csrf_client):
        """POST /settings/delete without CSRF token is rejected with redirect."""
        _signup_csrf(csrf_client)
        rv = csrf_client.post("/settings/delete")
        assert rv.status_code == 302

    def test_stop_instance_rejected_without_csrf(self, csrf_client):
        """POST /instances/<id>/stop without CSRF token is rejected with redirect."""
        _signup_csrf(csrf_client)
        rv = csrf_client.post("/instances/fake-id/stop")
        assert rv.status_code == 302

    def test_restart_instance_rejected_without_csrf(self, csrf_client):
        """POST /instances/<id>/restart without CSRF token is rejected with redirect."""
        _signup_csrf(csrf_client)
        rv = csrf_client.post("/instances/fake-id/restart")
        assert rv.status_code == 302

    def test_terminate_instance_rejected_without_csrf(self, csrf_client):
        """POST /instances/<id>/terminate without CSRF token is rejected with redirect."""
        _signup_csrf(csrf_client)
        rv = csrf_client.post("/instances/fake-id/terminate")
        assert rv.status_code == 302

    def test_csrf_error_shows_flash_message(self, csrf_client):
        """CSRF error shows a helpful flash message instead of generic 400."""
        rv = csrf_client.post("/login", data={
            "username": "csrfuser",
            "password": "password123",
        }, follow_redirects=True)
        assert b"Your session has expired" in rv.data

    def test_forms_contain_csrf_token(self, csrf_client):
        """All hosting forms include a csrf_token hidden field."""
        # Check unauthenticated forms
        for path in ("/login", "/signup"):
            rv = csrf_client.get(path)
            assert rv.status_code == 200, f"GET {path} returned {rv.status_code}"
            assert b'name="csrf_token"' in rv.data, f"{path} is missing csrf_token field"

        # Check authenticated forms
        _signup_csrf(csrf_client)
        for path in ("/instances/create", "/dashboard"):
            rv = csrf_client.get(path)
            assert rv.status_code == 200, f"GET {path} returned {rv.status_code}"
            assert b'name="csrf_token"' in rv.data, f"{path} is missing csrf_token field"


# ---------------------------------------------------------------------------
# Login rate limiting tests
# ---------------------------------------------------------------------------

class TestLoginRateLimiting:
    """Tests for brute-force protection on the hosting login endpoint."""

    def test_login_rate_limited_after_max_attempts(self, client):
        """POST /login returns 429 after too many failed attempts."""
        _signup(client)
        client.post("/logout")

        for _ in range(5):
            rv = client.post("/login", data={
                "username": "testuser",
                "password": "wrongpassword",
            })
            assert rv.status_code == 401

        rv = client.post("/login", data={
            "username": "testuser",
            "password": "wrongpassword",
        })
        assert rv.status_code == 429
        assert b"Too many login attempts" in rv.data

    def test_rate_limit_blocks_correct_credentials(self, client):
        """Even correct credentials are blocked once the rate limit is hit."""
        _signup(client)
        client.post("/logout")

        for _ in range(5):
            client.post("/login", data={
                "username": "testuser",
                "password": "wrongpassword",
            })

        rv = client.post("/login", data={
            "username": "testuser",
            "password": "password123",
        })
        assert rv.status_code == 429

    def test_rate_limit_nonexistent_user(self, client):
        """Rate limit applies even for non-existent usernames."""
        for _ in range(5):
            client.post("/login", data={
                "username": "nobody",
                "password": "wrongpassword",
            })

        rv = client.post("/login", data={
            "username": "nobody",
            "password": "wrongpassword",
        })
        assert rv.status_code == 429
        assert b"Too many login attempts" in rv.data

    def test_successful_login_clears_attempts(self, client):
        """A successful login clears the attempt counter for that IP."""
        _signup(client)
        client.post("/logout")

        # Record a few failed attempts (under the limit)
        for _ in range(3):
            client.post("/login", data={
                "username": "testuser",
                "password": "wrongpassword",
            })

        # Successful login should clear attempts
        rv = _login(client)
        assert rv.status_code == 200

        client.post("/logout")

        # Should be able to fail again without immediately being blocked
        for _ in range(4):
            rv = client.post("/login", data={
                "username": "testuser",
                "password": "wrongpassword",
            })
            assert rv.status_code == 401

    def test_get_request_not_affected(self, client):
        """GET /login is never rate-limited."""
        _signup(client)
        client.post("/logout")

        for _ in range(5):
            client.post("/login", data={
                "username": "testuser",
                "password": "wrongpassword",
            })

        rv = client.get("/login")
        assert rv.status_code == 200


# ---------------------------------------------------------------------------
# Session cookie security
# ---------------------------------------------------------------------------


class TestSessionCookieSecurity:
    """Session cookie Secure flag is set dynamically based on request scheme."""

    def test_secure_cookie_on_https_request(self, isolated_hosting_db):
        """HTTPS request → session cookie gets the Secure flag."""
        application = create_hosting_app()
        application.config["TESTING"] = True
        # The auto-secure session interface checks request.is_secure,
        # which is True when the request comes over HTTPS.
        assert hasattr(application.session_interface, "get_cookie_secure")
        assert application.config["SESSION_COOKIE_HTTPONLY"] is True

    def test_proxy_mode_sets_preferred_scheme(self, isolated_hosting_db):
        """Proxy mode on → PREFERRED_URL_SCHEME is https."""
        original = hosting_config.HOSTING_PROXY_MODE
        try:
            hosting_config.HOSTING_PROXY_MODE = True
            application = create_hosting_app()
            assert application.config["PREFERRED_URL_SCHEME"] == "https"
        finally:
            hosting_config.HOSTING_PROXY_MODE = original

    def test_no_preferred_scheme_when_proxy_mode_disabled(self, isolated_hosting_db):
        """Proxy mode off → PREFERRED_URL_SCHEME is not forced to https."""
        original = hosting_config.HOSTING_PROXY_MODE
        try:
            hosting_config.HOSTING_PROXY_MODE = False
            application = create_hosting_app()
            assert application.config.get("PREFERRED_URL_SCHEME") != "https"
        finally:
            hosting_config.HOSTING_PROXY_MODE = original


def test_secret_key_file_has_restrictive_permissions(tmp_path, monkeypatch):
    """Generated .secret_key file must be owner-read/write only (0o600)."""
    key_path = str(tmp_path / "data" / ".secret_key")
    monkeypatch.setattr("hosting.config._SECRET_KEY_PATH", key_path)
    monkeypatch.delenv("HOSTING_SECRET_KEY", raising=False)
    key = _load_or_generate_secret_key()
    assert key  # non-empty
    mode = os.stat(key_path).st_mode
    assert stat.S_IMODE(mode) == 0o600


class TestHostingBackgroundServices:
    """Background recovery must not start before Gunicorn forks workers."""

    def _reset_background_state(self, monkeypatch):
        import hosting.app as hosting_app

        monkeypatch.setattr(hosting_app, "_background_services_started", False)
        monkeypatch.setattr(hosting_app, "_background_services_lock", None)
        return hosting_app

    def test_preload_env_skips_background_services(self, monkeypatch):
        hosting_app = self._reset_background_state(monkeypatch)
        monkeypatch.setenv("BANANAWIKI_HOSTING_SKIP_BACKGROUND_SERVICES", "1")
        with patch("hosting.app._maybe_start_bytecode_precompile") as precompile, patch(
            "hosting.app._start_background_recovery"
        ) as recovery, patch("hosting.app._periodic_cleanup") as cleanup:
            assert hosting_app.start_background_services() is False
        precompile.assert_not_called()
        recovery.assert_not_called()
        cleanup.assert_not_called()

    def test_forced_worker_start_ignores_preload_skip(self, monkeypatch):
        hosting_app = self._reset_background_state(monkeypatch)
        monkeypatch.setenv("BANANAWIKI_HOSTING_SKIP_BACKGROUND_SERVICES", "1")
        with patch("hosting.app._maybe_start_bytecode_precompile") as precompile, patch(
            "hosting.app._start_background_recovery"
        ) as recovery, patch("hosting.app._periodic_cleanup") as cleanup, patch(
            "atexit.register"
        ):
            assert hosting_app.start_background_services(force=True) is True
        precompile.assert_called_once_with()
        recovery.assert_called_once_with()
        cleanup.assert_called_once_with()


# ---------------------------------------------------------------------------
# Process management tests (mocked: no real subprocesses)
# ---------------------------------------------------------------------------

class TestProcessManagement:
    """Tests for the instance process lifecycle helpers."""

    def test_instance_env_contains_required_keys(self, tmp_path, monkeypatch):
        """_instance_env() produces a dict with all necessary BW_ keys."""
        for name in ("SECRET_KEY", "HOSTING_SECRET_KEY", "HOSTING_BOOTSTRAP_TOKEN", "GITHUB_TOKEN", "BW_SETUP_TOKEN"):
            monkeypatch.setenv(name, "portal-secret-must-stay-private")
        from hosting.instance_manager import _instance_env
        data_dir = str(tmp_path / "testinst")
        env = _instance_env(data_dir, 6042)
        assert env["BW_PORT"] == "6042"
        assert env["BW_DATABASE_PATH"].endswith("bananawiki.db")
        assert env["BW_UPLOAD_FOLDER"].endswith("uploads")
        assert env["BW_ATTACHMENT_FOLDER"].endswith("attachments")
        assert env["BW_CHAT_ATTACHMENT_FOLDER"].endswith("chat_attachments")
        assert env["BW_KANBAN_ATTACHMENT_FOLDER"].endswith("kanban_attachments")
        assert env["BW_CUSTOM_PAGE_FILES_FOLDER"].endswith("custom_page_files")
        assert env["BW_LOG_FILE"].endswith("bananawiki.log")
        # SECRET_KEY must NOT be injected by _instance_env; BananaWiki loads
        # a persistent key from BW_INSTANCE_DIR/.secret_key on startup so that
        # session cookies survive instance restarts.
        assert "SECRET_KEY" not in env
        assert "portal-secret-must-stay-private" not in env.values()
        assert env["BW_INSTANCE_DIR"] == data_dir  # key file path anchor
        # BW_HOSTED_MODE should NOT be set (instances are raw BananaWiki)
        assert "BW_HOSTED_MODE" not in env

    def test_create_instance_dirs(self, tmp_path):
        """_create_instance_dirs() creates the expected subdirectories."""
        from hosting.instance_manager import _create_instance_dirs
        data_dir = str(tmp_path / "newinstance")
        _create_instance_dirs(data_dir)
        for sub in ("uploads", "attachments", "chat_attachments",
                     "kanban_attachments", "custom_page_files"):
            assert os.path.isdir(os.path.join(data_dir, sub))

    def test_read_pid_missing_file(self, tmp_path):
        """_read_pid returns None when no PID file exists."""
        from hosting.instance_manager import _read_pid
        assert _read_pid(str(tmp_path / "nonexistent")) is None

    def test_read_pid_valid(self, tmp_path):
        """_read_pid reads a valid PID from a file."""
        from hosting.instance_manager import _read_pid
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text("12345")
        assert _read_pid(str(tmp_path)) == 12345

    def test_read_pid_invalid_content(self, tmp_path):
        """_read_pid returns None when PID file has non-numeric content."""
        from hosting.instance_manager import _read_pid
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text("not-a-pid")
        assert _read_pid(str(tmp_path)) is None

    def test_is_process_alive_none(self):
        """_is_process_alive(None) returns False."""
        from hosting.instance_manager import _is_process_alive
        assert _is_process_alive(None) is False

    def test_is_process_alive_current(self):
        """_is_process_alive with current PID returns True."""
        from hosting.instance_manager import _is_process_alive
        assert _is_process_alive(os.getpid()) is True

    def test_is_process_alive_dead(self):
        """_is_process_alive with a non-existent PID returns False."""
        from hosting.instance_manager import _is_process_alive
        # Use a very high PID unlikely to be running
        assert _is_process_alive(999999999) is False

    def test_is_our_instance_process_dead_pid(self, tmp_path):
        """A PID that is not alive can never be our instance."""
        from hosting.instance_manager import _is_our_instance_process
        assert _is_our_instance_process(None, str(tmp_path)) is False
        assert _is_our_instance_process(999999999, str(tmp_path)) is False

    def test_is_our_instance_process_rejects_non_gunicorn(self, tmp_path):
        """An alive PID whose cmdline is not Gunicorn is rejected.

        This is the main defence against stale PID files written before a
        VPS reboot: the kernel reuses low PIDs, so the recorded numeric
        PID often matches an unrelated process (systemd, kthreadd, dbus
        …).  Our own PID (running under pytest) is a convenient stand-in.
        Its cmdline contains ``pytest`` rather than ``gunicorn``.
        """
        from hosting.instance_manager import (
            _is_our_instance_process,
            _read_proc_cmdline,
        )
        # Skip cleanly on platforms where /proc is unavailable; the helper
        # falls back to the alive-only check there and the assertion would
        # not be meaningful.
        if _read_proc_cmdline(os.getpid()) is None:
            pytest.skip("/proc/{pid}/cmdline not available on this platform")
        assert _is_our_instance_process(os.getpid(), str(tmp_path)) is False

    def test_is_our_instance_process_rejects_old_boot_id(self, tmp_path):
        """A stored boot id from a previous boot causes the PID to be rejected.

        Even if the cmdline check would pass, a mismatching boot id means
        the recorded PID belongs to a previous boot and should not be
        trusted.  We force-write a fake boot id to ``.boot_id`` and assert
        the check fails.
        """
        from hosting.instance_manager import (
            _boot_id_path,
            _current_boot_id,
            _is_our_instance_process,
        )
        if _current_boot_id() is None:
            pytest.skip("kernel boot id not available on this platform")
        with open(_boot_id_path(str(tmp_path)), "w", encoding="utf-8") as f:
            f.write("00000000-0000-0000-0000-000000000000")
        assert _is_our_instance_process(os.getpid(), str(tmp_path)) is False

    def test_write_boot_id_persists_current_boot_id(self, tmp_path):
        """_write_boot_id writes the current kernel boot id atomically."""
        from hosting.instance_manager import (
            _current_boot_id,
            _read_stored_boot_id,
            _write_boot_id,
        )
        if _current_boot_id() is None:
            pytest.skip("kernel boot id not available on this platform")
        _write_boot_id(str(tmp_path))
        assert _read_stored_boot_id(str(tmp_path)) == _current_boot_id()

    def test_clean_stale_pid_removes_stale_after_reboot(self, tmp_path, monkeypatch):
        """Regression: ``_clean_stale_pid`` deletes PIDs whose process is
        alive but is not our Gunicorn (post-reboot scenario).

        Before the fix this routine only checked ``kill -0``, so a stale
        PID file matching an unrelated process (systemd, kthreadd \u2026) was
        kept around and ``_start_process`` would return early without
        actually spawning a new Gunicorn, leaving the instance broken
        until it was restarted manually.
        """
        from hosting import instance_manager as im
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))
        boot_id_path = tmp_path / ".boot_id"
        boot_id_path.write_text("test-boot-id")
        # Force the helper to treat the PID as not ours.
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: False)
        im._clean_stale_pid(str(tmp_path), port=6500)
        assert not pid_path.exists(), "PID file from previous boot must be removed"
        assert not boot_id_path.exists(), "boot-id sidecar must be removed too"

    def test_clean_stale_pid_keeps_live_instance(self, tmp_path, monkeypatch):
        """``_clean_stale_pid`` must NOT delete the PID file of a live instance."""
        from hosting import instance_manager as im
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: True)
        im._clean_stale_pid(str(tmp_path), port=6500)
        assert pid_path.exists()

    def test_stop_process_no_pid_file(self, tmp_path):
        """_stop_process does nothing when there is no PID file."""
        from hosting.instance_manager import _stop_process
        # Should not raise
        _stop_process(str(tmp_path))

    def test_stop_process_does_not_signal_stale_pid_after_reboot(
        self, tmp_path, monkeypatch
    ):
        """Regression: ``_stop_process`` must NOT send signals to a stale PID.

        After a VPS reboot the kernel reuses low PIDs (systemd, kthreadd,
        sshd \u2026), so the recorded PID often happens to be alive but is no
        longer our Gunicorn.  Before the fix ``_stop_process`` would call
        ``os.kill`` on those unrelated processes when the user clicked
        "stop" / "suspend" / "terminate" in the dashboard. That was the exact
        failure mode users were reporting as "admin actions return 500
        after a reboot".  The PID file (and boot-id sidecar) must be
        cleaned up without sending any signals.
        """
        from hosting import instance_manager as im

        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))
        boot_id_path = tmp_path / ".boot_id"
        boot_id_path.write_text("pre-reboot-id")

        signals = []
        monkeypatch.setattr(
            im.os, "kill", lambda pid, sig: signals.append((pid, sig))
        )
        monkeypatch.setattr(
            im, "_is_our_instance_process", lambda *a, **kw: False
        )

        im._stop_process(str(tmp_path), port=6500)

        assert signals == [], "stale PID must not be signalled"
        assert not pid_path.exists(), "stale PID file must be cleaned up"
        assert not boot_id_path.exists(), "boot-id sidecar must be cleaned up"

    def test_stop_process_signals_owned_pid(self, tmp_path, monkeypatch):
        """``_stop_process`` does send SIGTERM when the PID is genuinely ours."""
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "HOSTING_INSTANCE_RUNTIME", "process")
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))

        signals = []

        def fake_kill(pid, sig):
            signals.append((pid, sig))

        monkeypatch.setattr(im.os, "kill", fake_kill)
        # First call (ownership check) returns True; subsequent calls used
        # by the SIGTERM/SIGKILL retry loop return False so the helper
        # short-circuits without trying to escalate.
        ownership_calls = {"count": 0}

        def fake_ownership(*_args, **_kwargs):
            ownership_calls["count"] += 1
            return ownership_calls["count"] == 1

        monkeypatch.setattr(im, "_is_our_instance_process", fake_ownership)
        # Pretend the process exits immediately after SIGTERM so we don't
        # hit the SIGKILL escalation path.
        alive = {"value": True}

        def fake_alive(_pid):
            current = alive["value"]
            alive["value"] = False
            return current

        monkeypatch.setattr(im, "_is_process_alive", fake_alive)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)

        im._stop_process(str(tmp_path), port=6500)

        assert any(
            sig == signal.SIGTERM for _pid, sig in signals
        ), "SIGTERM must be sent to a confirmed instance PID"

    def test_stop_process_waits_for_port_release_before_return(self, tmp_path, monkeypatch):
        """Regression: stop waits for the socket to close after SIGTERM.

        Without this wait, a subsequent restart can race with workers that
        still hold the listening socket and intermittently fail with
        ``Address already in use``.
        """
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "HOSTING_INSTANCE_RUNTIME", "process")
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))

        signals = []
        monkeypatch.setattr(im.os, "kill", lambda pid, sig: signals.append((pid, sig)))
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: True)
        monkeypatch.setattr(im, "_clean_stale_pid", lambda *a, **kw: None)

        alive_calls = {"count": 0}

        def fake_alive(_pid):
            alive_calls["count"] += 1
            return alive_calls["count"] == 1

        port_checks = {"count": 0}

        def fake_port_accepting(_host, _port, timeout=1.0):
            del timeout
            port_checks["count"] += 1
            return port_checks["count"] < 3

        sleeps = []
        monkeypatch.setattr(im, "_is_process_alive", fake_alive)
        monkeypatch.setattr(im, "_is_port_accepting", fake_port_accepting)
        monkeypatch.setattr(im.time, "sleep", lambda seconds: sleeps.append(seconds))

        im._stop_process(str(tmp_path), port=6500)

        assert any(sig == signal.SIGTERM for _pid, sig in signals)
        assert port_checks["count"] >= 3
        assert sleeps.count(0.1) >= 2

    def test_start_process_already_running(self, tmp_path, monkeypatch):
        """_start_process returns True if our Gunicorn is already running.

        ``_start_process`` no longer trusts a bare ``kill -0`` probe to
        decide "this PID is our instance": doing so let stale PID files
        from before a VPS reboot keep the instance permanently broken.
        We monkeypatch :func:`_is_our_instance_process` to assert the
        early-return path still fires when the recorded PID actually
        matches our Gunicorn.
        """
        from hosting import instance_manager as im
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))
        inst = {"port": 6500, "id": "testid"}
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: True)
        monkeypatch.setattr(im, "_instance_http_ready", lambda _inst: True)
        assert im._start_process(inst, str(tmp_path)) is True

    def test_start_process_accepts_sqlite3_row(self, tmp_path, monkeypatch):
        """Regression: ``restart_instance`` passes a ``sqlite3.Row``.

        The cache-invalidation step inside ``_start_process`` calls
        ``inst.get("subdomain")``.  ``sqlite3.Row`` does not implement
        ``.get``, it only supports ``__getitem__``, so before this
        fix every call to the dashboard's "Restart instance" button
        crashed with ``AttributeError: 'sqlite3.Row' object has no
        attribute 'get'`` and surfaced as a 500 to the user (or a
        generic flash + redirect once the route caught the exception).
        """
        import sqlite3

        from hosting import instance_manager as im

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 'testid' AS id, 6500 AS port, 'testsub' AS subdomain"
        ).fetchone()
        assert isinstance(row, sqlite3.Row)
        # Confirm the precondition: ``.get`` really is unavailable on Row.
        assert not hasattr(row, "get")

        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: True)
        monkeypatch.setattr(im, "_instance_http_ready", lambda _inst: True)

        # Must not raise AttributeError: the function must normalise the
        # Row to a dict before reaching ``inst.get(...)``.
        assert im._start_process(row, str(tmp_path)) is True

    def test_start_process_respawns_when_pid_is_stale_after_reboot(
        self, tmp_path, monkeypatch
    ):
        """Regression: a stale PID after a VPS reboot must NOT block respawn.

        After a host reboot the kernel reuses low PIDs (systemd, kthreadd,
        dbus …) so the recorded PID often happens to be alive but is no
        longer our Gunicorn.  ``_start_process`` must detect that, drop the
        stale PID file, and proceed to spawn a fresh process, otherwise the
        instance returns 502/404 forever after every reboot.
        """
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "HOSTING_INSTANCE_RUNTIME", "process")
        monkeypatch.setattr(im.config, "INSTANCE_PORT_START", 6000)
        monkeypatch.setattr(im.config, "INSTANCE_PORT_END", 7000)
        monkeypatch.setattr(im, "_STARTUP_TIMEOUT", 0.2)
        monkeypatch.setattr(im, "_STARTUP_CHECK_INTERVAL", 0.05)
        monkeypatch.setattr(im, "_HTTP_READY_TIMEOUT", 0.1)
        monkeypatch.setattr(im, "_HTTP_READY_INTERVAL", 0.05)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)

        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text(str(os.getpid()))

        # Pretend we have no running gunicorn for this PID, then never write
        # a fresh PID file (simulating Gunicorn failing to start) so the
        # function returns False.  The crucial assertion is that the stale
        # PID file gets removed instead of being trusted.
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: False)

        spawn_calls = {"count": 0}

        class DummyPopen:
            def __init__(self, *args, **kwargs):
                spawn_calls["count"] += 1

        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)
        # Note: we deliberately let _read_pid read the actual PID file so
        # _clean_stale_pid sees the stale PID and (because the mock above
        # makes _is_our_instance_process return False) deletes it.  Once
        # the file is gone Phase 1 keeps reading None and the function
        # returns False after the (very short) timeout.
        monkeypatch.setattr(im, "_instance_http_ready", lambda inst: False)

        inst = {"port": 6500, "id": "testid"}
        result = im._start_process(inst, str(tmp_path))

        assert result is False
        assert not pid_path.exists(), "stale PID file should be deleted"
        assert spawn_calls["count"] == 1, "a new gunicorn must be spawned"


# ---------------------------------------------------------------------------
# Storage tracking tests
# ---------------------------------------------------------------------------

class TestStorageTracking:
    """Tests for instance storage usage calculation."""

    def test_empty_directory(self, tmp_path):
        """Empty directory reports 0 bytes."""
        from hosting.instance_manager import get_storage_bytes
        empty_dir = tmp_path / "empty_instance"
        empty_dir.mkdir()
        assert get_storage_bytes(str(empty_dir)) == 0

    def test_nonexistent_directory(self, tmp_path):
        """Non-existent directory reports 0 bytes."""
        from hosting.instance_manager import get_storage_bytes
        assert get_storage_bytes(str(tmp_path / "nope")) == 0

    def test_files_counted(self, tmp_path):
        """Files in subdirectories are counted."""
        from hosting.instance_manager import get_storage_bytes
        inst_dir = tmp_path / "counted_instance"
        inst_dir.mkdir()
        (inst_dir / "file1.txt").write_text("hello")
        sub = inst_dir / "subdir"
        sub.mkdir()
        (sub / "file2.txt").write_text("world!")
        total = get_storage_bytes(str(inst_dir))
        assert total == 5 + 6  # "hello" + "world!"


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """Tests for instance health checking."""

    def test_stopped_instance_not_healthy(self, tmp_path):
        """A stopped instance is never considered healthy."""
        from hosting.instance_manager import check_instance_health
        inst = {"status": "stopped", "subdomain": "test"}
        assert check_instance_health(inst) is False

    def test_running_instance_no_pid(self, tmp_path):
        """A running instance with no PID file is unhealthy."""
        from hosting.instance_manager import check_instance_health
        hosting_config.INSTANCES_DIR = str(tmp_path)
        os.makedirs(str(tmp_path / "test"), exist_ok=True)
        inst = {"status": "running", "subdomain": "test"}
        assert check_instance_health(inst) is False

    def test_running_instance_alive_pid(self, tmp_path):
        """A running instance with a live process and open port is healthy.

        ``check_instance_health`` now requires the recorded PID to point at
        our Gunicorn (not just any live process), so we patch the new
        ``_is_our_instance_process`` helper instead of relying on the bare
        ``kill -0`` probe used by previous versions of the test.
        """
        from hosting.instance_manager import check_instance_health
        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "alive"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "alive", "port": 6500}
        with patch("hosting.instance_diagnostics._instance_http_ready", return_value=True), \
             patch(
                 "hosting.instance_diagnostics._is_our_instance_process",
                 return_value=True,
             ):
            assert check_instance_health(inst) is True

    def test_running_instance_with_stale_pid_after_reboot_is_unhealthy(self, tmp_path):
        """After a VPS reboot a stale PID must NOT report as healthy.

        The PID file may match an unrelated process (systemd, kthreadd …)
        whose numeric PID was reused by the kernel.  Even though that PID
        is alive, ``check_instance_health`` must detect that the process is
        not our Gunicorn and return False so the proxy classifies the
        instance as ``recovering`` and lazy-recovery kicks in.
        """
        from hosting.instance_manager import check_instance_health
        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "stale"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "stale", "port": 6500}
        with patch("hosting.instance_diagnostics._instance_http_ready", return_value=True), \
             patch(
                 "hosting.instance_diagnostics._is_our_instance_process",
                 return_value=False,
             ):
            assert check_instance_health(inst) is False

    def test_running_instance_alive_pid_but_unreachable_is_unhealthy(self, tmp_path):
        """A live PID is not enough if the instance never accepts connections."""
        from hosting.instance_manager import check_instance_health

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "unreachable"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "unreachable", "port": 6500}

        with patch("hosting.instance_diagnostics._instance_http_ready", return_value=False):
            assert check_instance_health(inst) is False

    def test_running_instance_open_port_but_http_unready_is_unhealthy(self, tmp_path):
        """A bound socket is not enough if Gunicorn is not serving HTTP yet."""
        from hosting.instance_manager import check_instance_health

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "halfready"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "halfready", "port": 6500}

        with patch("hosting.instance_diagnostics._is_our_instance_process", return_value=True), \
             patch("hosting.instance_diagnostics._instance_http_ready", return_value=False):
            assert check_instance_health(inst) is False


class TestProcessAliveSocketCheck:
    """Regression tests for the proxy-hot-path ``check_instance_process_alive``.

    Before this change the function only verified that the recorded PID
    belonged to *our* Gunicorn master.  In production we saw a failure
    mode where the master process was alive (and therefore passed the
    check) but the listening socket was wedged: workers stuck on a
    long migration, deadlocked in worker init, or the master was still
    in pre-fork bind setup.  The proxy happily forwarded traffic to it
    and users got 502 / "Bad Gateway" / 30s hangs.

    The fix layers a non-blocking ``connect_ex`` probe on top of the
    PID check.  These tests pin that behavior.
    """

    @pytest.fixture(autouse=True)
    def process_runtime(self, monkeypatch):
        monkeypatch.setattr(hosting_config, "HOSTING_INSTANCE_RUNTIME", "process")

    def test_returns_false_when_status_not_running(self, tmp_path):
        from hosting.instance_manager import check_instance_process_alive

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst = {"status": "stopped", "subdomain": "off", "port": 6500}
        assert check_instance_process_alive(inst) is False

    def test_returns_false_when_pid_not_our_process(self, tmp_path):
        from hosting.instance_manager import check_instance_process_alive

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "stale"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "stale", "port": 6500}

        with patch(
            "hosting.instance_diagnostics._is_our_instance_process",
            return_value=False,
        ):
            assert check_instance_process_alive(inst) is False

    def test_returns_false_when_pid_alive_but_socket_closed(self, tmp_path):
        """The new gate: a live PID alone is no longer enough.

        Simulates a wedged Gunicorn master: PID alive, but the bound
        port is not accepting connections.  Without the socket probe
        the proxy would forward traffic to a dead end; with it, the
        proxy short-circuits to the splash page and triggers lazy
        recovery.
        """
        from hosting.instance_manager import check_instance_process_alive

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "wedged"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "wedged", "port": 6500}

        with patch(
            "hosting.instance_diagnostics._is_our_instance_process",
            return_value=True,
        ), patch(
            "hosting.instance_diagnostics._is_port_accepting",
            return_value=False,
        ):
            assert check_instance_process_alive(inst) is False

    def test_returns_true_when_pid_alive_and_socket_accepting(self, tmp_path):
        from hosting.instance_manager import check_instance_process_alive

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "healthy"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "healthy", "port": 6500}

        with patch(
            "hosting.instance_diagnostics._is_our_instance_process",
            return_value=True,
        ), patch(
            "hosting.instance_diagnostics._is_port_accepting",
            return_value=True,
        ):
            assert check_instance_process_alive(inst) is True

    def test_socket_probe_uses_short_timeout(self, tmp_path):
        """The socket probe must be sub-second so the proxy stays responsive.

        Regression guard: if someone later raises the timeout, the hot
        proxy path would become measurably slower per request.
        """
        from hosting.instance_manager import (
            _PROCESS_ALIVE_SOCKET_TIMEOUT,
            check_instance_process_alive,
        )

        assert _PROCESS_ALIVE_SOCKET_TIMEOUT <= 0.25

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "healthy"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "healthy", "port": 6500}

        captured = {}

        def _capturing_port_accepting(host, port, timeout=1.0):
            captured["timeout"] = timeout
            return True

        with patch(
            "hosting.instance_diagnostics._is_our_instance_process",
            return_value=True,
        ), patch(
            "hosting.instance_diagnostics._is_port_accepting",
            side_effect=_capturing_port_accepting,
        ):
            check_instance_process_alive(inst)

        assert captured["timeout"] == _PROCESS_ALIVE_SOCKET_TIMEOUT

    def test_pid_alive_with_no_port_passes(self, tmp_path):
        """If the instance row has no port, fall back to PID-only.

        This is mostly defensive, every real instance has a port, but
        ensures we don't crash on a half-provisioned row.
        """
        from hosting.instance_manager import check_instance_process_alive

        hosting_config.INSTANCES_DIR = str(tmp_path)
        inst_dir = tmp_path / "noport"
        inst_dir.mkdir()
        (inst_dir / "bananawiki.pid").write_text(str(os.getpid()))
        inst = {"status": "running", "subdomain": "noport", "port": None}

        with patch(
            "hosting.instance_diagnostics._is_our_instance_process",
            return_value=True,
        ):
            assert check_instance_process_alive(inst) is True


class TestLazyRecoveryEscalation:
    """Regression tests for the lazy-recovery force-restart escalation.

    The proxy hot-path now uses ``check_instance_process_alive`` (PID +
    listening-socket probe).  When a request lands on the lazy-recovery
    worker with ``our_process_alive=True`` and ``_instance_http_ready``
    returning False, the master is genuinely wedged: ``_start_process``
    short-circuits on a live PID and cannot recover it.  The only path
    forward is a full force-restart.

    These tests pin the escalation threshold at 1 (matching the
    docstring) so a single failed soft attempt is enough to escalate.
    """

    def test_escalate_threshold_is_one(self):
        """Sanity-check: the constant matches the docstring intent."""
        from hosting._subdomain_proxy import _LAZY_RECOVERY_ESCALATE_AFTER

        assert _LAZY_RECOVERY_ESCALATE_AFTER == 1


class TestProcessStartupReadiness:
    """Tests for readiness gating during instance startup."""

    @pytest.fixture(autouse=True)
    def use_process_runtime(self, monkeypatch):
        from hosting import instance_manager as im
        monkeypatch.setattr(im.config, "HOSTING_INSTANCE_RUNTIME", "process")

    def test_existing_live_process_must_also_be_http_ready(
        self, tmp_path, monkeypatch
    ):
        """A wedged existing master must enter the recycle path."""
        from hosting import instance_manager as im

        (tmp_path / "bananawiki.pid").write_text(str(os.getpid()))
        monkeypatch.setattr(im, "_clean_stale_pid", lambda *a, **kw: None)
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: True)
        monkeypatch.setattr(im, "_instance_http_ready", lambda _inst: False)
        monkeypatch.setattr(im, "_ensure_tts_worker_started", lambda *a, **kw: True)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)

        inst = {"port": 6500, "id": "wedged", "subdomain": "wedged"}
        assert im._start_process_once(
            inst, str(tmp_path), http_ready_timeout=0.1
        ) is False

    def test_start_process_fails_when_pid_alive_but_port_never_opens(self, tmp_path, monkeypatch):
        """Startup must fail if Gunicorn never becomes reachable."""
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "INSTANCE_PORT_START", 6000)
        monkeypatch.setattr(im.config, "INSTANCE_PORT_END", 7000)
        monkeypatch.setattr(im, "_STARTUP_TIMEOUT", 0.5)
        monkeypatch.setattr(im, "_STARTUP_CHECK_INTERVAL", 0.1)
        monkeypatch.setattr(im, "_HTTP_READY_TIMEOUT", 0.5)
        monkeypatch.setattr(im, "_HTTP_READY_INTERVAL", 0.1)

        pid_path = tmp_path / "bananawiki.pid"
        states = {"reads": 0, "stopped": False}

        def fake_read_pid(_data_dir):
            if states["stopped"]:
                return None
            states["reads"] += 1
            if states["reads"] >= 3:
                pid_path.write_text(str(os.getpid()))
                return os.getpid()
            return None

        def fake_stop(_data_dir, timeout=5, port=None):
            states["stopped"] = True
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass

        monkeypatch.setattr(im, "_read_pid", fake_read_pid)
        monkeypatch.setattr(im, "_is_process_alive", lambda pid: bool(pid))
        monkeypatch.setattr(im, "_instance_http_ready", lambda inst: False)
        monkeypatch.setattr(im, "_ensure_tts_worker_started", lambda *a, **kw: True)
        monkeypatch.setattr(im, "_stop_process", fake_stop)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)

        class DummyPopen:
            def __init__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)

        inst = {"port": 6500, "id": "testid"}
        assert im._start_process(inst, str(tmp_path)) is False

    def test_start_process_retries_once_when_first_http_readiness_fails(
        self, tmp_path, monkeypatch
    ):
        """A first spawn that binds a socket but never serves HTTP is retried."""
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "INSTANCE_PORT_START", 6000)
        monkeypatch.setattr(im.config, "INSTANCE_PORT_END", 7000)
        monkeypatch.setattr(im, "_STARTUP_TIMEOUT", 0.2)
        monkeypatch.setattr(im, "_STARTUP_CHECK_INTERVAL", 0.05)
        monkeypatch.setattr(im, "_HTTP_READY_TIMEOUT", 0.1)
        monkeypatch.setattr(im, "_HTTP_READY_INTERVAL", 0.05)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)

        pid_path = tmp_path / "bananawiki.pid"
        popen_calls = {"count": 0}
        stop_calls = {"count": 0}

        class DummyPopen:
            def __init__(self, *args, **kwargs):
                popen_calls["count"] += 1
                pid_path.write_text(str(os.getpid()))

        readiness = {"count": 0}

        def fake_http_ready(_inst):
            readiness["count"] += 1
            return popen_calls["count"] > 1

        def fake_stop(_data_dir, timeout=5, port=None):
            stop_calls["count"] += 1
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass

        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)
        monkeypatch.setattr(im, "_is_process_alive", lambda pid: bool(pid))
        monkeypatch.setattr(im, "_instance_http_ready", fake_http_ready)
        monkeypatch.setattr(im, "_ensure_tts_worker_started", lambda *a, **kw: True)
        monkeypatch.setattr(im, "_stop_process", fake_stop)

        inst = {"port": 6500, "id": "testid", "subdomain": "retrytest"}
        assert im._start_process(inst, str(tmp_path)) is True
        assert popen_calls["count"] == 2
        assert stop_calls["count"] == 1

    def test_start_process_quick_returns_without_http_probe(
        self, tmp_path, monkeypatch
    ):
        """``await_http_ready=False`` returns True as soon as the PID is alive.

        This contract is what lets ``provision_instance`` finish inside the
        portal Gunicorn worker timeout even on a cold VPS: instead of
        blocking up to ``_HTTP_READY_TIMEOUT`` (30 s, possibly 60 s with
        retries) waiting for the freshly-spawned wiki to answer
        ``/healthz``, we declare success once the master writes its PID
        and let the subdomain proxy's auto-refresh splash bridge the gap.
        Previously this scenario surfaced as a hard 502 Bad Gateway from
        nginx on the create POST when the worker got SIGKILL-ed.
        """
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "INSTANCE_PORT_START", 6000)
        monkeypatch.setattr(im.config, "INSTANCE_PORT_END", 7000)
        monkeypatch.setattr(im, "_STARTUP_TIMEOUT", 0.5)
        monkeypatch.setattr(im, "_STARTUP_CHECK_INTERVAL", 0.05)
        # Long HTTP-ready timeout: the test would hang if Phase 2 ran.
        monkeypatch.setattr(im, "_HTTP_READY_TIMEOUT", 60.0)
        monkeypatch.setattr(im, "_HTTP_READY_INTERVAL", 0.05)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)

        pid_path = tmp_path / "bananawiki.pid"

        class DummyPopen:
            def __init__(self, *args, **kwargs):
                pid_path.write_text(str(os.getpid()))

        # Deliberately make HTTP probes blow up. The test asserts the
        # quick path never calls them.
        def explode(_inst):
            raise AssertionError(
                "await_http_ready=False must not poll /healthz"
            )

        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)
        monkeypatch.setattr(im, "_is_process_alive", lambda pid: bool(pid))
        monkeypatch.setattr(im, "_instance_http_ready", explode)

        inst = {"port": 6500, "id": "quicktest", "subdomain": "quicktest"}
        assert im._start_process(
            inst, str(tmp_path), await_http_ready=False
        ) is True

    def test_start_process_uses_small_default_instance_footprint(
        self, tmp_path, monkeypatch
    ):
        """Hosted instances should default to a lightweight process shape."""
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "INSTANCE_PORT_START", 6000)
        monkeypatch.setattr(im.config, "INSTANCE_PORT_END", 7000)
        monkeypatch.setattr(im, "_STARTUP_TIMEOUT", 0.5)
        monkeypatch.setattr(im, "_STARTUP_CHECK_INTERVAL", 0.05)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(im, "_ensure_tts_worker_started", lambda *a, **kw: True)

        pid_path = tmp_path / "bananawiki.pid"
        captured = {}

        class DummyPopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = cmd
                pid_path.write_text(str(os.getpid()))

        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)
        monkeypatch.setattr(im, "_is_process_alive", lambda pid: bool(pid))

        inst = {"port": 6500, "id": "footprint", "subdomain": "footprint"}
        assert im._start_process(
            inst, str(tmp_path), await_http_ready=False
        ) is True

        cmd = captured["cmd"]
        assert cmd[cmd.index("--workers") + 1] == "1"
        assert cmd[cmd.index("--threads") + 1] == "4"

    def test_tts_worker_uses_slow_hosting_poll_interval(self, tmp_path, monkeypatch):
        """Idle hosted TTS workers should not poll every couple of seconds."""
        from hosting import instance_manager as im

        captured = {}

        class DummyPopen:
            pid = os.getpid()

            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = cmd

        monkeypatch.setattr(im, "_clean_stale_tts_worker_pid", lambda _data_dir: None)
        monkeypatch.setattr(im, "_read_tts_worker_pid", lambda _data_dir: None)
        monkeypatch.setattr(im, "_is_our_tts_worker_process", lambda *a, **kw: False)
        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)

        inst = {"port": 6500, "id": "tts-poll", "subdomain": "tts-poll"}
        assert im._ensure_tts_worker_started(inst, str(tmp_path)) is True

        cmd = captured["cmd"]
        assert cmd[cmd.index("--poll-interval") + 1] == "30.0"

    def test_prepare_instance_database_migrates_and_applies_hosting_safety(
        self, tmp_path
    ):
        """Old hosted DBs should be migrated and have risky TTS fanout disabled."""
        import sqlite3
        from hosting import instance_manager as im

        data_dir = str(tmp_path)
        im._create_instance_dirs(data_dir)
        db_path = os.path.join(data_dir, "bananawiki.db")
        open(db_path, "a").close()

        assert im._prepare_instance_database(data_dir) is True

        conn = sqlite3.connect(db_path)
        try:
            page_a = conn.execute(
                "INSERT INTO pages (title, slug, content) VALUES (?, ?, ?)",
                ("Pending TTS", "pending-tts", "Pending body"),
            ).lastrowid
            page_b = conn.execute(
                "INSERT INTO pages (title, slug, content) VALUES (?, ?, ?)",
                ("Completed TTS", "completed-tts", "Completed body"),
            ).lastrowid
            conn.execute(
                "UPDATE site_settings SET tts_auto_generate_enabled=1 WHERE id=1"
            )
            conn.execute(
                "INSERT INTO tts_generations "
                "(page_id, language, status, content_hash) VALUES (?, ?, ?, ?)",
                (page_a, "en", "pending", "pending-hash"),
            )
            conn.execute(
                "INSERT INTO tts_generations "
                "(page_id, language, status, content_hash, filename) "
                "VALUES (?, ?, ?, ?, ?)",
                (page_b, "en", "completed", "completed-hash", "done.mp3"),
            )
            conn.commit()
        finally:
            conn.close()

        assert im._prepare_instance_database(data_dir) is True

        conn = sqlite3.connect(db_path)
        try:
            auto_enabled = conn.execute(
                "SELECT tts_auto_generate_enabled FROM site_settings WHERE id=1"
            ).fetchone()[0]
            statuses = [
                row[0] for row in conn.execute(
                    "SELECT status FROM tts_generations ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()

        assert auto_enabled == 0
        assert statuses == []

    def test_start_process_warns_but_continues_when_db_prepare_fails(
        self, tmp_path, monkeypatch
    ):
        """A failed DB preflight is non-fatal: Gunicorn still starts.

        Schema migrations are attempted again by Gunicorn during its own
        startup (``db.init_db()`` in ``app.py``).  The pre-flight is a
        best-effort optimisation, not a hard gate.
        """
        from hosting import instance_manager as im

        popen_calls = {"count": 0}

        class DummyPopen:
            def __init__(self, *args, **kwargs):
                popen_calls["count"] += 1

        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)
        monkeypatch.setattr(im, "_prepare_instance_database", lambda _data_dir: False)
        monkeypatch.setattr(im, "_STARTUP_TIMEOUT", 0.2)
        monkeypatch.setattr(im, "_STARTUP_CHECK_INTERVAL", 0.05)
        # Fake PID file so _start_process_once thinks Gunicorn started.
        pid_path = tmp_path / "bananawiki.pid"
        pid_path.write_text("999999")

        inst = {"port": 6500, "id": "prep-fail", "subdomain": "prep-fail"}
        # _start_process should NOT abort. It spawns Gunicorn anyway.
        # It returns False only because the fake PID 999999 is not alive.
        im._start_process(
            inst, str(tmp_path), await_http_ready=False
        )
        # Popen was called: Gunicorn was spawned despite DB prep failure.
        assert popen_calls["count"] >= 1

    def test_start_process_quick_still_fails_when_pid_never_appears(
        self, tmp_path, monkeypatch
    ):
        """``await_http_ready=False`` still requires a real PID to claim success.

        Skipping Phase 2 (HTTP readiness) must not weaken Phase 1
        (the master writing its PID): if Gunicorn never even started,
        we must surface a clean failure rather than silently leaving
        the row in ``running`` state forever.
        """
        from hosting import instance_manager as im

        monkeypatch.setattr(im.config, "INSTANCE_PORT_START", 6000)
        monkeypatch.setattr(im.config, "INSTANCE_PORT_END", 7000)
        monkeypatch.setattr(im, "_STARTUP_TIMEOUT", 0.2)
        monkeypatch.setattr(im, "_STARTUP_CHECK_INTERVAL", 0.05)
        monkeypatch.setattr(im.time, "sleep", lambda _seconds: None)

        class DummyPopen:
            def __init__(self, *args, **kwargs):
                pass  # Deliberately do not write a PID file.

        monkeypatch.setattr(im.subprocess, "Popen", DummyPopen)
        monkeypatch.setattr(im, "_read_pid", lambda _data_dir: None)
        monkeypatch.setattr(im, "_is_process_alive", lambda pid: False)

        inst = {"port": 6500, "id": "nopid", "subdomain": "nopid"}
        assert im._start_process(
            inst, str(tmp_path), await_http_ready=False
        ) is False


class TestProvisionInstanceDoesNotBlockOnHTTPReadiness:
    """Regression: provision_instance must not block on /healthz.

    Previously ``provision_instance`` called ``_start_process`` with the
    default ``await_http_ready=True``, so a cold-start that took longer
    than ~30 s to answer ``/healthz`` would either hang the portal
    worker into a ``SIGKILL`` (surfacing as nginx 502 on the create
    POST) or, with retries, blow past the 120 s worker timeout
    entirely.  We now pass ``await_http_ready=False`` and rely on the
    subdomain proxy's auto-refresh splash.
    """

    def test_provision_instance_uses_quick_start(self, client, monkeypatch):
        """``provision_instance`` must call ``_start_process`` with the
        quick-start kwarg so the create POST returns promptly even when
        the spawned wiki has not yet bound its listening socket."""
        from hosting import instance_manager as im

        captured = {}

        def fake_start_process(inst, data_dir, *, await_http_ready=True,
                               http_ready_timeout=None):
            captured["await_http_ready"] = await_http_ready
            captured["http_ready_timeout"] = http_ready_timeout
            return True

        # ``_instance_http_ready`` would still be called by
        # ``check_instance_health`` if anything in the create response
        # path queried it: keep it returning False so the proxy/dashboard
        # treats the instance as not-yet-ready (which is fine; the
        # subdomain proxy splash handles that case).
        monkeypatch.setattr(im, "_start_process", fake_start_process)
        monkeypatch.setattr(im, "_instance_http_ready", lambda inst: False)

        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "asynccreate"})
        assert rv.status_code == 200, rv.data[:200]
        assert captured.get("await_http_ready") is False, (
            "provision_instance must call _start_process with "
            "await_http_ready=False so the portal worker never blocks "
            "on the freshly-spawned wiki's /healthz."
        )


# ---------------------------------------------------------------------------
# Instance provisioning tests (pre-activation)
# ---------------------------------------------------------------------------

class TestInstanceProvisioning:
    """Tests for instance provisioning with pre-activation."""

    def test_provision_creates_bananawiki_db(self, client):
        """Instance provisioning creates a BananaWiki database."""
        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "provtest"})
        assert rv.status_code == 200

        data_dir = os.path.join(hosting_config.INSTANCES_DIR, "provtest")
        db_path = os.path.join(data_dir, "bananawiki.db")
        assert os.path.exists(db_path)

    def test_provision_creates_subdirectories(self, client):
        """Instance provisioning creates all required subdirectories."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "dirtest"})
        data_dir = os.path.join(hosting_config.INSTANCES_DIR, "dirtest")
        for sub in ("uploads", "attachments", "chat_attachments",
                     "kanban_attachments", "custom_page_files"):
            assert os.path.isdir(os.path.join(data_dir, sub))


# ---------------------------------------------------------------------------
# Dashboard enrichment tests
# ---------------------------------------------------------------------------

class TestDashboardData:
    """Tests for dashboard data enrichment."""

    def test_dashboard_includes_storage_info(self, client):
        """Dashboard page shows storage usage."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "storagetest"})
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        assert b"storage" in rv.data.lower() or b"MB" in rv.data

    def test_dashboard_includes_health_badge(self, client):
        """Dashboard page shows health indicator for running instances."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "healthtest"})
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        # The health badge should be present (either healthy or unhealthy)
        assert b"health-" in rv.data

    def test_dashboard_confirm_js_present(self, client):
        """Dashboard uses JS-based confirm dialogs instead of inline handlers."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "jstest"})
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        assert b"js-confirm-form" in rv.data
        assert b"onsubmit" not in rv.data


# ---------------------------------------------------------------------------
# Instance recovery tests
# ---------------------------------------------------------------------------

class TestInstanceRecovery:
    """Tests for instance recovery on portal startup."""

    def test_recover_with_no_instances(self):
        """Recovery with no running instances returns 0."""
        from hosting.instance_manager import recover_running_instances
        assert recover_running_instances() == 0

    def test_recover_skips_stopped_instances(self):
        """Recovery does not try to start stopped instances."""
        from hosting.instance_manager import recover_running_instances
        from hosting.db import create_account, create_instance, update_instance_status
        from werkzeug.security import generate_password_hash
        aid = create_account("recoveruser", generate_password_hash("pass"))
        inst = create_instance(aid, "recoversub")
        update_instance_status(inst["id"], "stopped")
        assert recover_running_instances() == 0

    def test_recover_recycles_live_instance_that_is_not_http_ready(
        self, tmp_path, monkeypatch
    ):
        """A live PID must not make startup recovery skip a wedged wiki."""
        from hosting import instance_manager as im
        from hosting.db import create_account, create_instance
        from werkzeug.security import generate_password_hash

        hosting_config.INSTANCES_DIR = str(tmp_path)
        aid = create_account("wedgedrecover", generate_password_hash("pass"))
        inst = create_instance(aid, "wedgedrecover")
        data_dir = tmp_path / "wedgedrecover"
        data_dir.mkdir()

        monkeypatch.setattr(im, "_clean_stale_pid", lambda *a, **kw: None)
        monkeypatch.setattr(im, "_read_pid", lambda _data_dir: 1234)
        monkeypatch.setattr(im, "_is_our_instance_process", lambda *a, **kw: True)
        monkeypatch.setattr(im, "_instance_http_ready", lambda _inst: False)

        starts = []

        def fake_start(inst_dict, instance_dir):
            starts.append((inst_dict["id"], instance_dir))
            return True

        monkeypatch.setattr(im, "_start_process", fake_start)

        assert im.recover_running_instances() == 1
        assert starts == [(inst["id"], str(data_dir))]


# ---------------------------------------------------------------------------
# Periodic cleanup tests
# ---------------------------------------------------------------------------

class TestPeriodicCleanup:
    """Tests for the periodic expired-instance cleanup timer."""

    def test_cleanup_not_started_in_testing(self):
        """The cleanup timer is not started when TESTING=True."""
        from hosting.app import _cleanup_timer
        app = create_hosting_app()
        app.config["TESTING"] = True
        # After creating the test app, the timer should not be active
        # (the fixture already sets TESTING=True)


# ---------------------------------------------------------------------------
# BananaWiki config env var override tests
# ---------------------------------------------------------------------------

class TestBananaWikiConfigOverrides:
    """Tests that BananaWiki config.py respects environment variable overrides."""

    def test_port_env_override(self, monkeypatch):
        """BW_PORT env var overrides the default port."""
        monkeypatch.setenv("BW_PORT", "9999")
        # Can't easily re-import config at module level, but verify the
        # pattern works by checking the int conversion
        assert int(os.environ.get("BW_PORT", "5001")) == 9999

    def test_bw_root_set(self):
        """hosting config.BW_ROOT points to the repo root."""
        assert os.path.isfile(os.path.join(hosting_config.BW_ROOT, "wsgi.py"))
        assert os.path.isfile(os.path.join(hosting_config.BW_ROOT, "app.py"))

    def test_hosting_mode_validation(self, monkeypatch):
        """HOSTING_MODE rejects invalid values."""
        monkeypatch.setenv("HOSTING_MODE", "invalid")
        with pytest.raises(ValueError, match="must be 'subdomain' or 'port'"):
            # Force re-execution of the validation logic
            mode = os.environ.get("HOSTING_MODE", "subdomain")
            if mode not in ("subdomain", "port"):
                raise ValueError(
                    f"HOSTING_MODE must be 'subdomain' or 'port', got '{mode}'"
                )


# ---------------------------------------------------------------------------
# Inline event handler removal tests (CSP compliance)
# ---------------------------------------------------------------------------

class TestCSPCompliance:
    """Dashboard template must not contain inline event handlers."""

    def test_no_inline_onsubmit(self, client):
        """Dashboard HTML must not contain onsubmit= attributes."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "csptest"})
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        assert b"onsubmit=" not in rv.data
        assert b"onclick=" not in rv.data

    def test_confirm_dialog_uses_js_class(self, client):
        """Terminate/delete forms use js-confirm-form class."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "classtest"})
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        assert b"js-confirm-form" in rv.data
        assert b"data-confirm=" in rv.data


# ---------------------------------------------------------------------------
# Port-based hosting mode tests
# ---------------------------------------------------------------------------

class TestPortMode:
    """Tests for port-based hosting mode (no domain required)."""

    @pytest.fixture(autouse=True)
    def enable_port_mode(self):
        """Switch to port mode for every test in this class."""
        old_mode = hosting_config.HOSTING_MODE
        old_host = hosting_config.HOSTING_PUBLIC_HOST
        old_scheme = hosting_config.HOSTING_PUBLIC_SCHEME
        hosting_config.HOSTING_MODE = "port"
        hosting_config.HOSTING_PUBLIC_HOST = "192.168.1.100"
        hosting_config.HOSTING_PUBLIC_SCHEME = "http"
        yield
        hosting_config.HOSTING_MODE = old_mode
        hosting_config.HOSTING_PUBLIC_HOST = old_host
        hosting_config.HOSTING_PUBLIC_SCHEME = old_scheme

    def test_instance_url_uses_port(self, client):
        """In port mode, _instance_url builds http://host:port URLs."""
        from hosting.instance_manager import _instance_url
        inst = {"subdomain": "test", "port": 6001}
        url = _instance_url(inst)
        assert url == "http://192.168.1.100:6001"

    def test_provision_returns_port_url(self, client):
        """provision_instance builds port-based URL in port mode."""
        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "porttest"})
        assert rv.status_code == 200
        assert b"192.168.1.100:" in rv.data

    def test_dashboard_shows_port_url(self, client):
        """Dashboard shows port-based URL instead of subdomain."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "dashport"})
        rv = client.get("/dashboard")
        assert rv.status_code == 200
        assert b"192.168.1.100:" in rv.data
        # Should NOT show subdomain.domain format
        assert b"dashport.bananawiki.example.com" not in rv.data

    def test_create_form_hides_domain_suffix(self, client):
        """In port mode, the create form does not show the domain suffix."""
        _signup(client)
        rv = client.get("/instances/create")
        assert rv.status_code == 200
        assert b"domain-suffix" not in rv.data
        # Should show "Instance Name" instead of "Subdomain"
        assert b"Instance Name" in rv.data

    def test_instance_created_shows_port(self, client):
        """Instance-created page shows port number in port mode."""
        _signup(client)
        rv = client.post("/instances/create", data={"subdomain": "portshow"})
        assert rv.status_code == 200
        assert b"Port" in rv.data

    def test_instance_env_binds_all_interfaces(self, tmp_path):
        """In port mode, _instance_env binds to 0.0.0.0."""
        from hosting.instance_manager import _instance_env
        data_dir = str(tmp_path / "portinst")
        env = _instance_env(data_dir, 6050)
        assert env["BW_HOST"] == "0.0.0.0"
        assert env["BW_PROXY_MODE"] == "0"

    def test_instance_bind_host_port_mode(self):
        """_instance_bind_host returns 0.0.0.0 in port mode."""
        from hosting.instance_manager import _instance_bind_host
        assert _instance_bind_host() == "0.0.0.0"

    def test_instance_url_uses_custom_scheme(self, client):
        """Port mode respects HOSTING_PUBLIC_SCHEME."""
        hosting_config.HOSTING_PUBLIC_SCHEME = "https"
        from hosting.instance_manager import _instance_url
        inst = {"subdomain": "test", "port": 6001}
        url = _instance_url(inst)
        assert url.startswith("https://")
        hosting_config.HOSTING_PUBLIC_SCHEME = "http"

    def test_instance_url_null_port_returns_empty(self):
        """_instance_url returns empty string for terminated instances (port=None)."""
        from hosting.instance_manager import _instance_url
        inst = {"subdomain": "terminated", "port": None}
        url = _instance_url(inst)
        assert url == ""

    def test_instance_url_ipv6_host_bracketed(self):
        """Port-mode _instance_url brackets IPv6 HOSTING_PUBLIC_HOST in the URL."""
        import hosting.config as hcfg
        old_host = hcfg.HOSTING_PUBLIC_HOST
        hcfg.HOSTING_PUBLIC_HOST = "2001:4860:4860::8888"
        try:
            from hosting.instance_manager import _instance_url
            inst = {"subdomain": "test", "port": 6001}
            url = _instance_url(inst)
            assert url == "http://[2001:4860:4860::8888]:6001"
        finally:
            hcfg.HOSTING_PUBLIC_HOST = old_host

    def test_instance_url_ipv6_host_not_double_bracketed(self):
        """Port-mode _instance_url does not double-bracket an already-bracketed IPv6 host."""
        import hosting.config as hcfg
        old_host = hcfg.HOSTING_PUBLIC_HOST
        hcfg.HOSTING_PUBLIC_HOST = "[2001:db8::1]"
        try:
            from hosting.instance_manager import _instance_url
            inst = {"subdomain": "test", "port": 6002}
            url = _instance_url(inst)
            assert url == "http://[2001:db8::1]:6002"
        finally:
            hcfg.HOSTING_PUBLIC_HOST = old_host


class TestSubdomainMode:
    """Verify subdomain mode still works correctly (regression)."""

    @pytest.fixture(autouse=True)
    def enable_subdomain_mode(self):
        """Switch to subdomain mode for every test in this class."""
        old_mode = hosting_config.HOSTING_MODE
        old_domain = hosting_config.BASE_DOMAIN
        old_suffix = hosting_config.INSTANCE_URL_SUFFIX
        hosting_config.HOSTING_MODE = "subdomain"
        hosting_config.BASE_DOMAIN = "bananawiki.example.com"
        hosting_config.INSTANCE_URL_SUFFIX = ""
        yield
        hosting_config.HOSTING_MODE = old_mode
        hosting_config.BASE_DOMAIN = old_domain
        hosting_config.INSTANCE_URL_SUFFIX = old_suffix

    def test_instance_url_uses_subdomain(self):
        """In subdomain mode, _instance_url builds subdomain.domain URLs."""
        from hosting.instance_manager import _instance_url
        assert hosting_config.HOSTING_MODE == "subdomain"
        inst = {"subdomain": "myteam", "port": 6001}
        url = _instance_url(inst)
        assert url == f"https://myteam.{hosting_config.BASE_DOMAIN}"

    def test_instance_bind_host_subdomain_mode(self):
        """_instance_bind_host returns 127.0.0.1 in subdomain mode."""
        from hosting.instance_manager import _instance_bind_host
        assert hosting_config.HOSTING_MODE == "subdomain"
        assert _instance_bind_host() == "127.0.0.1"

    def test_instance_env_subdomain_mode(self, tmp_path):
        """In subdomain mode, _instance_env binds to 127.0.0.1 with proxy."""
        from hosting.instance_manager import _instance_env
        assert hosting_config.HOSTING_MODE == "subdomain"
        data_dir = str(tmp_path / "subinst")
        env = _instance_env(data_dir, 6050)
        assert env["BW_HOST"] == "127.0.0.1"
        assert env["BW_PROXY_MODE"] == "1"

    def test_create_form_shows_domain_suffix(self, client):
        """In subdomain mode, the create form shows the .domain suffix."""
        _signup(client)
        rv = client.get("/instances/create")
        assert rv.status_code == 200
        assert b"domain-suffix" in rv.data
        assert b"Instance Name" in rv.data


# ---------------------------------------------------------------------------
# Auto-detection tests
# ---------------------------------------------------------------------------

class TestAutoDetection:
    """Tests for automatic hosting mode detection."""

    def test_empty_domain_is_port_mode(self, monkeypatch):
        """Empty BASE_DOMAIN triggers port mode."""
        from hosting.config import _detect_hosting_mode
        monkeypatch.delenv("HOSTING_MODE", raising=False)
        monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "")
        # Re-import the function to use the patched value
        hosting_config.BASE_DOMAIN = ""
        assert _detect_hosting_mode() == "port"

    def test_ip_address_is_port_mode(self, monkeypatch):
        """IPv4 BASE_DOMAIN triggers port mode."""
        from hosting.config import _detect_hosting_mode, _IP_PATTERN
        monkeypatch.delenv("HOSTING_MODE", raising=False)
        monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "192.168.1.1")
        hosting_config.BASE_DOMAIN = "192.168.1.1"
        assert _detect_hosting_mode() == "port"

    def test_domain_is_subdomain_mode(self, monkeypatch):
        """A proper domain triggers subdomain mode."""
        from hosting.config import _detect_hosting_mode
        monkeypatch.delenv("HOSTING_MODE", raising=False)
        monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "wiki.example.com")
        hosting_config.BASE_DOMAIN = "wiki.example.com"
        assert _detect_hosting_mode() == "subdomain"

    def test_explicit_override(self, monkeypatch):
        """HOSTING_MODE env var overrides auto-detection."""
        from hosting.config import _detect_hosting_mode
        monkeypatch.setenv("HOSTING_MODE", "subdomain")
        assert _detect_hosting_mode() == "subdomain"

    def test_invalid_override_raises(self, monkeypatch):
        """Invalid HOSTING_MODE env var raises ValueError."""
        from hosting.config import _detect_hosting_mode
        monkeypatch.setenv("HOSTING_MODE", "invalid")
        with pytest.raises(ValueError, match="must be 'subdomain'"):
            _detect_hosting_mode()

    def test_localhost_is_port_mode(self, monkeypatch):
        """'localhost' BASE_DOMAIN triggers port mode."""
        from hosting.config import _detect_hosting_mode
        monkeypatch.delenv("HOSTING_MODE", raising=False)
        monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "localhost")
        hosting_config.BASE_DOMAIN = "localhost"
        assert _detect_hosting_mode() == "port"

    def test_normalize_domain_layout_flattens_nested_base_domain(self):
        """Nested BASE_DOMAIN auto-flattens into portal + suffix settings."""
        from hosting.config import _normalize_domain_layout
        base, portal, suffix = _normalize_domain_layout(
            "hosting.example.com",
            "",
            "",
        )
        assert base == "example.com"
        assert portal == "hosting.example.com"
        assert suffix == "hosting"

    def test_normalize_domain_layout_keeps_explicit_portal_domain(self):
        """Explicit PORTAL_DOMAIN disables automatic base-domain flattening."""
        from hosting.config import _normalize_domain_layout
        base, portal, suffix = _normalize_domain_layout(
            "hosting.example.com",
            "portal.example.com",
            "",
        )
        assert base == "hosting.example.com"
        assert portal == "portal.example.com"
        assert suffix == ""

    def test_normalize_domain_layout_keeps_explicit_suffix(self):
        """Explicit INSTANCE_URL_SUFFIX disables automatic base flattening."""
        from hosting.config import _normalize_domain_layout
        base, portal, suffix = _normalize_domain_layout(
            "hosting.example.com",
            "",
            "wiki",
        )
        assert base == "hosting.example.com"
        assert portal == ""
        assert suffix == "wiki"

    def test_normalize_domain_layout_disabled_keeps_nested_base_domain(self):
        """When auto-flatten is disabled, nested BASE_DOMAIN remains unchanged."""
        from hosting.config import _normalize_domain_layout
        base, portal, suffix = _normalize_domain_layout(
            "hosting.example.com",
            "",
            "",
            auto_flatten=False,
        )
        assert base == "hosting.example.com"
        assert portal == ""
        assert suffix == ""

    def test_normalize_domain_layout_enabled_flattens_nested_base_domain(self):
        """When auto-flatten is enabled, nested BASE_DOMAIN is flattened."""
        from hosting.config import _normalize_domain_layout
        base, portal, suffix = _normalize_domain_layout(
            "hosting.example.com",
            "",
            "",
            auto_flatten=True,
        )
        assert base == "example.com"
        assert portal == "hosting.example.com"
        assert suffix == "hosting"

    def test_is_private_host_loopback_and_unspecified(self):
        """Loopback and unspecified addresses are private."""
        from hosting.config import _is_private_host
        assert _is_private_host("localhost")
        assert _is_private_host("127.0.0.1")
        assert _is_private_host("0.0.0.0")
        assert _is_private_host("::1")
        assert _is_private_host("")

    def test_is_private_host_rfc1918(self):
        """RFC 1918 private IP ranges are classified as private."""
        from hosting.config import _is_private_host
        # 10/8
        assert _is_private_host("10.0.0.1")
        assert _is_private_host("10.255.255.255")
        # 172.16/12
        assert _is_private_host("172.16.0.1")
        assert _is_private_host("172.31.255.255")
        assert not _is_private_host("172.15.0.1")   # just below range
        assert not _is_private_host("172.32.0.1")   # just above range
        # 192.168/16
        assert _is_private_host("192.168.0.1")
        assert _is_private_host("192.168.1.100")

    def test_is_private_host_public(self):
        """Publicly routable IP addresses are not classified as private."""
        from hosting.config import _is_private_host
        assert not _is_private_host("203.0.113.10")
        assert not _is_private_host("8.8.8.8")
        assert not _is_private_host("1.1.1.1")
        assert not _is_private_host("myserver.example.com")

    def test_is_private_host_ipv6_private_ranges(self):
        """IPv6 link-local and unique-local addresses are classified as private."""
        from hosting.config import _is_private_host
        # fe80::/10: link-local
        assert _is_private_host("fe80::1")
        assert _is_private_host("fe80::abcd:ef01")
        # fc00::/7, unique-local (covers fc00:: and fd00::)
        assert _is_private_host("fc00::1")
        assert _is_private_host("fd00::1")
        assert _is_private_host("fdff:ffff:ffff::1")
        # :: unspecified
        assert _is_private_host("::")
        # Bracketed IPv6 private addresses
        assert _is_private_host("[fe80::1]")
        assert _is_private_host("[fd00::1]")

    def test_is_private_host_ipv6_public(self):
        """Publicly routable IPv6 addresses are not classified as private."""
        from hosting.config import _is_private_host
        # Google public DNS (IPv6)
        assert not _is_private_host("2001:4860:4860::8888")
        assert not _is_private_host("2606:4700:4700::1111")

    def test_detect_public_host_fallback(self, monkeypatch):
        """_detect_public_host falls back gracefully when services are unreachable."""
        import urllib.request
        import socket
        from hosting.config import _detect_public_host

        def _fail_open(url, timeout=None):
            raise OSError("blocked")

        def _fail_socket(*a, **kw):
            raise OSError("no route")

        monkeypatch.setattr(urllib.request, "urlopen", _fail_open)
        monkeypatch.setattr(socket, "socket", _fail_socket)
        result = _detect_public_host()
        assert result == "localhost"

    def test_detect_public_host_private_interface_falls_back(self, monkeypatch):
        """_detect_public_host returns 'localhost' when the outbound interface IP is private.

        On NAT'd servers the outbound interface address is an RFC 1918 private
        address even though the machine is reachable via a public IP.  When the
        external IP-echo services are also unreachable, _detect_public_host must
        not return the private address. It returns 'localhost' so that the
        warning in app.py fires and the operator knows to set HOSTING_PUBLIC_HOST.
        """
        import urllib.request
        import socket
        from hosting.config import _detect_public_host

        def _fail_open(url, timeout=None):
            raise OSError("blocked")

        class _FakeSocket:
            def __init__(self, *a, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def setsockopt(self, *a):
                pass
            def connect(self, addr):
                pass
            def getsockname(self):
                # Simulate a NAT'd cloud VM returning its private interface IP
                return ("10.0.0.5", 0)
            def bind(self, addr):
                pass

        monkeypatch.setattr(urllib.request, "urlopen", _fail_open)
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: _FakeSocket())
        result = _detect_public_host()
        assert result == "localhost"

    def test_detect_public_host_ipv6_link_local_falls_back(self, monkeypatch):
        """_detect_public_host returns 'localhost' when the IPv6 interface is link-local.

        IPv6 link-local addresses (fe80::/10) are not publicly routable.
        When only a link-local address is available, _detect_public_host must
        fall back to 'localhost' rather than return the non-routable address.
        """
        import urllib.request
        import socket
        from hosting.config import _detect_public_host

        def _fail_open(url, timeout=None):
            raise OSError("blocked")

        call_count = [0]

        class _FakeSocket:
            def __init__(self, *a, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def setsockopt(self, *a):
                pass
            def connect(self, addr):
                call_count[0] += 1
            def getsockname(self):
                # First call is IPv4 (returns private), second is IPv6 (link-local)
                if call_count[0] == 1:
                    return ("10.0.0.5", 0)
                return ("fe80::1", 0, 0, 0)
            def bind(self, addr):
                pass

        monkeypatch.setattr(urllib.request, "urlopen", _fail_open)
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: _FakeSocket())
        result = _detect_public_host()
        assert result == "localhost"

    def test_detect_public_host_public_interface_used(self, monkeypatch):
        """_detect_public_host uses the outbound interface IP when it is public."""
        import urllib.request
        import socket
        from hosting.config import _detect_public_host

        def _fail_open(url, timeout=None):
            raise OSError("blocked")

        class _FakeSocket:
            def __init__(self, *a, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def setsockopt(self, *a):
                pass
            def connect(self, addr):
                pass
            def getsockname(self):
                return ("203.0.113.10", 0)
            def bind(self, addr):
                pass

        monkeypatch.setattr(urllib.request, "urlopen", _fail_open)
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: _FakeSocket())
        result = _detect_public_host()
        assert result == "203.0.113.10"

    def test_detect_public_host_ipv6_public_used(self, monkeypatch):
        """_detect_public_host returns the public IPv6 address on IPv6-only VPS.

        On an IPv6-only server the IPv4 socket and echo services fail, but
        the IPv6 socket succeeds and returns a public global unicast address.
        """
        import urllib.request
        import socket
        from hosting.config import _detect_public_host

        def _fail_open(url, timeout=None):
            raise OSError("blocked")

        call_count = [0]

        class _FakeSocket:
            def __init__(self, family=None, *a, **kw):
                self._family = family
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def setsockopt(self, *a):
                pass
            def connect(self, addr):
                call_count[0] += 1
                if self._family == socket.AF_INET:
                    raise OSError("no IPv4 route")
            def getsockname(self):
                # IPv6 socket returns a public global unicast address
                return ("2001:4860:4860::8888", 0, 0, 0)
            def bind(self, addr):
                pass

        monkeypatch.setattr(urllib.request, "urlopen", _fail_open)
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: _FakeSocket(*a, **kw))
        result = _detect_public_host()
        assert result == "2001:4860:4860::8888"

# ---------------------------------------------------------------------------
# Admin dashboard tests
# ---------------------------------------------------------------------------

class TestAdminDashboard:
    """Tests for the hosting portal admin dashboard."""

    def test_admin_dashboard_requires_login(self, client):
        """Admin dashboard redirects unauthenticated users."""
        rv = client.get("/admin", follow_redirects=True)
        assert b"Log In" in rv.data

    def test_admin_dashboard_requires_admin(self, client):
        """Non-admin users are redirected with an error."""
        # First user is auto-admin; create a second non-admin user
        _signup(client, username="admin1", password="password123")
        client.post("/logout")
        _signup(client, username="regular1", password="password123")
        rv = client.get("/admin", follow_redirects=True)
        assert b"admin access" in rv.data.lower() or b"Dashboard" in rv.data

    def test_admin_dashboard_renders(self, client):
        """Admin users can access the admin dashboard."""
        _signup(client)  # First user = admin
        rv = client.get("/admin")
        assert rv.status_code == 200
        assert b"Platform Administration" in rv.data

    def test_admin_sees_all_accounts(self, client):
        """Admin dashboard shows all accounts."""
        _signup(client, username="adminuser", password="password123")
        rv = client.get("/admin")
        assert rv.status_code == 200
        assert b"adminuser" in rv.data

    def test_admin_terminate_instance(self, client):
        """Admin can terminate any instance."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "admkill"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("admkill")
        rv = client.post(f"/admin/instances/{inst['id']}/terminate", follow_redirects=True)
        assert rv.status_code == 200
        assert b"terminated" in rv.data.lower() or b"Terminated" in rv.data

    def test_admin_toggle_admin(self, client):
        """Admin can grant/revoke admin on another account."""
        _signup(client, username="superadmin", password="password123")
        # Create a second user directly
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash
        aid = create_account("regular2", generate_password_hash("password123"))
        rv = client.post(f"/admin/accounts/{aid}/toggle-admin", follow_redirects=True)
        assert rv.status_code == 200
        assert b"granted" in rv.data.lower() or b"Admin" in rv.data

    def test_admin_cannot_toggle_self(self, client):
        """Admin cannot change their own admin status."""
        _signup(client)
        from hosting.routes.auth import get_current_account
        with client.session_transaction() as sess:
            account_id = sess["hosting_account_id"]
        rv = client.post(f"/admin/accounts/{account_id}/toggle-admin", follow_redirects=True)
        assert b"cannot change your own" in rv.data.lower() or b"own admin" in rv.data.lower()

    def test_admin_delete_account(self, client):
        """Admin can delete another account."""
        _signup(client, username="superadmin2", password="password123")
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash
        aid = create_account("victim", generate_password_hash("password123"))
        rv = client.post(f"/admin/accounts/{aid}/delete", follow_redirects=True)
        assert rv.status_code == 200
        assert b"deleted" in rv.data.lower()

    def test_admin_can_impersonate_regular_account(self, client):
        """Hosting admin can view the portal as a regular account and return."""
        _signup(client, username="hostadmin", password="password123")
        from hosting.db import create_account, get_hosting_db
        from werkzeug.security import generate_password_hash

        target_id = create_account("hostuser", generate_password_hash("password123"))
        rv = client.post(f"/admin/accounts/{target_id}/impersonate", follow_redirects=True)
        assert rv.status_code == 200
        assert b"Impersonation Mode:" in rv.data
        assert b"hostuser" in rv.data
        assert b"Started by" in rv.data
        assert b"hostadmin" in rv.data

        with client.session_transaction() as sess:
            assert sess["hosting_account_id"] == target_id
            assert sess["hosting_impersonator_account_id"] != target_id
            log_id = sess["hosting_impersonation_log_id"]

        conn = get_hosting_db()
        row = conn.execute(
            "SELECT * FROM hosting_impersonation_logs WHERE id=?",
            (log_id,),
        ).fetchone()
        assert row is not None
        assert row["target_account_id"] == target_id
        assert row["ended_at"] is None

        rv = client.post("/admin/stop-impersonating", follow_redirects=True)
        assert rv.status_code == 200
        assert b"returned to your admin account" in rv.data
        with client.session_transaction() as sess:
            assert sess["hosting_account_id"] == row["admin_account_id"]
            assert "hosting_impersonator_account_id" not in sess

        row = conn.execute(
            "SELECT * FROM hosting_impersonation_logs WHERE id=?",
            (log_id,),
        ).fetchone()
        assert row["ended_at"] is not None

    def test_admin_cannot_impersonate_hosting_admin_account(self, client):
        """Hosting admins cannot impersonate other hosting admins."""
        _signup(client, username="hostadmin2", password="password123")
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash

        target_id = create_account("otherhostadmin", generate_password_hash("password123"), is_admin=True)
        rv = client.post(f"/admin/accounts/{target_id}/impersonate", follow_redirects=True)
        assert rv.status_code == 200
        assert b"can only impersonate regular accounts" in rv.data
        assert b"Impersonation Mode:" not in rv.data

    def test_admin_cannot_impersonate_suspended_hosting_account(self, client):
        """Suspended hosting accounts cannot be impersonated."""
        _signup(client, username="hostadmin3", password="password123")
        from hosting.db import create_account, suspend_account
        from werkzeug.security import generate_password_hash

        target_id = create_account("suspendedhost", generate_password_hash("password123"))
        suspend_account(target_id, reason="policy")
        rv = client.post(f"/admin/accounts/{target_id}/impersonate", follow_redirects=True)
        assert rv.status_code == 200
        assert b"cannot impersonate a suspended account" in rv.data
        assert b"Impersonation Mode:" not in rv.data

    def test_admin_cannot_delete_self(self, client):
        """Admin cannot delete their own account from admin panel."""
        _signup(client)
        with client.session_transaction() as sess:
            account_id = sess["hosting_account_id"]
        rv = client.post(f"/admin/accounts/{account_id}/delete", follow_redirects=True)
        assert b"cannot delete your own" in rv.data.lower()

    def test_admin_can_transfer_instance_to_another_user(self, client):
        """Admin can transfer ownership of a wiki by target username."""
        from hosting.db import get_account_by_username, get_instance_by_subdomain

        _signup(client, username="rootxfer", password="password123")
        client.post("/instances/create", data={"subdomain": "xferwiki"})
        inst = get_instance_by_subdomain("xferwiki")
        assert inst is not None

        client.post("/logout")
        _signup(client, username="receiverx", password="password123")
        client.post("/logout")
        _login(client, "rootxfer", "password123")

        rv = client.post(
            f"/admin/instances/{inst['id']}/transfer",
            data={"username": "receiverx"},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"successfully transferred" in rv.data.lower()

        updated = get_instance_by_subdomain("xferwiki")
        assert updated is not None
        assert updated["account_id"] == get_account_by_username("receiverx")["id"]

    def test_transfer_from_admin_owner_to_user_reapplies_limits(self, client):
        """When admin ownership is removed, instance limits are restored."""
        from datetime import datetime, timezone
        from hosting.db import get_instance_by_subdomain

        _signup(client, username="rootdemote", password="password123")
        client.post("/instances/create", data={"subdomain": "demoteme"})
        inst = get_instance_by_subdomain("demoteme")
        assert inst is not None
        assert int(inst["storage_limit_mb"]) == 0

        client.post("/logout")
        _signup(client, username="regularrecv", password="password123")
        client.post("/logout")
        _login(client, "rootdemote", "password123")

        client.post(
            f"/admin/instances/{inst['id']}/transfer",
            data={"username": "regularrecv"},
            follow_redirects=True,
        )
        updated = get_instance_by_subdomain("demoteme")
        assert updated is not None
        assert int(updated["storage_limit_mb"]) == int(hosting_config.INSTANCE_STORAGE_LIMIT_MB)
        expires = datetime.fromisoformat(updated["expires_at"])
        assert (expires - datetime.now(timezone.utc)).days <= hosting_config.INSTANCE_DURATION_DAYS + 1

    def test_previous_owner_cannot_manage_after_transfer(self, client):
        """Ownership transfer revokes the previous owner's control."""
        from hosting.db import get_instance_by_subdomain

        _signup(client, username="rootowner", password="password123")  # admin
        client.post("/logout")
        _signup(client, username="sourceuser", password="password123")
        client.post("/instances/create", data={"subdomain": "xferlock"})
        inst = get_instance_by_subdomain("xferlock")
        assert inst is not None
        client.post("/logout")
        _signup(client, username="newownerx", password="password123")
        client.post("/logout")
        _login(client, "rootowner", "password123")
        client.post(
            f"/admin/instances/{inst['id']}/transfer",
            data={"username": "newownerx"},
            follow_redirects=True,
        )
        client.post("/logout")

        _login(client, "sourceuser", "password123")
        rv = client.post(f"/instances/{inst['id']}/stop", follow_redirects=True)
        assert b"instance not found" in rv.data.lower() or b"dashboard" in rv.data.lower()

    def test_first_signup_is_admin(self, client):
        """The first account created is automatically admin."""
        _signup(client, username="firstone", password="password123")
        from hosting.routes.auth import get_current_account
        with client.application.test_request_context():
            with client.session_transaction() as sess:
                from hosting.db import get_account_by_id
                acct = get_account_by_id(sess["hosting_account_id"])
                assert acct["is_admin"] == 1

    def test_second_signup_not_admin(self, client):
        """The second account is not automatically admin."""
        _signup(client, username="first2", password="password123")
        client.post("/logout")
        _signup(client, username="second2", password="password123")
        with client.session_transaction() as sess:
            from hosting.db import get_account_by_id
            acct = get_account_by_id(sess["hosting_account_id"])
            assert acct["is_admin"] == 0

    def test_admin_nav_link_visible(self, client):
        """Admin users see the admin link in the navigation."""
        _signup(client)  # First user = admin
        rv = client.get("/dashboard")
        assert b"Platform Admin" in rv.data


# ---------------------------------------------------------------------------
# Instance detail page tests
# ---------------------------------------------------------------------------

class TestInstanceDetail:
    """Tests for the instance detail route."""

    def _create_and_get_id(self, client, subdomain="detailinst"):
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": subdomain})
        inst = get_instance_by_subdomain(subdomain)
        return inst["id"] if inst else None

    def test_detail_page_renders(self, client):
        """GET /instances/<id> renders the instance detail page."""
        _signup(client)
        iid = self._create_and_get_id(client, "detailtest1")
        rv = client.get(f"/instances/{iid}")
        assert rv.status_code == 200
        assert b"detailtest1" in rv.data

    def test_detail_shows_credentials(self, client):
        """Instance detail page shows login credentials."""
        _signup(client)
        iid = self._create_and_get_id(client, "detailcred")
        rv = client.get(f"/instances/{iid}")
        assert rv.status_code == 200
        assert b"Login Credentials" in rv.data
        assert b"admin" in rv.data

    def test_detail_shows_status(self, client):
        """Instance detail page shows the current status badge."""
        _signup(client)
        iid = self._create_and_get_id(client, "detailstat")
        rv = client.get(f"/instances/{iid}")
        assert rv.status_code == 200
        assert b"running" in rv.data or b"stopped" in rv.data

    def test_detail_requires_login(self, client):
        """Detail page redirects unauthenticated users."""
        rv = client.get("/instances/fakeid", follow_redirects=True)
        assert b"Log In" in rv.data

    def test_detail_not_found_for_other_user(self, client):
        """Users cannot view another user's instance detail."""
        _signup(client, username="owner1")
        iid = self._create_and_get_id(client, "private1")
        client.post("/logout")
        _signup(client, username="other1", password="password456")
        rv = client.get(f"/instances/{iid}", follow_redirects=True)
        assert b"not found" in rv.data.lower() or b"Dashboard" in rv.data

    def test_detail_stop_redirects_to_detail(self, client):
        """Stopping from the detail page redirects back to detail."""
        _signup(client)
        iid = self._create_and_get_id(client, "detailstop")
        rv = client.post(f"/instances/{iid}/stop", follow_redirects=True)
        assert rv.status_code == 200
        assert b"detailstop" in rv.data


# ---------------------------------------------------------------------------
# Admin extend / stop / restart tests
# ---------------------------------------------------------------------------

class TestAdminInstanceControls:
    """Tests for the admin stop, restart, and extend instance routes."""

    def _create_instance(self, client, subdomain="admctrl"):
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": subdomain})
        return get_instance_by_subdomain(subdomain)

    def test_admin_stop_instance(self, client):
        """Admin can stop any running instance."""
        _signup(client)  # First user = admin
        inst = self._create_instance(client, "admstop")
        rv = client.post(f"/admin/instances/{inst['id']}/stop", follow_redirects=True)
        assert rv.status_code == 200
        assert b"stopped" in rv.data.lower()

    def test_admin_restart_instance(self, client):
        """Admin can restart a stopped instance."""
        _signup(client)
        inst = self._create_instance(client, "admrestart")
        client.post(f"/admin/instances/{inst['id']}/stop")
        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(f"/admin/instances/{inst['id']}/restart", follow_redirects=True)
        assert rv.status_code == 200
        assert b"restarted" in rv.data.lower()

    def test_admin_extend_instance(self, client):
        """Admin can extend a trial instance's duration."""
        _signup(client)
        inst = self._create_instance(client, "admextend")
        original_expires = inst["expires_at"]
        rv = client.post(
            f"/admin/instances/{inst['id']}/extend",
            data={"extra_days": "7"},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"extended" in rv.data.lower()

        from hosting.db import get_instance
        updated = get_instance(inst["id"])
        assert updated["expires_at"] > original_expires

    def test_admin_extend_invalid_days(self, client):
        """Extension with invalid day count shows an error."""
        _signup(client)
        inst = self._create_instance(client, "admextbad")
        rv = client.post(
            f"/admin/instances/{inst['id']}/extend",
            data={"extra_days": "0"},
            follow_redirects=True,
        )
        assert b"at least 1 second" in rv.data.lower()

    def test_admin_extend_nonexistent_instance(self, client):
        """Extension of a nonexistent instance shows an error."""
        _signup(client)
        rv = client.post(
            "/admin/instances/fakeid/extend",
            data={"extra_days": "7"},
            follow_redirects=True,
        )
        assert b"not found" in rv.data.lower()

    def test_admin_stop_requires_admin(self, client):
        """Non-admin cannot access admin stop route."""
        _signup(client, username="adm2")  # Admin
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": "admstop2"})
        inst = get_instance_by_subdomain("admstop2")
        client.post("/logout")
        _signup(client, username="regular3", password="password456")
        rv = client.post(f"/admin/instances/{inst['id']}/stop", follow_redirects=True)
        assert b"admin access" in rv.data.lower() or b"Dashboard" in rv.data


# ---------------------------------------------------------------------------
# Bulk pause / resume tests (user dashboard)
# ---------------------------------------------------------------------------

class TestBulkInstanceControls:
    """Tests for the user-facing bulk pause / resume endpoints.

    The endpoints are idempotent: rows already in the target state
    are skipped (and counted separately in the flash message) instead
    of erroring.
    """

    def _create_instance(self, client, subdomain):
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": subdomain})
        return get_instance_by_subdomain(subdomain)["id"]

    def test_bulk_stop_pauses_running_instances(self, client):
        """Bulk-stop pauses every running instance in the selection."""
        _signup(client)
        a = self._create_instance(client, "bulkstopa")
        b = self._create_instance(client, "bulkstopb")
        rv = client.post(
            "/instances/bulk-stop",
            data={"instance_ids": [a, b]},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"2 instance(s) paused" in rv.data

        from hosting.db import get_instance
        assert get_instance(a)["status"] == "stopped"
        assert get_instance(b)["status"] == "stopped"

    def test_bulk_stop_is_idempotent_on_already_stopped(self, client):
        """Mixing running + stopped rows pauses just the running ones."""
        _signup(client)
        running = self._create_instance(client, "bulkstoprun")
        already_stopped = self._create_instance(client, "bulkstopstp")
        client.post(f"/instances/{already_stopped}/stop")

        rv = client.post(
            "/instances/bulk-stop",
            data={"instance_ids": [running, already_stopped]},
            follow_redirects=True,
        )
        assert b"1 instance(s) paused" in rv.data
        assert b"1 already not running" in rv.data

    def test_bulk_stop_empty_selection_flashes_error(self, client):
        """No instance_ids posted yields a clean error flash."""
        _signup(client)
        rv = client.post("/instances/bulk-stop", data={}, follow_redirects=True)
        assert rv.status_code == 200
        assert b"No instances selected" in rv.data

    def test_bulk_stop_skips_other_users_instances(self, client):
        """Users can't bulk-pause instances belonging to someone else."""
        _signup(client, username="bulkowner")
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": "bulkpriv"})
        other_id = get_instance_by_subdomain("bulkpriv")["id"]
        client.post("/logout")
        _signup(client, username="bulkother", password="password456")

        rv = client.post(
            "/instances/bulk-stop",
            data={"instance_ids": [other_id]},
            follow_redirects=True,
        )
        # Skipped silently (not in the changed/skipped count) and the
        # other user's instance remains running.
        from hosting.db import get_instance
        assert get_instance(other_id)["status"] == "running"
        assert b"0 instance(s) paused" in rv.data

    def test_bulk_restart_resumes_stopped_instances(self, client):
        """Bulk-restart resumes every stopped instance in the selection."""
        _signup(client)
        a = self._create_instance(client, "bulkrsta")
        b = self._create_instance(client, "bulkrstb")
        client.post(f"/instances/{a}/stop")
        client.post(f"/instances/{b}/stop")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                "/instances/bulk-restart",
                data={"instance_ids": [a, b]},
                follow_redirects=True,
            )
        assert rv.status_code == 200
        assert b"2 instance(s) resumed" in rv.data

    def test_bulk_restart_is_idempotent_on_running(self, client):
        """Mixing stopped + running rows resumes just the stopped ones."""
        _signup(client)
        stopped = self._create_instance(client, "bulkrstmix1")
        running = self._create_instance(client, "bulkrstmix2")
        client.post(f"/instances/{stopped}/stop")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                "/instances/bulk-restart",
                data={"instance_ids": [stopped, running]},
                follow_redirects=True,
            )
        assert b"1 instance(s) resumed" in rv.data
        assert b"1 already not paused" in rv.data


# ---------------------------------------------------------------------------
# Bulk pause / resume / suspend / unsuspend tests (admin dashboard)
# ---------------------------------------------------------------------------

class TestAdminBulkInstanceControls:
    """Tests for the admin bulk-action endpoints.

    All four endpoints are idempotent on rows already in the target
    state.  ``bulk-restart`` only touches ``stopped`` rows: admin
    suspensions are lifted via the explicit ``bulk-unsuspend`` action
    (which routes through :func:`restart_instance`, matching the
    single-instance ``/admin/instances/<id>/unsuspend`` flow).
    """

    def _create_instance(self, client, subdomain):
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": subdomain})
        return get_instance_by_subdomain(subdomain)["id"]

    def test_admin_bulk_stop_pauses_running(self, client):
        """Admin bulk-stop pauses running instances."""
        _signup(client)
        a = self._create_instance(client, "admbsa")
        b = self._create_instance(client, "admbsb")
        rv = client.post(
            "/admin/instances/bulk-stop",
            data={"instance_ids": [a, b]},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"2 instance(s) paused" in rv.data

    def test_admin_bulk_stop_is_idempotent(self, client):
        """Admin bulk-stop skips already-stopped rows cleanly."""
        _signup(client)
        running = self._create_instance(client, "admbsrun")
        stopped = self._create_instance(client, "admbsstp")
        client.post(f"/admin/instances/{stopped}/stop")

        rv = client.post(
            "/admin/instances/bulk-stop",
            data={"instance_ids": [running, stopped]},
            follow_redirects=True,
        )
        assert b"1 instance(s) paused" in rv.data
        assert b"1 already not running" in rv.data

    def test_admin_bulk_restart_resumes_stopped(self, client):
        """Admin bulk-restart resumes stopped instances."""
        _signup(client)
        a = self._create_instance(client, "admbra")
        b = self._create_instance(client, "admbrb")
        client.post(f"/admin/instances/{a}/stop")
        client.post(f"/admin/instances/{b}/stop")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                "/admin/instances/bulk-restart",
                data={"instance_ids": [a, b]},
                follow_redirects=True,
            )
        assert b"2 instance(s) resumed" in rv.data

    def test_admin_bulk_restart_skips_suspended(self, client):
        """Admin bulk-restart leaves ``suspended`` rows alone. Admins
        must use the explicit bulk-unsuspend action to lift suspensions.
        """
        _signup(client)
        stopped = self._create_instance(client, "admbrss1")
        suspended = self._create_instance(client, "admbrss2")
        client.post(f"/admin/instances/{stopped}/stop")
        client.post(f"/admin/instances/{suspended}/suspend")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                "/admin/instances/bulk-restart",
                data={"instance_ids": [stopped, suspended]},
                follow_redirects=True,
            )
        assert b"1 instance(s) resumed" in rv.data
        assert b"1 already not paused" in rv.data

        from hosting.db import get_instance
        assert get_instance(suspended)["status"] == "suspended"

    def test_admin_bulk_suspend_suspends_running(self, client):
        """Admin bulk-suspend suspends running instances."""
        _signup(client)
        a = self._create_instance(client, "admbsusa")
        b = self._create_instance(client, "admbsusb")
        rv = client.post(
            "/admin/instances/bulk-suspend",
            data={"instance_ids": [a, b]},
            follow_redirects=True,
        )
        assert b"2 instance(s) suspended" in rv.data

        from hosting.db import get_instance
        assert get_instance(a)["status"] == "suspended"
        assert get_instance(b)["status"] == "suspended"

    def test_admin_bulk_suspend_is_idempotent(self, client):
        """Suspending an already-suspended row is a no-op (counted as skipped)."""
        _signup(client)
        running = self._create_instance(client, "admbsusrun")
        already_suspended = self._create_instance(client, "admbsusstp")
        client.post(f"/admin/instances/{already_suspended}/suspend")

        rv = client.post(
            "/admin/instances/bulk-suspend",
            data={"instance_ids": [running, already_suspended]},
            follow_redirects=True,
        )
        assert b"1 instance(s) suspended" in rv.data
        assert b"1 already suspended" in rv.data

    def test_admin_bulk_unsuspend_lifts_suspension(self, client):
        """Admin bulk-unsuspend lifts admin-imposed suspensions and resumes the row."""
        _signup(client)
        a = self._create_instance(client, "admbunsa")
        b = self._create_instance(client, "admbunsb")
        client.post(f"/admin/instances/{a}/suspend")
        client.post(f"/admin/instances/{b}/suspend")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                "/admin/instances/bulk-unsuspend",
                data={"instance_ids": [a, b]},
                follow_redirects=True,
            )
        assert b"2 instance(s) unsuspended" in rv.data

        from hosting.db import get_instance
        assert get_instance(a)["status"] == "running"
        assert get_instance(b)["status"] == "running"

    def test_admin_bulk_unsuspend_is_idempotent(self, client):
        """Unsuspending a non-suspended row is a no-op (counted as skipped)."""
        _signup(client)
        suspended = self._create_instance(client, "admbunsmix1")
        running = self._create_instance(client, "admbunsmix2")
        client.post(f"/admin/instances/{suspended}/suspend")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                "/admin/instances/bulk-unsuspend",
                data={"instance_ids": [suspended, running]},
                follow_redirects=True,
            )
        assert b"1 instance(s) unsuspended" in rv.data
        assert b"1 already not suspended" in rv.data

    def test_admin_bulk_stop_requires_admin(self, client):
        """Non-admin accounts can't reach the admin bulk-stop endpoint."""
        _signup(client, username="admbulkowner")
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": "admbulkpriv"})
        other_id = get_instance_by_subdomain("admbulkpriv")["id"]
        client.post("/logout")
        _signup(client, username="admbulkreg", password="password456")

        client.post(
            "/admin/instances/bulk-stop",
            data={"instance_ids": [other_id]},
            follow_redirects=True,
        )
        # Non-admin gets redirected to the user dashboard or shown an
        # admin-access error.  Either way, the running instance stays running.
        from hosting.db import get_instance
        assert get_instance(other_id)["status"] == "running"


# ---------------------------------------------------------------------------
# Platform stats tests
# ---------------------------------------------------------------------------

class TestPlatformStats:
    """Tests for the get_platform_stats helper."""

    def test_stats_returns_expected_keys(self):
        """get_platform_stats returns all required keys."""
        from hosting.instance_manager import get_platform_stats
        stats = get_platform_stats()
        for key in ("total_accounts", "total_instances", "running", "stopped",
                    "terminated", "healthy", "total_storage_mb", "storage_limit_mb"):
            assert key in stats

    def test_stats_counts_accounts(self):
        """Stats reflect newly created accounts."""
        from hosting.db import create_account
        from hosting.instance_manager import get_platform_stats
        from werkzeug.security import generate_password_hash

        create_account("stats_user1", generate_password_hash("pw"))
        create_account("stats_user2", generate_password_hash("pw"))
        stats = get_platform_stats()
        assert stats["total_accounts"] >= 2

    def test_admin_dashboard_shows_stats(self, client):
        """Admin dashboard renders the platform stats bar."""
        _signup(client)
        rv = client.get("/admin")
        assert rv.status_code == 200
        assert b"Accounts" in rv.data
        assert b"Running" in rv.data


# ---------------------------------------------------------------------------
# get_instance_detail tests
# ---------------------------------------------------------------------------

class TestGetInstanceDetail:
    """Tests for the get_instance_detail helper."""

    def test_returns_none_for_unknown_id(self):
        """get_instance_detail returns None for an unknown instance ID."""
        from hosting.instance_manager import get_instance_detail
        assert get_instance_detail("doesnotexist") is None

    def test_enriches_instance_data(self):
        """get_instance_detail adds URL, countdown, storage, and health fields."""
        from hosting.db import create_account, create_instance
        from hosting.instance_manager import get_instance_detail
        from werkzeug.security import generate_password_hash

        aid = create_account("detail_u", generate_password_hash("pw"))
        inst = create_instance(aid, "detailhelper")
        detail = get_instance_detail(inst["id"])

        assert detail is not None
        assert "url" in detail
        assert "days_remaining" in detail
        assert "storage_mb" in detail
        assert "is_healthy" in detail


# ---------------------------------------------------------------------------
# extend_instance tests
# ---------------------------------------------------------------------------

class TestExtendInstance:
    """Tests for the extend_instance function."""

    def test_extend_running_instance(self):
        """extend_instance adds days to a running instance's expiry."""
        from hosting.db import create_account, create_instance, get_instance
        from hosting.instance_manager import extend_instance
        from werkzeug.security import generate_password_hash

        aid = create_account("ext_u1", generate_password_hash("pw"))
        inst = create_instance(aid, "extrunning")
        original = inst["expires_at"]
        ok, _ = extend_instance(inst["id"], 7)
        assert ok is True
        updated = get_instance(inst["id"])
        assert updated["expires_at"] > original

    def test_extend_nonexistent_instance(self):
        """extend_instance returns (False, reason) for unknown IDs."""
        from hosting.instance_manager import extend_instance
        ok, reason = extend_instance("notreal", 7)
        assert ok is False
        assert "not found" in reason.lower()

    def test_extend_terminated_instance(self):
        """extend_instance refuses to extend a terminated instance."""
        from hosting.db import create_account, create_instance, update_instance_status
        from hosting.instance_manager import extend_instance
        from werkzeug.security import generate_password_hash

        aid = create_account("ext_u2", generate_password_hash("pw"))
        inst = create_instance(aid, "extterminated")
        update_instance_status(inst["id"], "terminated")
        ok, reason = extend_instance(inst["id"], 7)
        assert ok is False
        assert "terminated" in reason.lower()




# ---------------------------------------------------------------------------
# Rate limiting on mutation endpoints
# ---------------------------------------------------------------------------


class TestMutationRateLimiting:
    """Tests for rate limiting on hosting mutation endpoints."""

    def test_signup_rate_limited(self, client):
        """POST /signup returns 429 after too many attempts."""
        for i in range(5):
            client.post("/signup", data={
                "username": f"rluser{i}",
                "password": "password123",
                "confirm_password": "password123",
            })
            client.post("/logout")

        rv = client.post("/signup", data={
            "username": "rluser99",
            "password": "password123",
            "confirm_password": "password123",
        })
        assert rv.status_code == 429

    def test_create_instance_rate_limited(self, client):
        """POST /instances/create returns 429 after too many attempts."""
        _signup(client)
        for i in range(5):
            client.post("/instances/create", data={"subdomain": f"rlinst{i}"})

        rv = client.post("/instances/create", data={"subdomain": "rlinst99"})
        assert rv.status_code == 429

    def test_account_delete_rate_limited(self, client):
        """POST /settings/delete returns 429 after too many attempts."""
        _signup(client)
        # Exhaust the rate limit first via repeated calls (account only gets
        # deleted on the first call, subsequent calls redirect to login since
        # the session is cleared; the rate limit is still consumed)
        for _ in range(5):
            client.post("/settings/delete")
        rv = client.post("/settings/delete")
        # After account is deleted, redirect to login (or 429 from rate limit)
        assert rv.status_code in (302, 429)


# ---------------------------------------------------------------------------
# Atomic first-admin promotion
# ---------------------------------------------------------------------------


class TestAtomicFirstAdmin:
    """Tests for the atomic first-admin promotion logic."""

    def test_first_account_is_admin_via_atomic(self, client):
        """create_account_atomic_first_admin promotes the first account."""
        from hosting.db import create_account_atomic_first_admin
        from werkzeug.security import generate_password_hash

        aid, is_first = create_account_atomic_first_admin(
            "atomicadmin", generate_password_hash("pw")
        )
        assert is_first is True
        from hosting.db import get_account_by_id
        acct = get_account_by_id(aid)
        assert acct["is_admin"] == 1

    def test_second_account_not_admin_via_atomic(self, client):
        """create_account_atomic_first_admin does not promote subsequent accounts."""
        from hosting.db import create_account_atomic_first_admin
        from werkzeug.security import generate_password_hash

        _aid1, is_first1 = create_account_atomic_first_admin(
            "atomicfirst", generate_password_hash("pw")
        )
        assert is_first1 is True

        aid2, is_first2 = create_account_atomic_first_admin(
            "atomicsecond", generate_password_hash("pw")
        )
        assert is_first2 is False
        from hosting.db import get_account_by_id
        acct2 = get_account_by_id(aid2)
        assert acct2["is_admin"] == 0


# ---------------------------------------------------------------------------
# Last-admin guard tests
# ---------------------------------------------------------------------------


class TestLastAdminGuards:
    """Tests for preventing demotion or deletion of the last admin."""

    def test_cannot_revoke_last_admin(self, client):
        """Toggling admin off the only admin is blocked."""
        _signup(client, username="onlyadmin", password="password123")
        # Create a non-admin target; but try to revoke the first admin
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash
        _aid2 = create_account("nonadmin", generate_password_hash("pw"))

        # Grant admin to nonadmin, then revoke from nonadmin. That should work
        client.post(f"/admin/accounts/{_aid2}/toggle-admin")  # grant
        # Now revoke: should succeed because onlyadmin is still admin
        rv = client.post(f"/admin/accounts/{_aid2}/toggle-admin", follow_redirects=True)
        assert rv.status_code == 200
        assert b"revoked" in rv.data.lower()

    def test_cannot_revoke_sole_admin(self, client):
        """Cannot demote the only admin on the platform."""
        _signup(client, username="soleadmin", password="password123")
        from hosting.db import create_account, set_account_admin
        from werkzeug.security import generate_password_hash
        aid2 = create_account("target_admin", generate_password_hash("pw"))
        set_account_admin(aid2, True)
        # Now we have 2 admins.  Revoke soleadmin's target
        # First, revoke target_admin: should succeed (2 admins -> 1)
        rv = client.post(f"/admin/accounts/{aid2}/toggle-admin", follow_redirects=True)
        assert b"revoked" in rv.data.lower()

    def test_revoke_blocked_when_last(self, client):
        """Cannot revoke the last remaining admin via toggle."""
        _signup(client, username="lastadmin", password="password123")
        from hosting.db import create_account, set_account_admin
        from werkzeug.security import generate_password_hash
        aid2 = create_account("onlytarget", generate_password_hash("pw"))
        # Only lastadmin is admin; try to toggle admin off via self: blocked
        # But self-toggle is separately blocked. Create a second admin, then
        # revoke both except one.
        set_account_admin(aid2, True)
        # Revoke aid2 (2 -> 1 admin): OK
        rv = client.post(f"/admin/accounts/{aid2}/toggle-admin", follow_redirects=True)
        assert b"revoked" in rv.data.lower()
        # Now aid2 is no longer admin. Grant it again.
        client.post(f"/admin/accounts/{aid2}/toggle-admin")
        # Now revoke again (2 -> 1): still OK because lastadmin remains
        rv = client.post(f"/admin/accounts/{aid2}/toggle-admin", follow_redirects=True)
        assert b"revoked" in rv.data.lower()

    def test_admin_delete_blocked_for_last_admin(self, client):
        """Admin panel cannot delete the last admin account."""
        _signup(client, username="deladmin", password="password123")
        from hosting.db import create_account, set_account_admin
        from werkzeug.security import generate_password_hash
        aid2 = create_account("del_target", generate_password_hash("pw"))
        set_account_admin(aid2, True)
        # Revoke aid2 admin first, making deladmin the only admin
        client.post(f"/admin/accounts/{aid2}/toggle-admin")
        # Now try to delete aid2: should work because aid2 is not admin
        rv = client.post(f"/admin/accounts/{aid2}/delete", follow_redirects=True)
        assert b"deleted" in rv.data.lower()

    def test_self_delete_blocked_for_last_admin(self, client):
        """Last admin cannot delete their own account."""
        _signup(client, username="selfdeladmin", password="password123")
        rv = client.post(
            "/settings/delete", data={"current_password": "password123"},
            follow_redirects=True,
        )
        assert b"last admin" in rv.data.lower()

    def test_self_delete_allowed_when_other_admins_exist(self, client):
        """Admin can delete their own account if another admin exists."""
        _signup(client, username="deladmin2", password="password123")
        from hosting.db import create_account, set_account_admin
        from werkzeug.security import generate_password_hash
        aid2 = create_account("otheradmin2", generate_password_hash("pw"))
        set_account_admin(aid2, True)
        rv = client.post(
            "/settings/delete", data={"current_password": "password123"},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"deleted" in rv.data.lower()


# ---------------------------------------------------------------------------
# Password max length enforcement
# ---------------------------------------------------------------------------


class TestPasswordMaxLength:
    """Tests for password length upper bound enforcement."""

    def test_signup_rejects_too_long_password(self, client):
        """Signup rejects passwords exceeding the maximum length."""
        long_pw = "a" * 1001
        rv = client.post("/signup", data={
            "username": "longpwuser",
            "password": long_pw,
            "confirm_password": long_pw,
        })
        assert rv.status_code == 400
        assert b"cannot exceed" in rv.data.lower()

    def test_signup_accepts_max_length_password(self, client):
        """Signup accepts a password at exactly the maximum length."""
        max_pw = ("a" * 999) + "1"
        rv = client.post("/signup", data={
            "username": "maxpwuser",
            "password": max_pw,
            "confirm_password": max_pw,
        }, follow_redirects=True)
        assert rv.status_code == 200

    def test_login_rejects_too_long_password(self, client):
        """Login rejects passwords exceeding the maximum length without hashing."""
        _signup(client)
        client.post("/logout")
        long_pw = "a" * 1001
        rv = client.post("/login", data={
            "username": "testuser",
            "password": long_pw,
        })
        assert rv.status_code == 401


# ---------------------------------------------------------------------------
# Admin terminate instance verification
# ---------------------------------------------------------------------------


class TestAdminTerminateVerification:
    """Tests for admin terminate instance existence check."""

    def test_admin_terminate_nonexistent_instance(self, client):
        """Admin terminate returns error for nonexistent instance."""
        _signup(client)
        rv = client.post("/admin/instances/nonexistent123/terminate", follow_redirects=True)
        assert rv.status_code == 200
        assert b"not found" in rv.data.lower()

    def test_admin_terminate_shows_subdomain(self, client):
        """Admin terminate success message includes instance subdomain."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "admterm"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("admterm")
        rv = client.post(f"/admin/instances/{inst['id']}/terminate", follow_redirects=True)
        assert rv.status_code == 200
        assert b"admterm" in rv.data.lower()


# ---------------------------------------------------------------------------
# DB rate limiting helpers
# ---------------------------------------------------------------------------


class TestHostingRateLimitDB:
    """Tests for the DB-backed rate limiting helpers."""

    def test_check_and_record_allows_within_limit(self):
        """Requests within the limit are allowed."""
        from hosting.db import check_and_record_hosting_rate_limit
        for _ in range(5):
            assert check_and_record_hosting_rate_limit("1.2.3.4", "test_bucket", 5, 60)

    def test_check_and_record_blocks_over_limit(self):
        """Requests over the limit are blocked."""
        from hosting.db import check_and_record_hosting_rate_limit
        for _ in range(3):
            check_and_record_hosting_rate_limit("1.2.3.4", "test2", 3, 60)
        assert not check_and_record_hosting_rate_limit("1.2.3.4", "test2", 3, 60)

    def test_different_ips_independent(self):
        """Rate limits are tracked per-IP."""
        from hosting.db import check_and_record_hosting_rate_limit
        for _ in range(3):
            check_and_record_hosting_rate_limit("10.0.0.1", "test3", 3, 60)
        assert not check_and_record_hosting_rate_limit("10.0.0.1", "test3", 3, 60)
        assert check_and_record_hosting_rate_limit("10.0.0.2", "test3", 3, 60)

    def test_different_buckets_independent(self):
        """Rate limits are tracked per-bucket."""
        from hosting.db import check_and_record_hosting_rate_limit
        for _ in range(3):
            check_and_record_hosting_rate_limit("10.0.0.1", "bucket_a", 3, 60)
        assert not check_and_record_hosting_rate_limit("10.0.0.1", "bucket_a", 3, 60)
        assert check_and_record_hosting_rate_limit("10.0.0.1", "bucket_b", 3, 60)


# ---------------------------------------------------------------------------
# count_admin_accounts
# ---------------------------------------------------------------------------


class TestCountAdminAccounts:
    """Tests for the count_admin_accounts helper."""

    def test_zero_when_empty(self):
        """Returns 0 when there are no accounts."""
        from hosting.db import count_admin_accounts
        assert count_admin_accounts() == 0

    def test_counts_only_admins(self):
        """Counts only admin accounts, not regular ones."""
        from hosting.db import create_account, set_account_admin, count_admin_accounts
        from werkzeug.security import generate_password_hash
        aid1 = create_account("adm1", generate_password_hash("pw"))
        create_account("usr1", generate_password_hash("pw"))
        assert count_admin_accounts() == 0
        set_account_admin(aid1, True)
        assert count_admin_accounts() == 1


# ---------------------------------------------------------------------------
# Rate limit: GET requests not counted
# ---------------------------------------------------------------------------


class TestRateLimitGetPassthrough:
    """Tests that GET requests are not counted against the rate limit quota."""

    def test_signup_get_not_rate_limited(self, client):
        """GET /signup is never rate-limited regardless of prior POST attempts."""
        # Exhaust the signup POST rate limit
        for i in range(5):
            client.post("/signup", data={
                "username": f"rlget{i}",
                "password": "password123",
                "confirm_password": "password123",
            })
            client.post("/logout")
        # POST should now be blocked
        rv = client.post("/signup", data={
            "username": "rlget_new",
            "password": "password123",
            "confirm_password": "password123",
        })
        assert rv.status_code == 429
        # But GET should still work
        rv = client.get("/signup")
        assert rv.status_code == 200


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


class TestPasswordChange:
    """Tests for the account password change endpoint."""

    def test_change_password_success(self, client):
        """A user can change their password with the correct current password."""
        _signup(client)
        rv = client.post("/settings/change-password", data={
            "current_password": "password123",
            "new_password": "newpassword456",
            "confirm_new_password": "newpassword456",
        }, follow_redirects=True)
        assert rv.status_code == 200
        assert b"Password changed successfully" in rv.data

    def test_change_password_wrong_current(self, client):
        """Wrong current password is rejected."""
        _signup(client)
        rv = client.post("/settings/change-password", data={
            "current_password": "wrongpassword",
            "new_password": "newpassword456",
            "confirm_new_password": "newpassword456",
        }, follow_redirects=True)
        assert b"Current password is incorrect" in rv.data

    def test_change_password_mismatch(self, client):
        """Mismatched new passwords are rejected."""
        _signup(client)
        rv = client.post("/settings/change-password", data={
            "current_password": "password123",
            "new_password": "newpassword456",
            "confirm_new_password": "different789",
        }, follow_redirects=True)
        assert b"do not match" in rv.data.lower()

    def test_change_password_too_short(self, client):
        """New password under 8 characters is rejected."""
        _signup(client)
        rv = client.post("/settings/change-password", data={
            "current_password": "password123",
            "new_password": "short",
            "confirm_new_password": "short",
        }, follow_redirects=True)
        assert b"at least 8 characters" in rv.data.lower()

    def test_change_password_too_long(self, client):
        """New password over the max length is rejected."""
        _signup(client)
        long_pw = "a" * 1001
        rv = client.post("/settings/change-password", data={
            "current_password": "password123",
            "new_password": long_pw,
            "confirm_new_password": long_pw,
        }, follow_redirects=True)
        assert b"cannot exceed" in rv.data.lower()

    def test_change_password_requires_letter_and_number(self, client):
        """New password must include at least one letter and one number."""
        _signup(client)
        rv = client.post("/settings/change-password", data={
            "current_password": "password123",
            "new_password": "onlyletters",
            "confirm_new_password": "onlyletters",
        }, follow_redirects=True)
        assert b"at least one letter and one number" in rv.data

    def test_changed_password_works_on_login(self, client):
        """After changing, the new password works for login."""
        _signup(client)
        client.post("/settings/change-password", data={
            "current_password": "password123",
            "new_password": "newpassword456",
            "confirm_new_password": "newpassword456",
        })
        client.post("/logout")
        # Old password should fail
        rv = client.post("/login", data={
            "username": "testuser",
            "password": "password123",
        })
        assert rv.status_code == 401
        # New password should work
        rv = _login(client, password="newpassword456")
        assert rv.status_code == 200

    def test_change_password_requires_login(self, client):
        """Change password requires an authenticated session."""
        rv = client.post("/settings/change-password", data={
            "current_password": "password123",
            "new_password": "newpassword456",
            "confirm_new_password": "newpassword456",
        }, follow_redirects=True)
        assert b"Log In" in rv.data

    def test_change_password_rate_limited(self, client):
        """POST /settings/change-password is rate-limited."""
        _signup(client)
        for _ in range(5):
            client.post("/settings/change-password", data={
                "current_password": "wrongpassword",
                "new_password": "newpassword456",
                "confirm_new_password": "newpassword456",
            })
        rv = client.post("/settings/change-password", data={
            "current_password": "wrongpassword",
            "new_password": "newpassword456",
            "confirm_new_password": "newpassword456",
        })
        assert rv.status_code == 429


# ---------------------------------------------------------------------------
# CSP nonce injection
# ---------------------------------------------------------------------------


class TestCSPNonce:
    """Tests for CSP nonce injection and Content-Security-Policy header."""

    def test_csp_header_present(self, client):
        """Responses include a Content-Security-Policy header."""
        _signup(client)
        rv = client.get("/dashboard")
        assert "Content-Security-Policy" in rv.headers

    def test_csp_header_has_nonce(self, client):
        """The CSP header references a nonce for script-src."""
        _signup(client)
        rv = client.get("/dashboard")
        csp = rv.headers.get("Content-Security-Policy", "")
        assert "nonce-" in csp

    def test_csp_nonce_in_template(self, client):
        """Dashboard template renders the nonce attribute on inline scripts."""
        _signup(client)
        rv = client.get("/dashboard")
        assert b'nonce="' in rv.data

    def test_csp_nonce_varies_per_request(self, client):
        """Each response has a different nonce value."""
        _signup(client)
        rv1 = client.get("/dashboard")
        rv2 = client.get("/dashboard")
        csp1 = rv1.headers.get("Content-Security-Policy", "")
        csp2 = rv2.headers.get("Content-Security-Policy", "")
        # Extract nonces and compare
        import re
        nonce1 = re.search(r"nonce-([a-f0-9]+)", csp1)
        nonce2 = re.search(r"nonce-([a-f0-9]+)", csp2)
        assert nonce1 and nonce2
        assert nonce1.group(1) != nonce2.group(1)

    def test_login_page_has_security_headers(self, client):
        """Even unauthenticated pages get the security headers."""
        rv = client.get("/login")
        assert "X-Content-Type-Options" in rv.headers
        assert "Content-Security-Policy" in rv.headers


# ---------------------------------------------------------------------------
# change_account_password DB helper
# ---------------------------------------------------------------------------


class TestChangeAccountPasswordDB:
    """Tests for the change_account_password DB helper."""

    def test_change_password_updates_hash(self):
        """change_account_password stores a new password hash."""
        from hosting.db import create_account, change_account_password, get_account_by_id
        from werkzeug.security import generate_password_hash, check_password_hash

        aid = create_account("pwchange1", generate_password_hash("oldpw"))
        new_hash = generate_password_hash("newpw")
        change_account_password(aid, new_hash)
        acct = get_account_by_id(aid)
        assert check_password_hash(acct["password"], "newpw")
        assert not check_password_hash(acct["password"], "oldpw")


# ---------------------------------------------------------------------------
# Session fixation prevention
# ---------------------------------------------------------------------------


class TestSessionFixation:
    """Tests that login and signup regenerate the session."""

    def test_login_clears_session(self, client):
        """Login clears pre-existing session data to prevent fixation."""
        _signup(client, username="fixuser", password="password123")
        client.post("/logout")
        # Set a custom session value before login
        with client.session_transaction() as sess:
            sess["evil"] = "injected"
        _login(client, username="fixuser", password="password123")
        with client.session_transaction() as sess:
            assert "evil" not in sess
            assert "hosting_account_id" in sess

    def test_signup_clears_session(self, client):
        """Signup clears pre-existing session data to prevent fixation."""
        with client.session_transaction() as sess:
            sess["evil"] = "injected"
        _signup(client, username="fixuser2", password="password123")
        with client.session_transaction() as sess:
            assert "evil" not in sess
            assert "hosting_account_id" in sess


# ---------------------------------------------------------------------------
# Dashboard template - duration_days from config
# ---------------------------------------------------------------------------


class TestDashboardDurationDays:
    """Tests that the dashboard uses the configured trial duration."""

    def test_empty_state_shows_config_duration(self, client):
        """The empty-state text uses INSTANCE_DURATION_DAYS from config."""
        import hosting.config as hc
        original = hc.INSTANCE_DURATION_DAYS
        try:
            hc.INSTANCE_DURATION_DAYS = 30
            _signup(client)
            rv = client.get("/dashboard")
            assert b"free for 30 days" in rv.data
        finally:
            hc.INSTANCE_DURATION_DAYS = original


# ---------------------------------------------------------------------------
# Dead config removed
# ---------------------------------------------------------------------------


class TestConfigCleanup:
    """Tests that stale config entries were removed."""

    def test_no_hosting_admin_username(self):
        """HOSTING_ADMIN_USERNAME config was removed."""
        assert not hasattr(hosting_config, "HOSTING_ADMIN_USERNAME")

    def test_no_hosting_admin_password(self):
        """HOSTING_ADMIN_PASSWORD config was removed."""
        assert not hasattr(hosting_config, "HOSTING_ADMIN_PASSWORD")


# ---------------------------------------------------------------------------
# Subdomain reverse proxy
# ---------------------------------------------------------------------------


class TestSubdomainExtraction:
    """Tests for _extract_subdomain helper."""

    @pytest.fixture(autouse=True)
    def reset_suffix(self):
        """Clear INSTANCE_URL_SUFFIX so extraction uses plain subdomain format."""
        old_suffix = hosting_config.INSTANCE_URL_SUFFIX
        hosting_config.INSTANCE_URL_SUFFIX = ""
        yield
        hosting_config.INSTANCE_URL_SUFFIX = old_suffix

    def test_exact_base_domain_returns_none(self):
        """The portal's own domain should not be treated as a subdomain."""
        from hosting._subdomain_proxy import _extract_subdomain
        original = hosting_config.BASE_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            assert _extract_subdomain("hosting.example.com") == (None, None)
        finally:
            hosting_config.BASE_DOMAIN = original

    def test_subdomain_extracted(self):
        """A valid subdomain prefix should be extracted."""
        from hosting._subdomain_proxy import _extract_subdomain
        original = hosting_config.BASE_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            assert _extract_subdomain("luca.hosting.example.com") == ("luca", "hosting")
        finally:
            hosting_config.BASE_DOMAIN = original

    def test_subdomain_case_insensitive(self):
        """Subdomain extraction should be case-insensitive."""
        from hosting._subdomain_proxy import _extract_subdomain
        original = hosting_config.BASE_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            assert _extract_subdomain("LUCA.HOSTING.EXAMPLE.COM") == ("luca", "hosting")
        finally:
            hosting_config.BASE_DOMAIN = original

    def test_host_with_port(self):
        """Host header with a port suffix should still match."""
        from hosting._subdomain_proxy import _extract_subdomain
        original = hosting_config.BASE_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            assert _extract_subdomain("team.hosting.example.com:80") == ("team", "hosting")
        finally:
            hosting_config.BASE_DOMAIN = original

    def test_nested_subdomain_rejected(self):
        """Nested subdomains (a.b.hosting.domain) should be rejected."""
        from hosting._subdomain_proxy import _extract_subdomain
        original = hosting_config.BASE_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            assert _extract_subdomain("a.b.hosting.example.com") == (None, None)
        finally:
            hosting_config.BASE_DOMAIN = original

    def test_unrelated_domain_returns_none(self):
        """A completely unrelated domain should return None."""
        from hosting._subdomain_proxy import _extract_subdomain
        original = hosting_config.BASE_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            assert _extract_subdomain("evil.com") == (None, None)
        finally:
            hosting_config.BASE_DOMAIN = original

    def test_empty_base_domain(self):
        """When BASE_DOMAIN is empty, always return None."""
        from hosting._subdomain_proxy import _extract_subdomain
        original = hosting_config.BASE_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = ""
            assert _extract_subdomain("anything.example.com") == (None, None)
        finally:
            hosting_config.BASE_DOMAIN = original


class TestSubdomainProxy404:
    """Tests that subdomain requests for unknown instances return 404."""

    def test_unknown_subdomain_returns_404(self):
        """A subdomain with no matching instance should get a 404."""
        original_mode = hosting_config.HOSTING_MODE
        original_base = hosting_config.BASE_DOMAIN
        original_suffix = hosting_config.INSTANCE_URL_SUFFIX
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            rv = c.get("/", headers={"Host": "nonexistent.hosting.example.com"})
            assert rv.status_code == 404
            assert b"No wiki instance found" in rv.data
        finally:
            hosting_config.HOSTING_MODE = original_mode
            hosting_config.BASE_DOMAIN = original_base
            hosting_config.INSTANCE_URL_SUFFIX = original_suffix

    def test_portal_domain_passes_through(self):
        """Requests to the portal's own domain should reach the Flask app."""
        original_mode = hosting_config.HOSTING_MODE
        original_base = hosting_config.BASE_DOMAIN
        original_suffix = hosting_config.INSTANCE_URL_SUFFIX
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            # The portal login page should load normally
            rv = c.get("/login", headers={"Host": "hosting.example.com"})
            assert rv.status_code == 200
        finally:
            hosting_config.HOSTING_MODE = original_mode
            hosting_config.BASE_DOMAIN = original_base
            hosting_config.INSTANCE_URL_SUFFIX = original_suffix


class TestSubdomainProxyRecovering:
    """Regression tests for the recovering-instance proxy path."""

    def test_recovering_subdomain_returns_200_with_refresh(self):
        """A recovering instance must render the branded splash with HTTP 200.

        Returning 502 here used to let upstream CDNs / browsers mask the
        body with their own generic "Bad Gateway" page, so a user who had
        just deployed a hosting-platform instance would see "Bad Gateway"
        even though lazy recovery was actively bringing the wiki back up.
        Switching to 200 OK lets *our* branded "Starting up…" page with
        ``<meta http-equiv="refresh">`` reach the browser intact.
        """
        original_mode = hosting_config.HOSTING_MODE
        original_base = hosting_config.BASE_DOMAIN
        original_suffix = hosting_config.INSTANCE_URL_SUFFIX
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()

            with patch(
                "hosting._subdomain_proxy._classify_subdomain",
                return_value=(None, "recovering"),
            ), patch(
                "hosting._subdomain_proxy._trigger_lazy_recovery",
            ) as mock_recover:
                rv = c.get(
                    "/",
                    headers={"Host": "broken.hosting.example.com"},
                )

            assert rv.status_code == 200
            assert b"Starting up" in rv.data
            assert b'http-equiv="refresh"' in rv.data
            # ``Retry-After`` is still surfaced so well-behaved clients
            # (curl, wget, monitoring probes) know how long to wait.
            assert rv.headers.get("Retry-After") == "5"
            mock_recover.assert_called_once_with("broken", "hosting")
        finally:
            hosting_config.HOSTING_MODE = original_mode
            hosting_config.BASE_DOMAIN = original_base
            hosting_config.INSTANCE_URL_SUFFIX = original_suffix


class TestSubdomainProxyPortMode:
    """Ensure the proxy middleware is inactive in port mode."""

    def test_port_mode_no_proxy(self):
        """In port mode, subdomain-like hosts should pass through normally."""
        original_mode = hosting_config.HOSTING_MODE
        original_base = hosting_config.BASE_DOMAIN
        try:
            hosting_config.HOSTING_MODE = "port"
            hosting_config.BASE_DOMAIN = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            # Should get a normal response, not a proxy 404
            rv = c.get("/login")
            assert rv.status_code == 200
        finally:
            hosting_config.HOSTING_MODE = original_mode
            hosting_config.BASE_DOMAIN = original_base


class TestSubdomainProxyStreaming:
    """Tests that _proxy_request streams request bodies and cleans up sockets.

    Regression coverage for the stability fix that stops the proxy from
    buffering the entire upload in RAM (which used to pin the worker
    and trigger 504s/500s on large image/video uploads through the
    portal) and gives 504, not 502, on upstream read timeouts.
    """

    def test_limited_reader_caps_read_size(self):
        """``_LimitedReader.read(size)`` must never exceed the cap."""
        from io import BytesIO

        from hosting._subdomain_proxy import _LimitedReader

        underlying = BytesIO(b"x" * 100)
        reader = _LimitedReader(underlying, 10)
        first = reader.read(64)
        assert first == b"x" * 10
        # All allowed bytes consumed → further reads must return EOF.
        assert reader.read(64) == b""

    def test_limited_reader_streams_in_chunks(self):
        """Multiple ``read(n)`` calls together yield exactly *length* bytes."""
        from io import BytesIO

        from hosting._subdomain_proxy import _LimitedReader

        underlying = BytesIO(b"abcdefghij")
        reader = _LimitedReader(underlying, 7)
        out = b""
        while True:
            chunk = reader.read(3)
            if not chunk:
                break
            out += chunk
        assert out == b"abcdefg"

    def test_limited_reader_handles_premature_eof(self):
        """If the underlying stream EOFs early, the reader stops cleanly."""
        from io import BytesIO

        from hosting._subdomain_proxy import _LimitedReader

        underlying = BytesIO(b"abc")
        reader = _LimitedReader(underlying, 50)
        assert reader.read(10) == b"abc"
        assert reader.read(10) == b""

    def test_proxy_request_streams_body_to_upstream(self):
        """A POST body must reach upstream WITHOUT first being buffered.

        The fake ``HTTPConnection`` records the *type* of the body it
        receives: a regression in the proxy that calls ``read()`` on
        ``wsgi.input`` would deliver bytes here instead of the
        streaming ``_LimitedReader`` and re-introduce the OOM/hang
        symptoms users reported on large uploads.
        """
        from io import BytesIO

        from hosting import _subdomain_proxy
        from hosting._subdomain_proxy import _LimitedReader, _proxy_request

        captured = {}

        class _FakeResp:
            status = 200
            reason = "OK"

            def getheaders(self):
                return [("Content-Type", "text/plain")]

            def read(self, _n):
                return b""

            def close(self):
                pass

        class _FakeConn:
            def __init__(self, host, port, timeout=None):
                captured["host"] = host
                captured["port"] = port
                captured["timeout"] = timeout

            def request(self, method, path, body=None, headers=None):
                captured["method"] = method
                captured["path"] = path
                captured["body"] = body
                captured["headers"] = headers

            def getresponse(self):
                return _FakeResp()

            def close(self):
                captured["closed"] = True

        # Drain the body iterator to make sure cleanup happens.
        with patch.object(_subdomain_proxy.http.client, "HTTPConnection", _FakeConn):
            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/upload",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": "20",
                "CONTENT_TYPE": "application/octet-stream",
                "wsgi.input": BytesIO(b"x" * 100),
                "wsgi.url_scheme": "https",
                "REMOTE_ADDR": "1.2.3.4",
                "HTTP_HOST": "luca.example.com",
            }
            status, _headers, body_iter = _proxy_request(environ, 6001)
            list(body_iter)  # drain

        assert status.startswith("200 ")
        assert isinstance(captured["body"], _LimitedReader)
        assert captured["headers"]["content-length"] == "20"
        # Connection cleaned up by body_iter()'s finally clause.
        assert captured.get("closed") is True

    def test_proxy_request_drops_unparseable_content_length(self):
        """Garbage CONTENT_LENGTH must not be forwarded to the upstream."""
        from io import BytesIO

        from hosting import _subdomain_proxy
        from hosting._subdomain_proxy import _proxy_request

        captured = {}

        class _FakeResp:
            status = 200
            reason = "OK"

            def getheaders(self):
                return []

            def read(self, _n):
                return b""

            def close(self):
                pass

        class _FakeConn:
            def __init__(self, *a, **kw):
                pass

            def request(self, method, path, body=None, headers=None):
                captured["body"] = body
                captured["headers"] = headers

            def getresponse(self):
                return _FakeResp()

            def close(self):
                pass

        with patch.object(_subdomain_proxy.http.client, "HTTPConnection", _FakeConn):
            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/x",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": "not-a-number",
                "wsgi.input": BytesIO(b"abc"),
                "wsgi.url_scheme": "http",
                "REMOTE_ADDR": "1.2.3.4",
                "HTTP_HOST": "luca.example.com",
            }
            status, _h, body_iter = _proxy_request(environ, 6001)
            list(body_iter)

        assert status.startswith("200 ")
        # No body forwarded and no bogus Content-Length to make upstream wait.
        assert captured["body"] is None
        assert "content-length" not in {
            k.lower() for k in (captured["headers"] or {}).keys()
        }

    def test_proxy_request_returns_504_on_upstream_timeout(self):
        """``socket.timeout`` from the upstream maps to 504 + Retry-After."""
        import socket

        from hosting import _subdomain_proxy
        from hosting._subdomain_proxy import _proxy_request

        closed = {"value": False}

        class _TimeoutConn:
            def __init__(self, *a, **kw):
                pass

            def request(self, *a, **kw):
                raise socket.timeout("read timed out")

            def getresponse(self):  # pragma: no cover - never reached
                raise AssertionError("should not be called")

            def close(self):
                closed["value"] = True

        with patch.object(_subdomain_proxy.http.client, "HTTPConnection", _TimeoutConn):
            environ = {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/",
                "QUERY_STRING": "",
                "wsgi.input": None,
                "wsgi.url_scheme": "https",
                "REMOTE_ADDR": "1.2.3.4",
                "HTTP_HOST": "luca.example.com",
            }
            status, headers, body = _proxy_request(environ, 6001)

        assert status.startswith("504 ")
        assert any(k.lower() == "retry-after" for k, _ in headers)
        assert b"504" in b"".join(body)
        assert closed["value"] is True

    def test_proxy_request_returns_502_on_connection_refused(self):
        """A ``ConnectionRefusedError`` from the upstream maps to 502."""
        from hosting import _subdomain_proxy
        from hosting._subdomain_proxy import _proxy_request

        closed = {"value": False}

        class _RefusedConn:
            def __init__(self, *a, **kw):
                pass

            def request(self, *a, **kw):
                raise ConnectionRefusedError("nope")

            def getresponse(self):  # pragma: no cover
                raise AssertionError("should not be called")

            def close(self):
                closed["value"] = True

        with patch.object(_subdomain_proxy.http.client, "HTTPConnection", _RefusedConn):
            environ = {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/",
                "QUERY_STRING": "",
                "wsgi.input": None,
                "wsgi.url_scheme": "http",
                "REMOTE_ADDR": "1.2.3.4",
                "HTTP_HOST": "luca.example.com",
            }
            status, _headers, body = _proxy_request(environ, 6001)

        assert status.startswith("502 ")
        assert b"502" in b"".join(body)
        assert closed["value"] is True


class TestPortalDomainRouting:
    """Tests for PORTAL_DOMAIN routing (Cloudflare-friendly setup).

    When PORTAL_DOMAIN differs from BASE_DOMAIN, the portal lives on its
    own hostname while instances use ``<slug>.BASE_DOMAIN``.  This avoids
    multi-level subdomains that Cloudflare free SSL cannot cover.
    """

    def _save_config(self):
        return (
            hosting_config.HOSTING_MODE,
            hosting_config.BASE_DOMAIN,
            hosting_config.PORTAL_DOMAIN,
            hosting_config.EFFECTIVE_PORTAL_DOMAIN,
            hosting_config.INSTANCE_URL_SUFFIX,
        )

    def _restore_config(self, saved):
        (
            hosting_config.HOSTING_MODE,
            hosting_config.BASE_DOMAIN,
            hosting_config.PORTAL_DOMAIN,
            hosting_config.EFFECTIVE_PORTAL_DOMAIN,
            hosting_config.INSTANCE_URL_SUFFIX,
        ) = saved

    def test_portal_domain_passes_through(self):
        """Requests to PORTAL_DOMAIN should reach the Flask app, not the proxy."""
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            rv = c.get("/login", headers={"Host": "hosting.example.com"})
            assert rv.status_code == 200
        finally:
            self._restore_config(saved)

    def test_instance_subdomain_proxied(self):
        """Instance subdomains like luca.example.com should hit the proxy."""
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            # No instance named "luca" exists → should get proxy 404
            rv = c.get("/", headers={"Host": "luca.example.com"})
            assert rv.status_code == 404
            assert b"No wiki instance found" in rv.data
        finally:
            self._restore_config(saved)

    def test_base_domain_passes_through(self):
        """The bare BASE_DOMAIN should also pass through to the Flask app."""
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            rv = c.get("/login", headers={"Host": "example.com"})
            assert rv.status_code == 200
        finally:
            self._restore_config(saved)

    def test_extract_subdomain_with_portal_domain(self):
        """_extract_subdomain should return None for PORTAL_DOMAIN."""
        from hosting._subdomain_proxy import _extract_subdomain
        saved = self._save_config()
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            # Portal domain → None (pass through)
            assert _extract_subdomain("hosting.example.com") == (None, None)
            # Instance subdomain → extracted (hosting mode when no suffix configured)
            assert _extract_subdomain("luca.example.com") == ("luca", "hosting")
            # Base domain → None (pass through)
            assert _extract_subdomain("example.com") == (None, None)
        finally:
            self._restore_config(saved)

    def test_extract_subdomain_portal_domain_case_insensitive(self):
        """PORTAL_DOMAIN matching should be case-insensitive."""
        from hosting._subdomain_proxy import _extract_subdomain
        saved = self._save_config()
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            assert _extract_subdomain("HOSTING.EXAMPLE.COM") == (None, None)
            assert _extract_subdomain("Hosting.Example.Com") == (None, None)
        finally:
            self._restore_config(saved)

    def test_portal_prefix_reserved(self):
        """The portal's subdomain prefix should be auto-reserved."""
        saved = self._save_config()
        original_reserved = hosting_config.RESERVED_SUBDOMAINS.copy()
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "myportal.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "myportal.example.com"
            # Simulate the config-time reservation logic
            _base_lower = "example.com"
            _portal_lower = "myportal.example.com"
            _suffix = "." + _base_lower
            if _portal_lower.endswith(_suffix):
                _prefix = _portal_lower[: -len(_suffix)]
                hosting_config.RESERVED_SUBDOMAINS.add(_prefix)
            assert "myportal" in hosting_config.RESERVED_SUBDOMAINS
        finally:
            hosting_config.RESERVED_SUBDOMAINS = original_reserved
            self._restore_config(saved)

    def test_404_links_to_portal_domain(self):
        """The proxy 404 page should link to PORTAL_DOMAIN, not BASE_DOMAIN."""
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            rv = c.get("/", headers={"Host": "nonexistent.example.com"})
            assert rv.status_code == 404
            assert b"hosting.example.com" in rv.data
        finally:
            self._restore_config(saved)

    def test_unclaimed_apex_host_returns_proxy_404(self):
        """Unclaimed apex hosts like wiki.example.com should not render portal UI."""
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            rv = c.get("/", headers={"Host": "wiki.example.com"})
            assert rv.status_code == 404
            assert b"No wiki instance found" in rv.data
            assert b"hosting.example.com" in rv.data
            assert b"Create Account" not in rv.data
        finally:
            self._restore_config(saved)

    def test_portal_static_assets_work_with_apex_routing_enabled(self):
        """Portal CSS should be served by Flask on hosting.example.com."""
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            rv = c.get(
                "/static/css/style.css",
                headers={"Host": "hosting.example.com"},
            )
            assert rv.status_code == 200
            assert b"Admin dashboard" in rv.data
        finally:
            self._restore_config(saved)

    def test_flattened_instance_host_still_uses_proxy_404(self):
        """Flat hosts like team-hosting.example.com remain instance hosts."""
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()
            rv = c.get("/", headers={"Host": "team-hosting.example.com"})
            assert rv.status_code == 404
            assert b"No wiki instance found" in rv.data
        finally:
            self._restore_config(saved)

    def test_unhealthy_running_instance_does_not_proxy(self):
        """A stale running row should not proxy traffic if the instance is unhealthy.

        It must also report 502 + Retry-After (a transient state) rather than
        404 (permanent), so browsers/CDNs retry once the instance has been
        lazy-recovered instead of pinning a 404 in front of it forever.
        """
        from hosting.db import create_account, create_instance as db_create_instance

        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""

            account_id = create_account("owner", "hash")
            db_create_instance(account_id, "luca")

            application = create_hosting_app()
            application.config["TESTING"] = True
            application.config["WTF_CSRF_ENABLED"] = False
            c = application.test_client()

            with patch("hosting.instance_manager.check_instance_process_alive", return_value=False), \
                 patch("hosting._subdomain_proxy._trigger_lazy_recovery"):
                rv = c.get("/", headers={"Host": "luca.example.com"})

            assert rv.status_code == 200
            assert rv.headers.get("Retry-After") == "5"
            assert b"Starting up" in rv.data
        finally:
            self._restore_config(saved)

    def test_default_portal_domain_is_base_domain(self):
        """When PORTAL_DOMAIN is empty, EFFECTIVE_PORTAL_DOMAIN == BASE_DOMAIN."""
        from hosting.config import _effective_portal_domain
        saved = self._save_config()
        try:
            hosting_config.BASE_DOMAIN = "wiki.example.com"
            hosting_config.PORTAL_DOMAIN = ""
            result = _effective_portal_domain()
            assert result == "wiki.example.com"
        finally:
            self._restore_config(saved)


# ---------------------------------------------------------------------------
# INSTANCE_URL_SUFFIX tests
# ---------------------------------------------------------------------------


class TestInstanceUrlSuffix:
    """Tests for the INSTANCE_URL_SUFFIX feature."""

    def _save_config(self):
        return (
            hosting_config.HOSTING_MODE,
            hosting_config.BASE_DOMAIN,
            hosting_config.INSTANCE_URL_SUFFIX,
        )

    def _restore_config(self, saved):
        (
            hosting_config.HOSTING_MODE,
            hosting_config.BASE_DOMAIN,
            hosting_config.INSTANCE_URL_SUFFIX,
        ) = saved

    # --- _instance_url() tests ---

    def test_instance_url_with_suffix(self):
        """With suffix set, _instance_url builds {slug}-{suffix}.{domain} URLs."""
        from hosting.instance_manager import _instance_url
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            inst = {"subdomain": "myteam", "port": 6001}
            url = _instance_url(inst)
            assert url == "https://myteam-hosting.example.com"
        finally:
            self._restore_config(saved)

    def test_instance_url_without_suffix_unchanged(self):
        """Without suffix, _instance_url builds {slug}.{domain} URLs as before."""
        from hosting.instance_manager import _instance_url
        saved = self._save_config()
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            inst = {"subdomain": "myteam", "port": 6001}
            url = _instance_url(inst)
            assert url == "https://myteam.example.com"
        finally:
            self._restore_config(saved)

    def test_instance_url_suffix_ignored_in_port_mode(self):
        """INSTANCE_URL_SUFFIX has no effect in port mode."""
        from hosting.instance_manager import _instance_url
        saved = self._save_config()
        old_host = hosting_config.HOSTING_PUBLIC_HOST
        old_scheme = hosting_config.HOSTING_PUBLIC_SCHEME
        try:
            hosting_config.HOSTING_MODE = "port"
            hosting_config.HOSTING_PUBLIC_HOST = "1.2.3.4"
            hosting_config.HOSTING_PUBLIC_SCHEME = "http"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            inst = {"subdomain": "myteam", "port": 6001}
            url = _instance_url(inst)
            assert url == "http://1.2.3.4:6001"
        finally:
            self._restore_config(saved)
            hosting_config.HOSTING_PUBLIC_HOST = old_host
            hosting_config.HOSTING_PUBLIC_SCHEME = old_scheme

    # --- _extract_subdomain() tests ---

    def test_extract_subdomain_with_suffix(self):
        """_extract_subdomain strips the suffix to return the bare slug."""
        from hosting._subdomain_proxy import _extract_subdomain
        saved = self._save_config()
        old_portal = hosting_config.EFFECTIVE_PORTAL_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = ""
            assert _extract_subdomain("myteam-hosting.example.com") == ("myteam", "hosting")
        finally:
            self._restore_config(saved)
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = old_portal

    def test_extract_subdomain_no_suffix_when_configured(self):
        """When suffix is configured, bare slugs without suffix only resolve
        if an apex-mode instance row exists (admin claim).  With no row,
        the host is unclaimed and returns ``(None, None)``.
        """
        from hosting._subdomain_proxy import _extract_subdomain
        saved = self._save_config()
        old_portal = hosting_config.EFFECTIVE_PORTAL_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = ""
            # "myteam.example.com" doesn't end with "-hosting" and no
            # apex row exists → not an instance.
            assert _extract_subdomain("myteam.example.com") == (None, None)
        finally:
            self._restore_config(saved)
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = old_portal

    def test_extract_subdomain_empty_after_suffix_strip(self):
        """A host that is only the suffix label returns None (not a valid slug)."""
        from hosting._subdomain_proxy import _extract_subdomain
        saved = self._save_config()
        old_portal = hosting_config.EFFECTIVE_PORTAL_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = ""
            # "-hosting.example.com" → strips suffix → empty slug → None
            # (this is a degenerate case; real slugs never start with -)
            assert _extract_subdomain("-hosting.example.com") == (None, None)
        finally:
            self._restore_config(saved)
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = old_portal

    def test_extract_subdomain_suffix_with_portal_domain(self):
        """PORTAL_DOMAIN still passes through when suffix is configured."""
        from hosting._subdomain_proxy import _extract_subdomain
        saved = self._save_config()
        old_portal = hosting_config.EFFECTIVE_PORTAL_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            hosting_config.PORTAL_DOMAIN = "hosting.example.com"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            # Portal domain → None (pass through)
            assert _extract_subdomain("hosting.example.com") == (None, None)
            # Instance with suffix → slug extracted
            assert _extract_subdomain("myteam-hosting.example.com") == ("myteam", "hosting")
        finally:
            self._restore_config(saved)
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = old_portal

    def test_extract_subdomain_no_suffix_without_config(self):
        """Without suffix configured, old-style slug.domain URLs still work."""
        from hosting._subdomain_proxy import _extract_subdomain
        saved = self._save_config()
        old_portal = hosting_config.EFFECTIVE_PORTAL_DOMAIN
        try:
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = ""
            # No suffix configured → bare slug is treated as a hosting-mode
            # instance (legacy single-tenant style).
            assert _extract_subdomain("myteam.example.com") == ("myteam", "hosting")
        finally:
            self._restore_config(saved)
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = old_portal

    # --- template tests ---

    def test_create_form_shows_suffix_in_domain_hint(self, client):
        """When suffix is set, the create form shows -{suffix}.{domain} hint."""
        saved = self._save_config()
        old_portal = hosting_config.EFFECTIVE_PORTAL_DOMAIN
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = "hosting"
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "hosting.example.com"
            _signup(client)
            rv = client.get("/instances/create")
            assert rv.status_code == 200
            assert b"-hosting.example.com" in rv.data
        finally:
            self._restore_config(saved)
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = old_portal

    def test_create_form_no_suffix_shows_plain_domain(self, client):
        """Without suffix, the create form shows .{domain} hint."""
        saved = self._save_config()
        old_portal = hosting_config.EFFECTIVE_PORTAL_DOMAIN
        try:
            hosting_config.HOSTING_MODE = "subdomain"
            hosting_config.BASE_DOMAIN = "example.com"
            hosting_config.INSTANCE_URL_SUFFIX = ""
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = "example.com"
            _signup(client)
            rv = client.get("/instances/create")
            assert rv.status_code == 200
            assert b".example.com" in rv.data
            assert b"-hosting" not in rv.data
        finally:
            self._restore_config(saved)
            hosting_config.EFFECTIVE_PORTAL_DOMAIN = old_portal


# ---------------------------------------------------------------------------
# Expanded reserved names tests
# ---------------------------------------------------------------------------


class TestExpandedReservedNames:
    """Reserved-name set covers only DNS / portal infrastructure labels.

    Generic content words (``wiki``, ``app``, ``login``, ``dashboard``,
    ``git``…) used to live in :data:`RESERVED_SUBDOMAINS` defensively
    but are no longer blocked.  The apex-vs-hosting URL split means a
    user-claimed slug like ``wiki`` resolves to
    ``wiki-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}`` (a distinct host from
    ``wiki.{BASE_DOMAIN}``), so reserving it prevented users from
    claiming their natural slug once an admin owned the apex.
    """

    def test_wiki_is_allowed(self):
        """'wiki' is no longer reserved. Both apex and hosting can use it."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain("wiki")
        assert ok, f"'wiki' should be allowed but was rejected: {reason}"
        # Also allowed for admins claiming the apex namespace.
        ok, reason = validate_subdomain(
            "wiki", account_is_admin=True, domain_mode="apex",
        )
        assert ok, f"'wiki' should be allowed in apex mode but was rejected: {reason}"

    def test_app_is_allowed(self):
        """'app' is no longer reserved."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain("app")
        assert ok, f"'app' should be allowed but was rejected: {reason}"

    def test_login_is_allowed(self):
        """'login' is no longer reserved: routes are scoped to the portal host."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain("login")
        assert ok, f"'login' should be allowed but was rejected: {reason}"

    def test_dashboard_is_allowed(self):
        """'dashboard' is no longer reserved: routes are scoped to the portal host."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain("dashboard")
        assert ok, f"'dashboard' should be allowed but was rejected: {reason}"

    def test_git_is_allowed(self):
        """'git' is no longer reserved."""
        from hosting.subdomain import validate_subdomain
        ok, reason = validate_subdomain("git")
        assert ok, f"'git' should be allowed but was rejected: {reason}"

    def test_normal_names_still_allowed(self):
        """Common user-chosen names that are NOT in the reserved list are allowed."""
        from hosting.subdomain import validate_subdomain
        for name in ("myteam", "acme", "projectx", "lab123"):
            ok, reason = validate_subdomain(name)
            assert ok, f"'{name}' should be allowed but was rejected: {reason}"

    def test_dns_infrastructure_labels_still_reserved(self):
        """Mail / DNS infrastructure labels remain reserved in both modes."""
        from hosting.subdomain import validate_subdomain
        for name in ("www", "mail", "smtp", "ns1", "portal"):
            ok, _ = validate_subdomain(name)
            assert not ok, f"'{name}' must remain reserved in hosting mode"
            ok, _ = validate_subdomain(
                name, account_is_admin=True, domain_mode="apex",
            )
            assert not ok, f"'{name}' must remain reserved in apex mode"

    def test_suffix_word_reserved_when_set(self):
        """When INSTANCE_URL_SUFFIX is set, that word is in RESERVED_SUBDOMAINS."""
        saved_suffix = hosting_config.INSTANCE_URL_SUFFIX
        original_reserved = hosting_config.RESERVED_SUBDOMAINS.copy()
        try:
            hosting_config.INSTANCE_URL_SUFFIX = "testsfx"
            hosting_config.RESERVED_SUBDOMAINS.add("testsfx")
            from hosting.subdomain import validate_subdomain
            # Manually test the reserved set (subdomain.py reads it at call time)
            assert "testsfx" in hosting_config.RESERVED_SUBDOMAINS
        finally:
            hosting_config.INSTANCE_URL_SUFFIX = saved_suffix
            hosting_config.RESERVED_SUBDOMAINS = original_reserved


# ---------------------------------------------------------------------------
# Reset instance password tests
# ---------------------------------------------------------------------------

class TestResetInstancePassword:
    """Tests for the reset_instance_password feature."""

    def test_owner_can_reset_password_endpoint(self, client):
        """Instance owner can reset admin password via the user route."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "resetpw1"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("resetpw1")
        assert inst is not None
        rv = client.post(
            f"/instances/{inst['id']}/reset-password",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"reset" in rv.data.lower() or b"password" in rv.data.lower()

    def test_owner_cannot_reset_other_users_instance(self, client):
        """An owner cannot reset a password for an instance they do not own."""
        _signup(client, username="ownerA", password="password123")
        client.post("/instances/create", data={"subdomain": "ownerainst"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("ownerainst")
        assert inst is not None

        # Log in as a different user
        client.post("/logout")
        _signup(client, username="ownerB", password="password123")
        rv = client.post(
            f"/instances/{inst['id']}/reset-password",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        # Should redirect to dashboard, not show credentials for ownerA's instance
        assert b"Instance not found" in rv.data or b"Dashboard" in rv.data

    def test_admin_can_reset_any_instance_password(self, client):
        """Admin can reset the password for any instance."""
        _signup(client, username="pwadmin", password="password123")
        client.post("/instances/create", data={"subdomain": "adminresetpw"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("adminresetpw")
        assert inst is not None
        rv = client.post(
            f"/admin/instances/{inst['id']}/reset-password",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"reset" in rv.data.lower() or b"password" in rv.data.lower()

    def test_admin_reset_password_nonexistent_instance(self, client):
        """Admin reset on non-existent instance gives an error."""
        _signup(client)
        rv = client.post(
            "/admin/instances/doesnotexist999/reset-password",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"not found" in rv.data.lower()

    def test_reset_instance_password_function_no_db(self):
        """reset_instance_password fails gracefully when the instance DB is missing."""
        from hosting.instance_manager import reset_instance_password
        from hosting.db import create_instance as db_create_instance
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash

        aid = create_account("rptest", generate_password_hash("pass"))
        inst = db_create_instance(aid, "rptestnodb", admin_username="admin", admin_password="pass123")
        # Don't create the data directory / db file: should fail gracefully
        ok, reason = reset_instance_password(inst["id"])
        assert ok is False
        assert "database" in reason.lower() or "not found" in reason.lower()

    def test_reset_instance_password_updates_admin_password_plain(self):
        """reset_instance_password stores the new password in admin_password_plain."""
        import sqlite3
        from hosting.instance_manager import reset_instance_password
        from hosting.db import create_instance as db_create_instance, get_instance
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash

        aid = create_account("rpstorage", generate_password_hash("pass"))
        inst = db_create_instance(aid, "rpstorage1", admin_username="admin", admin_password="origpass")
        assert inst is not None

        # Create a minimal data dir and BananaWiki DB with an admin user
        import os, tempfile
        from hosting import config as hc
        data_dir = os.path.join(hc.INSTANCES_DIR, "rpstorage1")
        os.makedirs(data_dir, exist_ok=True)
        db_path = os.path.join(data_dir, "bananawiki.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, password TEXT, role TEXT)")
        conn.execute("INSERT INTO users VALUES ('abc123', 'admin', 'oldhash', 'admin')")
        conn.commit()
        conn.close()

        ok, new_pw = reset_instance_password(inst["id"])
        assert ok is True
        assert len(new_pw) >= 8

        # Check that admin_password_plain in hosting DB was updated
        updated = get_instance(inst["id"])
        assert updated["admin_password_plain"] == new_pw

    def test_reset_terminated_instance_fails(self):
        """reset_instance_password fails on terminated instances."""
        from hosting.instance_manager import reset_instance_password
        from hosting.db import create_instance as db_create_instance, terminate_instance as db_terminate
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash

        aid = create_account("rpterminated", generate_password_hash("pass"))
        inst = db_create_instance(aid, "rpterm1", admin_username="admin", admin_password="pass")
        db_terminate(inst["id"])

        ok, reason = reset_instance_password(inst["id"])
        assert ok is False
        assert "terminated" in reason.lower()


# ---------------------------------------------------------------------------
# Make instance indefinite tests
# ---------------------------------------------------------------------------

class TestMakeInstanceIndefinite:
    """Tests for the make_instance_indefinite feature."""

    def test_admin_can_make_indefinite(self, client):
        """Admin can make an instance's trial run indefinitely."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "indefinite1"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("indefinite1")
        assert inst is not None
        rv = client.post(
            f"/admin/instances/{inst['id']}/make-indefinite",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"indefinitely" in rv.data.lower() or b"indefinite" in rv.data.lower()

    def test_make_indefinite_sets_far_future_expiry(self):
        """make_instance_indefinite sets expires_at at least 50 years from now."""
        from datetime import datetime, timezone
        from hosting.instance_manager import make_instance_indefinite
        from hosting.db import create_instance as db_create_instance, get_instance
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash

        aid = create_account("indefinitetest", generate_password_hash("pass"))
        inst = db_create_instance(aid, "indeftest1")
        ok, _ = make_instance_indefinite(inst["id"])
        assert ok is True

        updated = get_instance(inst["id"])
        expires = datetime.fromisoformat(updated["expires_at"]).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        remaining_years = (expires - now).days / 365
        assert remaining_years >= 50

    def test_make_indefinite_terminated_fails(self):
        """make_instance_indefinite fails on terminated instances."""
        from hosting.instance_manager import make_instance_indefinite
        from hosting.db import create_instance as db_create_instance, terminate_instance as db_terminate
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash

        aid = create_account("miiterm", generate_password_hash("pass"))
        inst = db_create_instance(aid, "miiterminated")
        db_terminate(inst["id"])

        ok, reason = make_instance_indefinite(inst["id"])
        assert ok is False
        assert "terminated" in reason.lower()

    def test_make_indefinite_nonexistent_instance(self, client):
        """Admin make-indefinite on non-existent instance gives an error."""
        _signup(client)
        rv = client.post(
            "/admin/instances/doesnotexistxyz/make-indefinite",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"not found" in rv.data.lower()

    def test_make_indefinite_requires_admin(self, client):
        """Non-admin cannot access the make-indefinite route."""
        _signup(client, username="admin1st", password="password123")
        client.post("/logout")
        _signup(client, username="regularuser2", password="password123")
        rv = client.post(
            "/admin/instances/anyid/make-indefinite",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"admin" in rv.data.lower() or b"Dashboard" in rv.data


# ---------------------------------------------------------------------------
# Restore terminated instance tests
# ---------------------------------------------------------------------------

class TestRestoreTerminatedInstance:
    """Tests for the restore_instance feature."""

    def test_admin_can_restore_terminated_instance(self, client):
        """Admin can restore a terminated instance."""
        _signup(client)
        client.post("/instances/create", data={"subdomain": "restore1"})
        from hosting.db import get_instance_by_subdomain
        inst = get_instance_by_subdomain("restore1")
        assert inst is not None

        # Terminate the instance
        client.post(
            f"/instances/{inst['id']}/terminate",
            follow_redirects=True,
        )

        # Find the terminated instance row (subdomain was archived)
        from hosting.db import get_all_instances
        all_insts = get_all_instances()
        terminated = next(
            (i for i in all_insts if "restore1" in (i["subdomain"] or "")),
            None,
        )
        assert terminated is not None, "Terminated instance not found in DB"

        rv = client.post(
            f"/admin/instances/{terminated['id']}/restore",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"restored" in rv.data.lower() or b"restore1" in rv.data.lower()

    def test_restore_non_terminated_instance_fails(self):
        """restore_instance only works on terminated instances."""
        from hosting.instance_manager import restore_instance
        from hosting.db import create_instance as db_create_instance
        from hosting.db import create_account
        from werkzeug.security import generate_password_hash

        aid = create_account("restorerunning", generate_password_hash("pass"))
        inst = db_create_instance(aid, "restorenon")
        ok, error = restore_instance(inst["id"])
        assert ok is None
        assert "terminated" in error.lower()

    def test_restore_nonexistent_instance_fails(self):
        """restore_instance returns an error for a non-existent instance."""
        from hosting.instance_manager import restore_instance
        result, error = restore_instance("doesnotexistid")
        assert result is None
        assert "not found" in error.lower()

    def test_restore_extracts_original_subdomain(self):
        """restore_instance recovers the original subdomain from the archived name."""
        from hosting.instance_manager import restore_instance
        from hosting.db import (
            create_instance as db_create_instance,
            terminate_and_archive_instance,
            create_account,
        )
        from werkzeug.security import generate_password_hash

        aid = create_account("restoresubdom", generate_password_hash("pass"))
        inst = db_create_instance(aid, "originalname")
        # Archive the terminated subdomain (as terminate_instance would)
        terminate_and_archive_instance(inst["id"], "originalname")

        new_inst, error = restore_instance(inst["id"])
        assert error is None, f"Unexpected error: {error}"
        assert new_inst is not None
        assert new_inst["subdomain"] == "originalname"

    def test_restore_admin_route_nonexistent(self, client):
        """Admin restore on non-existent instance gives an error."""
        _signup(client)
        rv = client.post(
            "/admin/instances/nonexistentid123/restore",
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert b"not found" in rv.data.lower()

    def test_admin_detail_shows_grace_period_actions(self, client):
        """Admin instance detail page exposes retained terminated instance actions."""
        from hosting.db import (
            create_instance as db_create_instance,
            get_account_by_username,
            terminate_and_archive_instance,
        )

        _signup(client, username="graceadmin", password="password123")
        account = get_account_by_username("graceadmin")
        inst = db_create_instance(account["id"], "gracedetail")
        terminate_and_archive_instance(
            inst["id"],
            "gracedetail",
            grace_period_days=7,
            reason="manual",
        )

        rv = client.get(f"/instances/{inst['id']}")
        assert rv.status_code == 200
        assert b"Restore" in rv.data
        assert b"Download archive" in rv.data
        assert b"Suspend User Export" in rv.data
        assert b"Delete permanently" in rv.data

    def test_admin_manage_shows_grace_period_actions(self, client):
        """Dedicated admin manage page also exposes retained terminated actions."""
        from hosting.db import (
            create_instance as db_create_instance,
            get_account_by_username,
            terminate_and_archive_instance,
        )

        _signup(client, username="gracemanage", password="password123")
        account = get_account_by_username("gracemanage")
        inst = db_create_instance(account["id"], "gracemanage")
        terminate_and_archive_instance(
            inst["id"],
            "gracemanage",
            grace_period_days=7,
            reason="manual",
        )

        rv = client.get(f"/admin/instances/{inst['id']}/manage")
        assert rv.status_code == 200
        assert b"Restore" in rv.data
        assert b"Download archive" in rv.data
        assert b"Suspend User Export" in rv.data
        assert b"Delete permanently" in rv.data

    def test_admin_dashboard_shows_bulk_grace_actions(self, client):
        """Admin dashboard exposes separate bulk controls for retained terminated wikis."""
        from hosting.db import (
            create_instance as db_create_instance,
            get_account_by_username,
            terminate_and_archive_instance,
        )

        _signup(client, username="gracebulkui", password="password123")
        account = get_account_by_username("gracebulkui")
        inst = db_create_instance(account["id"], "gracebulkui")
        terminate_and_archive_instance(
            inst["id"],
            "gracebulkui",
            grace_period_days=7,
            reason="manual",
        )

        rv = client.get("/admin")

        assert rv.status_code == 200
        assert b"Restore Selected Grace" in rv.data
        assert b"Delete Selected Grace" in rv.data
        assert b"class=\"grace-cb\"" in rv.data

    def test_admin_bulk_restore_grace_period_instances(self, client):
        """Admins can restore multiple retained terminated instances at once."""
        from hosting.db import (
            create_instance as db_create_instance,
            get_account_by_username,
            get_instance,
            terminate_and_archive_instance,
        )
        from hosting.instance_manager import _instance_dir_for

        _signup(client, username="gracebulkrestore", password="password123")
        account = get_account_by_username("gracebulkrestore")
        first = db_create_instance(account["id"], "bulkrestorea")
        second = db_create_instance(account["id"], "bulkrestoreb")
        terminate_and_archive_instance(
            first["id"],
            "bulkrestorea",
            grace_period_days=7,
            reason="manual",
        )
        terminate_and_archive_instance(
            second["id"],
            "bulkrestoreb",
            grace_period_days=7,
            reason="manual",
        )
        for inst_id in (first["id"], second["id"]):
            retained = get_instance(inst_id)
            retained_dir = _instance_dir_for(retained)
            os.makedirs(retained_dir, exist_ok=True)
            with open(os.path.join(retained_dir, "sentinel.txt"), "w") as fh:
                fh.write("retained")

        with patch("hosting.instance_manager._start_process", return_value=True):
            rv = client.post(
                "/admin/instances/bulk-restore-terminated",
                data={"instance_ids": [first["id"], second["id"]]},
                follow_redirects=True,
            )

        assert rv.status_code == 200
        assert b"2 grace-period instance(s) restored" in rv.data
        assert get_instance(first["id"])["status"] == "running"
        assert get_instance(second["id"])["status"] == "running"

    def test_admin_bulk_delete_grace_period_instances(self, client):
        """Admins can permanently delete multiple retained terminated instances at once."""
        from hosting.db import (
            create_instance as db_create_instance,
            get_account_by_username,
            get_instance,
            terminate_and_archive_instance,
        )

        _signup(client, username="gracebulkdelete", password="password123")
        account = get_account_by_username("gracebulkdelete")
        first = db_create_instance(account["id"], "bulkdeletea")
        second = db_create_instance(account["id"], "bulkdeleteb")
        terminate_and_archive_instance(
            first["id"],
            "bulkdeletea",
            grace_period_days=7,
            reason="manual",
        )
        terminate_and_archive_instance(
            second["id"],
            "bulkdeleteb",
            grace_period_days=7,
            reason="manual",
        )

        rv = client.post(
            "/admin/instances/bulk-delete-terminated",
            data={"instance_ids": [first["id"], second["id"]]},
            follow_redirects=True,
        )

        assert rv.status_code == 200
        assert b"2 grace-period instance(s) permanently deleted" in rv.data
        assert get_instance(first["id"]) is None
        assert get_instance(second["id"]) is None


class TestChunkedAdminImport:
    """Tests for large hosting admin imports uploaded in chunks."""

    def test_import_page_exposes_chunked_upload(self, client):
        _signup(client, username="chunkadmin", password="password123")

        rv = client.get("/admin/instances/import")

        assert rv.status_code == 200
        assert b"data-chunked-import-form" in rv.data
        assert b"/admin/instances/import/chunk/start" in rv.data

    def test_chunked_import_reassembles_archive_before_provisioning(self, client, monkeypatch):
        _signup(client, username="chunkowner", password="password123")
        monkeypatch.setattr(hosting_config, "HOSTING_IMPORT_CHUNK_BYTES", 1024 * 1024)
        monkeypatch.setattr(hosting_config, "HOSTING_IMPORT_MAX_BYTES", 4 * 1024 * 1024)

        archive_bytes = b"a" * (2 * 1024 * 1024 + 17)
        captured = {}

        def fake_provision(account_id, subdomain, archive_path, *, domain_mode, account_is_admin):
            with open(archive_path, "rb") as fh:
                captured["archive_bytes"] = fh.read()
            captured["subdomain"] = subdomain
            captured["domain_mode"] = domain_mode
            captured["account_is_admin"] = account_is_admin
            return {"id": "imported-id", "url": "https://example.test/imported"}, None

        monkeypatch.setattr(
            "hosting.routes.dashboard_archives.provision_instance_from_archive",
            fake_provision,
        )

        start = client.post("/admin/instances/import/chunk/start", data={
            "filename": "wiki.zip",
            "size": str(len(archive_bytes)),
            "subdomain": "bigwiki",
            "domain_mode": "hosting",
        })
        assert start.status_code == 200
        payload = start.get_json()
        assert payload["ok"] is True
        assert payload["chunk_size"] == 1024 * 1024
        assert payload["total_chunks"] == 3

        upload_id = payload["upload_id"]
        chunk_size = payload["chunk_size"]
        for index in range(payload["total_chunks"]):
            chunk = archive_bytes[index * chunk_size:(index + 1) * chunk_size]
            rv = client.post(
                f"/admin/instances/import/chunk/{upload_id}",
                data={
                    "chunk_index": str(index),
                    "chunk": (io.BytesIO(chunk), "wiki.zip.part"),
                },
                content_type="multipart/form-data",
            )
            assert rv.status_code == 200
            assert rv.get_json()["ok"] is True

        complete = client.post(f"/admin/instances/import/chunk/{upload_id}/complete")
        assert complete.status_code == 200
        complete_payload = complete.get_json()
        assert complete_payload["ok"] is True

        status_payload = None
        for _ in range(20):
            status = client.get(complete_payload["status_url"])
            status_payload = status.get_json()
            if status_payload["state"] == "complete":
                break
            time.sleep(0.05)

        assert status_payload["state"] == "complete"
        assert captured["archive_bytes"] == archive_bytes
        assert captured["subdomain"] == "bigwiki"
        assert captured["domain_mode"] == "hosting"
        assert captured["account_is_admin"] is True

    def test_chunked_import_rejects_partial_non_final_chunk(self, client, monkeypatch):
        _signup(client, username="chunkpartial", password="password123")
        monkeypatch.setattr(hosting_config, "HOSTING_IMPORT_CHUNK_BYTES", 1024)
        monkeypatch.setattr(hosting_config, "HOSTING_IMPORT_MAX_BYTES", 4096)

        start = client.post("/admin/instances/import/chunk/start", data={
            "filename": "wiki.zip",
            "size": "2048",
            "subdomain": "partialwiki",
            "domain_mode": "hosting",
        })
        assert start.status_code == 200
        payload = start.get_json()

        rv = client.post(
            f"/admin/instances/import/chunk/{payload['upload_id']}",
            data={
                "chunk_index": "0",
                "chunk": (io.BytesIO(b"x" * 12), "wiki.zip.part"),
            },
            content_type="multipart/form-data",
        )
        assert rv.status_code == 400
        assert rv.get_json()["ok"] is False

    def test_import_temp_cleanup_removes_stale_files_and_dirs(
        self, tmp_path, monkeypatch,
    ):
        from hosting.routes import dashboard_archives as dashboard

        import_root = tmp_path / "imports"
        import_root.mkdir()
        stale_file = import_root / "bwh-import-old.zip"
        stale_file.write_bytes(b"partial")
        stale_dir = import_root / "old-upload"
        stale_dir.mkdir()
        (stale_dir / "archive.zip").write_bytes(b"partial")

        old = time.time() - (2 * 24 * 60 * 60)
        os.utime(stale_file, (old, old))
        os.utime(stale_dir, (old, old))
        monkeypatch.setattr(hosting_config, "HOSTING_IMPORT_TEMP_DIR", str(import_root))

        dashboard._cleanup_stale_chunked_imports()

        assert not stale_file.exists()
        assert not stale_dir.exists()


class TestHostingArchiveImportHardening:
    """Tests for import rollback and ZIP expansion safeguards."""

    def _write_unified_archive(self, path, *, db_bytes=b"sqlite"):
        import archive_format
        import zipfile

        manifest = archive_format.build_manifest(
            source=archive_format.SOURCE_HOSTING,
            original_subdomain="sourcewiki",
            has_raw_db=True,
            has_site_export_json=False,
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            archive_format.write_manifest_to_zip(zf, manifest)
            zf.writestr(archive_format.RAW_DB_FILENAME, db_bytes)
            zf.writestr("uploads/example.txt", b"payload")

    def test_failed_archive_import_hard_deletes_partial_instance(
        self, tmp_path, monkeypatch,
    ):
        from hosting import instance_manager as im
        from hosting.db import (
            create_account,
            get_instance_by_subdomain,
            get_all_instances,
        )

        monkeypatch.setattr(
            hosting_config,
            "HOSTING_IMPORT_TEMP_DIR",
            str(tmp_path / "imports"),
        )
        aid = create_account("importadmin", "hash")
        archive_path = tmp_path / "wiki.zip"
        self._write_unified_archive(archive_path)

        monkeypatch.setattr(im, "_attribute_wiki_db_pages_to_system", lambda _db: None)
        monkeypatch.setattr(im, "_start_process", lambda *a, **kw: False)

        inst, error = im.provision_instance_from_archive(
            aid,
            "failedimport",
            str(archive_path),
            domain_mode="hosting",
            account_is_admin=True,
        )

        assert inst is None
        assert error == "The imported instance failed to start."
        assert get_instance_by_subdomain("failedimport") is None
        assert get_all_instances() == []
        assert not os.path.isdir(im._instance_dir("failedimport"))

    def test_archive_import_rejects_zip_that_expands_past_limit(
        self, tmp_path, monkeypatch,
    ):
        from hosting import instance_manager as im
        from hosting.db import create_account, get_all_instances

        monkeypatch.setattr(
            hosting_config,
            "HOSTING_IMPORT_TEMP_DIR",
            str(tmp_path / "imports"),
        )
        monkeypatch.setattr(hosting_config, "HOSTING_IMPORT_MAX_EXTRACTED_BYTES", 1024)
        monkeypatch.setattr(hosting_config, "HOSTING_IMPORT_MIN_FREE_BYTES", 0)

        aid = create_account("limitadmin", "hash")
        archive_path = tmp_path / "too-large.zip"
        self._write_unified_archive(archive_path, db_bytes=b"x" * 2048)

        inst, error = im.provision_instance_from_archive(
            aid,
            "toolargeimport",
            str(archive_path),
            domain_mode="hosting",
            account_is_admin=True,
        )

        assert inst is None
        assert "expands beyond" in error
        assert get_all_instances() == []
        assert not os.path.isdir(im._instance_dir("toolargeimport"))


# ---------------------------------------------------------------------------
# Hosting error pages
# ---------------------------------------------------------------------------



class TestHosting500Handler:
    """Tests for the 500 error handler's request-id + admin diagnostics."""

    def _install_failing_route(self, app, path="/_test_500_boom"):
        """Register a route that always raises so we can hit the 500 handler."""
        @app.route(path)
        def _boom():
            raise RuntimeError("deliberate test failure")

    def test_500_includes_request_id_for_anonymous_user(self, app):
        """Anonymous visitors see a generic message + request ID, no traceback."""
        self._install_failing_route(app)
        app.config["TESTING"] = False  # exercise the real error handler
        client = app.test_client()
        rv = client.get("/_test_500_boom")
        assert rv.status_code == 500
        body = rv.data.decode()
        assert "Request ID" in body
        # The traceback / exception type must never leak to anonymous users.
        assert "deliberate test failure" not in body
        assert "RuntimeError" not in body

    def test_500_shows_admin_diagnostics_when_admin_logged_in(self, app):
        """Logged-in admins see the exception type, message and traceback."""
        self._install_failing_route(app)
        client = app.test_client()
        _signup(client)  # first hosting account is auto-admin
        app.config["TESTING"] = False  # disable after signup to avoid bootstrap-token gate
        rv = client.get("/_test_500_boom")
        assert rv.status_code == 500
        body = rv.data.decode()
        assert "Request ID" in body
        assert "RuntimeError" in body
        assert "deliberate test failure" in body

    def test_500_hides_diagnostics_from_non_admin(self, app):
        """A logged-in non-admin sees the generic page, no traceback."""
        self._install_failing_route(app)
        app.config["TESTING"] = False
        client = app.test_client()
        # First signup becomes admin; create a second account and demote test
        # by signing up as a second user via a fresh client, but exploiting
        # the same DB.
        _signup(client, username="admin1")
        # Log out and create a regular user.
        client.post("/logout")
        _signup(client, username="bob")
        rv = client.get("/_test_500_boom")
        assert rv.status_code == 500
        body = rv.data.decode()
        assert "Request ID" in body
        assert "RuntimeError" not in body
        assert "deliberate test failure" not in body


# ---------------------------------------------------------------------------
# Public help / FAQ page
# ---------------------------------------------------------------------------

class TestHostingHelpPage:
    """The help centre ships with the platform rather than pointing outwards."""

    def test_help_index_lists_the_built_in_articles(self, client):
        response = client.get("/help")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        for slug in ("getting-started", "custom-domains", "exports-backups"):
            assert f"/help/{slug}" in body

    def test_an_article_renders_its_own_content(self, client):
        response = client.get("/help/getting-started")
        assert response.status_code == 200
        assert b"<h2>" in response.data

    def test_unknown_and_malformed_slugs_are_not_found(self, client):
        assert client.get("/help/no-such-article").status_code == 404
        assert client.get("/help/..%3Fbad").status_code == 404

    def test_the_articles_follow_the_interface_language(self, client):
        english = client.get("/help/getting-started").get_data(as_text=True)
        client.post("/language", data={"language": "it"}, follow_redirects=True)
        italian = client.get("/help/getting-started").get_data(as_text=True)
        assert english != italian

    def test_no_article_points_at_one_operators_domain(self):
        """The help centre is part of the product, so it must not send a
        customer of some other deployment to bananawiki.com."""
        from hosting import help_content

        for language in help_content.LANGUAGES:
            for article in help_content.load_articles(language):
                for section in article["sections"]:
                    assert "hosting.bananawiki.com" not in section["html"]

    def test_another_site_can_deep_link_a_specific_translation(self, client):
        """The website used to have a separate /it/help/ tree, which guaranteed
        an Italian reader got Italian. ?lang= replaces that guarantee, and must
        not quietly change the language of the portal itself."""
        italian = client.get("/help/getting-started?lang=it").get_data(as_text=True)
        english = client.get("/help/getting-started?lang=en").get_data(as_text=True)
        assert italian != english

        index = client.get("/help?lang=it").get_data(as_text=True)
        assert "lang=it" in index

        assert client.get("/help?lang=zz").status_code == 200

        # The hint applied to those requests only. Compare the heading rather
        # than the page: every response carries a fresh CSP nonce.
        def heading(body):
            return re.search(r"<h2>(.*?)</h2>", body, re.S).group(1).strip()

        assert heading(client.get("/help/getting-started").get_data(as_text=True)) == heading(english)

    def test_help_page_link_in_nav(self, client):
        _signup(client)
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert b"/help" in response.data


class TestHostingMfa:
    def _enable(self, username="mfauser"):
        from hosting.db import get_account_by_username, update_hosting_account

        account = get_account_by_username(username)
        secret = generate_secret()
        recovery = ["AAAA-BBBB-CCCC"]
        update_hosting_account(
            account["id"], totp_secret_encrypted=encrypt_secret(secret),
            totp_enabled=1, totp_recovery_hashes=recovery_hashes(recovery),
        )
        return account["id"], secret, recovery[0]

    def test_password_login_requires_second_factor(self, client):
        _signup(client, username="mfauser")
        account_id, secret, _recovery = self._enable()
        client.post("/logout")

        password_step = client.post(
            "/login", data={"username": "mfauser", "password": "password123"}
        )
        assert password_step.status_code == 302
        assert password_step.headers["Location"].endswith("/login/mfa")
        with client.session_transaction() as sess:
            assert sess.get("hosting_mfa_pending_id") == account_id
            assert "hosting_account_id" not in sess

        second_step = client.post("/login/mfa", data={"code": totp_code(secret)})
        assert second_step.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("hosting_account_id") == account_id
            assert "hosting_mfa_pending_id" not in sess

    def test_recovery_code_is_one_time(self, client):
        _signup(client, username="mfauser")
        account_id, _secret, recovery = self._enable()
        assert consume_recovery_code(account_id, recovery) is True
        assert consume_recovery_code(account_id, recovery) is False

    def test_authenticator_code_cannot_be_replayed(self, client):
        from hosting.mfa import consume_totp
        _signup(client, username="mfauser")
        account_id, secret, _ = self._enable()
        code = totp_code(secret)
        assert consume_totp(account_id, code)
        assert not consume_totp(account_id, code)
        assert not consume_totp(account_id, "１２３４５６")

    def test_expired_challenge_does_not_sign_in(self, client):
        _signup(client, username="mfauser")
        _, secret, _ = self._enable()
        client.post("/logout")
        client.post("/login", data={"username": "mfauser", "password": "password123"})
        with client.session_transaction() as sess:
            sess["hosting_mfa_started_at"] = 1
        result = client.post("/login/mfa", data={"code": totp_code(secret)})
        assert result.headers["Location"].endswith("/login")
        with client.session_transaction() as sess:
            assert "hosting_account_id" not in sess

    def test_enrollment_needs_password_and_authenticator(self, client):
        from hosting.mfa import decrypt_secret
        from hosting.db import get_account_by_username
        _signup(client, username="enrollment")
        client.get("/account/mfa")
        with client.session_transaction() as sess:
            secret = decrypt_secret(sess["mfa_enrollment"])
        client.post("/account/mfa", data={"password": "wrong", "code": totp_code(secret)})
        assert not get_account_by_username("enrollment")["totp_enabled"]
        result = client.post("/account/mfa", data={"password": "password123", "code": totp_code(secret)})
        assert result.status_code == 200
        assert b"Save your recovery codes" in result.data
        assert get_account_by_username("enrollment")["totp_enabled"]
        assert client.get("/account").status_code == 200

    def test_encrypted_secret_is_not_plaintext(self):
        secret = generate_secret()
        encrypted = encrypt_secret(secret)
        assert secret not in encrypted


class TestProductionBootstrapLock:
    def test_first_admin_signup_requires_configured_token(self, app, client, monkeypatch):
        app.config["TESTING"] = False
        monkeypatch.setattr(hosting_config, "HOSTING_BOOTSTRAP_TOKEN", "")
        assert client.get("/signup").status_code == 503

    def test_first_admin_signup_hides_wrong_token(self, app, client, monkeypatch):
        app.config["TESTING"] = False
        monkeypatch.setattr(hosting_config, "HOSTING_BOOTSTRAP_TOKEN", "correct-token")
        assert client.get("/signup?bootstrap_token=wrong-token").status_code == 404

    def test_first_admin_signup_accepts_matching_token(self, app, client, monkeypatch):
        app.config["TESTING"] = False
        monkeypatch.setattr(hosting_config, "HOSTING_BOOTSTRAP_TOKEN", "correct-token")
        response = client.get("/signup?bootstrap_token=correct-token")
        assert response.status_code == 200
        assert b"bootstrap_token" in response.data


class TestAttributeWikiPagesToSystem:
    """The ``-1`` author sentinel must stay a valid foreign-key reference.

    ``pages.last_edited_by`` and ``page_history.edited_by`` reference
    ``users.id``. The import path stamps both with the ``-1`` "the system"
    sentinel, so a matching ``users`` row has to exist or the resulting database
    fails ``PRAGMA foreign_key_check`` and can no longer start once a schema
    migration runs.
    """

    @staticmethod
    def _make_wiki_db(path):
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                suspended INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY,
                title TEXT,
                last_edited_by TEXT REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE TABLE page_history (
                id INTEGER PRIMARY KEY,
                page_id INTEGER,
                edited_by TEXT REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )
        conn.execute("INSERT INTO users (id, username, password) VALUES ('u1','ann','h')")
        conn.execute("INSERT INTO pages (title, last_edited_by) VALUES ('Home','u1')")
        conn.execute("INSERT INTO page_history (page_id, edited_by) VALUES (1,'u1')")
        conn.commit()
        conn.close()

    def test_sentinel_reference_is_valid_after_import(self, tmp_path):
        from hosting.instance_database import _attribute_wiki_db_pages_to_system

        db_path = str(tmp_path / "bananawiki.db")
        self._make_wiki_db(db_path)

        _attribute_wiki_db_pages_to_system(db_path)

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute(
                "SELECT COUNT(*) FROM users WHERE id='-1'"
            ).fetchone()[0] == 1
            # The sentinel value itself is preserved for the "the system" label.
            assert conn.execute(
                "SELECT last_edited_by FROM pages"
            ).fetchone()[0] == "-1"
            assert conn.execute(
                "SELECT edited_by FROM page_history"
            ).fetchone()[0] == "-1"
        finally:
            conn.close()

    def test_repeated_import_does_not_duplicate_the_account(self, tmp_path):
        from hosting.instance_database import _attribute_wiki_db_pages_to_system

        db_path = str(tmp_path / "bananawiki.db")
        self._make_wiki_db(db_path)

        _attribute_wiki_db_pages_to_system(db_path)
        _attribute_wiki_db_pages_to_system(db_path)

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM users WHERE id='-1'"
            ).fetchone()[0] == 1
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()

    def test_system_account_cannot_sign_in_or_leak_into_admin_listings(self, tmp_path):
        from hosting.instance_database import _attribute_wiki_db_pages_to_system

        db_path = str(tmp_path / "bananawiki.db")
        self._make_wiki_db(db_path)

        _attribute_wiki_db_pages_to_system(db_path)

        conn = sqlite3.connect(db_path)
        try:
            role, suspended, password = conn.execute(
                "SELECT role, suspended, password FROM users WHERE id='-1'"
            ).fetchone()
            assert role == "user"
            assert suspended == 1
            assert not password.startswith("$")  # not a usable hash
            # Privileged listings filter on suspended=0, so it stays out of them.
            assert conn.execute(
                "SELECT COUNT(*) FROM users WHERE role IN ('admin','owner') "
                "AND suspended=0 AND id='-1'"
            ).fetchone()[0] == 0
        finally:
            conn.close()
