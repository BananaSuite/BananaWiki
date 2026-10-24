"""Single sign-out: the wiki ends the platform sessions the hosting portal asks it to end."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from bananawiki.core import http
from bananawiki.core.timeutil import now_sql, sql_in
from bananawiki.wiki.features.auth import platform_oauth

from .test_auth_oauth import OAUTH_ENV, FakePortal


class SignoutPortal(FakePortal):
    """A portal that also answers ``/oauth/signouts`` from :attr:`events`."""

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []
        self.status = 200
        self.page = 200
        self.asked: list[dict] = []

    def request(self, method, url, *, headers=None, body=None, **kwargs):
        parts = urlsplit(url)
        if parts.path != "/oauth/signouts":
            return super().request(method, url, headers=headers, body=body, **kwargs)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        self.asked.append({"query": query, "authorization": (headers or {}).get("Authorization", "")})
        if self.status != 200:
            return http.HttpResponse(status=self.status, body=b'{"error": "nope"}')
        after = int(query["after"])
        newer = [event for event in self.events if event["id"] > after]
        latest = max((event["id"] for event in self.events), default=0)
        payload = {"events": newer[: self.page], "latest": latest, "more": len(newer) > self.page}
        return http.HttpResponse(status=200, body=json.dumps(payload).encode())


@pytest.fixture
def portal(monkeypatch):
    fake = SignoutPortal()
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda url: False)
    return fake


@pytest.fixture
def oauth_app(app_factory):
    return app_factory(environ=OAUTH_ENV)


@pytest.fixture
def wiki_db(oauth_app):
    from bananawiki.core.sqlite import Session

    session = Session(oauth_app.extensions["bananawiki.database"].connect())
    yield session
    session.conn.close()


def _session(wiki_db, user, *, method="platform_oauth", created=None):
    created = created or sql_in(minutes=-10)
    row_id = f"s{wiki_db.scalar('SELECT COUNT(*) FROM user_sessions') + 1}"
    wiki_db.execute(
        "INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at, auth_method) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (row_id, row_id + "-hash", user["id"], created, created, sql_in(days=1), method))
    return row_id


def _revoked(wiki_db, session_id):
    return wiki_db.one("SELECT revoked_at, revoked_reason FROM user_sessions WHERE id = ?", (session_id,))


def _link(wiki_db, user, account_id="acct-1"):
    wiki_db.execute("INSERT INTO platform_oauth_links (account_id, user_id, portal_username, linked_at) "
                    "VALUES (?, ?, 'portaluser', ?)", (account_id, user["id"], now_sql()))


def _run(app):
    from bananawiki.wiki.db import connection_scope

    with app.app_context(), connection_scope():
        return platform_oauth.apply_signouts()


def test_only_platform_sessions_that_started_before_the_sign_out_end(oauth_app, wiki_db, portal, make_user):
    user = make_user("member")
    _link(wiki_db, user)
    old_platform = _session(wiki_db, user, created=sql_in(minutes=-30))
    old_with_code = _session(wiki_db, user, method="platform_oauth+totp", created=sql_in(minutes=-30))
    password = _session(wiki_db, user, method="password", created=sql_in(minutes=-30))
    portal.events = [{"id": 7, "account_id": "acct-1", "wiki_user_id": user["id"], "reason": "password_changed",
                      "at": str(sql_in(minutes=-5))}]
    later = _session(wiki_db, user, created=sql_in(minutes=-1))
    assert _run(oauth_app) == 2
    assert _revoked(wiki_db, old_platform)["revoked_reason"] == "platform_signout"
    assert _revoked(wiki_db, old_with_code)["revoked_at"]
    assert _revoked(wiki_db, password)["revoked_at"] is None, "the wiki's own password sessions stay"
    assert _revoked(wiki_db, later)["revoked_at"] is None, "a sign-in after the sign-out stays"
    assert wiki_db.scalar("SELECT signout_cursor FROM platform_oauth_state") == 7
    assert portal.asked[0]["query"] == {"after": "0", "instance_id": "inst-1"}
    assert portal.asked[0]["authorization"].startswith("Basic ")
    assert _run(oauth_app) == 0 and portal.asked[-1]["query"]["after"] == "7"


def test_the_signed_out_session_cannot_be_used_any_more(oauth_app, wiki_db, portal, make_user, login):
    user = make_user("member")
    _link(wiki_db, user)
    browser = oauth_app.test_client()
    login(browser, user)
    wiki_db.execute("UPDATE user_sessions SET auth_method = 'platform_oauth', created_at = ?", (sql_in(minutes=-2),))
    assert browser.get("/_probe/private").data == b"private"
    portal.events = [{"id": 1, "account_id": "acct-1", "wiki_user_id": "", "reason": "signed_out",
                      "at": now_sql()}]
    _run(oauth_app)
    assert browser.get("/_probe/private").status_code == 302


def test_the_local_link_decides_whose_sessions_end(oauth_app, wiki_db, portal, make_user):
    linked, other, unlinked = make_user("linked"), make_user("other"), make_user("unlinked")
    _link(wiki_db, linked)
    _link(wiki_db, other, "acct-other")
    sessions = {name: _session(wiki_db, user) for name, user in
                (("linked", linked), ("other", other), ("unlinked", unlinked))}
    portal.events = [
        # The portal names another wiki account for acct-1: the link here wins.
        {"id": 1, "account_id": "acct-1", "wiki_user_id": other["id"], "reason": "suspended", "at": now_sql()},
        # A link only the portal knows (made by 1.4): the account named is used, being linked to nobody here.
        {"id": 2, "account_id": "acct-14", "wiki_user_id": unlinked["id"], "reason": "suspended", "at": now_sql()},
        # Never someone linked to another portal account here.
        {"id": 3, "account_id": "acct-gone", "wiki_user_id": other["id"], "reason": "deleted", "at": now_sql()},
    ]
    assert _run(oauth_app) == 2
    assert _revoked(wiki_db, sessions["linked"])["revoked_at"]
    assert _revoked(wiki_db, sessions["unlinked"])["revoked_at"]
    assert _revoked(wiki_db, sessions["other"])["revoked_at"] is None


def test_pages_are_followed_and_a_restored_portal_starts_over(oauth_app, wiki_db, portal, make_user):
    user = make_user("member")
    _link(wiki_db, user)
    portal.page = 2
    portal.events = [{"id": n, "account_id": "acct-x", "wiki_user_id": "", "reason": "signed_out",
                      "at": now_sql()} for n in range(1, 6)]
    _run(oauth_app)
    assert wiki_db.scalar("SELECT signout_cursor FROM platform_oauth_state") == 5
    # The portal came back from an older backup: its ids are lower than the cursor.
    session_id = _session(wiki_db, user)
    portal.events = [{"id": 2, "account_id": "acct-1", "wiki_user_id": "", "reason": "signed_out",
                      "at": now_sql()}]
    assert _run(oauth_app) == 1
    assert _revoked(wiki_db, session_id)["revoked_at"]
    assert wiki_db.scalar("SELECT signout_cursor FROM platform_oauth_state") == 2


@pytest.mark.parametrize("event", [
    {"id": "1", "account_id": "acct-1", "wiki_user_id": "", "at": "2026-01-01 00:00:00"},
    {"id": 1, "account_id": "", "wiki_user_id": "", "at": "2026-01-01 00:00:00"},
    {"id": 1, "account_id": "acct-1", "wiki_user_id": "", "at": "yesterday"},
    {"id": 1, "account_id": "acct-1", "wiki_user_id": "", "at": 1767225600},
    {"id": 0, "account_id": "acct-1", "wiki_user_id": "", "at": "2026-01-01 00:00:00"},
    {"id": 1, "account_id": ["acct-1"], "wiki_user_id": "", "at": "2026-01-01 00:00:00"},
    {"id": 1, "account_id": "acct-1", "wiki_user_id": 5, "at": "2026-01-01 00:00:00"},
    "not an event",
])
def test_an_invalid_answer_changes_nothing(oauth_app, wiki_db, portal, make_user, monkeypatch, event):
    user = make_user("member")
    _link(wiki_db, user)
    session_id = _session(wiki_db, user, created="2025-01-01 00:00:00")
    answer = json.dumps({"events": [event], "latest": 1}).encode()
    monkeypatch.setattr(platform_oauth.http, "request",
                        lambda method, url, **kwargs: http.HttpResponse(status=200, body=answer))
    assert _run(oauth_app) == 0
    assert _revoked(wiki_db, session_id)["revoked_at"] is None
    assert not wiki_db.scalar("SELECT signout_cursor FROM platform_oauth_state")


@pytest.mark.parametrize("answer", [b"[]", b"{}", b'{"events": {}}', b"not json"])
def test_an_answer_of_the_wrong_shape_changes_nothing(oauth_app, wiki_db, portal, monkeypatch, answer):
    monkeypatch.setattr(platform_oauth.http, "request",
                        lambda method, url, **kwargs: http.HttpResponse(status=200, body=answer))
    assert _run(oauth_app) == 0
    assert not wiki_db.scalar("SELECT signout_cursor FROM platform_oauth_state")


def test_a_future_time_is_read_as_now(oauth_app, wiki_db, portal, make_user):
    user = make_user("member")
    _link(wiki_db, user)
    portal.events = [{"id": 1, "account_id": "acct-1", "wiki_user_id": "", "reason": "x",
                      "at": "2999-01-01 00:00:00"}]
    _run(oauth_app)
    later = _session(wiki_db, user, created=sql_in(minutes=1))
    assert _revoked(wiki_db, later)["revoked_at"] is None


@pytest.mark.parametrize("status", [404, 401, 500])
def test_a_portal_that_does_not_answer_is_retried_later(oauth_app, wiki_db, portal, make_user, status):
    portal.status = status
    assert _run(oauth_app) == 0
    assert not wiki_db.scalar("SELECT signout_cursor FROM platform_oauth_state")


def test_nothing_happens_without_platform_sign_in(app, portal):
    from bananawiki.wiki.db import connection_scope

    with app.app_context(), connection_scope():
        assert platform_oauth.apply_signouts() == 0
    assert portal.asked == []


def test_the_job_runs_every_minute():
    from bananawiki.wiki.features.auth import FEATURE

    assert any(job.name == "auth.platform_signouts" and job.interval == 60 for job in FEATURE.jobs)
