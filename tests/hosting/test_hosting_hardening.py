"""Regression tests for the portal's security and stability review."""

from __future__ import annotations

import pytest

from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.runtime import DomainCheck

from .hosting_support import PASSWORD, build_portal, portal_environ


def test_analytics_run_at_most_a_few_tasks_per_wiki_and_minute(portal, runtime, web, make_account, make_wiki, login):
    from bananawiki.hosting.routes import dashboard

    owner = make_account()
    wiki = make_wiki(owner)
    login(web, owner)
    answers = [web.get(f"/instances/{wiki['id']}/analytics").status_code
               for _ in range(dashboard.ANALYTICS_PER_MINUTE + 3)]
    assert answers[:dashboard.ANALYTICS_PER_MINUTE] == [200] * dashboard.ANALYTICS_PER_MINUTE
    assert set(answers[dashboard.ANALYTICS_PER_MINUTE:]) == {302}, "then back to the wiki's page"
    assert len(runtime.called("analytics")) == dashboard.ANALYTICS_PER_MINUTE


def test_the_wiki_page_lists_accounts_a_few_times_a_minute_at_most(portal, runtime, web, make_account, make_wiki,
                                                                    login):
    from bananawiki.hosting.routes import dashboard

    owner = make_account()
    wiki = make_wiki(owner)
    login(web, owner)
    for _ in range(dashboard.USER_LISTS_PER_MINUTE + 5):
        assert web.get(f"/instances/{wiki['id']}").status_code == 200
    assert len(runtime.called("list_users")) == dashboard.USER_LISTS_PER_MINUTE


@pytest.fixture
def domain_portal(tmp_path):
    return build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test"))


def test_an_unproven_claim_cannot_keep_the_real_owner_of_a_domain_out(domain_portal):
    from bananawiki.hosting import accounts, domains, instances

    with domain_portal.test_request_context("/"), connection_scope() as session:
        squatter, owner = accounts.create("squatter", PASSWORD), accounts.create("realowner", PASSWORD)
        squatting = instances.create(squatter, "squat")[0]
        real = instances.create(owner, "real")[0]
        session.execute("UPDATE instances SET custom_domain_allowed = 1")
        domains.claim(instances.get(squatting["id"]), "docs.example.org", squatter["id"])
        claimed = domains.claim(instances.get(real["id"]), "docs.example.org", owner["id"])
        assert claimed.get("takeover"), "the real owner may claim it too"
        token = claimed["verification_token"]
        domain_portal.extensions["bananawiki.hosting.runtime"].check_domain = (
            lambda domain, proof: DomainCheck(ownership=proof == token, routing=True, dns_error=False))
        verified = domains.verify(instances.get(real["id"]), owner["id"])
        assert verified["instance_id"] == real["id"] and verified["verified_at"]
        assert domains.binding(squatting["id"]) is None
        from bananawiki.hosting.errors import ServiceError

        with pytest.raises(ServiceError) as caught:
            domains.claim(instances.get(squatting["id"]), "docs.example.org", squatter["id"])
        assert caught.value.key == "hosting.domains.taken", "a proven domain stays with its owner"


def test_no_wiki_gets_the_portals_own_address(tmp_path):
    from bananawiki.hosting import accounts, instances
    from bananawiki.hosting.errors import ServiceError

    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, PORTAL_DOMAIN="app-hosting.wiki.test"))
    with portal.test_request_context("/"), connection_scope():
        owner = accounts.create("someone", PASSWORD)
        with pytest.raises(ServiceError) as caught:
            instances.create(owner, "app")
        assert caught.value.key == "hosting.slug.reserved"
        assert instances.create(owner, "apps")[0]


def test_a_verification_link_is_only_issued_for_the_address_it_is_mailed_to(portal, make_account, query):
    from bananawiki.hosting import accounts

    account = make_account(email="first@example.org")
    query("UPDATE accounts SET email = 'second@example.org' WHERE id = ?", (account["id"],))
    with portal.app_context(), connection_scope():
        assert accounts.issue_verification(account) == "", "the address changed since the account was read"
        assert accounts.issue_verification(accounts.get(account["id"]))
    row = query("SELECT email_verification_token_hash FROM accounts WHERE id = ?", (account["id"],), one=True)
    assert row["email_verification_token_hash"]


