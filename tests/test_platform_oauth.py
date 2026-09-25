"""Tests for the platform OAuth SSO feature (provider + consumer)."""

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _signup(client, username="oauthadmin", password="Pass1234"):
    return client.post("/signup", data={
        "username": username,
        "password": password,
        "confirm_password": password,
    }, follow_redirects=True)


class TestHostingOAuthProvider:
    """Tests for the OAuth provider routes on the hosting portal."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        from hosting import config as hosting_config
        hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
        hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
        # Tests spawn Gunicorn subprocesses directly; pin to process runtime.
        hosting_config.HOSTING_INSTANCE_RUNTIME = "process"
        os.makedirs(hosting_config.INSTANCES_DIR, exist_ok=True)
        from hosting.db import init_hosting_db
        init_hosting_db()
        # Patch _start_process to avoid actually forking Gunicorn in tests.
        monkeypatch.setattr(
            "hosting.instance_manager._start_process",
            lambda *a, **kw: True,
        )

    @pytest.fixture
    def app(self):
        from hosting.app import create_hosting_app
        application = create_hosting_app()
        application.config["TESTING"] = True
        application.config["WTF_CSRF_ENABLED"] = False
        return application

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    def _enable_oauth(self):
        from hosting.db import update_hosting_settings
        update_hosting_settings(platform_oauth_enabled=1)

    def _signup_and_login(self, client, username="oauthadmin", password="Pass1234"):
        _signup(client, username, password)
        return client

    def _create_instance_via_client(self, client, subdomain="oauthwiki"):
        from hosting.db import get_instance_by_subdomain
        client.post("/instances/create", data={"subdomain": subdomain})
        return get_instance_by_subdomain(subdomain)

    def _set_oauth_creds(self, instance_id, client_id="test_client_id", secret="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"):
        from hosting.routes.oauth import _hash_secret
        from hosting.db._connection import get_hosting_db_context
        secret_hash = _hash_secret(secret)
        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE instances SET oauth_client_id=?, oauth_client_secret_hash=? WHERE id=?",
                (client_id, secret_hash, instance_id),
            )
            conn.commit()

    def _get_account_id(self, client):
        with client.session_transaction() as sess:
            return sess.get("hosting_account_id")

    def _setup_oauth_env(self, client, subdomain="oauthwiki", username="oauthadmin"):
        from hosting.db import update_hosting_settings
        # Disable email-required policy so POST routes don't redirect.
        update_hosting_settings(email_required=0, ask_email_existing_users=0)
        self._enable_oauth()
        self._signup_and_login(client, username=username)
        inst = self._create_instance_via_client(client, subdomain=subdomain)
        self._set_oauth_creds(inst["id"])
        return inst, self._get_account_id(client)

    def _redirect_uri(self, inst):
        """Return a redirect target on the instance's own origin.

        The portal only issues codes to the wiki that owns the client, so a
        test redirect must live on that instance's URL.
        """
        from hosting.instance_paths import _instance_url
        return _instance_url(inst).rstrip("/") + "/callback"

    def _do_auth_code_flow(self, client, inst, client_id="test_client_id"):
        rv = client.post(
            f"/oauth/authorize?client_id={client_id}&response_type=code"
            f"&redirect_uri={self._redirect_uri(inst)}&state=x",
            data={"action": "allow"}, follow_redirects=False,
        )
        return rv.headers["Location"].split("code=")[1].split("&")[0]

    # --- 404 when disabled ---

    def test_endpoints_return_404_when_oauth_disabled(self, client):
        assert client.get("/oauth/authorize?client_id=x&response_type=code").status_code == 404
        assert client.post("/oauth/token").status_code == 404
        assert client.get("/oauth/userinfo").status_code == 404

    # --- Authorize ---

    def test_authorize_requires_login(self, client):
        inst, _ = self._setup_oauth_env(client)
        client.post("/logout")
        rv = client.get(
            "/oauth/authorize?client_id=test_client_id&response_type=code"
            f"&redirect_uri={self._redirect_uri(inst)}&state=xyz"
        )
        assert rv.status_code == 302
        assert "/login" in rv.headers["Location"]

    def test_authorize_shows_consent_page(self, client):
        inst, _ = self._setup_oauth_env(client)
        rv = client.get(
            "/oauth/authorize?client_id=test_client_id&response_type=code"
            f"&redirect_uri={self._redirect_uri(inst)}&state=xyz"
        )
        assert rv.status_code == 200
        assert b"Authorise" in rv.data or b"authorise" in rv.data

    def test_authorize_rejects_invalid_client_id(self, client):
        self._enable_oauth()
        self._signup_and_login(client)
        rv = client.get(
            "/oauth/authorize?client_id=nonexistent&response_type=code"
            "&redirect_uri=http://wiki/callback&state=xyz"
        )
        assert rv.status_code == 400

    def test_authorize_rejects_foreign_redirect_uri(self, client):
        """A code must never be sent to an origin the client does not own."""
        inst, _ = self._setup_oauth_env(client)
        rv = client.get(
            "/oauth/authorize?client_id=test_client_id&response_type=code"
            "&redirect_uri=https://attacker.example/callback&state=xyz"
        )
        assert rv.status_code == 400

    def test_token_rejects_mismatched_redirect_uri(self, client):
        """The exchange must present the redirect_uri the code was issued for."""
        inst, _ = self._setup_oauth_env(client)
        code = self._do_auth_code_flow(client, inst)
        rv = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": "test_client_id",
            "client_secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "redirect_uri": self._redirect_uri(inst) + "/other",
        })
        assert rv.status_code == 400
        assert "invalid_grant" in rv.get_json().get("error", "")

    def test_authorize_issues_code(self, client):
        inst, _ = self._setup_oauth_env(client)
        rv = client.post(
            "/oauth/authorize?client_id=test_client_id&response_type=code"
            f"&redirect_uri={self._redirect_uri(inst)}&state=xyz",
            data={"action": "allow"},
        )
        assert rv.status_code == 302
        location = rv.headers["Location"]
        assert "code=" in location
        assert "state=xyz" in location
        assert location.startswith(self._redirect_uri(inst))

    def test_authorize_deny_returns_to_dashboard(self, client):
        inst, _ = self._setup_oauth_env(client)
        rv = client.post(
            "/oauth/authorize?client_id=test_client_id&response_type=code"
            f"&redirect_uri={self._redirect_uri(inst)}&state=xyz",
            data={"action": "deny"},
        )
        assert rv.status_code == 302
        assert "/dashboard" in rv.headers["Location"]

    # --- Token ---

    def test_token_exchanges_code(self, client):
        inst, _ = self._setup_oauth_env(client)
        code = self._do_auth_code_flow(client, inst)
        rv = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "test_client_id",
            "client_secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "redirect_uri": self._redirect_uri(inst),
        })
        assert rv.status_code == 200
        data = rv.get_json()
        assert "access_token" in data
        assert data["token_type"] == "Bearer"

    def test_token_rejects_used_code(self, client):
        inst, _ = self._setup_oauth_env(client)
        code = self._do_auth_code_flow(client, inst)
        client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": "test_client_id",
            "client_secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "redirect_uri": self._redirect_uri(inst),
        })
        rv = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": "test_client_id",
            "client_secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "redirect_uri": self._redirect_uri(inst),
        })
        assert rv.status_code == 400
        assert "invalid_grant" in rv.get_json().get("error", "")

    def test_token_rejects_bad_secret(self, client):
        inst, _ = self._setup_oauth_env(client)
        code = self._do_auth_code_flow(client, inst)
        rv = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": "test_client_id",
            "client_secret": "wrong",
            "redirect_uri": self._redirect_uri(inst),
        })
        assert rv.status_code == 401

    def test_token_rejects_missing_params(self, client):
        self._enable_oauth()
        rv = client.post("/oauth/token", data={"grant_type": "authorization_code"})
        assert rv.status_code == 400

    # --- UserInfo ---

    def test_userinfo_returns_account_data(self, client):
        inst, _ = self._setup_oauth_env(client)
        code = self._do_auth_code_flow(client, inst)
        rv = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": "test_client_id",
            "client_secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "redirect_uri": self._redirect_uri(inst),
        })
        access_token = rv.get_json()["access_token"]
        rv = client.get("/oauth/userinfo",
                        headers={"Authorization": f"Bearer {access_token}"})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["username"] == "oauthadmin"

    def test_userinfo_rejects_invalid_token(self, client):
        self._enable_oauth()
        rv = client.get("/oauth/userinfo",
                        headers={"Authorization": "Bearer bogus"})
        assert rv.status_code == 401

    def test_userinfo_rejects_expired_token(self, client):
        inst, _ = self._setup_oauth_env(client)
        code = self._do_auth_code_flow(client, inst)
        rv = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": "test_client_id",
            "client_secret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "redirect_uri": self._redirect_uri(inst),
        })
        import time as _time
        import base64, hmac, hashlib, json as _json
        from hosting import config as hconfig
        sk = hconfig.HOSTING_SECRET_KEY.encode("utf-8")
        payload = {
            "sub": "1", "client_id": "test_client_id",
            "iat": int(_time.time()) - 7200,
            "exp": int(_time.time()) - 3600,
            "scope": "openid profile",
        }
        b64 = base64.urlsafe_b64encode(
            _json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("utf-8").rstrip("=")
        sig = hmac.new(sk, b64.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        rv = client.get("/oauth/userinfo",
                        headers={"Authorization": f"Bearer {b64}.{sig}"})
        assert rv.status_code == 401

    # --- Link status ---

    def test_link_status_returns_linked(self, client):
        inst, acct_id = self._setup_oauth_env(client)
        from hosting.db._connection import get_hosting_db_context
        with get_hosting_db_context() as conn:
            conn.execute(
                "INSERT INTO hosting_oauth_account_links "
                "(instance_id, account_id, wiki_user_id, wiki_username) "
                "VALUES (?, ?, ?, ?)",
                (inst["id"], acct_id, "wu42", "wikiuser"),
            )
            conn.commit()
        rv = client.get(
            f"/oauth/link-status?instance_id={inst['id']}&wiki_user_id=wu42"
        )
        assert rv.status_code == 200
        assert rv.get_json()["linked"] is True
        assert rv.get_json()["hosting_username"] == "wikiuser"

    def test_link_status_returns_not_linked(self, client):
        inst, _ = self._setup_oauth_env(client)
        rv = client.get(
            f"/oauth/link-status?instance_id={inst['id']}&wiki_user_id=nonexist"
        )
        assert rv.status_code == 200
        assert rv.get_json()["linked"] is False

    def test_link_status_missing_params(self, client):
        self._enable_oauth()
        assert client.get("/oauth/link-status").status_code == 400

    # --- Create / unlink ---

    _TEST_CLIENT_SECRET = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def test_create_link(self, client):
        inst, acct_id = self._setup_oauth_env(client)
        rv = client.post("/oauth/link", json={
            "instance_id": inst["id"],
            "account_id": acct_id,
            "wiki_user_id": "wu99",
            "wiki_username": "u99",
            "client_secret": self._TEST_CLIENT_SECRET,
        })
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True

    def test_create_link_rejects_duplicate(self, client):
        inst, acct_id = self._setup_oauth_env(client)
        client.post("/oauth/link", json={
            "instance_id": inst["id"], "account_id": acct_id,
            "wiki_user_id": "u1", "wiki_username": "u1",
            "client_secret": self._TEST_CLIENT_SECRET,
        })
        rv = client.post("/oauth/link", json={
            "instance_id": inst["id"], "account_id": acct_id,
            "wiki_user_id": "u2", "wiki_username": "u2",
            "client_secret": self._TEST_CLIENT_SECRET,
        })
        assert rv.status_code == 409

    def test_unlink_removes_link(self, client):
        inst, acct_id = self._setup_oauth_env(client)
        client.post("/oauth/link", json={
            "instance_id": inst["id"], "account_id": acct_id,
            "wiki_user_id": "u1", "wiki_username": "u1",
            "client_secret": self._TEST_CLIENT_SECRET,
        })
        rv = client.post("/oauth/unlink", json={
            "instance_id": inst["id"], "account_id": acct_id,
            "client_secret": self._TEST_CLIENT_SECRET,
        })
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True
        rv = client.get(
            f"/oauth/link-status?instance_id={inst['id']}&wiki_user_id=u1"
        )
        assert rv.get_json()["linked"] is False

    def test_create_link_rejects_bad_secret(self, client):
        inst, acct_id = self._setup_oauth_env(client)
        rv = client.post("/oauth/link", json={
            "instance_id": inst["id"],
            "account_id": acct_id,
            "wiki_user_id": "wu99",
            "wiki_username": "u99",
            "client_secret": "this-is-the-wrong-secret",
        })
        assert rv.status_code == 403
        assert rv.get_json()["error"] == "invalid_client_secret"


class TestWikiOAuthConsumer:
    """Tests for the OAuth consumer routes on the wiki side."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("BW_PLATFORM_OAUTH_ENABLED", "1")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_CLIENT_ID", "wiki_client_1")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_CLIENT_SECRET", "wikisecret123")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_PORTAL_BASE", "http://portal.test")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_AUTHORIZE_URL",
                           "http://portal.test/oauth/authorize")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_TOKEN_URL",
                           "http://portal.test/oauth/token")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_USERINFO_URL",
                           "http://portal.test/oauth/userinfo")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_LINK_URL",
                           "http://portal.test/oauth/link")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_UNLINK_URL",
                           "http://portal.test/oauth/unlink")
        monkeypatch.setenv("BW_PLATFORM_OAUTH_LINK_STATUS_URL",
                           "http://portal.test/oauth/link-status")
        monkeypatch.setenv("BW_PLATFORM_INSTANCE_ID", "inst-1")
        from werkzeug.security import generate_password_hash
        import db
        if not db.get_user_by_username("admin"):
            db.create_user("admin", generate_password_hash("admin123"), role="admin")
            db.update_site_settings(setup_done=1)

    @pytest.fixture
    def client(self):
        from app import app
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            yield c

    def test_login_redirects_to_authorize(self, client):
        rv = client.get("/platform-oauth/login")
        assert rv.status_code == 302
        loc = rv.headers["Location"]
        assert "client_id=wiki_client_1" in loc
        assert "response_type=code" in loc
        assert "http://portal.test/oauth/authorize" in loc

    def test_login_redirects_authenticated_user_home(self, client):
        client.post("/login", data={
            "username": "admin", "password": "admin123",
        })
        rv = client.get("/platform-oauth/login")
        assert rv.status_code == 302
        assert rv.headers["Location"] == "/"

    def test_callback_missing_code_shows_error(self, client):
        rv = client.get("/platform-oauth/callback?state=bad")
        assert rv.status_code == 302
        assert "/login" in rv.headers["Location"]

    @patch("routes.platform_oauth.open_http")
    def test_callback_full_flow_new_user(self, mock_urlopen, client):
        class FakeResp:
            def __init__(self, data):
                self.data = data
            def read(self):
                return self.data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        tkn = json.dumps({"access_token": "at1", "token_type": "Bearer",
                          "expires_in": 3600}).encode("utf-8")
        uinfo = json.dumps({"sub": "ha1", "username": "newuser"}).encode("utf-8")
        link = json.dumps({"ok": True}).encode("utf-8")
        client.get("/platform-oauth/login")
        with client.session_transaction() as s:
            st = next(iter(s.get("platform_oauth_states", {})), "ts")
        mock_urlopen.side_effect = [FakeResp(tkn), FakeResp(uinfo), FakeResp(link)]
        rv = client.get(f"/platform-oauth/callback?code=c1&state={st}")
        assert rv.status_code == 302
        assert rv.headers["Location"] == "/"

    def test_callback_expired_state(self, client):
        rv = client.get("/platform-oauth/callback?code=c&state=bogus")
        assert rv.status_code == 302
        assert "/login" in rv.headers["Location"]

    def test_merge_requires_session(self, client):
        rv = client.post("/platform-oauth/merge", data={"password": "x"})
        assert rv.status_code == 302
        assert "/login" in rv.headers["Location"]

    def test_unlink_requires_password(self, client):
        client.post("/login", data={
            "username": "admin", "password": "admin123",
        })
        rv = client.post("/settings/unlink-platform-account",
                         data={"password": "wrong"})
        assert rv.status_code == 302

    def test_context_processor_shows_oauth_button(self, client):
        rv = client.get("/login")
        assert rv.status_code == 200
        assert b"Log in with Platform Account" in rv.data

    def test_link_page_requires_login(self, client):
        rv = client.get("/settings/link-platform-account")
        assert rv.status_code == 302
        assert "/login" in rv.headers["Location"]


class TestWikiOAuthConsumerDisabled:
    """Tests when platform OAuth is not enabled."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.delenv("BW_PLATFORM_OAUTH_ENABLED", raising=False)
        from werkzeug.security import generate_password_hash
        import db
        if not db.get_user_by_username("admin"):
            db.create_user("admin", generate_password_hash("admin123"), role="admin")
            db.update_site_settings(setup_done=1)

    @pytest.fixture
    def client(self):
        from app import app
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            yield c

    def test_login_page_hides_oauth_button(self, client):
        rv = client.get("/login")
        assert rv.status_code == 200
        assert b"Log in with Platform Account" not in rv.data
