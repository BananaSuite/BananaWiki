"""Regression tests for the SW4 findings of the full re-audit pass 13 (hosting portal)."""
# ruff: noqa: F811  (pytest fixtures imported from tests.hosting.conftest are parameters here)

from __future__ import annotations

import pytest

from bananawiki.hosting import domains, maintenance
from bananawiki.hosting.errors import ServiceError
from bananawiki.hosting.runtime import DomainCheck
from tests.hosting.conftest import login, make_account, make_wiki, portal, query, runtime, web  # noqa: F401
from tests.hosting.hosting_support import build_portal, portal_environ
from tests.hosting.test_cross_wiki_sign_in import platform  # noqa: F401
from tests.security.test_iw2 import _basic, _client, _code, _credentials

# SW4-1: a made-up code sent with a wiki's own credentials costs a read only


def test_sw4_1_made_up_codes_take_no_write_lock(platform, monkeypatch):
    from bananawiki.core import sqlite as core_sqlite
    from bananawiki.hosting import limits

    monkeypatch.setattr(limits, "BAD_GRANT_PER_MINUTE", 3)
    client_id, secret = _credentials(platform)
    wiki = _client(platform, "10.0.0.5")
    began = []
    transaction = core_sqlite.Session.transaction

    def counting(self):
        if not self.conn.in_transaction:
            began.append(1)
        return transaction(self)

    monkeypatch.setattr(core_sqlite.Session, "transaction", counting)
    form = {"grant_type": "authorization_code", "code": "made-up", "redirect_uri": "https://team.example/cb"}
    codes = [wiki.post("/oauth/token", data=form, headers=_basic(client_id, secret)).status_code for _ in range(20)]
    assert codes == [400] * 3 + [429] * 17
    # Three refusals were recorded (each a short write); none over the limit wrote or locked anything.
    assert len(began) <= 3
    good = {"grant_type": "authorization_code", "code": _code(platform, platform["owner"], client_id),
            "redirect_uri": "https://team.example/cb"}
    assert wiki.post("/oauth/token", data=good, headers=_basic(client_id, secret)).status_code == 200
    # A replayed code is still refused (see the next test for what a replay costs).
    assert wiki.post("/oauth/token", data=good, headers=_basic(client_id, secret)).status_code == 429


def test_sw4_1_refused_and_replayed_real_codes_take_no_write_lock(platform, monkeypatch, query):
    from bananawiki.core import sqlite as core_sqlite
    from bananawiki.hosting import limits

    monkeypatch.setattr(limits, "BAD_GRANT_PER_MINUTE", 3)
    client_id, secret = _credentials(platform)
    wiki = _client(platform, "10.0.0.5")
    code = _code(platform, platform["owner"], client_id)
    began = []
    transaction = core_sqlite.Session.transaction

    def counting(self):
        if not self.conn.in_transaction:
            began.append(1)
        return transaction(self)

    def send(form, times):
        return [wiki.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, **form},
                          headers=_basic(client_id, secret)).status_code for _ in range(times)]

    monkeypatch.setattr(core_sqlite.Session, "transaction", counting)
    # A real, unused code with the wrong redirect URI: never consumed, so it could be sent until it expires.
    assert send({"redirect_uri": "https://evil.example/cb"}, 50) == [400] * 3 + [429] * 47
    assert len(began) <= 3
    good = {"redirect_uri": "https://team.example/cb"}
    assert send(good, 1) == [200]
    tokens = "SELECT COUNT(*) AS n FROM hosting_oauth_access_tokens WHERE client_id = ?"
    assert query(tokens, (client_id,))[0]["n"] == 1
    began.clear()
    # The first replay revokes the token it gave; later ones have nothing to revoke and cost reads only.
    assert send(good, 50) == [429] * 50
    assert query(tokens, (client_id,))[0]["n"] == 0
    assert len(began) <= 1


# SW4-2: verifying a domain the wiki already answers on publishes no routes