def test_keeping_the_current_session_never_brings_back_a_revoked_one(portal, web, make_account, login, query):
    from flask import g

    from bananawiki.hosting import auth

    account = make_account()
    login(web, account)
    (row,) = query("SELECT id FROM hosting_account_sessions WHERE account_id = ?", (account["id"],))
    query("UPDATE hosting_account_sessions SET revoked_at = '2026-01-01 00:00:00' WHERE id = ?", (row["id"],))
    with portal.test_request_context("/"), connection_scope():
        g.auth_session = {"id": row["id"]}
        auth.keep_current_session(account["id"], 99)
    assert query("SELECT revoked_at FROM hosting_account_sessions WHERE id = ?", (row["id"],), one=True)[
        "revoked_at"] == "2026-01-01 00:00:00"


def test_changing_the_password_keeps_this_browser_signed_in(web, make_account, login):
    login(web, make_account())
    response = web.post("/account/change-password", data={
        "current_password": PASSWORD, "new_password": "another long passphrase 7",
        "confirm_new_password": "another long passphrase 7"})
    assert response.status_code == 302
    assert web.get("/account").status_code == 200


@pytest.mark.parametrize("path", ["/notices/99999999999999999999999/dismiss",
                                  "/transfers/99999999999999999999999/accept"])
def test_an_id_too_large_for_the_database_is_a_bad_request(tmp_path, make_account, path):
    portal = build_portal(tmp_path / "csrf", csrf=True)
    from .hosting_support import csrf_token

    with portal.app_context(), connection_scope():
        from bananawiki.hosting import accounts

        accounts.create("bigint", PASSWORD)
    client = portal.test_client()
    client.post("/login", data={"username": "bigint", "password": PASSWORD, "csrf_token": csrf_token(client)})
    assert client.post(path, data={"csrf_token": csrf_token(client, "/account")}).status_code == 400


def test_suspending_an_account_republishes_the_routes_at_once(portal, runtime, web, make_account, make_wiki, login):
    admin, owner = make_account(admin=True), make_account()
    make_wiki(owner)
    login(web, admin)
    before = len(runtime.called("sync_routes"))
    web.post(f"/admin/accounts/{owner['id']}/suspend", data={"duration": "permanent", "notify_user": "0"})
    assert len(runtime.called("sync_routes")) > before


def test_notices_to_other_people_are_charged_to_the_account_that_acted(portal, web, make_account, make_wiki,
                                                                        login, query):
    from bananawiki.hosting import email

    owner = make_account(email="owner@example.org")
    friend, heir = make_account(email="friend@example.org"), make_account(email="heir@example.org")
    wiki = make_wiki(owner)
    login(web, owner)
    with email.capture_outbox() as outbox:
        web.post(f"/instances/{wiki['id']}/collaborators/add", data={"username": friend["username"],
                                                                      "role": "full_access"})
        web.post(f"/instances/{wiki['id']}/transfer", data={"username": heir["username"]})
    assert {message.to for message in outbox} == {"friend@example.org", "heir@example.org"}
    charged = query("SELECT kind, account_id FROM hosting_email_log ORDER BY id")
    assert [(row["kind"], row["account_id"]) for row in charged] == [
        ("collaborator_added", owner["id"]), ("transfer_offered", owner["id"])], \
        "adding and removing people cannot spend the platform's budget for free"


def test_only_a_confirmed_old_address_hears_that_it_was_replaced(portal, web, make_account, login, query):
    from bananawiki.hosting import email

    account = make_account(email="typo@example.org")
    login(web, account)
    with email.capture_outbox() as outbox:
        web.post("/account", data={"username": account["username"], "email": "stranger@example.net",
                                   "current_password": PASSWORD})
    assert "typo@example.org" not in {message.to for message in outbox}, "an unconfirmed address is never mailed"
    query("UPDATE accounts SET email_verified_at = '2026-01-01 00:00:00' WHERE id = ?", (account["id"],))
    with email.capture_outbox() as outbox:
        web.post("/account", data={"username": account["username"], "email": "settled@example.net",
                                   "current_password": PASSWORD})
    assert "stranger@example.net" in {message.to for message in outbox}


def test_only_the_owner_offers_a_wiki_to_someone(portal, web, make_account, make_wiki, login, query):
    admin, owner, other = make_account(admin=True), make_account(), make_account()
    wiki = make_wiki(owner)
    login(web, admin)
    assert web.post(f"/instances/{wiki['id']}/transfer", data={"username": other["username"]}).status_code == 403
    assert query("SELECT COUNT(*) AS n FROM instance_ownership_transfers", one=True)["n"] == 0
