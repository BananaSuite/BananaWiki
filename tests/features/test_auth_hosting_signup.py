"""Sign-up from a wiki that takes hosting accounts only: "Create an account" on the sign-in page starts the
portal's sign-up (``prompt=create``), shown only while the portal takes new accounts, and the wiki account
is created at once on the way back, under the wiki's sign-up policy."""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

import pytest

from bananawiki.wiki.features.auth import platform_oauth

from .test_auth_oauth import OAUTH_ENV, PORTAL, FakePortal, _answer, _callback, _start

CREATE_LINK = 'href="/platform-oauth/login?create=1"'


class SignupPortal(FakePortal):
    """The fake portal, with its OAuth metadata (``signup``: open, invite or closed; None: no answer)."""

    def __init__(self):
        super().__init__()
        self.signup: str | None = "open"

    def request(self, method, url, *, headers=None, body=None, **kwargs):
        if urlsplit(url).path == "/.well-known/oauth-authorization-server":
            self.calls.append((method, url, {}))
            if self.signup is None:
                return _answer(503, b"{}")
            return _answer(200, json.dumps({"issuer": PORTAL, "signup": self.signup}).encode())
        return super().request(method, url, headers=headers, body=body, **kwargs)


@pytest.fixture
def portal(monkeypatch):
    fake = SignupPortal()
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda url: False)
    return fake


@pytest.fixture
def wiki(app_factory):
    """A wiki that takes hosting accounts only, open to new members."""
    app = app_factory(environ={**OAUTH_ENV, "BW_PLATFORM_SIGNIN_MODE": "platform"})
    _db(app).execute("UPDATE site_settings SET open_signup = 1")
    return app


def _db(app):
    from bananawiki.core.sqlite import Session

    return Session(app.extensions["bananawiki.database"].connect())


def _create(client, portal, sub="acct-new", username="newbie"):
    """Click "Create an account", sign up at the portal, come back: the callback's response."""
    query = _start(client, "/platform-oauth/login?create=1")
    assert query["prompt"] == ["create"]
    portal.profile = {"sub": sub, "username": username}
    return _callback(client, query)


def test_the_link_is_shown_while_the_portal_takes_new_accounts(wiki, portal):
    client = wiki.test_client()
    page = client.get("/login").get_data(as_text=True)
    assert CREATE_LINK in page and "Create an account" in page
    assert 'href="/signup"' not in page
    portal.signup = "invite"
    wiki.extensions.pop("bananawiki.portal_signup", None)
    assert CREATE_LINK in client.get("/login").get_data(as_text=True)


@pytest.mark.parametrize("answer", ["closed", None])
def test_the_link_is_hidden_when_the_portal_is_closed_or_silent(wiki, portal, answer):
    portal.signup = answer
    assert "create=1" not in wiki.test_client().get("/login").get_data(as_text=True)


def test_the_portal_is_asked_once_in_a_while(wiki, portal):
    client = wiki.test_client()
    for _ in range(3):
        client.get("/login")
    assert sum(call[1].endswith("/.well-known/oauth-authorization-server") for call in portal.calls) == 1


def test_both_mode_keeps_its_own_sign_up_and_adds_none(app_factory, portal):
    app = app_factory(environ={**OAUTH_ENV, "BW_PLATFORM_SIGNIN_MODE": "both"})
    _db(app).execute("UPDATE site_settings SET open_signup = 1")
    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)
    assert 'href="/signup"' in page and "create=1" not in page
    assert "prompt" not in _start(client, "/platform-oauth/login?create=1"), "create=1 is for hosting-only wikis"


def test_a_new_portal_account_gets_its_wiki_account_without_the_choice(wiki, portal):
    client = wiki.test_client()
    response = _create(client, portal)
    assert response.status_code == 302 and "/platform-oauth/connect" not in response.headers["Location"]
    db = _db(wiki)
    user = db.one("SELECT * FROM users WHERE username = 'newbie'")
    assert user is not None and user["password_set"] == 0
    assert db.scalar("SELECT account_id FROM platform_oauth_links WHERE user_id = ?", (user["id"],)) == "acct-new"
    assert portal.links[user["id"]] == "acct-new"
    assert client.get("/_probe/private").data == b"private"


def test_a_taken_name_falls_back_to_the_create_form(wiki, portal, make_user):
    make_user("newbie")
    client = wiki.test_client()
    response = _create(client, portal)
    page = response.get_data(as_text=True)
    assert response.status_code == 400 and 'action="/platform-oauth/signup"' in page
    assert "newbie2 is still free" in page and 'value="newbie"' in page
    assert _db(wiki).scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    created = client.post("/platform-oauth/signup", data={"username": "newbie2"})
    assert created.status_code == 302
    assert _db(wiki).scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie2'") == 1


def test_the_wiki_sign_up_policy_still_applies(wiki, portal):
    db = _db(wiki)
    db.execute("UPDATE site_settings SET open_signup = 0")
    client = wiki.test_client()
    response = _create(client, portal)
    page = response.get_data(as_text=True)
    assert response.status_code == 400 and 'name="invite_code"' in page, "an invite is still needed"
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie'") == 0
    db.execute("UPDATE site_settings SET open_signup = 1, approval_required = 1")
    response = _create(wiki.test_client(), portal, sub="acct-other", username="other")
    assert response.headers["Location"].endswith("/account-status")
    assert db.scalar("SELECT approval_status FROM users WHERE username = 'other'") == "pending"


def test_no_account_is_created_while_acting(wiki, portal):
    client = wiki.test_client()
    query = _start(client, "/platform-oauth/login?create=1")
    portal.profile = {"sub": "acct-new", "username": "newbie",
                      "act": {"sub": "admin-1", "preferred_username": "chief"},
                      "act_expires_at": int(time.time()) + 900}
    response = _callback(client, query)
    assert response.headers["Location"].endswith("/login")
    assert _db(wiki).scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie'") == 0


def test_a_sign_in_without_create_still_asks(wiki, portal):
    client = wiki.test_client()
    query = _start(client)
    assert "prompt" not in query
    portal.profile = {"sub": "acct-new", "username": "newbie"}
    assert _callback(client, query).headers["Location"].endswith("/platform-oauth/connect")
