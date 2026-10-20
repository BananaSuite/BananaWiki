"""Who keeps power over a wiki: transfers, administrators without two-step
verification, lapsed custom domains and second-factor throttling."""

from __future__ import annotations

import re

import pytest

from bananawiki.hosting import mfa
from bananawiki.hosting.runtime import DomainCheck

from .hosting_support import PASSWORD, build_portal, portal_environ


def _settings(query, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    query(f"UPDATE hosting_settings SET {assignments} WHERE id = 1", tuple(values.values()))


def _client(portal, address: str):
    client = portal.test_client()
    client.environ_base["REMOTE_ADDR"] = address
    return client


# Ownership transfer


def _transfer(portal, make_account, make_wiki, login, query, **accept):
    owner, recipient = make_account(), make_account()
    wiki = make_wiki(owner, "handover")
    owner_client, recipient_client = _client(portal, "10.0.0.1"), _client(portal, "10.0.0.2")
    login(owner_client, owner)
    login(recipient_client, recipient)
    owner_client.post(f"/instances/{wiki['id']}/transfer", data={"username": recipient["username"]})
    transfer = query("SELECT id FROM instance_ownership_transfers", one=True)
    response = recipient_client.post(f"/transfers/{transfer['id']}/accept", data=accept)
    assert response.status_code == 302
    assert query("SELECT account_id FROM instances", one=True)["account_id"] == recipient["id"]
    return owner, owner_client, wiki


def test_accepting_a_transfer_removes_the_previous_owner_by_default(portal, make_account, make_wiki, login, query):
    owner, owner_client, wiki = _transfer(portal, make_account, make_wiki, login, query)
    assert query("SELECT * FROM instance_collaborators", one=True) is None
    assert owner_client.get(f"/instances/{wiki['id']}").status_code == 404
    owner_client.post(f"/instances/{wiki['id']}/reset-password")
    owner_client.post(f"/instances/{wiki['id']}/terminate")
    assert query("SELECT status FROM instances", one=True)["status"] == "running"


def test_the_recipient_may_keep_the_previous_owner_as_a_viewer(portal, make_account, make_wiki, login, query):
    owner, owner_client, wiki = _transfer(portal, make_account, make_wiki, login, query, previous_owner="view")
    row = query("SELECT role, permissions FROM instance_collaborators WHERE account_id = ?", (owner["id"],), one=True)
    assert row == {"role": "custom", "permissions": '["view"]'}
    assert owner_client.get(f"/instances/{wiki['id']}").status_code == 200
    assert owner_client.post(f"/instances/{wiki['id']}/reset-password").status_code == 404
    owner_client.post(f"/instances/{wiki['id']}/terminate")
    assert query("SELECT status FROM instances", one=True)["status"] == "running"


def test_full_access_cannot_be_chosen_for_the_previous_owner(portal, make_account, make_wiki, login, query):
    owner, _client_, _wiki = _transfer(portal, make_account, make_wiki, login, query, previous_owner="full_access")
    assert query("SELECT * FROM instance_collaborators", one=True) is None


def test_the_transfer_offer_says_what_happens_to_the_previous_owner(portal, make_account, make_wiki, login, query):
    owner, recipient = make_account(), make_account()
    wiki = make_wiki(owner, "explained")
    owner_client, recipient_client = _client(portal, "10.0.0.1"), _client(portal, "10.0.0.2")
    login(owner_client, owner)
    login(recipient_client, recipient)
    assert "lose access" in owner_client.get(f"/instances/{wiki['id']}/collaborators").get_data(as_text=True)
    owner_client.post(f"/instances/{wiki['id']}/transfer", data={"username": recipient["username"]})
    page = recipient_client.get("/dashboard").get_data(as_text=True)
    assert 'name="previous_owner"' in page and 'value="view"' in page


# Administrators without two-step verification


def test_an_admin_without_required_mfa_has_no_power_over_tenant_wikis(portal, make_account, make_wiki, login,
                                                                      query):
    from bananawiki.hosting import collaborators, instances
    from bananawiki.hosting.db import connection_scope

    owner, admin = make_account(), make_account(admin=True)
    wiki = make_wiki(owner, "tenant")
    _settings(query, admin_mfa_required=1)
    client = _client(portal, "10.0.0.3")
    login(client, admin)
    for method, path in (("get", f"/instances/{wiki['id']}"), ("post", f"/instances/{wiki['id']}/reset-password"),
                         ("post", f"/instances/{wiki['id']}/terminate"), ("get", "/dashboard")):
        response = getattr(client, method)(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/account/mfa"), path
    assert query("SELECT status FROM instances", one=True)["status"] == "running"
    assert client.get("/account/mfa").status_code == 200
    with portal.test_request_context("/"), connection_scope():
        admin_row = query("SELECT * FROM accounts WHERE id = ?", (admin["id"],), one=True)
        assert collaborators.permissions_of(instances.get(wiki["id"]), admin_row) == set()


# Lapsed custom domains


@pytest.fixture
def domain_portal(tmp_path):
    environ = portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test")
    return build_portal(tmp_path, environ=environ)


def _lapsed_binding(portal):
    """A wiki whose proof for docs.example.org lapsed long ago, and another wiki that wants the name."""
    from bananawiki.hosting import accounts, instances
    from bananawiki.hosting.db import connection_scope

    with portal.test_request_context("/"), connection_scope() as session:
        old_owner = accounts.create("previous", PASSWORD)
        new_owner = accounts.create("buyer", PASSWORD)
        old = instances.create(old_owner, "previous-wiki")[0]
        new = instances.create(new_owner, "buyer-wiki")[0]
        session.execute("UPDATE instances SET custom_domain_allowed = 1")
        session.execute("INSERT INTO instance_custom_domains (domain, instance_id, verification_token, created_at, "
                        "verified_at, verified_until) VALUES ('docs.example.org', ?, 'old-token', "
                        "'2020-01-01 00:00:00', '2020-01-01 00:00:00', datetime('now', '-8 days'))", (old["id"],))
    return old, new, new_owner


def _dns(portal, **published: str) -> None:
    """Public DNS: the TXT token published for each domain (routing to the platform is fine)."""
    def check(domain: str, token: str) -> DomainCheck:
        return DomainCheck(ownership=published.get(domain.replace(".", "_")) == token, routing=True, dns_error=False)

    portal.extensions["bananawiki.hosting.runtime"].check_domain = check


def test_a_recently_lapsed_proof_still_holds_the_domain(domain_portal):
    from bananawiki.hosting import domains, instances
    from bananawiki.hosting.db import connection_scope
    from bananawiki.hosting.errors import ServiceError

    old, new, new_owner = _lapsed_binding(domain_portal)
    with domain_portal.test_request_context("/"), connection_scope() as session:
        session.execute("UPDATE instance_custom_domains SET verified_until = datetime('now', '-2 days')")
        with pytest.raises(ServiceError) as caught:
            domains.claim(instances.get(new["id"]), "docs.example.org", new_owner["id"])
    assert caught.value.key == "hosting.domains.taken", "a proof that lapsed recently may still be renewed"


def test_a_long_lapsed_domain_changes_wiki_only_once_the_claimant_proves_it(domain_portal):
    from bananawiki.hosting import domains, instances
    from bananawiki.hosting.db import connection_scope
    from bananawiki.hosting.errors import ServiceError

    old, new, new_owner = _lapsed_binding(domain_portal)
    with domain_portal.test_request_context("/"), connection_scope() as session:
        claimed = domains.claim(instances.get(new["id"]), "docs.example.org", new_owner["id"])
        assert claimed["domain"] == "docs.example.org" and claimed["verified_at"] is None
        token = claimed["verification_token"]
        assert token != "old-token"
        assert domains.binding(old["id"])["verification_token"] == "old-token", "claiming alone releases nothing"
        _dns(domain_portal, docs_example_org="unrelated")
        with pytest.raises(ServiceError):
            domains.verify(instances.get(new["id"]), new_owner["id"])
        assert domains.binding(old["id"]) is not None, "a failed proof releases nothing either"
        _dns(domain_portal, docs_example_org=token)
        verified = domains.verify(instances.get(new["id"]), new_owner["id"])
        assert verified["instance_id"] == new["id"] and verified["verified_at"]
        assert domains.binding(old["id"]) is None
        lapsed = session.one("SELECT actor_id, reason FROM hosting_events WHERE action = 'domain.lapsed'")
    assert lapsed == {"actor_id": None, "reason": "docs.example.org"}, "nobody but the platform released it"


def test_an_owner_whose_dns_came_back_keeps_the_domain(domain_portal):
    from bananawiki.hosting import domains, instances
    from bananawiki.hosting.db import connection_scope
    from bananawiki.hosting.errors import ServiceError

    old, new, new_owner = _lapsed_binding(domain_portal)
    with domain_portal.test_request_context("/"), connection_scope():
        claimed = domains.claim(instances.get(new["id"]), "docs.example.org", new_owner["id"])
        _dns(domain_portal, docs_example_org="old-token")  # the DNS migration finished
        assert domains.refresh() == 1
        assert domains.binding(old["id"])["verified_until"] > claimed["created_at"]
        _dns(domain_portal, docs_example_org=claimed["verification_token"])
        with pytest.raises(ServiceError) as caught:
            domains.verify(instances.get(new["id"]), new_owner["id"])
        assert caught.value.key == "hosting.domains.taken"
        assert domains.binding(old["id"]) is not None and domains.binding(new["id"]) is None


# Second-factor throttling


def _enrol(portal, client) -> list[str]:
    html = client.get("/account/mfa").get_data(as_text=True)
    # The page shows the key in groups of four.
    secret = re.search(r'class="secret-box[^"]*">([A-Z2-7 ]{32,40})<', html).group(1).replace(" ", "")
    with portal.app_context():
        code = mfa.totp_code(secret)
    response = client.post("/account/mfa", data={"password": PASSWORD, "code": code})
    return list(dict.fromkeys(re.findall(r"\b[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}\b", response.get_data(as_text=True))))


def test_someone_holding_the_password_cannot_lock_the_owner_out_of_the_second_step(portal, make_account, login):
    user = make_account()
    owner = _client(portal, "10.0.1.1")
    login(owner, user)
    codes = _enrol(portal, owner)
    owner.post("/logout")
    for number in range(4):  # several addresses, so only the per-account buckets apply
        stranger = _client(portal, f"10.0.2.{number}")
        assert stranger.post("/login", data={"username": user["username"], "password": PASSWORD}).status_code == 302
        for _attempt in range(3):
            stranger.post("/login/mfa", data={"code": "000000"})
    assert stranger.post("/login/mfa", data={"code": "000000"}).status_code == 429
    response = owner.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.headers["Location"].endswith("/login/mfa")
    assert owner.post("/login/mfa", data={"code": codes[0]}).status_code == 302
    assert owner.get("/dashboard").status_code == 200


def _wrong_codes_checked(client, limit: int = 40) -> int:
    """Wrong codes the portal checks for one pending sign-in before refusing (429)."""
    for number in range(limit):
        if client.post("/login/mfa", data={"code": "000000"}).status_code == 429:
            return number
    return limit


def _second_step(client, user):
    response = client.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.headers["Location"].endswith("/login/mfa")
    return client


def test_device_cookies_from_before_two_step_verification_give_no_extra_guesses(portal, make_account, login):
    """Someone who knew the password before the owner turned two-step verification on kept
    every browser cookie they collected; none of them is a browser that passed both steps."""
    user = make_account()
    collected = []
    for number in range(3):
        browser = _client(portal, f"10.0.3.{number}")
        login(browser, user)
        collected.append(browser.get_cookie("bwh_device").value)
    owner = _client(portal, "10.0.4.1")
    login(owner, user)
    _enrol(portal, owner)
    attacker = _second_step(_client(portal, "10.0.5.1"), user)
    checked = 0
    for number, cookie in enumerate(collected):
        attacker.environ_base["REMOTE_ADDR"] = f"10.0.5.{number}"  # and as many addresses
        attacker.set_cookie("bwh_device", cookie)
        checked += _wrong_codes_checked(attacker, 10)
    assert checked == 10, "the old cookies share the budget of browsers the account does not know"


def test_every_browser_together_has_a_ceiling_on_codes(portal, make_account, login):
    from bananawiki.hosting.routes import auth as auth_routes

    user = make_account()
    owner = _client(portal, "10.0.6.1")
    login(owner, user)
    codes = _enrol(portal, owner)
    browsers = []
    for number in range(4):  # browsers that passed both steps: each has a budget of its own
        browser = _second_step(_client(portal, f"10.0.7.{number}"), user)
        assert browser.post("/login/mfa", data={"code": codes[number]}).status_code == 302
        browser.post("/logout")
        browsers.append(_second_step(browser, user))
    checked = sum(_wrong_codes_checked(browser, 10) for browser in browsers)
    assert checked == auth_routes.MFA_ACCOUNT_LIMIT < 4 * 10


def test_turning_two_step_verification_on_keeps_the_owner_s_browser_known(portal, make_account, login):
    user = make_account()
    owner = _client(portal, "10.0.8.1")
    login(owner, user)
    _enrol(portal, owner)
    stranger = _second_step(_client(portal, "10.0.9.1"), user)  # knows the password, not the code
    assert _wrong_codes_checked(stranger) == 10
    owner.post("/logout")
    _second_step(owner, user)
    assert owner.post("/login/mfa", data={"code": "000000"}).status_code == 200, "the owner's browser is known"
