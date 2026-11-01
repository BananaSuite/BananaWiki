"""Regression tests for the RW4 findings of the full re-audit pass 12 (hosting portal)."""
# ruff: noqa: F811  (pytest fixtures imported from tests.hosting.conftest are parameters here)

from __future__ import annotations

import time

import pytest

from bananawiki.hosting import domains
from tests.hosting.conftest import login, make_account, make_wiki, query, runtime, web  # noqa: F401
from tests.hosting.hosting_support import build_portal, portal_environ
from tests.hosting.test_hosting_wikihosts import upstream  # noqa: F401

DOMAIN_TARGET = "edge.wiki.test"


@pytest.fixture
def portal(tmp_path):
    """The usual test portal, ready for custom domains (a CNAME target set)."""
    domains._addresses_cache.clear()
    return build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET=DOMAIN_TARGET))


ALL_PERMISSIONS = ["view", "start_stop", "terminate", "reset_password", "reset_wiki", "download", "toggle_mode",
                   "manage_collaborators", "use_case", "analytics"]

# RW4-1: collaborators the previous owner added leave with them unless the recipient keeps each one


def _offer_with_team(portal, make_account, login, make_wiki, query):
    old, sock, friend, new = (make_account(name) for name in ("oldowner", "sockpuppet", "friend", "newowner"))
    inst = make_wiki(old)
    browser = portal.test_client()
    login(browser, old)
    for username, role in (("sockpuppet", "full_access"), ("friend", "custom")):
        assert browser.post(f"/instances/{inst['id']}/collaborators/add",
                            data={"username": username, "role": role, "permissions": ["view"]}).status_code == 302
    browser.post(f"/instances/{inst['id']}/transfer", data={"username": "newowner"})
    offer = query("SELECT id FROM instance_ownership_transfers WHERE instance_id = ? AND status = 'pending'",
                  (inst["id"],), one=True)
    return inst, offer["id"], browser, (old, sock, friend, new)


def _members(query, inst):
    return {row["username"] for row in query(
        "SELECT a.username FROM instance_collaborators c JOIN accounts a ON a.id = c.account_id "
        "WHERE c.instance_id = ?", (inst["id"],))}


def test_rw4_1_previous_owners_collaborators_do_not_survive_a_transfer(portal, make_account, login,
                                                                      make_wiki, query):
    inst, offer, old_browser, (old, sock, _friend, new) = _offer_with_team(portal, make_account, login, make_wiki,
                                                                           query)
    recipient = portal.test_client()
    login(recipient, new)
    page = recipient.get("/dashboard").get_data(as_text=True)
    assert "sockpuppet" in page and 'name="keep"' in page  # the recipient sees who has access
    assert recipient.post(f"/transfers/{offer}/accept", data={"previous_owner": "none"}).status_code == 302
    assert query("SELECT account_id FROM instances WHERE id = ?", (inst["id"],), one=True)["account_id"] == new["id"]
    assert _members(query, inst) == set()
    # The sock can no longer hand the previous owner every permission back.
    sock_browser = portal.test_client()
    login(sock_browser, sock)
    sock_browser.post(f"/instances/{inst['id']}/collaborators/add",
                      data={"username": "oldowner", "role": "custom", "permissions": ALL_PERMISSIONS})
    assert _members(query, inst) == set()
    assert old_browser.post(f"/instances/{inst['id']}/reset-password").status_code == 404
    assert sock_browser.post(f"/instances/{inst['id']}/reset-password").status_code == 404


def test_rw4_1_recipient_keeps_the_collaborators_they_tick(portal, make_account, login, make_wiki,
                                                           query):
    from bananawiki.hosting import collaborators

    inst, offer, old_browser, (_old, _sock, friend, new) = _offer_with_team(portal, make_account, login,
                                                                            make_wiki, query)
    from bananawiki.hosting.db import connection_scope

    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        team = collaborators.incoming_transfers(new["id"])[0]["team"]
    assert {member["username"] for member in team} == {"sockpuppet", "friend"}
    kept = [member["keep"] for member in team if member["account_id"] == friend["id"]]
    recipient = portal.test_client()
    login(recipient, new)
    assert recipient.post(f"/transfers/{offer}/accept",
                          data={"previous_owner": "view", "keep": kept}).status_code == 302
    assert _members(query, inst) == {"friend", "oldowner"}
    assert query("SELECT permissions FROM instance_collaborators WHERE instance_id = ? AND account_id = ?",
                 (inst["id"], friend["id"]), one=True)["permissions"] == '["view"]'


