# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Platform sign-in after an account merge, end to end: a wiki talking to the real portal."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from bananawiki.core import http
from bananawiki.core.sqlite import Session
from bananawiki.hosting.db import connection_scope
from bananawiki.wiki.features.auth import platform_oauth

PORTAL = "https://portal.wiki.test"


class Network:
    """Delivers the wiki's server-to-server calls to the portal app."""

    def __init__(self, portal):
        self.client = portal.test_client()

    def request(self, method, url, *, headers=None, body=None, **_kwargs):
        parts = urlsplit(url)
        response = self.client.open(parts.path, method=method, query_string=parts.query, headers=headers or {},
                                    data=body)
        return http.HttpResponse(status=response.status_code, body=response.get_data())


@pytest.fixture
def setup(portal, app_factory, make_account, make_wiki, query, monkeypatch):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    owner, admin = make_account(), make_account(admin=True)
    inst = make_wiki(owner, "signin")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth, urls

        client_id, secret = oauth.rotate_credentials(inst["id"])
        wiki_url = urls.instance_url(inst)
    wiki = app_factory(environ={
        "BW_PLATFORM_OAUTH_ENABLED": "1", "BW_PLATFORM_OAUTH_CLIENT_ID": client_id,
        "BW_PLATFORM_OAUTH_CLIENT_SECRET": secret, "BW_PLATFORM_OAUTH_PORTAL_BASE": PORTAL,
        "BW_PLATFORM_OAUTH_AUTHORIZE_URL": f"{PORTAL}/oauth/authorize",
        "BW_PLATFORM_OAUTH_TOKEN_URL": f"{PORTAL}/oauth/token",
        "BW_PLATFORM_OAUTH_USERINFO_URL": f"{PORTAL}/oauth/userinfo",
        "BW_PLATFORM_OAUTH_LINK_URL": f"{PORTAL}/oauth/link", "BW_PLATFORM_OAUTH_UNLINK_URL": f"{PORTAL}/oauth/unlink",
        "BW_PLATFORM_OAUTH_LINK_STATUS_URL": f"{PORTAL}/oauth/link-status", "BW_PLATFORM_INSTANCE_ID": inst["id"],
    })
    monkeypatch.setattr(platform_oauth.http, "request", Network(portal).request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda _url: False)
    context = {"portal": portal, "wiki": wiki, "url": wiki_url, "inst": inst, "admin": admin}
    _wiki_sql(context, "UPDATE site_settings SET open_signup = 1")
    return context


def _wiki_sql(setup, sql: str, params: tuple = ()) -> list[dict]:
    """Run *sql* on the wiki's own database; a SELECT returns its rows."""
    session = Session(setup["wiki"].extensions["bananawiki.database"].connect())
    try:
        if sql.startswith("SELECT"):
            return session.all(sql, params)
        session.execute(sql, params)
        return []
    finally:
        session.conn.close()


def _sign_in(setup, login, account):
    """A browser signs in to the portal as *account*, then to the wiki with it; returns the wiki client."""
    browser, wiki = setup["portal"].test_client(), setup["wiki"].test_client()
    login(browser, account)
    start = wiki.get("/platform-oauth/login", base_url=setup["url"])
    authorize = urlsplit(start.headers["Location"])
    consent = browser.post(f"{authorize.path}?{authorize.query}", data={"action": "allow"})
    callback = urlsplit(consent.headers["Location"])
    assert parse_qs(callback.query).get("code"), consent.headers["Location"]
    wiki.get(f"{callback.path}?{callback.query}", base_url=setup["url"])
    return wiki


def _merge(setup, source, target):
    with setup["portal"].test_request_context("/"), connection_scope():
        from bananawiki.hosting import merges

        merges.admin_merge(source["username"], target["username"], setup["admin"])


def test_the_merge_target_signs_in_as_the_sources_wiki_account(setup, make_account, login, query):
    source, target = make_account(), make_account()
    assert _sign_in(setup, login, source).get("/_probe/private", base_url=setup["url"]).status_code == 200
    (wiki_user,) = _wiki_sql(setup, "SELECT id, username FROM users WHERE username = ?", (source["username"],))
    _merge(setup, source, target)
    wiki = _sign_in(setup, login, target)
    assert wiki.get("/_probe/private", base_url=setup["url"]).status_code == 200
    assert _wiki_sql(setup, "SELECT account_id, user_id FROM platform_oauth_links") == \
        [{"account_id": target["id"], "user_id": wiki_user["id"]}]
    assert not _wiki_sql(setup, "SELECT id FROM users WHERE username = ?", (target["username"],)), \
        "no second wiki account"
    assert query("SELECT account_id, wiki_user_id FROM hosting_oauth_account_links") == \
        [{"account_id": target["id"], "wiki_user_id": wiki_user["id"]}]
    assert _sign_in(setup, login, target).get("/_probe/private", base_url=setup["url"]).status_code == 200


def test_unlinking_on_the_wiki_after_a_merge_clears_the_portal_link(setup, make_account, login, query):
    source, target = make_account(), make_account()
    _sign_in(setup, login, source)
    (wiki_user,) = _wiki_sql(setup, "SELECT * FROM users WHERE username = ?", (source["username"],))
    _merge(setup, source, target)
    from bananawiki.wiki.db import connection_scope as wiki_scope

    with setup["wiki"].test_request_context("/"), wiki_scope():
        platform_oauth.unlink(wiki_user)  # the row here still names the source
    assert query("SELECT * FROM hosting_oauth_account_links") == []
    assert _wiki_sql(setup, "SELECT * FROM platform_oauth_links") == []


def test_a_stale_portal_link_does_not_take_a_wiki_account_over(setup, make_account, login, query):
    owner, stranger = make_account(), make_account()
    _sign_in(setup, login, owner)
    (wiki_user,) = _wiki_sql(setup, "SELECT id FROM users WHERE username = ?", (owner["username"],))
    # A second portal row for the same wiki account, as an unlink that missed it could leave behind.
    query("INSERT INTO hosting_oauth_account_links (instance_id, account_id, wiki_user_id, wiki_username) "
          "VALUES (?, ?, ?, 'stale')", (setup["inst"]["id"], stranger["id"], wiki_user["id"]))
    wiki = _sign_in(setup, login, stranger)
    assert wiki.get("/_probe/private", base_url=setup["url"]).status_code == 302
    assert _wiki_sql(setup, "SELECT account_id, user_id FROM platform_oauth_links") == \
        [{"account_id": owner["id"], "user_id": wiki_user["id"]}]
