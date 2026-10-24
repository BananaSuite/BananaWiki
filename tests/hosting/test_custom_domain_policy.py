"""Custom domains: who may use them, asking for one, the guided DNS setup and its live check."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bananawiki.hosting import domains
from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.runtime import DomainCheck

from .hosting_support import build_portal, portal_environ

TARGET = "edge.wiki.test"
REASON = "Our school documentation should live at docs.example.org."


@pytest.fixture
def portal(tmp_path):
    """A platform ready for custom domains (subdomain mode with a CNAME target)."""
    domains._addresses_cache.clear()
    return build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET=TARGET))


def _client(portal, login, account):
    client = portal.test_client()
    login(client, account)
    return client


def _claim(client, wiki, domain="docs.example.org"):
    return client.post(f"/instances/{wiki['id']}/domain", data={"action": "claim", "domain": domain})


def _bound(query, wiki):
    return query("SELECT * FROM instance_custom_domains WHERE instance_id = ?", (wiki["id"],), one=True)


def _verified(query, wiki, domain="docs.example.org"):
    query("INSERT INTO instance_custom_domains (domain, instance_id, verification_token, created_at, verified_at, "
          "verified_until, last_checked_at) VALUES (?, ?, 'tok', '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
          "'2999-01-01 00:00:00', '2026-01-01 00:00:00')", (domain, wiki["id"]))


def _in(portal, check):
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        return check()


def _policy(portal, login, admin, policy):
    response = _client(portal, login, admin).post("/admin/settings", data={"action": "save_custom_domains",
                                                                          "custom_domains_policy": policy})
    assert response.status_code == 302


# Who may use them


def test_by_default_only_administrators_wikis_may_use_custom_domains(portal, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    theirs, ours = make_wiki(owner), make_wiki(admin)
    assert query("SELECT custom_domains_policy FROM hosting_settings", one=True)["custom_domains_policy"] == "admins"
    owner_client = _client(portal, login, owner)
    _claim(owner_client, theirs)
    assert _bound(query, theirs) is None
    page = owner_client.get(f"/instances/{theirs['id']}/domain").get_data(as_text=True)
    assert "cannot use a custom domain yet" in page and "/features/custom_domain/requests" in page
    _claim(_client(portal, login, admin), ours, "help.example.org")
    assert _bound(query, ours)["domain"] == "help.example.org", "an administrator's wiki always may"


def test_administrators_choose_selected_accounts_or_everyone(portal, make_account, make_wiki, login, query):
    admin, chosen, other = make_account(admin=True), make_account(), make_account()
    chosen_wiki, other_wiki = make_wiki(chosen), make_wiki(other)
    admin_client = _client(portal, login, admin)
    admin_client.post(f"/admin/accounts/{chosen['id']}/custom-domains", data={"allowed": "1"})
    assert query("SELECT custom_domains_allowed FROM accounts WHERE id = ?", (chosen["id"],), one=True)[
        "custom_domains_allowed"] == 1
    page = admin_client.get(f"/admin/accounts/{chosen['id']}").get_data(as_text=True)
    assert "This account may use custom domains." in page and "This counts once" in page, "moot until 'selected'"
    _claim(_client(portal, login, chosen), chosen_wiki)
    assert _bound(query, chosen_wiki) is None, "the policy still says administrators only"
    _policy(portal, login, admin, "selected")
    _claim(_client(portal, login, chosen), chosen_wiki)
    _claim(_client(portal, login, other), other_wiki, "wiki.other.org")
    assert _bound(query, chosen_wiki) and _bound(query, other_wiki) is None
    _policy(portal, login, admin, "everyone")
    _claim(_client(portal, login, other), other_wiki, "wiki.other.org")
    assert _bound(query, other_wiki)
    events = {row["action"] for row in query("SELECT action FROM hosting_events")}
    assert {"domain.account.granted", "settings.changed"} <= events
    assert _client(portal, login, other).post("/admin/settings", data={
        "action": "save_custom_domains", "custom_domains_policy": "admins"}).status_code in (302, 403, 404)
    assert query("SELECT custom_domains_policy FROM hosting_settings", one=True)["custom_domains_policy"] == "everyone"
    admin_client.post("/admin/settings", data={"action": "save_custom_domains", "custom_domains_policy": "nobody"})
    assert query("SELECT custom_domains_policy FROM hosting_settings", one=True)["custom_domains_policy"] == "everyone"


def test_routing_and_certificates_follow_the_permission(portal, runtime, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    _verified(query, wiki)
    served = lambda: _in(portal, lambda: (bool(domains.resolve("docs.example.org")),  # noqa: E731
                                           domains.certificate_allowed("docs.example.org"),
                                           domains.active_domain(wiki["id"])))
    assert served() == (False, False, None), "a proven domain of a wiki that may not use one stays dark"
    _policy(portal, login, admin, "everyone")
    assert served() == (True, True, "docs.example.org")
    assert "docs.example.org" in [name for spec in runtime.routes for name in spec.hostnames]
    _policy(portal, login, admin, "admins")
    assert served() == (False, False, None) and _bound(query, wiki), "it stops answering, but its claim stays"
    assert "docs.example.org" not in [name for spec in runtime.routes for name in spec.hostnames]
    _policy(portal, login, admin, "selected")
    _client(portal, login, admin).post(f"/admin/accounts/{owner['id']}/custom-domains", data={"allowed": "1"})
    assert served()[0]
    _client(portal, login, admin).post(f"/admin/accounts/{owner['id']}/custom-domains", data={"allowed": "0"})
    assert not served()[0]


def test_taking_a_wikis_own_grant_away(portal, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    admin_client = _client(portal, login, admin)
    admin_client.post(f"/admin/instances/{wiki['id']}/domain-permission", data={"allowed": "1"})
    _verified(query, wiki)
    assert _in(portal, lambda: domains.active_domain(wiki["id"]))
    admin_client.post(f"/admin/instances/{wiki['id']}/domain-permission", data={"allowed": "0"})
    assert _bound(query, wiki) is None, "nothing else allows it: the domain goes, as before"
    # While the policy allows everyone, taking the wiki's own grant away leaves its domain alone.
    _policy(portal, login, admin, "everyone")
    admin_client.post(f"/admin/instances/{wiki['id']}/domain-permission", data={"allowed": "1"})
    _verified(query, wiki)
    admin_client.post(f"/admin/instances/{wiki['id']}/domain-permission", data={"allowed": "0"})
    assert _bound(query, wiki) and _in(portal, lambda: domains.active_domain(wiki["id"]))


# Asking for one


def test_an_owner_asks_and_an_administrator_approves(portal, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    owner_client = _client(portal, login, owner)
    card = owner_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "Ask for one" in card and f"/instances/{wiki['id']}/domain" in card
    response = owner_client.post(f"/instances/{wiki['id']}/features/custom_domain/requests",
                                 data={"reason": REASON, "back": "domain"})
    assert response.headers["Location"].endswith(f"/instances/{wiki['id']}/domain")
    request = query("SELECT * FROM instance_feature_requests", one=True)
    assert request["feature"] == "custom_domain" and request["status"] == "pending"
    assert "You asked for a custom domain" in owner_client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)
    admin_client = _client(portal, login, admin)
    overview = admin_client.get("/admin").get_data(as_text=True)
    assert REASON in overview and "Custom domain" in overview
    tools = admin_client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert f"/admin/feature-requests/{request['id']}/approve" in tools, "answered on the wiki's admin page too"
    admin_client.post(f"/admin/feature-requests/{request['id']}/approve", data={"review_note": "Enjoy"})
    assert query("SELECT custom_domain_allowed FROM instances WHERE id = ?", (wiki["id"],), one=True)[
        "custom_domain_allowed"] == 1
    notice = query("SELECT * FROM hosting_account_notices WHERE account_id = ?", (owner["id"],), one=True)
    assert notice["object_id"] == "custom_domain" and notice["outcome"] == "approved"
    page = owner_client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)
    assert 'name="domain"' in page, "the owner sets the domain up right away"
    assert "Set it up" in owner_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "Allowed for this wiki." in admin_client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    _claim(owner_client, wiki)
    assert _bound(query, wiki)


def test_allowing_on_the_domain_page_answers_a_waiting_request(portal, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    _client(portal, login, owner).post(f"/instances/{wiki['id']}/features/custom_domain/requests",
                                       data={"reason": REASON})
    admin_client = _client(portal, login, admin)
    page = admin_client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)
    assert "Approve the request" in page and REASON in page
    admin_client.post(f"/admin/instances/{wiki['id']}/domain-permission", data={"allowed": "1"})
    assert query("SELECT status FROM instance_feature_requests", one=True)["status"] == "approved"


def test_requests_are_refused_when_they_make_no_sense(portal, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    _policy(portal, login, admin, "everyone")
    _client(portal, login, owner).post(f"/instances/{wiki['id']}/features/custom_domain/requests",
                                       data={"reason": REASON})
    assert query("SELECT COUNT(*) AS n FROM instance_feature_requests", one=True)["n"] == 0, "already allowed"


def test_without_a_target_nothing_is_offered(tmp_path, make_account, login):
    plain = build_portal(tmp_path / "plain")
    from bananawiki.hosting import accounts, instances

    with plain.test_request_context("/"), connection_scope(plain.extensions["bananawiki.hosting.database"]):
        owner = accounts.create("plainowner", "correct horse 42")
        wiki = instances.create(owner, "plainwiki")[0]
    client = plain.test_client()
    client.post("/login", data={"username": "plainowner", "password": "correct horse 42"})
    client.post(f"/instances/{wiki['id']}/features/custom_domain/requests", data={"reason": REASON})
    with plain.app_context(), connection_scope(plain.extensions["bananawiki.hosting.database"]) as db:
        assert db.scalar("SELECT COUNT(*) FROM instance_feature_requests") == 0
    assert "not configured" in client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)


# The guide


def test_a_subdomain_gets_a_cname_and_a_root_domain_gets_addresses(portal, runtime, make_account, make_wiki, login):
    admin = make_account(admin=True)
    runtime.addresses[TARGET] = ("203.0.113.7", "2001:db8::7")
    sub, root = make_wiki(admin, "subwiki"), make_wiki(admin, "rootwiki")
    client = _client(portal, login, admin)
    _claim(client, sub, "docs.example.org")
    page = client.get(f"/instances/{sub['id']}/domain").get_data(as_text=True)
    assert "is a subdomain of" in page and 'data-copy="_bananawiki-challenge.docs"' in page
    assert 'data-copy="_bananawiki-challenge.docs.example.org"' in page
    assert '<span class="mono">CNAME</span>' in page and f'data-copy="{TARGET}"' in page
    assert "203.0.113.7" in page, "A/AAAA work for a subdomain too"
    _claim(client, root, "example.org")
    page = client.get(f"/instances/{root['id']}/domain").get_data(as_text=True)
    assert "is a root domain" in page and 'data-copy="_bananawiki-challenge"' in page
    assert '<span class="mono">A</span>' in page and 'data-copy="203.0.113.7"' in page
    assert '<span class="mono">AAAA</span>' in page and 'data-copy="2001:db8::7"' in page
    assert "ALIAS" in page and "redirect www.example.org to example.org" in page
    assert '<span class="mono">CNAME</span>' not in page
    # The owner can correct the guess (an apex the labels do not show).
    corrected = client.get(f"/instances/{sub['id']}/domain?kind=root").get_data(as_text=True)
    assert "is a root domain" in corrected and 'data-copy="203.0.113.7"' in corrected


def test_zones_are_guessed_like_registrars_sell_names():
    assert domains.zone_of("docs.example.org") == "example.org"
    assert domains.zone_of("example.org") == "example.org"
    assert domains.zone_of("wiki.school.example.co.uk") == "example.co.uk"
    assert domains.zone_of("a.b.example.com") == "example.com"


def test_without_platform_addresses_root_domains_are_told_about_alias_records(portal, make_account, make_wiki, login):
    admin = make_account(admin=True)
    wiki = make_wiki(admin)
    client = _client(portal, login, admin)
    _claim(client, wiki, "example.org")
    page = client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)
    assert "no addresses for root domains yet" in page
    settings = client.get("/admin/settings").get_data(as_text=True)
    assert "None found" in settings and '<span class="host-part">edge.</span>' in settings


# The live check


def test_the_page_checks_dns_and_turns_the_domain_on_by_itself(portal, runtime, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    _policy(portal, login, admin, "everyone")
    client = _client(portal, login, owner)
    _claim(client, wiki)
    answer = client.post(f"/instances/{wiki['id']}/domain/check")
    data = answer.get_json()
    assert answer.status_code == 200 and data["state"] == "pending" and data["poll"] == 30 and not data["verified"]
    assert "Missing" in data["html"]
    token = _bound(query, wiki)["verification_token"]
    runtime.domain_details["docs.example.org"] = {
        "txt": ["something-else"], "cname": ["elsewhere.example.net"], "addresses": ["198.51.100.1"],
        "expected_addresses": [], "ownership": False, "routing": False, "proxied": False, "dns_error": False}
    data = client.post(f"/instances/{wiki['id']}/domain/check").get_json()
    assert data["html"].count("Wrong") == 2 and "elsewhere.example.net" in data["html"]
    runtime.domain_details["docs.example.org"] = {
        "txt": [token], "cname": [TARGET], "addresses": [], "expected_addresses": [], "ownership": True,
        "routing": True, "proxied": False, "dns_error": False}
    runtime.domain_results["docs.example.org"] = DomainCheck(ownership=True, routing=True)
    data = client.post(f"/instances/{wiki['id']}/domain/check").get_json()
    assert data["verified"] and data["state"] == "active" and data["poll"] == 0
    assert _bound(query, wiki)["verified_at"]
    page = client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)
    assert "Your wiki answers at" in page and "https://docs.example.org" in page


def test_the_check_is_limited_and_only_for_the_owner(portal, runtime, make_account, make_wiki, login, query):
    admin, owner, stranger, helper = make_account(admin=True), make_account(), make_account(), make_account()
    wiki = make_wiki(owner)
    _policy(portal, login, admin, "everyone")
    client = _client(portal, login, owner)
    _claim(client, wiki)
    for user in (stranger,):
        assert _client(portal, login, user).post(f"/instances/{wiki['id']}/domain/check").status_code == 404
    query("INSERT INTO instance_collaborators (instance_id, account_id, role, permissions, created_at) "
          "VALUES (?, ?, 'full_access', '[]', '2026-01-01 00:00:00')", (wiki["id"], helper["id"]))
    assert _client(portal, login, helper).post(f"/instances/{wiki['id']}/domain/check").status_code == 404
    answers = [client.post(f"/instances/{wiki['id']}/domain/check").status_code for _ in range(8)]
    assert answers[:6] == [200] * 6 and answers[6:] == [429, 429]
    assert len(runtime.called("inspect_domain")) == 6


def test_an_administrator_looking_as_the_owner_changes_nothing(portal, runtime, make_account, make_wiki, login,
                                                               query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    _policy(portal, login, admin, "everyone")
    _claim(_client(portal, login, owner), wiki)
    token = _bound(query, wiki)["verification_token"]
    runtime.domain_details["docs.example.org"] = {"txt": [token], "cname": [TARGET], "addresses": [],
                                                  "expected_addresses": [], "ownership": True, "routing": True,
                                                  "proxied": False, "dns_error": False}
    runtime.domain_results["docs.example.org"] = DomainCheck(ownership=True, routing=True)
    admin_client = _client(portal, login, admin)
    assert admin_client.post(f"/admin/accounts/{owner['id']}/impersonate").status_code == 302
    data = admin_client.post(f"/instances/{wiki['id']}/domain/check").get_json()
    assert data["state"] == "pending" and not data["verified"], "the checklist, never the verification"
    assert admin_client.post(f"/instances/{wiki['id']}/domain", data={"action": "remove"}).status_code == 403
    assert _bound(query, wiki)["verified_at"] is None


# Upgrading


def test_upgrade_lets_owners_ask_for_custom_domains_and_keeps_old_requests(tmp_path):
    from .test_hosting_migration import _legacy_database

    environ = portal_environ(tmp_path)
    path = Path(environ["HOSTING_DATABASE_PATH"])
    _legacy_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO instance_feature_requests (id, instance_id, feature, requested_by, reason, status) "
                 "VALUES (5, 'i1', 'public_access', 'a2', 'Please open it to everyone for our users.', 'pending')")
    conn.commit()
    conn.close()
    build_portal(tmp_path, environ=environ)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT id, feature, status FROM instance_feature_requests").fetchall() == [
        (5, "public_access", "pending")]
    conn.execute("INSERT INTO instance_feature_requests (instance_id, feature, requested_by, reason) "
                 "VALUES ('i1', 'custom_domain', 'a2', 'Our own domain for this wiki, please.')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO instance_feature_requests (instance_id, feature, requested_by, reason) "
                     "VALUES ('i1', 'root_shell', 'a2', 'x')")
    assert conn.execute("SELECT custom_domains_policy FROM hosting_settings").fetchone() == ("admins",)
    assert conn.execute("SELECT custom_domains_allowed FROM accounts WHERE id = 'a2'").fetchone() == (0,)
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                                              "AND tbl_name = 'instance_feature_requests'")}
    assert {"ux_instance_feature_requests_pending", "ix_instance_feature_requests_instance",
            "ix_instance_feature_requests_status"} <= indexes


def test_a_domain_follows_its_owner_at_once(portal, runtime, make_account, make_wiki, login, query):
    """Demoting an administrator (or moving the wiki) republishes the routes: the domain stops answering now,
    not at the next maintenance pass."""
    admin, boss = make_account(admin=True), make_account(admin=True)
    wiki = make_wiki(boss)
    _verified(query, wiki)
    _in(portal, lambda: __import__("bananawiki.hosting.instances", fromlist=["x"]).sync_routes())
    routed = lambda: "docs.example.org" in [n for spec in runtime.routes for n in spec.hostnames]  # noqa: E731
    assert routed()
    _client(portal, login, admin).post(f"/admin/accounts/{boss['id']}/toggle-admin")
    assert query("SELECT is_admin FROM accounts WHERE id = ?", (boss["id"],), one=True)["is_admin"] == 0
    assert not routed(), "policy 'admins': a former administrator's wiki stops answering at once"


def test_private_names_and_addresses_are_never_looked_up_or_shown(portal, runtime, make_account, make_wiki, login,
                                                                  query):
    from bananawiki.hosting.errors import ServiceError

    for name in ("nas.lan", "db.corp", "printer.home.arpa", "vault.service.consul", "box.home"):
        with pytest.raises(ServiceError) as refused:
            _in(portal, lambda name=name: domains._external(name))
        assert refused.value.key == "hosting.domains.not_public"
    assert _in(portal, lambda: domains._external("docs.example.org")) == "docs.example.org"
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    _policy(portal, login, admin, "everyone")
    client = _client(portal, login, owner)
    _claim(client, wiki)
    runtime.domain_details["docs.example.org"] = {
        "txt": [], "cname": ["db.internal-zone.lan"], "addresses": ["10.1.2.3", "fd00::1"], "expected_addresses": [],
        "ownership": False, "routing": False, "proxied": False, "dns_error": False}
    html = client.post(f"/instances/{wiki['id']}/domain/check").get_json()["html"]
    assert "10.1.2.3" not in html and "fd00::1" not in html and "internal-zone" not in html
    assert "not public" in html


def test_a_check_that_passes_once_but_not_twice_keeps_polling(portal, runtime, make_account, make_wiki, login,
                                                              query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    _policy(portal, login, admin, "everyone")
    client = _client(portal, login, owner)
    _claim(client, wiki)
    token = _bound(query, wiki)["verification_token"]
    runtime.domain_details["docs.example.org"] = {
        "txt": [token], "cname": [TARGET], "addresses": [], "expected_addresses": [], "ownership": True,
        "routing": True, "proxied": False, "dns_error": False}
    runtime.domain_results["docs.example.org"] = DomainCheck(ownership=False, routing=False, dns_error=True)
    answer = client.post(f"/instances/{wiki['id']}/domain/check")
    data = answer.get_json()
    assert answer.status_code == 200 and not data["verified"] and data["poll"] == 30
    assert not _bound(query, wiki)["verified_at"]