def test_rw4_1_a_tick_does_not_keep_access_raised_after_the_offer_was_shown(portal, make_account, login,
                                                                            make_wiki, query):
    from bananawiki.hosting import collaborators
    from bananawiki.hosting.db import connection_scope

    inst, offer, old_browser, (_old, _sock, friend, new) = _offer_with_team(portal, make_account, login,
                                                                            make_wiki, query)
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        team = collaborators.incoming_transfers(new["id"])[0]["team"]
    kept = [member["keep"] for member in team if member["account_id"] == friend["id"]]
    assert old_browser.post(f"/instances/{inst['id']}/collaborators/{friend['id']}/update",
                            data={"role": "full_access"}).status_code == 302
    recipient = portal.test_client()
    login(recipient, new)
    assert recipient.post(f"/transfers/{offer}/accept", data={"previous_owner": "none", "keep": kept}).status_code \
        == 302
    assert _members(query, inst) == set()


# RW4-2: Verify spends the per-wiki DNS-check budget, and one check's lookups share one deadline


def test_rw4_2_lookups_of_one_check_share_one_deadline(monkeypatch):
    import dns.resolver

    from bananawiki.hosting.runtime import dnscheck

    monkeypatch.setattr(dnscheck, "LIFETIME_SECONDS", 0.2)
    monkeypatch.setattr(dnscheck, "CHECK_SECONDS", 0.3, raising=False)
    lookups = []

    def slow(name, record_type, *, search=False, lifetime=None):  # answers each query just before it times out
        lookups.append(record_type)
        time.sleep(lifetime * 0.9)
        if record_type == "TXT":
            return []  # a proof that never matches
        raise dns.resolver.NoAnswer()

    monkeypatch.setattr(dns.resolver, "resolve", slow)
    started = time.monotonic()
    result = dnscheck.check("docs.example.org", "token", DOMAIN_TARGET, ())
    assert time.monotonic() - started < 0.45  # six lookups of 0.18 s each took 1.1 s
    assert result.dns_error and not result.ownership
    assert len(lookups) < 6


def test_rw4_2_verify_counts_against_the_wikis_dns_check_budget(portal, make_account, make_wiki, login, query,
                                                                runtime):
    admin, owner = make_account("boss", admin=True), make_account("owner")
    boss = portal.test_client()
    login(boss, admin)
    assert boss.post("/admin/settings", data={"action": "save_custom_domains",
                                              "custom_domains_policy": "everyone"}).status_code == 302
    wiki = make_wiki(owner)
    query("UPDATE instances SET status = 'running' WHERE id = ?", (wiki["id"],))
    browser = portal.test_client()
    login(browser, owner)
    browser.post(f"/instances/{wiki['id']}/domain", data={"action": "claim", "domain": "docs.example.org"})
    assert query("SELECT 1 FROM instance_custom_domains WHERE instance_id = ?", (wiki["id"],), one=True)
    for number in range(8):  # one request from each of eight networks, under each network's own limit
        browser.post(f"/instances/{wiki['id']}/domain", data={"action": "verify"},
                     environ_base={"REMOTE_ADDR": f"203.0.113.{number + 1}"})
    assert len(runtime.called("check_domain")) == 6  # the other two are refused before any lookup


# RW4-3: a request target with bytes outside ASCII reaches the wiki percent-encoded, never a 500


def _raw_get(portal, raw: str) -> tuple[str, bytes]:
    """GET *raw* on the wiki host through the portal's WSGI app, with RAW_URI as Gunicorn sets it."""
    from werkzeug.test import EnvironBuilder

    environ = EnvironBuilder(path="/caf", headers={"Host": "team-hosting.wiki.test"}).get_environ()
    environ["RAW_URI"] = raw
    status = []
    answer = portal.wsgi_app(environ, lambda line, headers, exc_info=None: status.append(line))
    try:
        return status[0], b"".join(answer)
    finally:
        getattr(answer, "close", lambda: None)()


def test_rw4_3_non_ascii_request_target_is_percent_encoded(portal, runtime, make_account, make_wiki, upstream):
    import json

    make_wiki(make_account(), "team")
    runtime.upstreams["team"] = upstream
    # Gunicorn hands raw bytes >= 0x80 in the target over decoded as Latin-1.
    for raw, sent in (("/caf\xe9?q=\xe9t\xe9", "/caf%E9?q=%E9t%E9"), ("/ok?a=%41", "/ok?a=%41"),
                      ("/tab\there", "/tab%09here")):
        status, body = _raw_get(portal, raw)
        assert status.startswith("200"), raw
        assert json.loads(body)["path"] == sent


def test_rw4_3_a_target_http_client_refuses_gets_the_status_page(web, runtime, make_account, make_wiki,
                                                                 upstream, monkeypatch):
    from bananawiki.hosting import wikihosts

    make_wiki(make_account(), "team")
    runtime.upstreams["team"] = upstream
    monkeypatch.setattr(wikihosts, "_target", lambda environ: "/caf\xe9")  # what the proxy used to send
    response = web.get("/caf", headers={"Host": "team-hosting.wiki.test"})
    assert response.status_code == 503  # the "bad gateway" status page
    assert response.headers["Content-Type"].startswith("text/html")
