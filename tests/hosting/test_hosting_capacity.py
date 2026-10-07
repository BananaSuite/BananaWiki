"""Real SQLite capacity reservations across overlapping HTTP creation requests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bananawiki.hosting import accounts, instances, settings
from bananawiki.hosting.db import connection_scope, db
from bananawiki.hosting.runtime import RuntimeFailure

from .hosting_support import PASSWORD, build_portal, portal_environ


def _limited_portal(tmp_path, *, account_limit: int = 1, platform_limit: int = 100):
    portal = build_portal(tmp_path, environ=portal_environ(
        tmp_path, MAX_INSTANCES_PER_ACCOUNT=str(account_limit),
    ))
    with portal.app_context(), connection_scope():
        settings.update(global_limit_enabled=1, global_limit_max_instances=platform_limit)
        owners = [accounts.create(f"capacity-user-{index}", PASSWORD) for index in range(2)]
    clients = [portal.test_client() for _ in owners]
    for client, owner in zip(clients, owners, strict=True):
        assert client.post("/login", data={"username": owner["username"], "password": PASSWORD}).status_code == 302
    return portal, clients, owners


def _create(client, slug: str):
    return client.post("/instances/create", data={
        "subdomain": slug, "declared_use_case": "A synthetic hosting capacity acceptance check.",
        "compliance_declared": "1",
    })


def _rows(portal):
    with portal.app_context(), connection_scope():
        return db.all("SELECT subdomain, status, port, account_id FROM instances ORDER BY subdomain")


@pytest.mark.parametrize("limit", ["account", "platform"])
def test_overlapping_http_creation_cannot_exceed_capacity(tmp_path, monkeypatch, limit):
    portal, clients, owners = _limited_portal(
        tmp_path, account_limit=1 if limit == "account" else 100,
        platform_limit=1 if limit == "platform" else 100,
    )
    if limit == "account":
        clients[1] = portal.test_client()
        assert clients[1].post("/login", data={
            "username": owners[0]["username"], "password": PASSWORD,
        }).status_code == 302
    barrier = threading.Barrier(2)
    generate_password = instances.generate_password

    def overlapping_password():
        password = generate_password()
        # Control only request scheduling, leaving the real admission checks,
        # SQLite write transactions and route behavior unchanged.
        barrier.wait(timeout=10)
        return password

    monkeypatch.setattr(instances, "generate_password", overlapping_password)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda index: _create(clients[index], f"capacity-race-{index}"), range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 400]
    assert len(_rows(portal)) == 1
    assert len(portal.extensions["bananawiki.hosting.runtime"].called("provision")) == 1


def test_inflight_provisioning_keeps_its_capacity_reservation(tmp_path, monkeypatch):
    portal, clients, _owners = _limited_portal(tmp_path, account_limit=100, platform_limit=1)
    runtime = portal.extensions["bananawiki.hosting.runtime"]
    provision = runtime.provision
    started, release = threading.Event(), threading.Event()

    def delayed_provision(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return provision(*args, **kwargs)

    monkeypatch.setattr(runtime, "provision", delayed_provision)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_create, clients[0], "capacity-inflight")
        try:
            assert started.wait(5)
            assert _create(clients[1], "capacity-overflow").status_code == 400
            assert _rows(portal)[0]["status"] == "stopped"
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
    assert runtime.called("provision") == ["capacity-inflight"]


def test_capacity_rechecks_current_policy_after_slow_preflight(tmp_path, monkeypatch):
    portal, clients, _owners = _limited_portal(tmp_path, account_limit=100, platform_limit=100)
    started, release = threading.Event(), threading.Event()
    generate_password = instances.generate_password

    def delayed_password():
        started.set()
        assert release.wait(10)
        return generate_password()

    monkeypatch.setattr(instances, "generate_password", delayed_password)
    with ThreadPoolExecutor(max_workers=1) as pool:
        create = pool.submit(_create, clients[0], "capacity-policy-change")
        try:
            assert started.wait(5)
            with portal.app_context(), connection_scope():
                settings.update(global_limit_max_instances=0)
        finally:
            release.set()
        assert create.result(timeout=10).status_code == 400
    assert _rows(portal) == []


def test_operator_only_creation_allows_staged_transfer_of_existing_wiki(tmp_path):
    portal, clients, owners = _limited_portal(tmp_path, account_limit=0, platform_limit=1)
    assert _create(clients[0], "capacity-self-service").status_code == 400
    with portal.app_context(), connection_scope():
        administrator = accounts.create("capacity-operator", PASSWORD, is_admin=True)
    operator = portal.test_client()
    assert operator.post("/login", data={
        "username": administrator["username"], "password": PASSWORD,
    }).status_code == 302
    assert _create(operator, "capacity-staged").status_code == 200
    with portal.app_context(), connection_scope():
        inst = instances.by_slug("capacity-staged", "hosting")
    assert operator.post(f"/instances/{inst['id']}/stop").status_code == 302
    assert _rows(portal)[0]["status"] == "stopped"
    # Operators provision and verify real host quotas at this stopped, still
    # trusted-owned point. This test covers the control-plane gate and handoff;
    # FakeRuntime does not claim a filesystem quota or firewall acceptance.
    assert operator.post(f"/admin/instances/{inst['id']}/transfer", data={
        "username": owners[0]["username"],
    }).status_code == 302
    assert _rows(portal)[0]["status"] == "stopped"
    assert _rows(portal)[0]["account_id"] == owners[0]["id"]
    assert clients[0].post(f"/instances/{inst['id']}/restart").status_code == 302
    assert _rows(portal)[0]["status"] == "running"
    assert _create(clients[0], "capacity-still-disabled").status_code == 400


def test_successful_failed_provision_cleanup_releases_capacity(tmp_path):
    portal, clients, _owners = _limited_portal(tmp_path, platform_limit=1)
    runtime = portal.extensions["bananawiki.hosting.runtime"]
    runtime.fail_next("provision", "no_space")
    assert _create(clients[0], "capacity-retry").status_code == 400
    assert _rows(portal)[0]["status"] == "terminated"
    assert _create(clients[0], "capacity-retry").status_code == 200


@pytest.mark.parametrize("stop_fails", [False, True])
def test_failed_cleanup_keeps_name_port_and_capacity_until_termination(tmp_path, monkeypatch, stop_fails):
    portal, clients, _owners = _limited_portal(tmp_path, platform_limit=1)
    runtime = portal.extensions["bananawiki.hosting.runtime"]
    provision = runtime.provision

    def provision_then_fail(*args, **kwargs):
        provision(*args, **kwargs)
        raise RuntimeFailure("start_failed", "synthetic post-start failure")

    monkeypatch.setattr(runtime, "provision", provision_then_fail)
    runtime.fail_next("destroy", "unavailable")
    if stop_fails:
        runtime.fail_next("stop", "unavailable")
    assert _create(clients[0], "capacity-leftover").status_code == 400
    row = _rows(portal)[0]
    assert row["subdomain"] == "capacity-leftover"
    assert row["status"] == ("running" if stop_fails else "stopped") and row["port"] is not None
    assert "capacity-leftover" in runtime.tenants
    assert runtime.tenants["capacity-leftover"].running == stop_fails
    assert _create(clients[1], "capacity-overflow").status_code == 400
    with portal.test_request_context("/"), connection_scope():
        settings.update(grace_period_days=0)
        inst = instances.by_slug("capacity-leftover", "hosting")
        instances.terminate(inst, actor_id=None, reason="cleanup_retry")
    assert _rows(portal)[0]["status"] == "terminated"
    assert "capacity-leftover" not in runtime.tenants
    monkeypatch.setattr(runtime, "provision", provision)
    assert _create(clients[1], "capacity-after-cleanup").status_code == 200