def _active(portal, wiki):
    with portal.app_context():
        return domains.active_domain(wiki["id"])


class TestSW42:
    @pytest.fixture
    def portal(self, tmp_path):
        """The usual test portal, ready for custom domains (a CNAME target set)."""
        domains._addresses_cache.clear()
        return build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test"))

    def test_sw4_2_reverifying_an_active_domain_publishes_no_routes(self, portal, make_account, make_wiki, login,
                                                                     query, runtime):
        admin, owner = make_account("boss", admin=True), make_account("owner")
        boss = portal.test_client()
        login(boss, admin)
        boss.post("/admin/settings", data={"action": "save_custom_domains", "custom_domains_policy": "everyone"})
        wiki = make_wiki(owner)
        query("UPDATE instances SET status = 'running' WHERE id = ?", (wiki["id"],))
        runtime.domain_results["docs.example.org"] = DomainCheck(ownership=True, routing=True, dns_error=False)
        browser = portal.test_client()
        login(browser, owner)
        browser.post(f"/instances/{wiki['id']}/domain", data={"action": "claim", "domain": "docs.example.org"})
        before = len(runtime.called("sync_routes"))
        browser.post(f"/instances/{wiki['id']}/domain", data={"action": "verify"})
        assert _active(portal, wiki) == "docs.example.org"
        assert len(runtime.called("sync_routes")) == before + 1  # a first verification publishes the domain
        until = query("SELECT verified_until FROM instance_custom_domains", one=True)["verified_until"]
        query("UPDATE instance_custom_domains SET verified_until = datetime(verified_until, '-1 hour')")
        for number in range(5):
            assert browser.post(f"/instances/{wiki['id']}/domain", data={"action": "verify"},
                                environ_base={"REMOTE_ADDR": f"203.0.113.{number + 1}"}).status_code == 302
        assert len(runtime.called("sync_routes")) == before + 1
        assert query("SELECT verified_until FROM instance_custom_domains", one=True)["verified_until"] >= until
        # A lapsed proof coming back is published again.
        query("DELETE FROM hosting_rate_limit_hits")
        query("UPDATE instance_custom_domains SET verified_until = datetime('now', '-1 hour')")
        assert _active(portal, wiki) is None
        browser.post(f"/instances/{wiki['id']}/domain", data={"action": "verify"},
                     environ_base={"REMOTE_ADDR": "203.0.113.50"})
        assert _active(portal, wiki) == "docs.example.org"
        assert len(runtime.called("sync_routes")) == before + 2


# SW4-3: the account list and analytics of a paused wiki share a daily budget per wiki


def test_sw4_3_tasks_on_a_paused_wiki_have_a_daily_budget(portal, make_account, make_wiki, login, query, runtime,
                                                          monkeypatch):
    from bananawiki.hosting.routes import dashboard

    monkeypatch.setattr(dashboard, "PAUSED_TASKS_PER_DAY", 5, raising=False)
    owner = make_account("owner")
    wiki = make_wiki(owner)
    browser = portal.test_client()
    login(browser, owner)
    assert browser.post(f"/instances/{wiki['id']}/stop").status_code == 302
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "stopped"
    for _ in range(4):
        assert browser.get(f"/instances/{wiki['id']}").status_code == 200
        browser.get(f"/instances/{wiki['id']}/analytics?days=365")
    # Ageing the per-minute buckets does not refill the day's.
    query("UPDATE hosting_rate_limit_hits SET hit_at = datetime(hit_at, '-61 seconds') "
          "WHERE bucket != 'hosting_wiki_paused_tasks'")
    for _ in range(4):
        assert browser.get(f"/instances/{wiki['id']}").status_code == 200  # the page, without the list
        assert browser.get(f"/instances/{wiki['id']}/analytics").status_code == 302
    assert len(runtime.called("list_users")) + len(runtime.called("analytics")) == 5
    # A running wiki runs its tasks in its own container: the per-minute limits only.
    query("UPDATE instances SET status = 'running' WHERE id = ?", (wiki["id"],))
    assert browser.get(f"/instances/{wiki['id']}").status_code == 200
    assert browser.get(f"/instances/{wiki['id']}/analytics").status_code == 200
    assert len(runtime.called("list_users")) + len(runtime.called("analytics")) == 7


