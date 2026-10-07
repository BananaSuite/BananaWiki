"""Sign-in with the hosting portal, and the SSRF-safe HTTP client it uses."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import PASSWORD

from bananawiki.core import http, web
from bananawiki.wiki.features.auth import platform_oauth

PORTAL = "https://portal.example"
OAUTH_ENV = {
    "BW_PLATFORM_OAUTH_ENABLED": "1",
    "BW_PLATFORM_OAUTH_CLIENT_ID": "bw_client",
    "BW_PLATFORM_OAUTH_CLIENT_SECRET": "s3cret",
    "BW_PLATFORM_OAUTH_PORTAL_BASE": PORTAL,
    "BW_PLATFORM_OAUTH_AUTHORIZE_URL": f"{PORTAL}/oauth/authorize",
    "BW_PLATFORM_OAUTH_TOKEN_URL": f"{PORTAL}/oauth/token",
    "BW_PLATFORM_OAUTH_USERINFO_URL": f"{PORTAL}/oauth/userinfo",
    "BW_PLATFORM_OAUTH_LINK_URL": f"{PORTAL}/oauth/link",
    "BW_PLATFORM_OAUTH_UNLINK_URL": f"{PORTAL}/oauth/unlink",
    "BW_PLATFORM_OAUTH_LINK_STATUS_URL": f"{PORTAL}/oauth/link-status",
    "BW_PLATFORM_INSTANCE_ID": "inst-1",
}


def _answer(status, body):
    return http.HttpResponse(status=status, body=body)


class FakePortal:
    """Stands in for the hosting portal's OAuth endpoints."""

    def __init__(self):
        self.profile = {"sub": "acct-1", "username": "portaluser"}
        self.links: dict[str, str] = {}  # wiki user id -> account id
        self.calls: list[tuple[str, str, dict]] = []
        self.token_down = False

    def request(self, method, url, *, headers=None, body=None, **kwargs):
        kwargs = {"headers": headers or {}}
        if body and (headers or {}).get("Content-Type") == "application/json":
            kwargs["json_body"] = json.loads(body)
        elif body:
            kwargs["form"] = {key: values[0] for key, values in parse_qs(body.decode()).items()}
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path
        if path == "/oauth/token":
            if self.token_down:
                return _answer(500, b"{}")
            return _answer(200, b'{"access_token": "tok-123", "token_type": "Bearer"}')
        if path == "/oauth/userinfo":
            return _answer(200, json.dumps(self.profile).encode())
        if path == "/oauth/link-status":
            user_id = parse_qs(urlsplit(url).query)["wiki_user_id"][0]
            if user_id in self.links:
                return _answer(200, f'{{"linked": true, "hosting_account_id": "{self.links[user_id]}"}}'.encode())
            return _answer(200, b'{"linked": false}')
        if path == "/oauth/link":
            body = kwargs["json_body"]
            self.links[body["wiki_user_id"]] = body["account_id"]
            return _answer(200, b'{"ok": true}')
        if path == "/oauth/unlink":
            body = kwargs["json_body"]
            self.links = {k: v for k, v in self.links.items() if v != body["account_id"]}
            return _answer(200, b'{"ok": true}')
        return _answer(404, b"{}")


@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda url: False)
    return fake


@pytest.fixture
def oauth_app(app_factory):
    return app_factory(environ=OAUTH_ENV)


@pytest.fixture
def oauth_client(oauth_app):
    return oauth_app.test_client()


@pytest.fixture
def oauth_db(oauth_app):
    from bananawiki.core.sqlite import Session

    session = Session(oauth_app.extensions["bananawiki.database"].connect())
    yield session
    session.conn.close()


def _start(client, path="/platform-oauth/login"):
    response = client.get(path)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["Location"]).query)
    return query


def _callback(client, query, path="/platform-oauth/callback"):
    return client.get(f"{path}?code=abc&state={query['state'][0]}")


def test_routes_404_when_not_configured(client):
    assert client.get("/platform-oauth/login").status_code == 404
    assert client.get("/platform-oauth/callback?code=x&state=y").status_code == 404
    assert b"platform-oauth" not in client.get("/login").data


def test_login_page_shows_portal_button(oauth_client):
    assert b"/platform-oauth/login" in oauth_client.get("/login").data


