"""The wiki side of platform sign-in matches the 1.6 portal (HTTP Basic client authentication)."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from bananawiki.core import http
from bananawiki.wiki.features.auth import platform_oauth

PORTAL = "https://portal.example"
ENVIRON = {
    "BW_PLATFORM_OAUTH_ENABLED": "1", "BW_PLATFORM_OAUTH_CLIENT_ID": "bw_client",
    "BW_PLATFORM_OAUTH_CLIENT_SECRET": "s3cret-value", "BW_PLATFORM_OAUTH_PORTAL_BASE": PORTAL,
    "BW_PLATFORM_OAUTH_AUTHORIZE_URL": f"{PORTAL}/oauth/authorize", "BW_PLATFORM_OAUTH_TOKEN_URL": f"{PORTAL}/oauth/token",
    "BW_PLATFORM_OAUTH_USERINFO_URL": f"{PORTAL}/oauth/userinfo", "BW_PLATFORM_OAUTH_LINK_URL": f"{PORTAL}/oauth/link",
    "BW_PLATFORM_OAUTH_UNLINK_URL": f"{PORTAL}/oauth/unlink",
    "BW_PLATFORM_OAUTH_LINK_STATUS_URL": f"{PORTAL}/oauth/link-status", "BW_PLATFORM_INSTANCE_ID": "inst-1",
}
BASIC = "Basic " + base64.b64encode(b"bw_client:s3cret-value").decode()


class StrictPortal:
    """Answers like the 1.6 portal: server-to-server endpoints need Basic client credentials."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, **_kwargs):
        headers = headers or {}
        self.calls.append((url, headers, body))
        if headers.get("Authorization") != BASIC:
            return http.HttpResponse(status=401, body=b'{"ok": false, "error": "invalid_client"}')
        path = urlsplit(url).path
        if path == "/oauth/link-status":
            assert parse_qs(urlsplit(url).query)["wiki_user_id"] == ["u1"]
            return http.HttpResponse(status=200, body=b'{"linked": true, "hosting_account_id": "acct-9"}')
        if path == "/oauth/token":
            return http.HttpResponse(status=200, body=b'{"access_token": "tok"}')
        return http.HttpResponse(status=200, body=b'{"ok": true}')


@pytest.fixture
def portal(monkeypatch):
    fake = StrictPortal()
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda _url: False)
    return fake


def test_link_status_token_and_link_calls_authenticate_with_basic(app_factory, portal):
    app = app_factory(environ=ENVIRON)
    with app.test_request_context("/"):
        assert platform_oauth._portal_link_status("u1") == {"linked": True, "hosting_account_id": "acct-9"}
        assert platform_oauth.exchange_code("code-1", "state-1", "login") == "tok"
        platform_oauth._portal_post("link_url", {"account_id": "acct-9", "wiki_user_id": "u1"})
    for _url, headers, body in portal.calls:
        assert headers["Authorization"] == BASIC
        assert b"s3cret-value" not in (body or b""), "the secret is never sent in a body"
    link_body = json.loads(portal.calls[-1][2])
    assert link_body == {"account_id": "acct-9", "wiki_user_id": "u1", "instance_id": "inst-1"}