# SW4-4: in port mode a moderated account's wikis are not run


class TestSW44:
    @pytest.fixture
    def portal(self, tmp_path):
        return build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_MODE="port", BASE_DOMAIN=""))

    @pytest.mark.parametrize(("action", "form", "undo"), [
        ("suspend", {"suspend_duration": "permanent", "suspend_reason": "abuse"}, "unsuspend"),
        ("schedule-deletion", {"deletion_days": "7", "reason": "abuse"}, "cancel-deletion"),
    ])
    def test_sw4_4_a_moderated_accounts_wikis_go_offline_in_port_mode(self, portal, make_account, make_wiki, login,
                                                                      query, runtime, action, form, undo):
        admin, owner = make_account("boss", admin=True), make_account("owner")
        wiki = make_wiki(owner)
        tenant = next(iter(runtime.tenants.values()))
        assert portal.config["HOSTING"].hosting_mode == "port" and tenant.running
        boss = portal.test_client()
        login(boss, admin)
        assert boss.post(f"/admin/accounts/{owner['id']}/{action}", data=form).status_code == 302
        assert not tenant.running
        # The row stays running (the wiki comes back afterwards), but maintenance does not start it.
        assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "running"
        maintenance.run_once(portal)
        assert not tenant.running
        # Neither does a restart by an administrator (refused, see the next test).
        with portal.test_request_context("/"):
            from bananawiki.hosting import instances

            with pytest.raises(ServiceError):
                instances.restart(instances.get(wiki["id"]), actor_id=admin["id"])
        assert not tenant.running
        assert boss.post(f"/admin/accounts/{owner['id']}/{undo}").status_code == 302
        maintenance.run_once(portal)
        assert tenant.running


    def test_sw4_4_no_operation_brings_a_held_wiki_up(self, portal, make_account, make_wiki, login, query, runtime):
        from bananawiki.hosting import instances

        admin, owner = make_account("boss", admin=True), make_account("owner")
        wiki = make_wiki(owner)
        boss = portal.test_client()
        login(boss, admin)
        boss.post(f"/admin/accounts/{owner['id']}/schedule-deletion", data={"deletion_days": "7", "reason": "abuse"})
        tenant = next(iter(runtime.tenants.values()))
        assert not tenant.running
        before = len(runtime.called("start")) + len(runtime.called("restart"))
        with portal.test_request_context("/"):
            with pytest.raises(ServiceError):
                instances.restart(instances.get(wiki["id"]), actor_id=owner["id"])
            assert instances.apply_policy(instances.get(wiki["id"]), actor_id=admin["id"])
            instances.reset_content(instances.get(wiki["id"]), actor_id=admin["id"])
            instances.stop(instances.get(wiki["id"]), actor_id=admin["id"])
            instances.start(instances.get(wiki["id"]), actor_id=admin["id"])
        # The policy was applied and the content reset, but the container never came up.
        assert runtime.called("apply_limits") and runtime.called("reset_content")
        assert len(runtime.called("start")) + len(runtime.called("restart")) == before
        assert not tenant.running
        assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "running"
        assert boss.post(f"/admin/accounts/{owner['id']}/cancel-deletion").status_code == 302
        maintenance.run_once(portal)
        assert tenant.running

    def test_sw4_3_tasks_on_a_held_wiki_have_the_daily_budget(self, portal, make_account, make_wiki, login, query,
                                                               runtime, monkeypatch):
        from bananawiki.hosting.routes import dashboard

        monkeypatch.setattr(dashboard, "PAUSED_TASKS_PER_DAY", 3, raising=False)
        admin, owner, friend = make_account("boss", admin=True), make_account("owner"), make_account("friend")
        wiki = make_wiki(owner)
        owners = portal.test_client()
        login(owners, owner)
        owners.post(f"/instances/{wiki['id']}/collaborators/add",
                    data={"username": "friend", "role": "custom", "permissions": ["view", "analytics"]})
        boss = portal.test_client()
        login(boss, admin)
        boss.post(f"/admin/accounts/{owner['id']}/suspend",
                  data={"suspend_duration": "permanent", "suspend_reason": "abuse"})
        assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "running"
        assert not next(iter(runtime.tenants.values())).running  # held: marked running, container down
        colleague = portal.test_client()
        login(colleague, friend)
        for _ in range(3):
            boss.get(f"/instances/{wiki['id']}")
            colleague.get(f"/instances/{wiki['id']}/analytics")
            query("UPDATE hosting_rate_limit_hits SET hit_at = datetime(hit_at, '-61 seconds') "
                  "WHERE bucket != 'hosting_wiki_paused_tasks'")
        assert len(runtime.called("list_users")) + len(runtime.called("analytics")) == 3

    def test_sw4_4_route_publication_sends_no_stop_to_a_wiki_that_is_not_up(self, portal, make_account, make_wiki,
                                                                           login, query, runtime):
        from bananawiki.hosting import instances

        admin, owner = make_account("boss", admin=True), make_account("owner")
        wiki = make_wiki(owner)
        boss = portal.test_client()
        login(boss, admin)
        boss.post(f"/admin/accounts/{owner['id']}/suspend",
                  data={"suspend_duration": "permanent", "suspend_reason": "abuse"})
        runtime.tenants.clear()  # its data is gone: the runtime answers "missing"
        stops = len(runtime.called("stop"))
        with portal.test_request_context("/"):
            for _ in range(3):
                instances.sync_routes()
        assert len(runtime.called("stop")) == stops
        assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "running"


