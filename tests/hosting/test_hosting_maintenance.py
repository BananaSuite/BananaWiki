"""The maintenance service pass and the 1.4 entry points under hosting/."""

from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path

from bananawiki.hosting import maintenance

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


def test_maintenance_shim_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(maintenance, "main", lambda argv=None: seen.setdefault("called", True) and 0)
    monkeypatch.setattr(sys, "argv", ["hosting.maintenance", "--once"])
    try:
        runpy.run_path(str(ROOT / "hosting" / "maintenance.py"), run_name="__main__")
    except SystemExit as exit_:
        assert exit_.code == 0
    assert seen == {"called": True}
