"""Custom domain ownership and tenant routing boundaries."""

from datetime import datetime, timedelta, timezone

import dns.resolver
import pytest

from hosting import config, db, domains
from hosting.app import create_hosting_app
from hosting.db._events import list_events
from helpers._passwords import generate_password_hash


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    monkeypatch.setattr(config, "BASE_DOMAIN", "platform.example.org")
    monkeypatch.setattr(config, "EFFECTIVE_PORTAL_DOMAIN", "portal.platform.example.org")
    monkeypatch.setattr(config, "HOSTING_MODE", "subdomain")
    monkeypatch.setattr(config, "INSTANCE_URL_SUFFIX", "hosting")
    monkeypatch.setattr(config, "HOSTING_CUSTOM_DOMAIN_TARGET", "domains.platform.example.org")
    monkeypatch.setattr(config, "HOSTING_CUSTOM_DOMAIN_IPS", ("203.0.113.8",))
    db.init_hosting_db()
    admin = db.create_account("domain-admin", generate_password_hash("password123"), is_admin=True)
    owner = db.create_account("domain-owner", generate_password_hash("password123"))
    instance = db.create_instance(owner, "customer", "admin", "temporary")
    return instance, owner, admin


def verified(tenant, monkeypatch):
    instance, owner, admin = tenant
    domains.set_permission(instance["id"], True, admin)
    binding = domains.claim_domain(instance["id"], "wiki.customer.example", owner)

    def records(name, kind):
        if kind == "TXT":
            return {binding["verification_token"]}
        if kind == "CNAME":
            return {config.HOSTING_CUSTOM_DOMAIN_TARGET}
        return {"203.0.113.8"} if kind == "A" else set()

    monkeypatch.setattr(domains, "_records", records)
    domains.verify_domain(instance["id"], owner)
    return binding


@pytest.mark.parametrize("value", [
    "https://wiki.example.org", "example.org:443", "user@example.org", "*.example.org",
    "127.0.0.1", "localhost", "wiki.local", "foo..org", "example.org/", "a\nb.example.org",
    "-name.example.org", "a" * 64 + ".org", "[::1]", "example.org?next=x",
])
def test_invalid_domains_are_rejected(value):
    with pytest.raises(ValueError):
        domains.normalize_domain(value)


def test_idn_and_case_are_canonicalized():
    assert domains.normalize_domain("BÜCHER.Example.") == "xn--bcher-kva.example"


def test_admin_permission_and_namespace_are_required(tenant):
    instance, owner, admin = tenant
    with pytest.raises(ValueError, match="administrator"):
        domains.claim_domain(instance["id"], "wiki.customer.example", owner)
    domains.set_permission(instance["id"], True, admin)
    with pytest.raises(ValueError, match="platform's own"):
        domains.claim_domain(instance["id"], config.EFFECTIVE_PORTAL_DOMAIN, owner)
    binding = domains.claim_domain(instance["id"], "wiki.customer.example", owner)
    assert not domains.resolve_domain(binding["domain"])
    assert not domains.certificate_allowed(binding["domain"])


def test_claim_cannot_take_another_tenants_verified_domain(tenant, monkeypatch):
    binding = verified(tenant, monkeypatch)
    instance, owner, admin = tenant
    other = db.create_instance(owner, "another", "admin", "temporary")
    domains.set_permission(other["id"], True, admin)
    with pytest.raises(ValueError, match="already assigned"):
        domains.claim_domain(other["id"], binding["domain"], owner)
    assert domains.resolve_domain(binding["domain"])["id"] == instance["id"]


def test_txt_must_match_even_when_cname_points_to_service(tenant, monkeypatch):
    instance, owner, admin = tenant
    domains.set_permission(instance["id"], True, admin)
    binding = domains.claim_domain(instance["id"], "wiki.customer.example", owner)
    monkeypatch.setattr(domains, "_records", lambda name, kind: {"wrong", "é"})
    with pytest.raises(ValueError, match="TXT"):
        domains.verify_domain(instance["id"], owner)
    assert not domains.certificate_allowed(binding["domain"])


def test_all_address_records_must_point_to_the_service(tenant, monkeypatch):
    instance, owner, admin = tenant
    domains.set_permission(instance["id"], True, admin)
    binding = domains.claim_domain(instance["id"], "wiki.customer.example", owner)

    def records(name, kind):
        if kind == "TXT":
            return {binding["verification_token"]}
        if kind == "CNAME":
            raise dns.resolver.NoAnswer
        return {"203.0.113.8", "203.0.113.9"} if kind == "A" else set()

    monkeypatch.setattr(domains, "_records", records)
    with pytest.raises(ValueError, match="does not point"):
        domains.verify_domain(instance["id"], owner)


