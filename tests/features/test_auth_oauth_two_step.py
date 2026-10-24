"""Linking a hosting-portal account to a wiki account that uses two-step verification.

The merge at /platform-oauth/merge proves ownership of the wiki account. When
that account uses two-step verification, the password alone must not create
the link: someone who holds the portal account and the wiki password, but not
the authenticator, would otherwise attach their portal account to it.
"""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.wiki.features.auth import platform_oauth

from .test_auth_oauth import OAUTH_ENV, FakePortal, _callback, _start
from .test_auth_two_step import _next_code, enrol, signed_in


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


def _links(oauth_db) -> int:
    return oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links")


def _local_with_two_step(oauth_app, make_user, login):
    local = make_user("portaluser")
    owner_browser = oauth_app.test_client()
    login(owner_browser, local)
    secret, codes = enrol(owner_browser)
    return local, secret, codes


def test_the_password_alone_does_not_link_an_account_with_two_step_verification(oauth_app, oauth_client, oauth_db,
                                                                               portal, make_user, login):
    local, secret, _codes = _local_with_two_step(oauth_app, make_user, login)
    assert _callback(oauth_client, _start(oauth_client)).headers["Location"].endswith("/platform-oauth/connect")
    response = oauth_client.post("/platform-oauth/merge", data={"password": PASSWORD})
    assert response.status_code == 302 and response.headers["Location"].endswith("/platform-oauth/merge")
    assert _links(oauth_db) == 0 and not signed_in(oauth_client)
    page = oauth_client.get("/platform-oauth/merge").get_data(as_text=True)
    assert 'name="code"' in page and 'name="password"' not in page
    assert oauth_client.post("/platform-oauth/merge", data={"code": "000000"}).status_code == 401
    assert _links(oauth_db) == 0 and not signed_in(oauth_client)
    response = oauth_client.post("/platform-oauth/merge", data={"code": _next_code(secret)})
    assert response.status_code == 302 and "/login/two-step" not in response.headers["Location"]
    assert oauth_db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-1'") == local["id"]
    assert signed_in(oauth_client), "both steps passed: no second code is asked for"
    methods = oauth_db.column("SELECT auth_method FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL",
                              (local["id"],))
    assert sorted(methods) == ["password", "platform_oauth+totp"], "the owner's browser, and this sign-in"


def test_a_recovery_code_also_completes_the_link(oauth_app, oauth_client, oauth_db, portal, make_user, login):
    local, _secret, codes = _local_with_two_step(oauth_app, make_user, login)
    _callback(oauth_client, _start(oauth_client))
    oauth_client.post("/platform-oauth/merge", data={"password": PASSWORD})
    assert oauth_client.post("/platform-oauth/merge", data={"code": codes[0]}).status_code == 302
    assert oauth_db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-1'") == local["id"]
    assert signed_in(oauth_client)


def test_a_code_without_the_password_step_links_nothing(oauth_app, oauth_client, oauth_db, portal, make_user, login):
    _local, secret, _codes = _local_with_two_step(oauth_app, make_user, login)
    _callback(oauth_client, _start(oauth_client))
    response = oauth_client.post("/platform-oauth/merge", data={"code": _next_code(secret)})
    assert response.status_code == 401
    assert _links(oauth_db) == 0 and not signed_in(oauth_client)


def test_the_code_step_lapses_when_the_password_changes(oauth_app, oauth_client, oauth_db, portal, make_user, login,
                                                       db):
    local, secret, _codes = _local_with_two_step(oauth_app, make_user, login)
    _callback(oauth_client, _start(oauth_client))
    oauth_client.post("/platform-oauth/merge", data={"password": PASSWORD})
    db.execute("UPDATE users SET password = 'changed-elsewhere' WHERE id = ?", (local["id"],))
    response = oauth_client.post("/platform-oauth/merge", data={"code": _next_code(secret)})
    assert response.status_code == 401, "back at the password step"
    assert _links(oauth_db) == 0 and not signed_in(oauth_client)


def test_wrong_codes_at_the_merge_count_against_the_two_step_limit(oauth_app, oauth_client, oauth_db, portal,
                                                                  make_user, login):
    from bananawiki.wiki.features.auth import signin

    _local, secret, _codes = _local_with_two_step(oauth_app, make_user, login)
    _callback(oauth_client, _start(oauth_client))
    oauth_client.post("/platform-oauth/merge", data={"password": PASSWORD})
    for _ in range(signin.TWO_STEP_MAX):
        assert oauth_client.post("/platform-oauth/merge", data={"code": "000000"}).status_code == 401
    assert oauth_client.post("/platform-oauth/merge", data={"code": _next_code(secret)}).status_code == 429
    assert _links(oauth_db) == 0


def test_the_code_step_is_translated(oauth_app, oauth_client, portal, make_user, login):
    _local_with_two_step(oauth_app, make_user, login)
    _callback(oauth_client, _start(oauth_client))
    oauth_client.post("/platform-oauth/merge", data={"password": PASSWORD})
    english = oauth_client.get("/platform-oauth/merge").get_data(as_text=True)
    italian = oauth_client.get("/platform-oauth/merge", headers={"Accept-Language": "it"}).get_data(as_text=True)
    assert "two-step verification" in english
    assert "verifica in due passaggi" in italian
