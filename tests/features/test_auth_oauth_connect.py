"""The first platform sign-in on a wiki: create a new account, or connect one the person already has here.

The scenario these tests follow: someone signed up to the hosting platform as
``alex``. On this wiki they already have an account called ``alexbanana``,
and somebody else holds the name ``alex``.
"""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.wiki.features.auth import platform_oauth

from .test_auth_oauth import OAUTH_ENV, FakePortal, _callback, _start
from .test_auth_two_step import _next_code, enrol, signed_in

OTHER_PASSWORD = "someone else entirely 42"


@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
    fake.profile = {"sub": "acct-alex", "username": "alex"}
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda url: False)
    return fake


@pytest.fixture
def oauth_app(app_factory):
    return app_factory(environ=OAUTH_ENV)


@pytest.fixture
def browser(oauth_app):
    return oauth_app.test_client()


@pytest.fixture
def wiki_db(oauth_app):
    from bananawiki.core.sqlite import Session

    session = Session(oauth_app.extensions["bananawiki.database"].connect())
    session.execute("UPDATE site_settings SET open_signup = 1")
    yield session
    session.conn.close()


@pytest.fixture
def people(make_user, wiki_db):
    """``alex`` belongs to someone else; ``alexbanana`` is the platform user's own account."""
    return {"other": make_user("alex", password=OTHER_PASSWORD), "mine": make_user("alexbanana")}


def _arrive(browser):
    response = _callback(browser, _start(browser))
    assert response.headers["Location"].endswith("/platform-oauth/connect"), response.headers["Location"]
    return browser.get("/platform-oauth/connect").get_data(as_text=True)


def _link_of(wiki_db, account_id="acct-alex"):
    return wiki_db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = ?", (account_id,))


def test_the_name_is_taken_by_someone_else_so_a_free_one_is_suggested(browser, portal, people, wiki_db):
    page = _arrive(browser)
    assert "An account named alex already exists on this wiki" in page
    assert 'id="new-username" name="username" value="alex2"' in page
    refused = browser.post("/platform-oauth/signup", data={"username": "alex"})
    assert refused.status_code == 400
    assert "That username is already taken. alex2 is still free." in refused.get_data(as_text=True)
    assert _link_of(wiki_db) is None and not signed_in(browser)


def test_the_other_persons_account_cannot_be_connected_without_its_password(browser, portal, people, wiki_db):
    _arrive(browser)
    refused = browser.post("/platform-oauth/merge", data={"username": "alex", "password": PASSWORD})
    assert refused.status_code == 401 and "Wrong username or password." in refused.get_data(as_text=True)
    assert _link_of(wiki_db) is None and not signed_in(browser)
    assert portal.links == {}


def test_an_existing_account_with_another_name_is_connected(browser, portal, people, wiki_db):
    _arrive(browser)
    response = browser.post("/platform-oauth/merge", data={"username": "alexbanana", "password": PASSWORD})
    assert response.status_code == 302
    assert _link_of(wiki_db) == people["mine"]["id"]
    assert portal.links == {people["mine"]["id"]: "acct-alex"}, "the portal knows the link too"
    assert signed_in(browser)
    assert wiki_db.scalar("SELECT COUNT(*) FROM users") == 2, "no account was created"
    # From now on the platform signs straight in to alexbanana.
    again = browser.application.test_client()
    assert "/platform-oauth/connect" not in _callback(again, _start(again)).headers["Location"]
    assert signed_in(again)


def test_creating_a_new_account_under_a_free_name(browser, portal, people, wiki_db):
    _arrive(browser)
    assert browser.post("/platform-oauth/signup", data={"username": "alex2"}).status_code == 302
    created = wiki_db.one("SELECT * FROM users WHERE username = 'alex2'")
    assert created["role"] == "user" and _link_of(wiki_db) == created["id"]
    assert signed_in(browser)


def test_the_same_name_is_offered_for_connecting_when_it_is_free_to_link(browser, portal, make_user, wiki_db):
    make_user("alex")  # the platform user's own account, not connected yet
    page = _arrive(browser)
    assert 'id="existing-username" name="username" value="alex"' in page
    assert "The account alex has the same name as your hosting account." in page
    assert browser.post("/platform-oauth/merge", data={"password": PASSWORD}).status_code == 302, \
        "the 1.6 form sent only the password"
    assert wiki_db.scalar("SELECT username FROM users WHERE id = ?", (_link_of(wiki_db),)) == "alex"


def test_an_account_connected_to_another_hosting_account_is_refused(browser, portal, people, wiki_db):
    wiki_db.execute("INSERT INTO platform_oauth_links (account_id, user_id, portal_username, linked_at) "
                    "VALUES ('acct-someone', ?, 'someone', '2026-01-01 00:00:00')", (people["mine"]["id"],))
    _arrive(browser)
    refused = browser.post("/platform-oauth/merge", data={"username": "alexbanana", "password": PASSWORD})
    assert refused.status_code == 409
    assert "already connected to a different hosting account" in refused.get_data(as_text=True)
    assert _link_of(wiki_db) is None and _link_of(wiki_db, "acct-someone") == people["mine"]["id"]
    assert not signed_in(browser)


