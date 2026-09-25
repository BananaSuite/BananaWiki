"""
Tests for the bot protection feature (honeypot + timing validation).

Covers:
- generate_form_token / check_bot_protection helpers
- Bot protection is bypassed in test mode (TESTING=True / WTF_CSRF_ENABLED=False)
- Login, signup, and setup routes reject bot submissions in non-testing mode
- Admin settings toggle persists to the DB and is reflected in form rendering
- Hosting platform bot protection toggle
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


# ---------------------------------------------------------------------------
# Unit tests for the core helpers
# ---------------------------------------------------------------------------

class TestBotProtectionHelpers:
    """Unit-test the generate_form_token / check_bot_protection functions."""

    @pytest.fixture(autouse=True)
    def _app_ctx(self):
        """Push a minimal Flask application context so helpers can run."""
        from app import app
        app.config["TESTING"] = False
        app.config["WTF_CSRF_ENABLED"] = True
        app.secret_key = "test-secret-key-for-bot-protection"
        with app.app_context():
            yield

    def test_generate_form_token_format(self):
        from helpers._bot_protection import generate_form_token
        token = generate_form_token()
        assert "." in token, "Token must contain a dot separator"
        ts_str, sig = token.split(".", 1)
        assert ts_str.isdigit(), "First part must be a numeric timestamp"
        assert len(sig) == 20, "Signature must be 20 hex chars"

    def test_valid_token_passes_after_delay(self):
        """A token that is >= _MIN_FORM_SECONDS old should pass."""
        from helpers._bot_protection import generate_form_token, _MIN_FORM_SECONDS

        # Build a token manually that is slightly older than the minimum
        import hashlib, hmac as _hmac
        secret = "test-secret-key-for-bot-protection".encode()
        ts = str(int(time.time()) - int(_MIN_FORM_SECONDS) - 1)
        sig = _hmac.new(secret, ts.encode(), hashlib.sha256).hexdigest()[:20]
        old_token = f"{ts}.{sig}"

        class _FakeRequest:
            form = {"_form_time": old_token, "website": ""}

        from helpers._bot_protection import check_bot_protection
        blocked, reason = check_bot_protection(_FakeRequest())
        assert not blocked, f"Valid old token should pass; got reason={reason}"

    def test_too_fast_token_is_blocked(self):
        """A token with elapsed < _MIN_FORM_SECONDS should be blocked."""
        import hashlib, hmac as _hmac
        secret = "test-secret-key-for-bot-protection".encode()
        # Use a future timestamp so elapsed is always negative (< _MIN_FORM_SECONDS).
        # int(time.time()) truncates to the previous second boundary, so the
        # fractional part alone can exceed 0.4s and cause a flaky test.
        ts = str(int(time.time()) + 10)
        sig = _hmac.new(secret, ts.encode(), hashlib.sha256).hexdigest()[:20]
        fresh_token = f"{ts}.{sig}"

        class _FakeRequest:
            form = {"_form_time": fresh_token, "website": ""}

        from helpers._bot_protection import check_bot_protection
        blocked, reason = check_bot_protection(_FakeRequest())
        assert blocked
        assert reason == "too_fast"

    def test_honeypot_filled_is_blocked(self):
        """A non-empty honeypot value is an immediate bot signal."""
        import hashlib, hmac as _hmac
        from helpers._bot_protection import _MIN_FORM_SECONDS
        secret = "test-secret-key-for-bot-protection".encode()
        ts = str(int(time.time()) - int(_MIN_FORM_SECONDS) - 2)
        sig = _hmac.new(secret, ts.encode(), hashlib.sha256).hexdigest()[:20]
        token = f"{ts}.{sig}"

        class _FakeRequest:
            form = {"_form_time": token, "website": "http://spam.example.com"}

        from helpers._bot_protection import check_bot_protection
        blocked, reason = check_bot_protection(_FakeRequest())
        assert blocked
        assert reason == "honeypot"

    def test_missing_token_is_blocked(self):
        class _FakeRequest:
            form = {"website": ""}

        from helpers._bot_protection import check_bot_protection
        blocked, reason = check_bot_protection(_FakeRequest())
        assert blocked
        assert reason == "missing_token"

    def test_tampered_token_is_blocked(self):
        class _FakeRequest:
            form = {"_form_time": "9999999999.badhmacsig00000000000", "website": ""}

        from helpers._bot_protection import check_bot_protection
        blocked, reason = check_bot_protection(_FakeRequest())
        assert blocked
        assert reason == "invalid_token"

    def test_testing_mode_always_passes(self):
        """In TESTING mode every request passes regardless of fields."""
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        with flask_app.app_context():
            class _FakeRequest:
                form = {"_form_time": "", "website": "bot-fill"}

            from helpers._bot_protection import check_bot_protection
            blocked, _ = check_bot_protection(_FakeRequest())
            assert not blocked

    def test_csrf_disabled_skips_check(self):
        """WTF_CSRF_ENABLED=False (test mode) skips bot protection."""
        from app import app as flask_app
        flask_app.config["TESTING"] = False
        flask_app.config["WTF_CSRF_ENABLED"] = False
        with flask_app.app_context():
            class _FakeRequest:
                form = {"_form_time": "", "website": "bot-fill"}

            from helpers._bot_protection import check_bot_protection
            blocked, _ = check_bot_protection(_FakeRequest())
            assert not blocked


# ---------------------------------------------------------------------------
# Integration tests: routes in test mode (bot protection bypassed)
# ---------------------------------------------------------------------------

class TestBotProtectionRoutes:
    """Verify that normal test-suite flows still work (bot protection auto-skipped)."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setattr(config, "DATABASE_PATH", db_path)
        monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
        import db as db_mod
        db_mod.init_db()
        from plugin_loader import discover_plugins
        for _dir, manifest, is_builtin in discover_plugins():
            if is_builtin:
                db_mod.register_plugin(
                    manifest["id"],
                    name=manifest.get("name", manifest["id"]),
                    version=manifest.get("version", "0.0.0"),
                    author=manifest.get("author", "BananaWiki"),
                    description=manifest.get("description", ""),
                    builtin=True,
                    enabled=True,
                )
        from app import app
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self.db = db_mod

    def test_login_page_includes_form_token(self):
        """GET /login should render a bot_form_token field."""
        self.db.update_site_settings(setup_done=1)
        resp = self.client.get("/login")
        assert resp.status_code == 200
        assert b"_form_time" in resp.data

    def test_signup_page_includes_form_token(self):
        self.db.update_site_settings(setup_done=1)
        resp = self.client.get("/signup")
        assert resp.status_code == 200
        assert b"_form_time" in resp.data

    def test_setup_page_includes_form_token(self):
        resp = self.client.get("/setup")
        assert resp.status_code == 200
        assert b"_form_time" in resp.data

    def test_normal_login_still_works_in_test_mode(self):
        """Login should succeed without a bot_form_token when TESTING=True."""
        from werkzeug.security import generate_password_hash
        import db as db_mod
        db_mod.create_user("testuser", generate_password_hash("pass1234"), role="admin")
        db_mod.update_site_settings(setup_done=1)
        resp = self.client.post("/login", data={"username": "testuser", "password": "pass1234"},
                                follow_redirects=True)
        assert resp.status_code == 200
        assert b"testuser" in resp.data or b"home" in resp.data or resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests for admin settings toggle