# SW4-5: collaborator and domain churn is bounded per wiki; taking access away never is


def _events(query, prefix):
    return query("SELECT COUNT(*) AS n FROM hosting_events WHERE action LIKE ?", (prefix + "%",), one=True)["n"]


def test_sw4_5_collaborator_churn_is_bounded_per_wiki(portal, make_account, make_wiki, login, query, monkeypatch):
    from bananawiki.hosting import collaborators

    monkeypatch.setattr(collaborators, "MAX_GRANTS_PER_DAY", 4, raising=False)
    owner, friend = make_account("owner"), make_account("friend")
    wiki = make_wiki(owner)
    browser = portal.test_client()
    login(browser, owner)
    base = f"/instances/{wiki['id']}/collaborators"
    for number in range(6):
        addr = {"REMOTE_ADDR": f"203.0.113.{number + 1}"}
        browser.post(f"{base}/add", environ_base=addr, data={"username": "friend", "role": "custom",
                                                            "permissions": ["view"]})
        # The same access again changes nothing and records nothing.
        browser.post(f"{base}/{friend['id']}/update", environ_base=addr, data={"role": "custom",
                                                                             "permissions": ["view"]})
        browser.post(f"{base}/{friend['id']}/remove", environ_base=addr)
    assert _events(query, "collaborator.added") == 4
    assert _events(query, "collaborator.updated") == 0
    assert _events(query, "collaborator.removed") == 4
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators", one=True)["n"] == 0
    # Past the budget access can still be taken away (a new day, with a budget of one grant).
    query("DELETE FROM hosting_rate_limit_hits")
    monkeypatch.setattr(collaborators, "MAX_GRANTS_PER_DAY", 1, raising=False)
    browser.post(f"{base}/add", data={"username": "friend", "role": "full_access"})
    for _ in range(3):
        browser.post(f"{base}/{friend['id']}/update", data={"role": "custom", "permissions": ["view", "download"]})
    assert _events(query, "collaborator.updated") == 1  # the reduction; the next two changed nothing
    for _ in range(3):
        assert browser.post(f"{base}/{friend['id']}/update",
                            data={"role": "custom", "permissions": ["view", "download", "analytics"]}).status_code == 302
    member = query("SELECT permissions FROM instance_collaborators", one=True)
    assert "analytics" not in member["permissions"]  # widening is a grant, refused
    assert browser.post(f"{base}/{friend['id']}/update", data={"role": "custom", "permissions": ["view"]})
    assert query("SELECT permissions FROM instance_collaborators", one=True)["permissions"] == '["view"]'
    assert browser.post(f"{base}/{friend['id']}/remove").status_code == 302
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators", one=True)["n"] == 0


