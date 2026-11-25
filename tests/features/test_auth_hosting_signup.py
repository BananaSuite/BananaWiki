"""Sign-up from a wiki that takes hosting accounts only: ``/platform-oauth/login?create=1`` starts the
portal's sign-up (``prompt=create``) and the wiki account is created at once on the way back, under the
wiki's sign-up policy. No page links to it: with the portal's button on the sign-in page there is no
"Create an account"."""

from __future__ import annotations

import time

import pytest

from bananawiki.wiki.features.auth import platform_oauth

from .test_auth_oauth import OAUTH_ENV, FakePortal, _callback, _start

@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
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


def test_the_sign_in_page_has_no_create_link_next_to_the_portal_button(wiki, portal, app_factory):
    page = wiki.test_client().get("/login").get_data(as_text=True)
    assert "No account yet?" not in page and "create=1" not in page and 'href="/signup"' not in page
    assert "Your account here is created when you first sign in" in page
    assert not portal.calls, "the portal is not asked about sign-up for the page"
    both = app_factory(environ={**OAUTH_ENV, "BW_PLATFORM_SIGNIN_MODE": "both"})
    _db(both).execute("UPDATE site_settings SET open_signup = 1")
    client = both.test_client()
    assert "No account yet?" not in client.get("/login").get_data(as_text=True)
    assert client.get("/signup").status_code == 200, "the wiki's own sign-up page stays for invite links"
    assert "prompt" not in _start(client, "/platform-oauth/login?create=1"), "create=1 is for hosting-only wikis"


def test_a_wiki_without_the_portal_keeps_its_create_link(app_factory):
    app = app_factory()
    _db(app).execute("UPDATE site_settings SET open_signup = 1")
    page = app.test_client().get("/login").get_data(as_text=True)
    assert "No account yet?" in page and 'href="/signup"' in page


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