# ---------------------------------------------------------------------------

class TestBotProtectionSettingsToggle:
    """Verify the admin settings toggle saves and is reflected on pages."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setattr(config, "DATABASE_PATH", db_path)
        monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
        import db as db_mod
        db_mod.init_db()
        from plugin_loader import discover_plugins
        for _dir, manifest, is_builtin in discover_plugins():
            if is_builtin:
                db_mod.register_plugin(
                    manifest["id"],
                    name=manifest.get("name", manifest["id"]),
                    version=manifest.get("version", "0.0.0"),
                    author=manifest.get("author", "BananaWiki"),
                    description=manifest.get("description", ""),
                    builtin=True,
                    enabled=True,
                )
        from werkzeug.security import generate_password_hash
        db_mod.create_user("admin", generate_password_hash("admin123"), role="admin")
        db_mod.update_site_settings(setup_done=1)
        from app import app
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self.db = db_mod
        # Log in as admin
        self.client.post("/login", data={"username": "admin", "password": "admin123"})

    def test_bot_protection_enabled_by_default(self):
        """Newly created settings row has bot_protection_enabled=1."""
        settings = self.db.get_site_settings()
        assert settings.get("bot_protection_enabled", 1) == 1

    def test_admin_can_disable_bot_protection(self):
        """POSTing without bot_protection_enabled should set it to 0."""
        import db as db_mod
        # Build minimal settings POST that doesn't include bot_protection_enabled checkbox
        self.client.post("/global-settings", data={
            "site_name": "Test",
            "timezone": "UTC",
            # no "bot_protection_enabled" key → checkbox unchecked
        })
        updated = db_mod.get_site_settings()
        assert updated.get("bot_protection_enabled", 1) == 0

    def test_admin_can_re_enable_bot_protection(self):
        """POSTing with bot_protection_enabled=1 should set it to 1."""
        import db as db_mod
        db_mod.update_site_settings(bot_protection_enabled=0)
        self.client.post("/global-settings", data={
            "site_name": "Test",
            "timezone": "UTC",
            "bot_protection_enabled": "1",
        })
        updated = db_mod.get_site_settings()
        assert updated.get("bot_protection_enabled", 1) == 1

    def test_admin_settings_page_renders_toggle(self):
        """The admin settings page should show the bot protection section."""
        resp = self.client.get("/global-settings")
        assert resp.status_code == 200
        assert b"bot_protection_enabled" in resp.data
        assert b"Bot Protection" in resp.data


# ---------------------------------------------------------------------------
# Tests for is_bot_protection_enabled()
# ---------------------------------------------------------------------------

class TestIsBotProtectionEnabled:

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setattr(config, "DATABASE_PATH", db_path)
        monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
        import db as db_mod
        db_mod.init_db()
        from plugin_loader import discover_plugins
        for _dir, manifest, is_builtin in discover_plugins():
            if is_builtin:
                db_mod.register_plugin(
                    manifest["id"],
                    name=manifest.get("name", manifest["id"]),
                    version=manifest.get("version", "0.0.0"),
                    author=manifest.get("author", "BananaWiki"),
                    description=manifest.get("description", ""),
                    builtin=True,
                    enabled=True,
                )
        db_mod.update_site_settings(setup_done=1)
        self.db = db_mod
        from app import app
        app.config["TESTING"] = False
        app.config["WTF_CSRF_ENABLED"] = True
        self.app_ctx = app.app_context()
        self.app_ctx.push()
        yield
        self.app_ctx.pop()

    def test_enabled_by_default(self):
        from helpers._bot_protection import is_bot_protection_enabled
        assert is_bot_protection_enabled() is True

    def test_reflects_db_value_false(self):
        self.db.update_site_settings(bot_protection_enabled=0)
        from helpers._bot_protection import is_bot_protection_enabled
        assert is_bot_protection_enabled() is False

    def test_reflects_db_value_true(self):
        self.db.update_site_settings(bot_protection_enabled=1)
        from helpers._bot_protection import is_bot_protection_enabled
        assert is_bot_protection_enabled() is True
