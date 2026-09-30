"""Feature requests, custom domains and account merges."""

from __future__ import annotations

import pytest

from bananawiki.hosting.runtime import DomainCheck

from .hosting_support import build_portal, portal_environ


def test_feature_request_waits_for_an_admin(portal, make_account, make_wiki, login, query):
    owner, admin = make_account(), make_account(admin=True)
    wiki = make_wiki(owner, "features")
    owner_client, admin_client = portal.test_client(), portal.test_client()
    login(owner_client, owner)
    login(admin_client, admin)
    owner_client.post(f"/instances/{wiki['id']}/features/public_access/requests", data={"reason": "Our documentation should be open to everyone."})
    request = query("SELECT id, status FROM instance_feature_requests", one=True)
    assert request["status"] == "pending"
    owner_client.post(f"/instances/{wiki['id']}/features/public_access/requests", data={"reason": "Asking a second time for the same thing."})
    assert query("SELECT COUNT(*) AS n FROM instance_feature_requests", one=True)["n"] == 1
    assert "Our documentation should be open" in admin_client.get("/admin").get_data(as_text=True)
    admin_client.post(f"/admin/feature-requests/{request['id']}/approve", data={"review_note": ""})
    assert query("SELECT public_wiki_allowed FROM instances", one=True)["public_wiki_allowed"] == 1
    assert owner_client.get(f"/instances/{wiki['id']}").status_code == 200


def test_unknown_features_are_refused(web, make_account, make_wiki, login, query):
    owner = make_account()
    wiki = make_wiki(owner)
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/features/root_shell/requests", data={"reason": "Please give me a root shell on the host."})
    assert query("SELECT COUNT(*) AS n FROM instance_feature_requests", one=True)["n"] == 0


@pytest.fixture
def domain_portal(tmp_path):
    environ = portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test")
    return build_portal(tmp_path, environ=environ)


def test_custom_domain_claim_and_verify(domain_portal, query):
    app = domain_portal
    runtime = app.extensions["bananawiki.hosting.runtime"]
    from bananawiki.hosting.db import connection_scope

    with app.test_request_context("/"), connection_scope():
        from bananawiki.hosting import accounts, instances

        owner = accounts.create("domainer", "correct horse 42")
        wiki = instances.create(owner, "branded")[0]
    client = app.test_client()
    client.post("/login", data={"username": "domainer", "password": "correct horse 42"})
    client.post(f"/instances/{wiki['id']}/domain", data={"action": "claim", "domain": "docs.example.org"})
    with app.app_context(), connection_scope() as session:
        assert session.scalar("SELECT COUNT(*) FROM instance_custom_domains") == 0, "not allowed yet"
        session.execute("UPDATE instances SET custom_domain_allowed = 1")
    client.post(f"/instances/{wiki['id']}/domain", data={"action": "claim", "domain": "docs.example.org"})
    client.post(f"/instances/{wiki['id']}/domain", data={"action": "verify"})
    with app.app_context(), connection_scope() as session:
        assert session.scalar("SELECT verified_at FROM instance_custom_domains") is None
    runtime.domain_results["docs.example.org"] = DomainCheck(ownership=True, routing=True, dns_error=False)
    client.post(f"/instances/{wiki['id']}/domain", data={"action": "verify"})
    with app.app_context(), connection_scope() as session:
        assert session.scalar("SELECT verified_at FROM instance_custom_domains")
    assert client.get("/internal/domains/authorize?domain=docs.example.org").status_code == 200
    assert "docs.example.org" in client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)


def test_proxied_custom_domain_is_verified_with_cloudflare_advice(domain_portal):
    app = domain_portal
    runtime = app.extensions["bananawiki.hosting.runtime"]
    from bananawiki.hosting.db import connection_scope

    with app.test_request_context("/"), connection_scope() as session:
        from bananawiki.hosting import accounts, instances

        accounts.create("orange", "correct horse 42")
        owner = accounts.create("cloudy", "correct horse 42")
        wiki = instances.create(owner, "clouded")[0]
        session.execute("UPDATE instances SET custom_domain_allowed = 1")
    client = app.test_client()
    client.post("/login", data={"username": "cloudy", "password": "correct horse 42"})
    client.post(f"/instances/{wiki['id']}/domain", data={"action": "claim", "domain": "docs.example.org"})
    page = client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)
    assert "Full (strict)" in page, "the Cloudflare hint is shown before verifying"
    runtime.domain_results["docs.example.org"] = DomainCheck(ownership=True, routing=True, proxied=True)
    response = client.post(f"/instances/{wiki['id']}/domain", data={"action": "verify"}, follow_redirects=True)
    text = response.get_data(as_text=True)
    assert "Domain verified" in text and "proxied by Cloudflare" in text
    with app.app_context(), connection_scope() as session:
        assert session.scalar("SELECT verified_at FROM instance_custom_domains")


def test_www_of_the_base_domain_gets_a_certificate(domain_portal):
    client = domain_portal.test_client()
    assert client.get("/internal/domains/authorize?domain=www.wiki.test").status_code == 200
    assert client.get("/internal/domains/authorize?domain=www.other.test").status_code == 403


def test_platform_domains_cannot_be_claimed(domain_portal):
    app = domain_portal
    from bananawiki.hosting.db import connection_scope

    with app.test_request_context("/"), connection_scope() as session:
        from bananawiki.hosting import accounts, domains, instances
        from bananawiki.hosting.errors import ServiceError

        owner = accounts.create("sneaky", "correct horse 42")
        wiki = instances.create(owner, "sneaky-wiki")[0]
        session.execute("UPDATE instances SET custom_domain_allowed = 1")
        with pytest.raises(ServiceError) as caught:
            domains.claim(instances.get(wiki["id"]), "other-hosting.wiki.test", owner["id"])
        assert caught.value.key == "hosting.domains.platform_domain"


def test_account_merge_moves_wikis_after_both_approve(portal, make_account, make_wiki, login, query):
    source, target, admin = make_account(), make_account(), make_account(admin=True)
    make_wiki(source, "moving")
    source_client, target_client = portal.test_client(), portal.test_client()
    login(source_client, source)
    login(target_client, target)
    source_client.post("/account/merge-request", data={"target_username": target["username"], "reason": "same person"})
    merge = query("SELECT id, status FROM hosting_account_merge_requests", one=True)
    assert merge["status"] == "pending"
    target_client.post(f"/account/merge/approve/{merge['id']}")
    assert query("SELECT status FROM hosting_account_merge_requests", one=True)["status"] == "approved"
    assert query("SELECT account_id FROM instances", one=True)["account_id"] == source["id"]
    admin_client = portal.test_client()
    login(admin_client, admin)
    admin_client.post(f"/admin/merge-requests/{merge['id']}/execute")
    assert query("SELECT account_id FROM instances", one=True)["account_id"] == target["id"]
    assert query("SELECT suspended FROM accounts WHERE id = ?", (source["id"],), one=True)["suspended"] == 1


def test_merge_request_to_self_is_refused(web, make_account, login, query):
    user = make_account()
    login(web, user)
    web.post("/account/merge-request", data={"target_username": user["username"]})
    assert query("SELECT COUNT(*) AS n FROM hosting_account_merge_requests", one=True)["n"] == 0
