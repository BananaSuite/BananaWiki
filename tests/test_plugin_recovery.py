"""Operator recovery of a hosted tenant after a bad plugin.

The platform restores only snapshots it took itself and keeps outside the
tenant's data folder.  Files in the tenant's own ``plugin_safety_snapshots``
folder, including the copy the wiki makes before it enables external code,
are never restored: a plugin that ran could have written them.
"""

import os
import sqlite3

from hosting import config
from hosting import instance_environment
from hosting import instance_manager


def _write_db(path, value):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _set_marker(path, value):
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE marker SET value = ?", (value,))
        conn.commit()
    finally:
        conn.close()


def _read_marker(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        conn.close()


def _tenant(tmp_path, monkeypatch, status):
    """Return the data folder of a tenant 'one' whose rows are faked."""
    monkeypatch.setattr(config, "INSTANCES_DIR", str(tmp_path / "instances"))
    monkeypatch.setattr(config, "HOSTING_PLATFORM_STATE_DIR", str(tmp_path / "platform"))
    tenant = tmp_path / "instances" / "alpha"
    tenant.mkdir(parents=True)
    monkeypatch.setattr(instance_manager, "get_instance", lambda _id: {
        "id": "one", "subdomain": "alpha", "domain_mode": "hosting",
        "status": status, "port": 6123,
    })
    monkeypatch.setattr(instance_manager, "_invalidate_instance_caches", lambda *_args: None)
    return tenant


def _snapshots_by_label():
    index = instance_manager._read_snapshot_index("one")
    folder = instance_environment.plugin_snapshot_dir("one")
    return {
        meta["label"]: os.path.join(folder, name)
        for name, meta in index.items()
    }


def test_restore_uses_pre_enable_snapshot_and_keeps_emergency_copy(tmp_path, monkeypatch):
    tenant = _tenant(tmp_path, monkeypatch, "stopped")
    monkeypatch.setattr(instance_manager, "_stop_process", lambda *_args, **_kwargs: None)
    live = tenant / "bananawiki.db"
    _write_db(live, "trusted")

    ok, message = instance_manager.capture_plugin_safety_snapshot("one")
    assert ok, message
    # The quarantine copy is newer but records the state after the plugin ran.
    _set_marker(live, "quarantined")
    instance_manager._capture_platform_snapshot("one", str(live), str(tenant), "operator-quarantine")
    _set_marker(live, "current")
    # The wiki's own pre-enable copy sits in the tenant's folder and is ignored.
    tenant_copies = tenant / "plugin_safety_snapshots"
    tenant_copies.mkdir()
    _write_db(tenant_copies / "20990101T000000Z-example.db", "tenant-written")

    ok, message = instance_manager.restore_latest_plugin_safety_snapshot("one")
    assert ok, message
    assert _read_marker(live) == "trusted"
    emergency = _snapshots_by_label()["before-restore"]
    assert _read_marker(emergency) == "current"
    assert instance_environment.instance_plugins_quarantined("one")


def test_restore_rejects_corrupt_snapshot_without_stopping_tenant(tmp_path, monkeypatch):
    tenant = _tenant(tmp_path, monkeypatch, "running")
    live = tenant / "bananawiki.db"
    _write_db(live, "current")
    ok, message = instance_manager.capture_plugin_safety_snapshot("one")
    assert ok, message
    with open(_snapshots_by_label()["pre-enable"], "wb") as fh:
        fh.write(b"not sqlite")
    stopped = []
    monkeypatch.setattr(instance_manager, "_stop_process", lambda *_a, **_k: stopped.append(True))

    ok, _message = instance_manager.restore_latest_plugin_safety_snapshot("one")
    # The damaged copy is refused before the tenant is touched.
    assert not ok
    assert stopped == []
    assert _read_marker(live) == "current"