def test_a_hosting_account_connected_meanwhile_is_not_connected_twice(browser, portal, people, wiki_db):
    _arrive(browser)
    # Another tab finished first and connected the hosting account to "alex2".
    other = wiki_db.insert("users", {"id": "u-tab", "username": "alex_tab", "password": "!", "role": "user"})
    wiki_db.execute("INSERT INTO platform_oauth_links (account_id, user_id, portal_username, linked_at) "
                    "VALUES ('acct-alex', 'u-tab', 'alex', '2026-01-01 00:00:00')")
    assert other is not None
    refused = browser.post("/platform-oauth/merge", data={"username": "alexbanana", "password": PASSWORD})
    assert refused.status_code == 409
    assert browser.post("/platform-oauth/signup", data={"username": "alex3"}).status_code == 409
    assert wiki_db.scalar("SELECT COUNT(*) FROM users WHERE username = 'alex3'") == 0
    assert _link_of(wiki_db) == "u-tab"


def test_without_open_sign_up_an_invite_is_needed_but_connecting_still_works(browser, portal, people, wiki_db):
    wiki_db.execute("UPDATE site_settings SET open_signup = 0")
    page = _arrive(browser)
    assert "only accepts new accounts with an invite code" in page and 'name="invite_code"' in page
    refused = browser.post("/platform-oauth/signup", data={"username": "alex2"})
    assert refused.status_code == 400 and "An invite code is required" in refused.get_data(as_text=True)
    assert browser.post("/platform-oauth/merge", data={"username": "alexbanana", "password": PASSWORD}
                        ).status_code == 302
    assert _link_of(wiki_db) == people["mine"]["id"]


def test_the_portal_refusing_the_link_is_explained(browser, portal, people, wiki_db, monkeypatch):
    _arrive(browser)
    original = portal.request

    def conflict(method, url, **kwargs):
        if url.endswith("/oauth/link"):
            from bananawiki.core import http

            return http.HttpResponse(status=409, body=b'{"ok": false}')
        return original(method, url, **kwargs)

    monkeypatch.setattr(platform_oauth.http, "request", conflict)
    refused = browser.post("/platform-oauth/signup", data={"username": "alex2"})
    assert refused.status_code == 409
    assert "already linked to another account of this wiki" in refused.get_data(as_text=True)
    assert wiki_db.scalar("SELECT COUNT(*) FROM users WHERE username = 'alex2'") == 0, "the new account is undone"


def test_an_expired_flow_starts_over(browser, portal, people):
    assert browser.get("/platform-oauth/connect").headers["Location"].endswith("/login")
    assert browser.post("/platform-oauth/signup", data={"username": "alex2"}).headers["Location"].endswith("/login")
    assert browser.post("/platform-oauth/merge", data={"username": "alexbanana", "password": PASSWORD}
                        ).headers["Location"].endswith("/login")


def test_connecting_an_account_with_two_step_verification_asks_its_code(oauth_app, browser, portal, people,
                                                                       wiki_db, login):
    owner = oauth_app.test_client()
    login(owner, people["mine"])
    secret, _codes = enrol(owner)
    _arrive(browser)
    response = browser.post("/platform-oauth/merge", data={"username": "alexbanana", "password": PASSWORD})
    assert response.headers["Location"].endswith("/platform-oauth/merge")
    assert _link_of(wiki_db) is None and not signed_in(browser)
    assert 'name="code"' in browser.get("/platform-oauth/merge").get_data(as_text=True)
    assert browser.post("/platform-oauth/merge", data={"code": _next_code(secret)}).status_code == 302
    assert _link_of(wiki_db) == people["mine"]["id"] and signed_in(browser)


def test_going_back_from_the_code_step_offers_the_choice_again(oauth_app, browser, portal, people, wiki_db, login):
    owner = oauth_app.test_client()
    login(owner, people["mine"])
    enrol(owner)
    _arrive(browser)
    browser.post("/platform-oauth/merge", data={"username": "alexbanana", "password": PASSWORD})
    page = browser.get("/platform-oauth/connect").get_data(as_text=True)
    assert 'name="password"' in page
    assert browser.get("/platform-oauth/merge").headers["Location"].endswith("/platform-oauth/connect")


@pytest.mark.parametrize(("portal_name", "taken", "expected"), [
    ("alex", [], "alex"),
    ("alex", ["alex", "alex2"], "alex3"),
    ("admin", [], "admin2"),
    ("Lùca!", [], "L_ca"),
    ("a", [], "a_member"),
    ("", [], "member"),
    ("x" * 80, [], "x" * 44),
])
def test_suggested_names_are_valid_and_free(oauth_app, make_user, portal_name, taken, expected):
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope

    for name in taken:
        make_user(name)
    with oauth_app.test_request_context(), connection_scope():
        suggestion = platform_oauth.suggest_username(portal_name)
        assert suggestion == expected
        assert accounts.validate_username(suggestion) and not accounts.username_taken(suggestion)
