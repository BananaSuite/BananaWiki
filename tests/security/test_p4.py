"""Regression tests for the PW4 findings of the full re-audit pass 10 (hosting portal: OAuth link loops,
server-to-server limits, API tokens, pause and resume)."""

from __future__ import annotations

import base64
import hashlib
import re

from bananawiki.hosting.db import connection_scope
from tests.hosting.conftest import (  # noqa: F401
    PASSWORD,
    login,
    make_account,
    make_wiki,
    portal,
    query,
    runtime,
    web,
)
from tests.hosting.hosting_support import build_portal


def _services(portal):  # noqa: F811
    class Scope:
        def __enter__(self):
            self._request = portal.test_request_context("/")
            self._request.__enter__()
            self._scope = connection_scope(portal.extensions["bananawiki.hosting.database"])
            self._scope.__enter__()

        def __exit__(self, *exc):
            self._scope.__exit__(*exc)
            self._request.__exit__(*exc)

    return Scope()


# PW4-1: a wiki's credentials link and unlink in a loop


def test_pw4_1_link_loop_records_one_event_a_day(portal, web, make_account, make_wiki, query):  # noqa: F811
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    owner = make_account("tenant1")
    wiki = make_wiki(owner, "churnwiki")
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect = "https://churnwiki.example/platform-oauth/callback"
    with _services(portal):
        from bananawiki.hosting import oauth

        client_id, secret = oauth.rotate_credentials(wiki["id"])
        code = oauth.issue_code(client_id, owner["id"], redirect, challenge, "S256")
    answer = web.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                            "client_secret": secret, "redirect_uri": redirect,
                                            "code_verifier": verifier})
    assert answer.status_code == 200
    token = answer.get_json()["access_token"]
    credentials = {"client_id": client_id, "client_secret": secret}
    for _ in range(20):
        assert web.post("/oauth/link", json={**credentials, "access_token": token, "wiki_user_id": "7"}).status_code \
            == 200
        assert web.post("/oauth/unlink", json={**credentials, "account_id": owner["id"]}).status_code == 200
    linked = query("SELECT COUNT(*) AS n FROM hosting_events WHERE action = 'oauth.linked'", one=True)["n"]
    assert linked == 1, "relinking the same account to the same wiki is logged once a day"


# PW4-2: with platform sign-in off, the server-to-server paths count against the global limit


def test_pw4_2_disabled_oauth_paths_count_against_the_global_limit(tmp_path, monkeypatch):
    from bananawiki.hosting import app as hosting_app

    monkeypatch.setattr(hosting_app, "GLOBAL_RATE_LIMIT", 5)
    client = build_portal(tmp_path).test_client()
    codes = [client.post("/oauth/token").status_code for _ in range(4)]
    codes += [client.get("/oauth/userinfo", headers={"Authorization": "Bearer x"}).status_code for _ in range(3)]
    assert codes == [404] * 5 + [429] * 2


# PW4-3: personal API tokens created and revoked in a loop


def _create_token(web, addr: str, name: str = "t"):  # noqa: F811
    env = {"REMOTE_ADDR": addr}
    page = web.get("/account", environ_base=env).get_data(as_text=True)
    nonce = re.search(r'name="form_nonce" value="([^"]+)"', page).group(1)
    return web.post("/account/api-tokens", environ_base=env, data={
        "form_nonce": nonce, "current_password": PASSWORD, "expires_in": "never", "name": name,
        "scopes": ["account:read"]})


def test_pw4_3_token_creations_are_capped_per_day(portal, make_account, login, query, monkeypatch):  # noqa: F811
    from bananawiki.hosting import api_tokens

    monkeypatch.setattr(api_tokens, "MAX_CREATED_PER_DAY", 3, raising=False)
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")
    me = make_account()
    browser = portal.test_client()
    login(browser, me)
    for n in range(3):
        assert "bwh_" in _create_token(browser, f"198.51.100.{n + 1}").get_data(as_text=True)
        token_id = query("SELECT MAX(id) AS i FROM hosting_api_tokens WHERE account_id = ?", (me["id"],), one=True)["i"]
        assert browser.post(f"/account/api-tokens/{token_id}/revoke",
                            environ_base={"REMOTE_ADDR": f"198.51.100.{n + 1}"}).status_code == 302
    refused = _create_token(browser, "198.51.100.50")
    assert "bwh_" not in refused.get_data(as_text=True)
    assert query("SELECT COUNT(*) AS n FROM hosting_api_tokens WHERE account_id = ?", (me["id"],), one=True)["n"] == 3
    page = browser.get("/account", environ_base={"REMOTE_ADDR": "198.51.100.50"}).get_data(as_text=True)
    assert "tokens in the last 24 hours" in page