@pytest.mark.parametrize("change", ["permission", "suspension", "account", "expiry", "verification", "denial"])
def test_access_and_certificate_issuance_stop_when_permission_or_lifecycle_changes(tenant, monkeypatch, change):
    binding = verified(tenant, monkeypatch)
    instance, owner, admin = tenant
    assert domains.certificate_allowed(binding["domain"])
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db.get_hosting_db_context() as conn:
        if change == "permission":
            conn.execute("UPDATE instances SET custom_domain_allowed=0 WHERE id=?", (instance["id"],))
        elif change == "suspension":
            conn.execute("UPDATE instances SET status='suspended' WHERE id=?", (instance["id"],))
        elif change == "account":
            conn.execute("UPDATE accounts SET suspended=1 WHERE id=?", (owner,))
        elif change == "denial":
            conn.execute("UPDATE accounts SET approval_status='denied' WHERE id=?", (owner,))
        elif change == "expiry":
            conn.execute("UPDATE instances SET expires_at=? WHERE id=?", (yesterday, instance["id"]))
        else:
            conn.execute("UPDATE instance_custom_domains SET verified_until=?", (yesterday,))
        conn.commit()
    assert domains.resolve_domain(binding["domain"]) is None
    assert not domains.certificate_allowed(binding["domain"])


def test_revocation_erases_binding_and_records_the_decision(tenant, monkeypatch):
    binding = verified(tenant, monkeypatch)
    instance, owner, admin = tenant
    domains.set_permission(instance["id"], False, admin)
    assert domains.get_domain(instance["id"]) is None
    assert not domains.certificate_allowed(binding["domain"])
    assert list_events("instance", instance["id"])[0]["action"] == "domain.permission.revoked"


def test_disabling_custom_domain_configuration_stops_existing_bindings(tenant, monkeypatch):
    binding = verified(tenant, monkeypatch)
    monkeypatch.setattr(config, "HOSTING_CUSTOM_DOMAIN_TARGET", "")
    assert domains.resolve_domain(binding["domain"]) is None
    assert not domains.certificate_allowed(binding["domain"])
    with pytest.raises(ValueError, match="not configured"):
        domains.verify_domain(tenant[0]["id"])


def test_portal_routes_enforce_ownership_admin_role_and_csrf(tenant, monkeypatch):
    instance, owner, admin = tenant
    app = create_hosting_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = app.test_client()
    client.post("/login", data={"username": "domain-owner", "password": "password123"})
    page = client.get(f"/instances/{instance['id']}/domain")
    assert page.status_code == 200
    assert b"must enable" in page.data
    client.post(f"/admin/instances/{instance['id']}/domain-permission", data={"allowed": "1"})
    assert not db.get_instance(instance["id"])["custom_domain_allowed"]
    other = db.create_account("domain-other", "hash")
    other_instance = db.create_instance(other, "other", "admin", "temporary")
    assert client.get(f"/instances/{other_instance['id']}/domain").status_code == 404
    client.post("/logout")
    client.post("/login", data={"username": "domain-admin", "password": "password123"})
    app.config["WTF_CSRF_ENABLED"] = True
    client.post(f"/admin/instances/{instance['id']}/domain-permission", data={"allowed": "1"})
    assert not db.get_instance(instance["id"])["custom_domain_allowed"]


def test_unknown_hosts_do_not_reach_portal_and_verified_domains_use_tenant_proxy(tenant, monkeypatch):
    binding = verified(tenant, monkeypatch)
    instance, owner, admin = tenant
    from hosting import _subdomain_proxy as proxy
    monkeypatch.setattr(proxy, "_classify_subdomain", lambda slug, mode: (6001, "ready"))
    monkeypatch.setattr(proxy, "_proxy_request", lambda *a, **kw: ("200 OK", [("Content-Type", "text/plain")], [b"tenant wiki"]))
    app = create_hosting_app()
    client = app.test_client()
    assert client.get("/login", base_url="https://unknown.customer.example").status_code == 404
    response = client.get("/", base_url="https://" + binding["domain"])
    assert response.status_code == 200
    assert response.data == b"tenant wiki"
    domains.set_permission(instance["id"], False, admin)
    assert client.get("/", base_url="https://" + binding["domain"]).status_code == 404


def test_account_decision_history_survives_reconsideration(tenant):
    instance, owner, admin = tenant
    db.update_hosting_account(owner, approval_status="pending")
    assert db.deny_hosting_account(owner, admin, "Please clarify the intended use.")
    assert db.approve_hosting_account(owner, admin, "The revised request is approved.")
    events = list_events("account", owner)
    assert [e["action"] for e in events] == ["account.approved", "account.denied"]
    assert events[-1]["reason"] == "Please clarify the intended use."
    db.set_pending_deletion(owner)
    assert not db.approve_hosting_account(owner, admin)
    assert db.get_account_by_id(owner)["pending_deletion"] == 1