def test_authorize_redirect_uses_state_and_pkce(oauth_client, portal):
    query = _start(oauth_client)
    assert query["client_id"] == ["bw_client"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"][0].endswith("/platform-oauth/callback")
    _callback(oauth_client, query)
    token_call = next(call for call in portal.calls if call[1].endswith("/oauth/token"))
    verifier = token_call[2]["form"]["code_verifier"]
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert digest == query["code_challenge"][0]


def test_callback_rejects_unknown_or_reused_state(oauth_client, portal):
    response = oauth_client.get("/platform-oauth/callback?code=abc&state=forged")
    assert response.headers["Location"].endswith("/login")
    assert not any(call[1].endswith("/oauth/token") for call in portal.calls)


def test_closed_signup_needs_an_invite(oauth_client, oauth_db, portal, make_user):
    admin = make_user("chief", role="admin")
    response = _callback(oauth_client, _start(oauth_client))
    assert response.headers["Location"].endswith("/platform-oauth/signup")
    assert oauth_db.scalar("SELECT COUNT(*) FROM users WHERE username = 'portaluser'") == 0
    response = oauth_client.post("/platform-oauth/signup", data={"username": "portaluser", "invite_code": "NOPE"})
    assert response.status_code == 400
    oauth_db.insert("invite_codes", {"code": "ABCD-1234", "created_by": admin["id"], "max_uses": 1})
    response = oauth_client.post("/platform-oauth/signup", data={"username": "portaluser", "invite_code": "abcd1234"})
    assert response.status_code == 302
    user = oauth_db.one("SELECT * FROM users WHERE username = 'portaluser'")
    assert user["role"] == "user"
    assert oauth_db.scalar("SELECT account_id FROM platform_oauth_links WHERE user_id = ?", (user["id"],)) == "acct-1"
    assert portal.links[user["id"]] == "acct-1"


def test_open_signup_provisions_and_respects_approval(oauth_client, oauth_db, portal):
    oauth_db.execute("UPDATE site_settings SET open_signup = 1, approval_required = 1")
    response = _callback(oauth_client, _start(oauth_client))
    assert response.headers["Location"].endswith("/account-status")
    assert oauth_db.scalar("SELECT approval_status FROM users WHERE username = 'portaluser'") == "pending"
    assert oauth_client.get("/_probe/private").headers["Location"].endswith("/account-status")


def test_invalid_portal_username_must_be_changed(oauth_client, oauth_db, portal):
    oauth_db.execute("UPDATE site_settings SET open_signup = 1")
    portal.profile = {"sub": "acct-9", "username": "admin"}  # reserved here
    response = _callback(oauth_client, _start(oauth_client))
    assert response.headers["Location"].endswith("/platform-oauth/signup")
    response = oauth_client.post("/platform-oauth/signup", data={"username": "admin"})
    assert response.status_code == 400
    response = oauth_client.post("/platform-oauth/signup", data={"username": "portal_admin"})
    assert response.status_code == 302
    assert oauth_db.scalar("SELECT COUNT(*) FROM users WHERE username = 'portal_admin'") == 1


def test_linked_account_signs_in_by_stable_id(oauth_client, oauth_db, portal, make_user):
    user = make_user("renamed_locally")
    oauth_db.execute("INSERT INTO platform_oauth_links (account_id, user_id, linked_at) VALUES ('acct-1', ?, "
                     "'2026-01-01 00:00:00')", (user["id"],))
    response = _callback(oauth_client, _start(oauth_client))
    assert response.status_code == 302
    assert oauth_client.get("/_probe/private").data == b"private"
    session_row = oauth_db.one("SELECT auth_method FROM user_sessions WHERE user_id = ?", (user["id"],))
    assert session_row["auth_method"] == "platform_oauth"


def test_access_token_is_never_stored(oauth_client, oauth_db, portal, make_user):
    user = make_user("tokencheck")
    oauth_db.execute("INSERT INTO platform_oauth_links (account_id, user_id, linked_at) VALUES ('acct-1', ?, "
                     "'2026-01-01 00:00:00')", (user["id"],))
    _callback(oauth_client, _start(oauth_client))
    with oauth_client.session_transaction() as cookie:
        assert "tok-123" not in repr(dict(cookie))


def test_merge_requires_local_password(oauth_client, oauth_db, portal, make_user):
    local = make_user("portaluser")
    response = _callback(oauth_client, _start(oauth_client))
    assert response.headers["Location"].endswith("/platform-oauth/merge")
    response = oauth_client.post("/platform-oauth/merge", data={"password": "wrong password"})
    assert response.status_code == 401
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    response = oauth_client.post("/platform-oauth/merge", data={"password": PASSWORD})
    assert response.status_code == 302
    assert oauth_db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-1'") == local["id"]


def test_merge_without_pending_flow_is_refused(oauth_client, portal):
    assert oauth_client.post("/platform-oauth/merge", data={"password": PASSWORD}).headers["Location"].endswith(
        "/login")


def test_1x_portal_link_is_adopted(oauth_client, oauth_db, portal, make_user):
    local = make_user("portaluser")
    portal.links[local["id"]] = "acct-1"
    response = _callback(oauth_client, _start(oauth_client))
    assert response.status_code == 302 and "/merge" not in response.headers["Location"]
    assert oauth_db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-1'") == local["id"]


def test_username_linked_to_other_portal_account_is_refused(oauth_client, oauth_db, portal, make_user):
    local = make_user("portaluser")
    portal.links[local["id"]] = "acct-other"
    response = _callback(oauth_client, _start(oauth_client))
    assert response.headers["Location"].endswith("/login")
    assert oauth_db.scalar("SELECT COUNT(*) FROM user_sessions") == 0


def test_portal_failure_is_reported_without_details(oauth_client, portal):
    portal.token_down = True
    response = _callback(oauth_client, _start(oauth_client))
    assert response.headers["Location"].endswith("/login")
    page = oauth_client.get("/login")
    assert b"500" not in page.data


def test_link_and_unlink_from_settings(oauth_app, oauth_client, oauth_db, portal, make_user, login):
    user = make_user("bob")
    login(oauth_client, user)
    assert oauth_client.get("/settings/link-platform-account").status_code == 200
    response = oauth_client.post("/settings/link-platform-account", data={"password": "wrong password"})
    assert response.status_code == 401
    response = oauth_client.post("/settings/link-platform-account", data={"password": PASSWORD})
    query = parse_qs(urlsplit(response.headers["Location"]).query)
    assert query["redirect_uri"][0].endswith("/platform-oauth/link-callback")
    _callback(oauth_client, query, "/platform-oauth/link-callback")
    assert oauth_db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-1'") == user["id"]
    oauth_client.post("/settings/unlink-platform-account", data={"password": "wrong password"})
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 1
    oauth_client.post("/settings/unlink-platform-account", data={"password": PASSWORD})
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    assert user["id"] not in portal.links


def _form_action(response):
    """The sources of the response's CSP ``form-action`` directive."""
    for directive in response.headers["Content-Security-Policy"].split(";"):
        name, _, sources = directive.strip().partition(" ")
        if name == "form-action":
            return sources
    return None


def test_link_page_lets_its_form_redirect_to_the_portal_only(oauth_client, portal, make_user, login):
    # Chromium checks the redirect that answers the link form against this page's form-action.
    login(oauth_client, make_user("bob"))
    page = oauth_client.get("/settings/link-platform-account")
    assert page.status_code == 200 and _form_action(page) == f"'self' {PORTAL}"
    refused = oauth_client.post("/settings/link-platform-account", data={"password": "wrong password"})
    assert refused.status_code == 401 and _form_action(refused) == f"'self' {PORTAL}"
    assert _form_action(oauth_client.get("/settings/profile")) == "'self'"
    assert _form_action(oauth_client.get("/login")) == "'self'"
    response = oauth_client.post("/settings/link-platform-account", data={"password": PASSWORD})
    _callback(oauth_client, parse_qs(urlsplit(response.headers["Location"]).query), "/platform-oauth/link-callback")
    linked = oauth_client.get("/settings/link-platform-account")
    assert b"/settings/unlink-platform-account" in linked.data and _form_action(linked) == "'self'"


def test_link_page_takes_only_the_origin_of_the_authorize_url(app_factory, make_user, login):
    app = app_factory(environ={**OAUTH_ENV, "BW_PLATFORM_OAUTH_AUTHORIZE_URL": "http://Portal.Local:8080/o/authorize?x=1"})
    client = app.test_client()
    login(client, make_user("bob"))
    assert _form_action(client.get("/settings/link-platform-account")) == "'self' http://portal.local:8080"


@pytest.mark.parametrize(("url", "origin"), [
    ("https://Portal.Example/oauth/authorize?next=/x", "https://portal.example"),
    ("http://127.0.0.1:8080/oauth/authorize", "http://127.0.0.1:8080"),
    ("", None), ("/oauth/authorize", None), ("javascript:alert(1)", None), ("ftp://portal.example/x", None),
    ("https://user:pw@portal.example/", None), ("https://portal.example:99999/", None), ("https://[::1]/", None),
    ("https://portal.example;script-src *", None), ("https://a b.example/", None),
])
def test_only_a_plain_origin_reaches_the_policy(url, origin):
    assert web.csp_origin(url) == origin


def test_link_callback_bound_to_the_user_who_started_it(oauth_app, oauth_db, portal, make_user, login):
    alice, mallory = make_user("alice"), make_user("mallory")
    client = oauth_app.test_client()
    login(client, alice)
    response = client.post("/settings/link-platform-account", data={"password": PASSWORD})
    query = parse_qs(urlsplit(response.headers["Location"]).query)
    client.post("/logout")
    login(client, mallory)
    _callback(client, query, "/platform-oauth/link-callback")
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0


def test_link_settings_require_sign_in(oauth_client):
    assert "/login" in oauth_client.get("/settings/link-platform-account").headers["Location"]


def test_server_calls_stay_on_the_portal_origin(oauth_app, portal):
    with oauth_app.test_request_context(), pytest.raises(platform_oauth.OAuthError):
        platform_oauth._call("GET", "https://evil.example/oauth/userinfo")


def test_private_portal_detection():
    assert platform_oauth.portal_is_private("http://127.0.0.1:5000")
    assert not platform_oauth.portal_is_private("https://[2001:4860:4860::8888]")
    assert not platform_oauth.portal_is_private("ftp://127.0.0.1")