def test_pw4_3_old_revoked_tokens_are_pruned(portal, make_account, query):  # noqa: F811
    me = make_account()
    with _services(portal):
        from bananawiki.hosting import api_tokens, maintenance

        old, _raw = api_tokens.create(me, "old", ["account:read"], None, 0)
        recent, _raw = api_tokens.create(me, "recent", ["account:read"], None, 0)
        live, _raw = api_tokens.create(me, "live", ["account:read"], None, 0)
        api_tokens.revoke(me["id"], old, me["id"])
        api_tokens.revoke(me["id"], recent, me["id"])
        api_tokens.db.execute("UPDATE hosting_api_tokens SET revoked_at = '2000-01-01 00:00:00' WHERE id = ?", (old,))
        maintenance._prune()
    left = {row["id"] for row in query("SELECT id FROM hosting_api_tokens WHERE account_id = ?", (me["id"],))}
    assert left == {recent, live}


# PW4-4: pausing and resuming one wiki in a loop


def test_pw4_4_api_pause_resume_is_bounded_per_wiki(portal, runtime, make_account, make_wiki, query):  # noqa: F811
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")
    owner = make_account()
    wiki = make_wiki(owner, "flapwiki")
    with _services(portal):
        from bananawiki.hosting import api_tokens, instances

        tokens = [api_tokens.create(owner, f"t{i}", ["instances:manage"], None, 0)[1] for i in range(3)]
        budget = getattr(instances, "STATE_CHANGES_PER_MINUTE", 6)
    client = portal.test_client()
    before = len(runtime.calls)
    codes = []
    for n in range(budget + 3):
        action = "pause" if n % 2 == 0 or n >= budget else "resume"
        codes.append(client.post(f"/api/v1/instances/{wiki['id']}/{action}",
                                 headers={"Authorization": f"Bearer {tokens[n % 3]}"},
                                 environ_base={"REMOTE_ADDR": f"203.0.113.{n + 1}"}).status_code)
    assert codes == [200] * budget + [429] * 3
    assert sum(1 for name, _ in runtime.calls[before:] if name in ("stop", "start")) == budget


def test_pw4_4_web_pause_resume_is_bounded_per_wiki_but_not_for_admins(portal, runtime, make_account,  # noqa: F811
                                                                      make_wiki, login, query):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner, "flapweb")
    browser = portal.test_client()
    login(browser, owner)
    with _services(portal):
        from bananawiki.hosting import instances

        budget = getattr(instances, "STATE_CHANGES_PER_MINUTE", 6)
    before = len(runtime.calls)
    for n in range(budget + 2):
        path = "stop" if n % 2 == 0 else "restart"
        browser.post(f"/instances/{wiki['id']}/{path}", environ_base={"REMOTE_ADDR": f"198.51.100.{n + 1}"})
    assert sum(1 for name, _ in runtime.calls[before:] if name in ("stop", "start")) == budget
    status = query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"]

    admin = make_account("rootadm", admin=True)
    staff = portal.test_client()
    login(staff, admin)
    before = len(runtime.calls)
    staff.post(f"/instances/{wiki['id']}/{'stop' if status == 'running' else 'restart'}")
    assert len([name for name, _ in runtime.calls[before:] if name in ("stop", "start")]) == 1


def _spend_day(portal, inst_id, hits, seconds_ago=3600):  # noqa: F811
    """Record *hits* state changes of the wiki an hour ago: the day's budget, not the minute's."""
    with _services(portal):
        from bananawiki.core.timeutil import sql_in
        from bananawiki.hosting import instances
        from bananawiki.hosting.db import db

        for _ in range(hits):
            db.execute("INSERT INTO hosting_rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
                       (inst_id, instances._STATE_CHANGES, sql_in(seconds=-seconds_ago)))


def test_pw4_4_refused_changes_do_not_spend_the_budget(portal, runtime, make_account, make_wiki, login,  # noqa: F811
                                                       query):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner, "noopstop")
    browser = portal.test_client()
    login(browser, owner)
    browser.post(f"/instances/{wiki['id']}/stop", environ_base={"REMOTE_ADDR": "198.51.100.1"})
    for n in range(8):  # a stopped wiki cannot be stopped again: nothing happens and nothing is counted
        browser.post(f"/instances/{wiki['id']}/stop", environ_base={"REMOTE_ADDR": f"198.51.100.{n + 2}"})
    assert query("SELECT COUNT(*) AS n FROM hosting_rate_limit_hits WHERE ip = ?", (wiki["id"],),
                 one=True)["n"] == 1
    browser.post(f"/instances/{wiki['id']}/restart", environ_base={"REMOTE_ADDR": "198.51.100.20"})
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "running"


