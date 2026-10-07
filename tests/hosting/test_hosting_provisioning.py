"""Unfinished seed/copy/import stays reserved and cannot be launched by recovery."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bananawiki.hosting import accounts, instances, maintenance, settings
from bananawiki.hosting.db import connection_scope, db
from bananawiki.hosting.errors import ServiceError
from bananawiki.hosting.migrations import v4_provisioning
from bananawiki.hosting.runtime import RuntimeFailure


def _create(client, slug="provision-proof"):
    return client.post("/instances/create", data={"subdomain": slug,
        "declared_use_case": "A synthetic interrupted tenant provisioning acceptance.", "compliance_declared": "1"})


def test_paused_http_creation_is_reserved_but_not_recovered_or_mutated(portal, runtime, make_account, login,
                                                                     monkeypatch):
    owner = make_account()
    client, observer = portal.test_client(), portal.test_client()
    login(client, owner)
    login(observer, owner)
    begun, release = threading.Event(), threading.Event()
    provision = runtime.provision
    recovered = []
    recover = runtime.recover

    def paused(*args, **kwargs):
        begun.set()
        assert release.wait(10)
        provision(*args, **kwargs)

    def capture(specs):
        recovered.extend(specs)
        return recover(specs)

    monkeypatch.setattr(runtime, "provision", paused)
    monkeypatch.setattr(runtime, "recover", capture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        create = pool.submit(_create, client)
        try:
            assert begun.wait(5)
            with portal.app_context(), connection_scope():
                inst = instances.by_slug("provision-proof", "hosting")
                assert (inst["status"], inst["provisioning_state"]) == ("stopped", "pending")
                settings.update(global_limit_max_instances=1)
            assert maintenance.recover(portal) == 0 and recovered == []
            for route in ("restart", "reset-password"):
                assert observer.post(f"/instances/{inst['id']}/{route}").status_code == 409
            assert observer.post(f"/instances/{inst['id']}/terminate").status_code == 302
            with portal.app_context(), connection_scope():
                assert instances.get(inst["id"])["provisioning_state"] == "pending"
            assert _create(observer, "provision-overflow").status_code == 400
            page = observer.get(f"/instances/{inst['id']}")
            assert page.status_code == 200 and b"Creating" in page.data
            assert f"/instances/{inst['id']}/restart".encode() not in page.data
            assert runtime.called("start") == runtime.called("restart") == runtime.called("destroy") == []
            assert runtime.called("reset_admin_password") == []
        finally:
            release.set()
        assert create.result(timeout=10).status_code == 200
    with portal.app_context(), connection_scope():
        ready = instances.get(inst["id"])
        assert (ready["status"], ready["provisioning_state"]) == ("running", "ready")
        assert ready["admin_password_plain"] is None


@pytest.mark.parametrize("operation", ["create", "duplicate", "import"])
def test_interruption_never_invents_credentials_or_recovers_partial_data(portal, runtime, make_account, make_wiki,
                                                                       monkeypatch, operation):
    owner = make_account()
    source = make_wiki(owner) if operation == "duplicate" else None

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt("synthetic worker crash")

    method = {"create": "provision", "duplicate": "duplicate", "import": "import_archive"}[operation]
    monkeypatch.setattr(runtime, method, interrupted)
    with portal.test_request_context("/"), connection_scope():
        with pytest.raises(KeyboardInterrupt):
            if operation == "create":
                instances.create(owner, "interrupted")
            elif operation == "duplicate":
                instances.duplicate(source, owner, "interrupted", "hosting", actor_id=owner["id"])
            else:
                instances.import_archive(owner, "interrupted", "hosting", None, actor_id=owner["id"])
        pending = instances.by_slug("interrupted", "hosting")
        assert pending["status"] == "stopped" and pending["provisioning_state"] == "pending"
        assert pending["admin_password_plain"] is None
        with pytest.raises(ServiceError, match="provisioning_incomplete"):
            instances.reset_admin_password(pending, actor_id=owner["id"])
        with pytest.raises(ServiceError, match="provisioning_incomplete"):
            instances.start(pending, actor_id=owner["id"])
        assert not instances.apply_policy(pending, actor_id=None)
        instances.terminate(pending, actor_id=None, reason="operator_cancel")
        assert instances.get(pending["id"])["status"] == "terminated"
        assert instances.get(pending["id"])["data_retained_until"] is None
    assert "interrupted" not in runtime.called("start")


@pytest.mark.parametrize("mutator", [
    lambda i, o: instances.rename(i, "renamed", "hosting", actor_id=o["id"]),
    lambda i, o: instances.move_to_owner(i, o, actor_id=o["id"]),
    lambda i, o: instances.set_storage_limit(i, 50, actor_id=o["id"]),
    lambda i, o: instances.set_upload_policy(i, 5, "exe", actor_id=o["id"]),
    lambda i, o: instances.set_easy_wiki(i, True, actor_id=o["id"]),
    lambda i, o: instances.reset_content(i, actor_id=o["id"]),
    lambda i, o: instances.suspend(i, actor_id=o["id"]),
    lambda i, o: instances.set_expiry(i, None, actor_id=o["id"]),
])
def test_pending_policy_and_lifecycle_changes_fail_before_runtime(ctx, runtime, make_account, mutator):
    owner = make_account()
    pending = instances._insert(owner, "pending-change", "hosting", admin_username="admin", custom_credentials=False,
                                easy_wiki=False, use_case="")
    before = list(runtime.calls)
    with pytest.raises(ServiceError, match="provisioning_incomplete"):
        mutator(pending, owner)
    assert runtime.calls == before
    assert instances.get(pending["id"])["provisioning_state"] == "pending"


def test_failed_cleanup_stays_reserved_and_excluded_from_recovery(portal, runtime, make_account,
                                                                 monkeypatch):
    owner = make_account()
    provision = runtime.provision

    def then_fail(*args, **kwargs):
        provision(*args, **kwargs)
        raise RuntimeFailure("start_failed", "synthetic")

    monkeypatch.setattr(runtime, "provision", then_fail)
    runtime.fail_next("destroy", "unavailable")
    runtime.fail_next("stop", "unavailable")
    with portal.test_request_context("/"), connection_scope():
        with pytest.raises(ServiceError):
            instances.create(owner, "failed-provision")
        failed = instances.by_slug("failed-provision", "hosting")
        assert failed["status"] == "running" and failed["provisioning_state"] == "failed"
        assert instances.describe(failed)["status"] == "provisioning_failed"
        with pytest.raises(ServiceError, match="provisioning_incomplete"):
            instances.restart(failed, actor_id=None)
        instances.sync_routes()
        assert runtime.routes == []
    before = len(runtime.called("recover"))
    assert maintenance.recover(portal) == 0
    assert len(runtime.called("recover")) == before + 1
    with portal.test_request_context("/"), connection_scope():
        instances.terminate(instances.get(failed["id"]), actor_id=None)
        assert instances.get(failed["id"])["status"] == "terminated"


def test_cancel_before_creator_lock_does_not_launch_an_orphan(portal, runtime, make_account, login, monkeypatch):
    owner = make_account()
    creator, operator = portal.test_client(), portal.test_client()
    login(creator, owner)
    login(operator, make_account(admin=True))
    begun, release = threading.Event(), threading.Event()
    provision = instances._provision

    def before_lock(*args, **kwargs):
        begun.set()
        assert release.wait(10)
        return provision(*args, **kwargs)

    monkeypatch.setattr(instances, "_provision", before_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        create = pool.submit(_create, creator)
        try:
            assert begun.wait(5)
            with portal.app_context(), connection_scope():
                pending = instances.by_slug("provision-proof", "hosting")
            assert operator.post(f"/admin/instances/{pending['id']}/terminate").status_code == 302
            with portal.app_context(), connection_scope():
                assert instances.get(pending["id"])["status"] == "terminated"
        finally:
            release.set()
        assert create.result(timeout=10).status_code == 400
    assert runtime.called("provision") == []
    monkeypatch.setattr(instances, "_provision", provision)
    assert _create(creator).status_code == 200


def test_owner_suspended_during_seed_is_cleaned_before_completion(ctx, runtime, make_account, monkeypatch):
    owner = make_account()
    provision = runtime.provision

    def deactivate(*args, **kwargs):
        provision(*args, **kwargs)
        accounts.suspend(owner["id"], actor_id=None, until=None, reason="synthetic", reason_visible=False,
                         time_visible=False, duration_label="permanent")

    monkeypatch.setattr(runtime, "provision", deactivate)
    with pytest.raises(ServiceError):
        instances.create(owner, "suspended-seed")
    failed = db.one("SELECT * FROM instances WHERE account_id = ?", (owner["id"],))
    assert failed["status"] == "terminated" and failed["provisioning_state"] == "failed"
    assert failed["admin_password_plain"] is None
    assert "suspended-seed" not in runtime.tenants


def test_global_policy_changed_during_seed_is_applied_before_completion(ctx, runtime, make_account, monkeypatch):
    owner = make_account()
    provision = runtime.provision

    def changed_policy(*args, **kwargs):
        assert args[0].policy.forbid_public_mode
        provision(*args, **kwargs)
        settings.update(forbid_non_admin_public_wikis=0)

    monkeypatch.setattr(runtime, "provision", changed_policy)
    inst, _, _ = instances.create(owner, "policy-seed")
    assert inst["status"] == "running" and inst["provisioning_state"] == "ready"
    assert not runtime.tenants["policy-seed"].spec.policy.forbid_public_mode
    assert runtime.called("apply_limits") == runtime.called("restart") == ["policy-seed"]


def test_v4_migration_preserves_legacy_status_and_marks_existing_rows_ready():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE instances (id TEXT, status TEXT CHECK(status IN ('running','stopped','suspended','terminated')))")
    conn.execute("INSERT INTO instances VALUES ('old', 'running')")
    v4_provisioning.upgrade(conn)
    v4_provisioning.upgrade(conn)
    assert conn.execute("SELECT status, provisioning_state FROM instances").fetchone() == ("running", "ready")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE instances SET provisioning_state='unknown'")
    conn.close()
