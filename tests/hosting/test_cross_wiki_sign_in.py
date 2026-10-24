"""One platform account across the platform's wikis, end to end: a wiki talking to the real portal.

Covers the whole round: consent asked once, the first sign-in's choice
between a new and an existing wiki account, later sign-ins without a click,
and the portal signing the account out of the wiki (sign-out button,
password change).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from bananawiki.core import http
from bananawiki.core.sqlite import Session
from bananawiki.hosting.db import connection_scope
from bananawiki.wiki.features.auth import platform_oauth

from .hosting_support import PASSWORD

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
def platform(portal, app_factory, make_account, make_wiki, query, monkeypatch):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    owner = make_account("wikiowner")
    inst = make_wiki(owner, "team")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth, urls
        from bananawiki.hosting.db import db

        client = oauth.client_for(db.one("SELECT * FROM instances WHERE id = ?", (inst["id"],)))
        wiki_url = urls.instance_url(inst)
    environ = {"BW_PLATFORM_OAUTH_ENABLED": "1", "BW_PLATFORM_OAUTH_CLIENT_ID": client.client_id,
               "BW_PLATFORM_OAUTH_CLIENT_SECRET": client.client_secret, "BW_PLATFORM_OAUTH_PORTAL_BASE": PORTAL,
               "BW_PLATFORM_INSTANCE_ID": inst["id"]}
    environ.update({f"BW_PLATFORM_OAUTH_{name.upper()}_URL": url.replace(client.portal_base, PORTAL)
                    for name, url in client.urls().items()})
    wiki = app_factory(environ=environ)
    monkeypatch.setattr(platform_oauth.http, "request", Network(portal).request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda _url: False)
    context = {"portal": portal, "wiki": wiki, "url": wiki_url, "inst": inst, "owner": owner}
    _wiki_sql(context, "UPDATE site_settings SET open_signup = 1")
    return context


def _wiki_sql(platform, sql: str, params: tuple = ()) -> list[dict]:
    session = Session(platform["wiki"].extensions["bananawiki.database"].connect())
    try:
        if sql.startswith("SELECT"):
            return session.all(sql, params)
        session.execute(sql, params)
        return []
    finally:
        session.conn.close()


def _open_wiki(platform, browser, wiki):
    """"Open wiki" from the portal: returns the wiki's answer to the callback, or the portal's consent page."""
    start = wiki.get("/platform-oauth/login", base_url=platform["url"])
    authorize = urlsplit(start.headers["Location"])
    answer = browser.get(f"{authorize.path}?{authorize.query}")
    if answer.status_code == 200:
        return None, answer
    callback = urlsplit(answer.headers["Location"])
    assert parse_qs(callback.query).get("code"), answer.headers["Location"]
    return wiki.get(f"{callback.path}?{callback.query}", base_url=platform["url"]), answer


def _signed_in(platform, wiki) -> bool:
    return wiki.get("/_probe/private", base_url=platform["url"]).data == b"private"


def _run_signouts(platform) -> int:
    from bananawiki.wiki.db import connection_scope as wiki_scope

    with platform["wiki"].app_context(), wiki_scope():
        return platform_oauth.apply_signouts()


def test_consent_once_choose_once_then_one_click(platform, make_account, login):
    member = make_account("alex")
    browser, wiki = platform["portal"].test_client(), platform["wiki"].test_client()
    login(browser, member)
    _answer, consent = _open_wiki(platform, browser, wiki)
    assert consent.status_code == 200, "the first time the portal asks"
    authorize = urlsplit(wiki.get("/platform-oauth/login", base_url=platform["url"]).headers["Location"])
    allowed = urlsplit(browser.post(f"{authorize.path}?{authorize.query}", data={"action": "allow"})
                       .headers["Location"])
    first = wiki.get(f"{allowed.path}?{allowed.query}", base_url=platform["url"])
    assert first.headers["Location"].endswith("/platform-oauth/connect"), "then the wiki asks which account"
    assert wiki.post("/platform-oauth/signup", data={"username": "alex"}, base_url=platform["url"]).status_code == 302
    assert _signed_in(platform, wiki)

    later = platform["wiki"].test_client()
    answer, _ = _open_wiki(platform, browser, later)
    assert answer is not None and "/platform-oauth/connect" not in answer.headers["Location"]
    assert _signed_in(platform, later), "from then on: no question on either side"

    page = browser.get("/account/wikis").get_data(as_text=True)
    assert "team" in page and ">alex<" in page


def test_the_owner_connects_the_wikis_administrator_account(platform, login):
    """The owner is never asked for consent on their own wiki and connects the administrator account once."""
    _wiki_sql(platform, "INSERT INTO users (id, username, password, role) VALUES ('adm', 'admin_team', ?, 'owner')",
              (_hash(platform),))
    browser, wiki = platform["portal"].test_client(), platform["wiki"].test_client()
    login(browser, platform["owner"])
    answer, _ = _open_wiki(platform, browser, wiki)
    assert answer.headers["Location"].endswith("/platform-oauth/connect")
    connected = wiki.post("/platform-oauth/merge", data={"username": "admin_team", "password": PASSWORD},
                          base_url=platform["url"])
    assert connected.status_code == 302 and _signed_in(platform, wiki)
    assert _wiki_sql(platform, "SELECT user_id FROM platform_oauth_links") == [{"user_id": "adm"}]


def _hash(platform) -> str:
    from bananawiki.wiki import accounts

    with platform["wiki"].app_context():
        return accounts.hash_password(PASSWORD)


def _member_signed_in(platform, make_account, login, username="member"):
    member = make_account(username)
    browser, wiki = platform["portal"].test_client(), platform["wiki"].test_client()
    login(browser, member)
    authorize = urlsplit(wiki.get("/platform-oauth/login", base_url=platform["url"]).headers["Location"])
    allowed = urlsplit(browser.post(f"{authorize.path}?{authorize.query}", data={"action": "allow"})
                       .headers["Location"])
    wiki.get(f"{allowed.path}?{allowed.query}", base_url=platform["url"])
    wiki.post("/platform-oauth/signup", data={"username": username}, base_url=platform["url"])
    assert _signed_in(platform, wiki)
    return member, browser, wiki


def test_signing_out_of_the_wiki_on_the_portal_ends_the_wiki_session(platform, make_account, login):
    member, browser, wiki = _member_signed_in(platform, make_account, login)
    browser.post(f"/account/wikis/{platform['inst']['id']}/sign-out")
    assert _run_signouts(platform) == 1
    assert not _signed_in(platform, wiki)
    again = platform["wiki"].test_client()
    answer, _ = _open_wiki(platform, browser, again)
    assert answer is not None and _signed_in(platform, again), "signing in again works at once"
    assert _run_signouts(platform) == 0, "the old sign-out never reaches the new session"
    assert _signed_in(platform, again)


def test_a_new_portal_password_ends_the_wiki_session(platform, make_account, login):
    member, browser, wiki = _member_signed_in(platform, make_account, login)
    browser.post("/account/change-password", data={"current_password": PASSWORD,
                                                   "new_password": "a brand new passphrase 42",
                                                   "confirm_new_password": "a brand new passphrase 42"})
    _run_signouts(platform)
    assert not _signed_in(platform, wiki)


def test_the_wiki_password_sessions_of_the_same_person_stay(platform, make_account, login):
    """Someone who also signs in with the wiki's own password keeps that session: it is the wiki's business."""
    _wiki_sql(platform, "INSERT INTO users (id, username, password, role) VALUES ('m1', 'member', ?, 'user')",
              (_hash(platform),))
    member, browser, wiki = make_account("member"), platform["portal"].test_client(), platform["wiki"].test_client()
    login(browser, member)
    authorize = urlsplit(wiki.get("/platform-oauth/login", base_url=platform["url"]).headers["Location"])
    allowed = urlsplit(browser.post(f"{authorize.path}?{authorize.query}", data={"action": "allow"})
                       .headers["Location"])
    wiki.get(f"{allowed.path}?{allowed.query}", base_url=platform["url"])
    wiki.post("/platform-oauth/merge", data={"username": "member", "password": PASSWORD}, base_url=platform["url"])
    password_browser = platform["wiki"].test_client()
    signed = password_browser.post("/login", data={"username": "member", "password": PASSWORD},
                                   base_url=platform["url"])
    assert signed.status_code == 302
    browser.post("/oauth/revoke")
    assert _run_signouts(platform) == 1
    assert not _signed_in(platform, wiki)
    assert password_browser.get("/_probe/private", base_url=platform["url"]).data == b"private"
