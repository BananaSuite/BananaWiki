"""The maintenance service pass and the 1.4 entry points under hosting/."""

from __future__ import annotations

import importlib.util
import runpy
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from bananawiki.core.timeutil import parse, sql_in, utcnow
from bananawiki.hosting import instances, maintenance

ROOT = Path(__file__).resolve().parents[2]


def test_pass_terminates_expired_wikis_and_lifts_timed_suspensions(portal, make_account, make_wiki, query, runtime):
    owner = make_account()
    expired = make_wiki(owner, "expired")
    paused = make_wiki(owner, "paused")
    query("UPDATE instances SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (expired["id"],))
    query("UPDATE instances SET status = 'suspended', suspended_at = '2000-01-01 00:00:00', "
          "suspended_until = '2000-01-02 00:00:00' WHERE id = ?", (paused["id"],))
    results = maintenance.run_once(portal)
    assert "failed" not in results.values(), results
    assert query("SELECT status FROM instances WHERE id = ?", (expired["id"],), one=True)["status"] == "terminated"
    assert query("SELECT status FROM instances WHERE id = ?", (paused["id"],), one=True)["status"] == "running"


def test_pass_deletes_grace_expired_data(portal, make_account, make_wiki, query, runtime):
    owner = make_account()
    wiki = make_wiki(owner, "gone")
    query("UPDATE instances SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (wiki["id"],))
    maintenance.run_once(portal)
    query("UPDATE instances SET data_retained_until = '2000-01-01 00:00:00' WHERE id = ?", (wiki["id"],))
    maintenance.run_once(portal)
    assert runtime.called("destroy")
    assert query("SELECT id FROM instances WHERE id = ?", (wiki["id"],), one=True) is None


def test_pass_runs_scheduled_account_deletions(portal, make_account, make_wiki, query):
    user = make_account(pending_deletion=1, pending_deletion_at="2000-01-01 00:00:00", pending_deletion_seconds=60)
    make_wiki(user, "leaving")
    maintenance.run_once(portal)
    assert query("SELECT deleted_at FROM accounts WHERE id = ?", (user["id"],), one=True)["deleted_at"]
    assert query("SELECT status FROM instances", one=True)["status"] == "terminated"


def test_denied_cleanup_keeps_admins(portal, make_account, query):
    query("UPDATE hosting_settings SET hosting_activation_denied_timeout_seconds = 60 WHERE id = 1")
    denied = make_account(approval_status="denied", denied_at="2000-01-01 00:00:00")
    admin = make_account(admin=True, approval_status="denied", denied_at="2000-01-01 00:00:00")
    maintenance.run_once(portal)
    row = query("SELECT deleted_at FROM accounts WHERE id = ?", (denied["id"],), one=True)
    assert row is None or row["deleted_at"]
    assert query("SELECT deleted_at FROM accounts WHERE id = ?", (admin["id"],), one=True)["deleted_at"] is None


def test_a_paused_countdown_keeps_the_data_and_resumes_where_it_stopped(portal, ctx, make_account, make_wiki, query,
                                                                       runtime):
    wiki = make_wiki(make_account(), "on-hold")
    instances.terminate(instances.get(wiki["id"]), actor_id=None)
    instances.set_grace_suspended(instances.get(wiki["id"]), True, actor_id=None)
    instances.set_grace_suspended(instances.get(wiki["id"]), True, actor_id=None)
    # Terminated 30 days ago and paused ten days ago, with eight days left: without the pause the data
    # would have been deleted two days ago.
    query("UPDATE hosting_events SET created_at = ? WHERE action = 'instance.grace_suspended'", (sql_in(days=-10),))
    query("UPDATE instances SET terminated_at = ?, data_retained_until = ? WHERE id = ?",
          (sql_in(days=-30), sql_in(days=-2), wiki["id"]))
    maintenance.run_once(portal)
    paused = instances.get(wiki["id"])
    assert paused is not None and instances.grace_active(paused)
    assert runtime.called("destroy") == []
    instances.set_grace_suspended(paused, False, actor_id=None)
    left = parse(instances.get(wiki["id"])["data_retained_until"]) - utcnow()
    assert timedelta(days=7, hours=23) < left <= timedelta(days=8, minutes=1), "the eight days left at the pause"
    assert query("SELECT COUNT(*) AS n FROM hosting_events WHERE action = 'instance.grace_suspended'",
                 one=True)["n"] == 1, "pausing a paused countdown does not restart the pause"
    maintenance.run_once(portal)
    assert instances.get(wiki["id"]) is not None


def test_one_wiki_whose_data_cannot_be_deleted_does_not_block_the_others(portal, ctx, make_account, make_wiki, query,
                                                                          runtime, monkeypatch):
    owner = make_account()
    stuck, gone = make_wiki(owner, "stuck-data"), make_wiki(owner, "gone-data")
    for number, wiki in enumerate((stuck, gone), start=1):
        instances.terminate(instances.get(wiki["id"]), actor_id=None)
        query("UPDATE instances SET data_retained_until = ? WHERE id = ?", (f"2000-01-0{number} 00:00:00", wiki["id"]))
    destroy = runtime.destroy

    def refuse_first(spec):
        if spec.instance_id == stuck["id"]:
            raise PermissionError(13, "Permission denied")
        destroy(spec)

    monkeypatch.setattr(runtime, "destroy", refuse_first)
    assert maintenance.run_once(portal)["grace periods"] == 1
    assert instances.get(stuck["id"]) is not None and instances.get(gone["id"]) is None


def test_expired_wikis_are_terminated_one_by_one(portal, make_account, make_wiki, query, runtime, monkeypatch):
    owner = make_account()
    first, second = make_wiki(owner, "first-expired"), make_wiki(owner, "second-expired")
    for number, wiki in enumerate((first, second), start=1):
        query("UPDATE instances SET expires_at = ? WHERE id = ?", (f"2000-01-0{number} 00:00:00", wiki["id"]))
    stop = runtime.stop

    def broken_first(spec):
        if spec.slug == "first-expired":
            raise OSError("container state unreadable")
        stop(spec)

    monkeypatch.setattr(runtime, "stop", broken_first)
    assert maintenance.run_once(portal)["expired wikis"] == 1
    statuses = {row["subdomain"].split("--")[0]: row["status"] for row in query("SELECT subdomain, status FROM instances")}
    assert statuses == {"first-expired": "running", "second-expired": "terminated"}


def test_one_failing_account_deletion_does_not_block_the_next(portal, make_account, make_wiki, query, runtime):
    due = {"pending_deletion": 1, "pending_deletion_seconds": 60}
    first = make_account(pending_deletion_at="2000-01-01 00:00:00", **due)
    second = make_account(pending_deletion_at="2000-01-02 00:00:00", **due)
    make_wiki(first, "unstoppable")
    runtime.fail_next("stop", "stop_failed")
    active = "SELECT id FROM accounts WHERE id = ? AND deleted_at IS NULL"
    assert maintenance.run_once(portal)["scheduled deletions"] == 1
    assert query(active, (first["id"],), one=True)
    assert query("SELECT status FROM instances", one=True)["status"] == "running", "nothing irreversible happened"
    assert query(active, (second["id"],), one=True) is None
    maintenance.run_once(portal)
    assert query(active, (first["id"],), one=True) is None, "retried on the next pass"


def _age_failures(query, step: str, minutes: int) -> None:
    query("UPDATE hosting_events SET created_at = ? WHERE action = ?", (sql_in(minutes=-minutes),
                                                                       f"maintenance.{step}.failed"))


def _events(query, subject_id: str, action: str) -> int:
    return query("SELECT COUNT(*) AS n FROM hosting_events WHERE subject_id = ? AND action = ?",
                 (subject_id, action), one=True)["n"]


def test_an_expired_wiki_failing_on_every_pass_waits_between_attempts(portal, make_account, make_wiki, query,
                                                                       runtime, monkeypatch):
    owner = make_account()
    stuck, later = make_wiki(owner, "stuck-expired"), make_wiki(owner, "later-expired")
    query("UPDATE instances SET expires_at = '2000-01-01 00:00:00'")
    stop, attempts = runtime.stop, []

    def broken(spec):
        if spec.slug == "stuck-expired":
            attempts.append(spec.slug)
            raise OSError("container state unreadable")
        stop(spec)

    monkeypatch.setattr(runtime, "stop", broken)
    for _ in range(4):
        maintenance.run_once(portal)
    assert len(attempts) == 3, "after three failures in a row the wiki waits"
    assert _events(query, stuck["id"], "maintenance.terminate_expired.failed") == 3
    assert query("SELECT status FROM instances WHERE id = ?", (later["id"],), one=True)["status"] == "terminated"
    monkeypatch.setattr(runtime, "stop", stop)
    _age_failures(query, "terminate_expired", 10)
    maintenance.run_once(portal)
    assert query("SELECT status FROM instances WHERE id = ?", (stuck["id"],), one=True)["status"] == "running"
    _age_failures(query, "terminate_expired", 16)
    maintenance.run_once(portal)
    assert query("SELECT status FROM instances WHERE id = ?", (stuck["id"],), one=True)["status"] == "terminated"
    assert _events(query, stuck["id"], "maintenance.terminate_expired.recovered") == 1


def test_an_account_deletion_failing_on_every_pass_waits_between_attempts(portal, make_account, make_wiki, query,
                                                                          runtime):
    user = make_account(pending_deletion=1, pending_deletion_at="2000-01-01 00:00:00", pending_deletion_seconds=60)
    make_wiki(user, "never-stops")
    for _ in range(3):
        runtime.fail_next("stop", "stop_failed")
        assert maintenance.run_once(portal)["scheduled deletions"] == 0
    assert maintenance.run_once(portal)["scheduled deletions"] == 0, "it waits, although it would work now"
    assert len(runtime.called("stop")) == 3
    _age_failures(query, "delete_account", 16)
    assert maintenance.run_once(portal)["scheduled deletions"] == 1
    assert _events(query, user["id"], "maintenance.delete_account.recovered") == 1


def test_retained_data_the_runtime_cannot_delete_counts_as_a_failure(portal, ctx, make_account, make_wiki, query,
                                                                     runtime):
    wiki = make_wiki(make_account(), "kept-data")
    instances.terminate(instances.get(wiki["id"]), actor_id=None)
    query("UPDATE instances SET data_retained_until = '2000-01-01 00:00:00' WHERE id = ?", (wiki["id"],))
    runtime.fail_next("destroy")
    assert maintenance.run_once(portal)["grace periods"] == 0
    assert _events(query, wiki["id"], "maintenance.purge_retained_data.failed") == 1
    assert maintenance.run_once(portal)["grace periods"] == 1
    assert _events(query, wiki["id"], "maintenance.purge_retained_data.recovered") == 1


def test_the_wait_doubles_up_to_a_day():
    assert instances.retry_wait(instances.RETRY_AFTER_FAILURES - 1) == timedelta(0)
    assert instances.retry_wait(instances.RETRY_AFTER_FAILURES) == timedelta(minutes=15)
    assert instances.retry_wait(instances.RETRY_AFTER_FAILURES + 1) == timedelta(minutes=30)
    assert instances.retry_wait(10_000) == timedelta(days=1)


def test_a_failing_step_does_not_stop_the_pass(portal, monkeypatch):
    def boom():
        raise RuntimeError("step failed")

    monkeypatch.setattr(maintenance.instances, "terminate_expired", boom)
    results = maintenance.run_once(portal)
    assert results["expired wikis"] == "failed"
    assert results["routes"] != "failed"


def test_recover_starts_running_wikis_then_publishes_their_routes(portal, make_account, make_wiki, runtime):
    make_wiki(make_account(), "comeback")
    maintenance.recover(portal)
    names = [name for name, _ in runtime.calls]
    # The updater's readiness check waits for routed wikis (C10): routes go out right after recovery.
    assert names[-2:] == ["recover", "sync_routes"]
    assert [spec.slug for spec in runtime.routes] == ["comeback"]


# ── hosting/ (the paths the 1.4 updater and systemd units use) ─────────


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_gunicorn_config_does_not_preload(monkeypatch):
    monkeypatch.setenv("HOSTING_HOST", "::1")
    monkeypatch.setenv("HOSTING_PORT", "5123")
    config = _load("hosting_gunicorn_conf", ROOT / "hosting" / "gunicorn.conf.py")
    assert config.preload_app is False
    assert config.bind == "[::1]:5123"
    assert config.worker_class == "gthread"


def test_gunicorn_config_gives_up_on_stalled_downloads(monkeypatch):
    monkeypatch.setenv("HOSTING_WRITE_TIMEOUT", "20")
    config = _load("hosting_gunicorn_conf", ROOT / "hosting" / "gunicorn.conf.py")
    timeouts = []
    worker = SimpleNamespace(wsgi=lambda _environ, _start: [b"backup"])
    config.post_worker_init(worker)
    assert list(worker.wsgi({"gunicorn.socket": SimpleNamespace(settimeout=timeouts.append)}, None)) == [b"backup"]
    assert timeouts == [20]


def test_maintenance_shim_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(maintenance, "main", lambda argv=None: seen.setdefault("called", True) and 0)
    monkeypatch.setattr(sys, "argv", ["hosting.maintenance", "--once"])
    try:
        runpy.run_path(str(ROOT / "hosting" / "maintenance.py"), run_name="__main__")
    except SystemExit as exit_:
        assert exit_.code == 0
    assert seen == {"called": True}