def test_sw4_5_collaborator_update_compares_effective_access(portal, make_account, make_wiki, login, query,
                                                              monkeypatch):
    from bananawiki.hosting import collaborators

    monkeypatch.setattr(collaborators, "MAX_GRANTS_PER_DAY", 1, raising=False)
    owner, friend = make_account("owner"), make_account("friend")
    wiki = make_wiki(owner)
    browser = portal.test_client()
    login(browser, owner)
    base = f"/instances/{wiki['id']}/collaborators"
    browser.post(f"{base}/add", data={"username": "friend", "role": "custom", "permissions": ["view"]})
    # "view" is always included: [] and ["view"] (or the same permissions in another order) are the same access.
    for number in range(10):
        perms = [[], ["view"]][number % 2]
        assert browser.post(f"{base}/{friend['id']}/update", data={"role": "custom", "permissions": perms},
                            environ_base={"REMOTE_ADDR": f"203.0.113.{number + 1}"}).status_code == 302
    assert _events(query, "collaborator.updated") == 0


class TestSW45Domains:
    @pytest.fixture
    def portal(self, tmp_path):
        domains._addresses_cache.clear()
        return build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test"))

    def test_sw4_5_domain_claim_churn_is_bounded_per_wiki(self, portal, make_account, make_wiki, login, query,
                                                           monkeypatch):
        monkeypatch.setattr(domains, "MAX_CLAIMS_PER_DAY", 3, raising=False)
        admin, owner = make_account("boss", admin=True), make_account("owner")
        boss = portal.test_client()
        login(boss, admin)
        boss.post("/admin/settings", data={"action": "save_custom_domains", "custom_domains_policy": "everyone"})
        wiki = make_wiki(owner)
        query("UPDATE instances SET status = 'running' WHERE id = ?", (wiki["id"],))
        browser = portal.test_client()
        login(browser, owner)
        for number in range(6):
            addr = {"REMOTE_ADDR": f"203.0.113.{number + 1}"}
            browser.post(f"/instances/{wiki['id']}/domain", environ_base=addr,
                         data={"action": "claim", "domain": f"d{number}.example.org"})
            browser.post(f"/instances/{wiki['id']}/domain", environ_base=addr, data={"action": "remove"})
        assert _events(query, "domain.claimed") == 3
        assert _events(query, "domain.removed") == 3  # each removal entry follows a counted claim
        assert query("SELECT COUNT(*) AS n FROM instance_custom_domains", one=True)["n"] == 0

    def test_sw4_5_removing_no_domain_records_nothing(self, portal, make_account, make_wiki, login, query):
        admin, owner = make_account("boss", admin=True), make_account("owner")
        boss = portal.test_client()
        login(boss, admin)
        boss.post("/admin/settings", data={"action": "save_custom_domains", "custom_domains_policy": "everyone"})
        wiki = make_wiki(owner)
        query("UPDATE instances SET status = 'running' WHERE id = ?", (wiki["id"],))
        browser = portal.test_client()
        login(browser, owner)
        for number in range(10):
            response = browser.post(f"/instances/{wiki['id']}/domain", data={"action": "remove"},
                                    environ_base={"REMOTE_ADDR": f"203.0.113.{number + 1}"})
            assert response.status_code == 302
        assert _events(query, "domain.removed") == 0