def test_pw4_4_a_spent_day_still_lets_the_wiki_resume(portal, runtime, make_account, make_wiki, login,  # noqa: F811
                                                      query):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner, "dayspent")
    browser = portal.test_client()
    login(browser, owner)
    browser.post(f"/instances/{wiki['id']}/stop")
    with _services(portal):
        from bananawiki.hosting import instances

        per_day = instances.STATE_CHANGES_PER_DAY
    _spend_day(portal, wiki["id"], per_day)
    browser.post(f"/instances/{wiki['id']}/restart", environ_base={"REMOTE_ADDR": "198.51.100.30"})
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "running"
    browser.post(f"/instances/{wiki['id']}/stop", environ_base={"REMOTE_ADDR": "198.51.100.31"})
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "running"


def test_pw4_4_resetting_the_wiki_counts_against_the_same_budget(portal, runtime, make_account, make_wiki,  # noqa: F811
                                                                 login):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner, "resetloop")
    browser = portal.test_client()
    login(browser, owner)
    with _services(portal):
        from bananawiki.hosting import instances

        budget = instances.STATE_CHANGES_PER_MINUTE
    before = len(runtime.calls)
    for n in range(budget + 3):  # each from another network: the page's own limit is per network
        browser.post(f"/instances/{wiki['id']}/reset-wiki", environ_base={"REMOTE_ADDR": f"192.0.2.{n + 1}"})
    assert sum(1 for name, _ in runtime.calls[before:] if name == "reset_content") == budget


def test_pw4_4_a_spent_day_reports_its_real_wait(portal, runtime, make_account, make_wiki, login,  # noqa: F811
                                                 query):  # noqa: F811
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")
    owner = make_account()
    wiki = make_wiki(owner, "daywait")
    with _services(portal):
        from bananawiki.hosting import api_tokens, instances

        token = api_tokens.create(owner, "day", ["instances:manage"], None, 0)[1]
        per_day = instances.STATE_CHANGES_PER_DAY
    _spend_day(portal, wiki["id"], per_day, seconds_ago=3600)
    reply = portal.test_client().post(f"/api/v1/instances/{wiki['id']}/pause",
                                      headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 429
    assert 82000 < reply.get_json()["retry_after"] <= 82800
    assert "minute" not in reply.get_json()["error"]

    browser = portal.test_client()
    login(browser, owner)
    page = browser.post(f"/instances/{wiki['id']}/stop", follow_redirects=True).get_data(as_text=True)
    assert "24 hours" in page and "about 23 h" in page


def test_pw4_4_easy_wiki_toggle_restarts_count_against_the_budget(portal, runtime, make_account,  # noqa: F811
                                                                  make_wiki, login, query):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner, "togglewiki")
    browser = portal.test_client()
    login(browser, owner)
    with _services(portal):
        from bananawiki.hosting import instances

        budget = getattr(instances, "STATE_CHANGES_PER_MINUTE", 6)
    before = len(runtime.calls)
    for n in range(budget + 4):  # a new network each time: only the per-wiki budget can stop it
        browser.post(f"/instances/{wiki['id']}/toggle-easy-wiki", data={"enable": "1" if n % 2 == 0 else "0"},
                     environ_base={"REMOTE_ADDR": f"198.51.100.{n + 1}"})
    assert sum(1 for name, _ in runtime.calls[before:] if name == "restart") == budget
    assert query("SELECT COUNT(*) AS n FROM hosting_rate_limit_hits WHERE ip = ?", (wiki["id"],),
                 one=True)["n"] == budget

    admin = make_account("modeadm", admin=True)
    staff = portal.test_client()
    login(staff, admin)
    mode = query("SELECT easy_wiki FROM instances WHERE id = ?", (wiki["id"],), one=True)["easy_wiki"]
    before = len(runtime.calls)
    staff.post(f"/admin/instances/{wiki['id']}/toggle-easy-wiki", data={"enable": "0" if mode else "1"})
    assert sum(1 for name, _ in runtime.calls[before:] if name == "restart") == 1
