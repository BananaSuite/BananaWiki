"""Security regressions for the hosting platform's host-side tenant handling.

These cover the fixes for the confirmed hosting findings:

* Portal code that reads or writes a tenant's data directory treats every
  entry as hostile: it refuses links and special files, opens paths without
  following links (also against a tenant racing the checks), and exports skip
  links and special files.  Tenants can plant any of these from a plugin.
* The plugin kill switch disables plugins by where their code lives, stays in
  force across restarts through a host-side marker, and starts a quarantined
  tenant with external plugins off.
* Plugin snapshot restore trusts only snapshots the platform itself took and
  keeps outside the tenant mount.
* The shared TTS GPU URL and token reach tenants only through the
  environment, only when the TTS policy allows, and are cleared from tenant
  databases.
* Portal password resets revoke the reset user's wiki API tokens.
* The portal reads a bounded page of a tenant's users plus a count.
* The operator's plugin denylist reaches tenants, and outbound tenant
  networking is warned about.

Several link scenarios are ported from the audit reproduction scripts: the
tenant only ever creates files and relative links inside its own data dir.
"""

import os
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hosting import config as hosting_config  # noqa: E402
from hosting import container_runtime  # noqa: E402
from hosting import instance_archives  # noqa: E402
from hosting import instance_database  # noqa: E402
from hosting import instance_environment  # noqa: E402
from hosting import instance_manager  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLATFORM_GPU_TOKEN = "PLATFORM-SHARED-GPU-TOKEN"


def _marker_db(path, value, *, plugins=None, users=None, tokens=None, gpu=None):
    """Write a small SQLite DB used as a stand-in for a tenant database."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker VALUES (?)", (value,))
        if plugins is not None:
            conn.execute(
                "CREATE TABLE plugins (id TEXT, enabled INT, builtin INT, disabled_at TEXT)"
            )
            for pid, enabled, builtin in plugins:
                conn.execute(
                    "INSERT INTO plugins (id, enabled, builtin) VALUES (?, ?, ?)",
                    (pid, enabled, builtin),
                )
        if users is not None:
            conn.execute(
                "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, "
                "password TEXT, role TEXT, created_at TEXT, "
                "force_password_change INTEGER NOT NULL DEFAULT 0)"
            )
            for uid, uname, role in users:
                conn.execute(
                    "INSERT INTO users (id, username, password, role, created_at) "
                    "VALUES (?, ?, 'h', ?, '2026-01-01')",
                    (uid, uname, role),
                )
        if tokens is not None:
            conn.execute(
                "CREATE TABLE api_service__tokens (id INTEGER PRIMARY KEY, "
                "user_id TEXT, active INTEGER)"
            )
            for tid, user_id, active in tokens:
                conn.execute(
                    "INSERT INTO api_service__tokens (id, user_id, active) "
                    "VALUES (?, ?, ?)",
                    (tid, user_id, active),
                )
        if gpu is not None:
            conn.execute(
                "CREATE TABLE site_settings (id INTEGER PRIMARY KEY, "
                "tts_gpu_enabled INTEGER DEFAULT 0, tts_gpu_url TEXT DEFAULT '', "
                "tts_gpu_auth_token TEXT DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO site_settings (id, tts_gpu_enabled, tts_gpu_url, "
                "tts_gpu_auth_token) VALUES (1, ?, ?, ?)",
                gpu,
            )
        conn.commit()
    finally:
        conn.close()


def _read_marker(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        conn.close()


def _query(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host layout like hosting/data: hosting.db, keys, instances/, state.

    The hosting DB is a real portal database so code that records passwords
    or reads settings works; the portal secret and backup key are plain files
    a tenant would love to read.
    """
    from hosting.db import init_hosting_db

    data = tmp_path / "data"
    instances = data / "instances"
    instances.mkdir(parents=True)
    monkeypatch.setattr(hosting_config, "INSTANCES_DIR", str(instances))
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(data / "hosting.db"))
    monkeypatch.setattr(
        hosting_config, "HOSTING_PLATFORM_STATE_DIR", str(data / "platform_state")
    )
    init_hosting_db()
    (data / ".secret_key").write_text("PORTAL-SECRET-KEY")
    (data / ".backup_encryption_key").write_bytes(b"K" * 32)
    monkeypatch.setattr(instance_manager, "_invalidate_instance_caches", lambda *_a, **_k: None)
    monkeypatch.setattr(instance_manager, "_stop_process", lambda *_a, **_k: None)
    return data


def _tenant(host, subdomain, value=None, **db_kwargs):
    data_dir = host / "instances" / subdomain
    data_dir.mkdir()
    if value is not None:
        _marker_db(str(data_dir / "bananawiki.db"), value, **db_kwargs)
    return data_dir


def _as_instance(monkeypatch, subdomain, *, status="stopped", instance_id="a"):
    row = {
        "id": instance_id, "subdomain": subdomain, "domain_mode": "hosting",
        "status": status, "port": 6001, "admin_username": "admin",
        "account_id": None,
    }
    monkeypatch.setattr(instance_manager, "get_instance", lambda _id: dict(row))
    monkeypatch.setattr(instance_archives, "get_instance", lambda _id: dict(row))
    return row


def _hosting_db_bytes(host):
    return (host / "hosting.db").read_bytes()


# ---------------------------------------------------------------------------
# A#16: portal code never follows tenant-planted links or opens special files.
# ---------------------------------------------------------------------------


def test_connect_tenant_db_refuses_link_fifo_and_escape(host):
    alpha = _tenant(host, "alpha", "alpha")
    beta = _tenant(host, "beta", "BETA")
    link = alpha / "linked.db"
    os.symlink("../beta/bananawiki.db", str(link))
    with pytest.raises(ValueError):
        instance_environment.connect_tenant_db(str(alpha), str(link))
    fifo = alpha / "fifo.db"
    os.mkfifo(str(fifo))
    with pytest.raises(ValueError):
        instance_environment.connect_tenant_db(str(alpha), str(fifo))
    with pytest.raises(ValueError):
        instance_environment.connect_tenant_db(str(alpha), str(beta / "bananawiki.db"))
    conn = instance_environment.connect_tenant_db(str(alpha), str(alpha / "bananawiki.db"))
    try:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "alpha"
    finally:
        conn.close()


def test_connect_tenant_db_refuses_fifo_journal_without_hanging(host):
    """A FIFO at -journal would block SQLite's hot-journal check forever."""
    alpha = _tenant(host, "alpha", "alpha")
    os.mkfifo(str(alpha / "bananawiki.db-journal"))
    outcome = {}

    def _open():
        try:
            instance_environment.connect_tenant_db(str(alpha), str(alpha / "bananawiki.db"))
            outcome["result"] = "opened"
        except ValueError:
            outcome["result"] = "refused"

    worker = threading.Thread(target=_open, daemon=True)
    worker.start()
    worker.join(10)
    assert outcome.get("result") == "refused"


def test_connect_tenant_db_holds_against_a_racing_link_swap(host):
    """A tenant swapping its DB for a link must never get the victim opened."""
    alpha = _tenant(host, "alpha")
    _tenant(host, "beta", "VICTIM")
    _marker_db(str(alpha / "regular.db"), "TENANT")
    db_path = str(alpha / "bananawiki.db")
    stop = threading.Event()

    def _swap():
        while not stop.is_set():
            try:
                os.link(str(alpha / "regular.db"), str(alpha / ".r"))
                os.replace(str(alpha / ".r"), db_path)
            except OSError:
                pass
            try:
                os.symlink("../beta/bananawiki.db", str(alpha / ".l"))
                os.replace(str(alpha / ".l"), db_path)
            except OSError:
                pass

    swapper = threading.Thread(target=_swap, daemon=True)
    swapper.start()
    seen = set()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                conn = instance_environment.connect_tenant_db(
                    str(alpha), db_path, read_only=True, timeout=0.1
                )
            except (ValueError, OSError, sqlite3.Error):
                continue
            try:
                seen.add(conn.execute("SELECT value FROM marker").fetchone()[0])
            except sqlite3.Error:
                pass
            finally:
                conn.close()
    finally:
        stop.set()
        swapper.join(5)
    assert "VICTIM" not in seen
    assert "TENANT" in seen


def test_write_tenant_file_replaces_a_planted_link(host):
    alpha = _tenant(host, "alpha")
    before = _hosting_db_bytes(host)
    os.symlink("../../hosting.db", str(alpha / ".starting"))
    instance_environment.write_tenant_file(str(alpha), ".starting", "123")
    assert not os.path.islink(alpha / ".starting")
    assert (alpha / ".starting").read_text() == "123"
    assert _hosting_db_bytes(host) == before


def test_start_process_does_not_write_through_planted_start_lock(host, monkeypatch):
    alpha = _tenant(host, "alpha", "alpha")
    before = _hosting_db_bytes(host)
    os.symlink("../../hosting.db", str(alpha / ".starting"))
    monkeypatch.setattr(instance_manager, "_prepare_instance_database", lambda _d: True)
    monkeypatch.setattr(instance_manager, "_start_process_once", lambda *_a, **_k: True)
    assert instance_manager._start_process(
        {"id": "a", "subdomain": "alpha", "port": 6001}, str(alpha)
    )
    assert _hosting_db_bytes(host) == before


def test_open_tenant_file_does_not_follow_a_linked_parent(host):
    alpha = _tenant(host, "alpha")
    (host / "outside").mkdir()
    (host / "outside" / "plugin.json").write_text('{"id": "host-file"}')
    (alpha / "external_plugins").mkdir()
    os.symlink("../../../outside", str(alpha / "external_plugins" / "evil"))
    with pytest.raises(OSError):
        instance_environment.read_tenant_file(
            str(alpha), str(alpha / "external_plugins" / "evil" / "plugin.json"),
            max_bytes=1024,
        )
    with pytest.raises(ValueError):
        instance_environment.read_tenant_file(
            str(alpha), str(host / "outside" / "plugin.json"), max_bytes=1024,
        )


def test_list_users_is_not_stalled_by_a_recursive_view(host, monkeypatch):
    alpha = _tenant(host, "alpha", "alpha")
    conn = sqlite3.connect(str(alpha / "bananawiki.db"))
    conn.execute(
        "CREATE VIEW users AS WITH RECURSIVE n(x) AS "
        "(SELECT 1 UNION ALL SELECT x + 1 FROM n) "
        "SELECT x AS username, 'user' AS role, '' AS created_at FROM n"
    )
    conn.commit()
    conn.close()
    _as_instance(monkeypatch, "alpha", status="running")
    started = time.monotonic()
    assert instance_manager.list_instance_users_page("a") == ([], 0)
    assert time.monotonic() - started < 10


def test_quarantine_refuses_symlinked_db_but_blocks_plugins(host, monkeypatch):
    alpha = _tenant(host, "alpha")
    beta = _tenant(host, "beta", "BETA-PRIVATE", plugins=[("ext", 1, 0)])
    os.symlink("../beta/bananawiki.db", str(alpha / "bananawiki.db"))
    _as_instance(monkeypatch, "alpha")

    ok, message = instance_manager.quarantine_instance_external_plugins("a")
    assert not ok
    assert "not safe" in message.lower()
    # The neighbour's DB was never opened or modified ...
    assert _query(str(beta / "bananawiki.db"), "SELECT enabled FROM plugins") == [(1,)]
    # ... no copy of it reached any snapshot ...
    snaps = instance_environment.plugin_snapshot_dir("a")
    assert not os.path.isdir(snaps) or not [n for n in os.listdir(snaps) if n.endswith(".db")]
    # ... and the kill switch is still in force.
    assert instance_environment.instance_plugins_quarantined("a")


def test_quarantine_ignores_links_planted_in_the_old_snapshot_folder(host, monkeypatch):
    """Audit scenario: pre-planted snapshot names pointing at hosting.db."""
    alpha = _tenant(host, "alpha", "ATTACKER-CRAFTED", plugins=[("ext", 1, 0)])
    snaps = alpha / "plugin_safety_snapshots"
    snaps.mkdir()
    for second in range(30):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() + second))
        os.symlink("../../../hosting.db", str(snaps / f"{stamp}-operator-quarantine.db"))
    before = _hosting_db_bytes(host)
    mode_before = os.stat(host / "hosting.db").st_mode
    _as_instance(monkeypatch, "alpha")

    ok, _message = instance_manager.quarantine_instance_external_plugins("a")
    assert ok
    assert _hosting_db_bytes(host) == before
    assert os.stat(host / "hosting.db").st_mode == mode_before


def test_restore_refuses_symlinked_live_db(host, monkeypatch):
    """Audit scenario: live DB swapped for a link to a victim before restore."""
    alpha = _tenant(host, "alpha", "alpha-own")
    beta = _tenant(host, "beta", "BETA-PRIVATE")
    _as_instance(monkeypatch, "alpha")
    ok, _msg = instance_manager.capture_plugin_safety_snapshot("a")
    assert ok
    for target in ("../beta/bananawiki.db", "../../hosting.db"):
        os.unlink(alpha / "bananawiki.db")
        os.symlink(target, str(alpha / "bananawiki.db"))
        before = _hosting_db_bytes(host)
        ok, message = instance_manager.restore_latest_plugin_safety_snapshot("a")
        assert not ok
        assert "not safe" in message.lower()
        assert _read_marker(str(beta / "bananawiki.db")) == "BETA-PRIVATE"
        assert _hosting_db_bytes(host) == before
    snaps = instance_environment.plugin_snapshot_dir("a")
    for name in os.listdir(snaps):
        if name.endswith(".db"):
            assert _read_marker(os.path.join(snaps, name)) == "alpha-own"


def test_build_archive_skips_links_and_special_files(host, monkeypatch, tmp_path):
    """Audit scenario: uploads that link to host secrets and a neighbour's DB."""
    alpha = _tenant(host, "alpha", "alpha")
    _tenant(host, "beta", "BETA-PRIVATE")
    uploads = alpha / "uploads"
    uploads.mkdir()
    os.symlink("../../../hosting.db", str(uploads / "p.png"))
    os.symlink("../../../.backup_encryption_key", str(uploads / "k.png"))
    os.symlink("../../../.secret_key", str(uploads / "s.png"))
    os.symlink("../../beta/bananawiki.db", str(uploads / "b.png"))
    os.symlink("../../beta", str(uploads / "beta_dir"))
    os.mkfifo(str(uploads / "pipe.png"))
    (uploads / "real.png").write_bytes(b"\x89PNG real bytes")
    (alpha / "storage" / "attachments").mkdir(parents=True)
    (alpha / "storage" / "attachments" / "doc.txt").write_text("attachment")
    # File times ZIP cannot store must not fail the export.
    os.utime(uploads / "real.png", (315532800 - 86400, 315532800 - 86400))
    os.utime(alpha / "storage" / "attachments" / "doc.txt", (32503680000, 32503680000))
    _as_instance(monkeypatch, "alpha", status="terminated")

    out = tmp_path / "out"
    out.mkdir()
    archive_path, _filename, error = instance_archives.build_instance_archive("a", str(out))
    assert error is None, error
    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
        assert "uploads/real.png" in names
        assert zf.read("uploads/real.png") == b"\x89PNG real bytes"
        assert zf.read("storage/attachments/doc.txt") == b"attachment"
        for leaked in ("p.png", "k.png", "s.png", "b.png", "pipe.png"):
            assert f"uploads/{leaked}" not in names
        assert not [n for n in names if n.startswith("uploads/beta_dir")]
        blobs = b"".join(zf.read(n) for n in names)
    assert b"PORTAL-SECRET-KEY" not in blobs
    assert b"K" * 32 not in blobs
    assert b"BETA-PRIVATE" not in blobs


def test_build_archive_refuses_symlinked_db(host, monkeypatch, tmp_path):
    alpha = _tenant(host, "alpha")
    _tenant(host, "beta", "BETA-PRIVATE")
    os.symlink("../beta/bananawiki.db", str(alpha / "bananawiki.db"))
    _as_instance(monkeypatch, "alpha", status="terminated")
    out = tmp_path / "out"
    out.mkdir()
    archive_path, _filename, error = instance_archives.build_instance_archive("a", str(out))
    assert archive_path is None
    assert error
    assert os.listdir(out) == []


def test_duplicate_refuses_symlinked_source_db(host, monkeypatch):
    src = _tenant(host, "src")
    _tenant(host, "victim", "VICTIM")
    os.symlink("../victim/bananawiki.db", str(src / "bananawiki.db"))
    _as_instance(monkeypatch, "src", instance_id="s")

    def _boom(*_a, **_k):
        raise AssertionError("provision_instance must not run for an unsafe source")

    monkeypatch.setattr(instance_manager, "provision_instance", _boom)
    inst, error = instance_manager.duplicate_instance("s", "acct", "clone")
    assert inst is None
    assert "not safe" in error.lower()


def test_duplicate_copies_real_assets_only_and_revokes_copied_tokens(host, monkeypatch):
    src = _tenant(
        host, "src", "SOURCE",
        users=[("u1", "admin", "admin")], tokens=[(1, "u1", 1)],
    )
    instance_manager._create_instance_dirs(str(src))
    (src / "storage" / "uploads" / "a.png").write_bytes(b"real upload")
    (src / "storage" / "uploads" / "nested").mkdir()
    (src / "storage" / "uploads" / "nested" / "b.png").write_bytes(b"nested upload")
    (src / "storage" / "attachments" / "c.pdf").write_bytes(b"attachment")
    os.symlink("../../../../.secret_key", str(src / "storage" / "uploads" / "leak.png"))
    os.mkfifo(str(src / "storage" / "uploads" / "pipe.png"))
    # A tenant repointing a portal-made asset link at a neighbour is ignored.
    victim = _tenant(host, "victim")
    (victim / "chat").mkdir()
    (victim / "chat" / "secret.txt").write_text("NEIGHBOUR")
    os.unlink(src / "chat_attachments")
    os.symlink("../victim/chat", str(src / "chat_attachments"))
    _as_instance(monkeypatch, "src", status="running", instance_id="s")

    clone = host / "instances" / "clone"

    def _fake_provision(*_a, **_k):
        instance_manager._create_instance_dirs(str(clone))
        _marker_db(str(clone / "bananawiki.db"), "EMPTY")
        return {"id": "c", "subdomain": "clone", "domain_mode": "hosting",
                "port": 6002, "status": "running"}, None

    monkeypatch.setattr(instance_manager, "provision_instance", _fake_provision)
    monkeypatch.setattr(instance_manager, "_start_process", lambda *_a, **_k: True)
    monkeypatch.setattr(instance_manager, "_spawn_post_restart_health_watch", lambda *_a, **_k: None)

    inst, error = instance_manager.duplicate_instance("s", "acct", "clone")
    assert error is None, error
    assert inst is not None
    assert _read_marker(str(clone / "bananawiki.db")) == "SOURCE"
    assert (clone / "uploads" / "a.png").read_bytes() == b"real upload"
    assert (clone / "uploads" / "nested" / "b.png").read_bytes() == b"nested upload"
    assert (clone / "attachments" / "c.pdf").read_bytes() == b"attachment"
    assert not os.path.lexists(clone / "uploads" / "leak.png")
    assert not os.path.lexists(clone / "uploads" / "pipe.png")
    assert not os.path.lexists(clone / "storage" / "chat_attachments" / "secret.txt")
    # Tokens issued for the source wiki do not work on the clone.
    assert _query(str(clone / "bananawiki.db"), "SELECT active FROM api_service__tokens") == [(0,)]
    assert _query(str(src / "bananawiki.db"), "SELECT active FROM api_service__tokens") == [(1,)]


def test_reset_wiki_does_not_follow_a_planted_storage_link(host, monkeypatch):
    alpha = _tenant(host, "alpha", "alpha")
    instance_manager._create_instance_dirs(str(alpha))
    victim = _tenant(host, "victim")
    instance_manager._create_instance_dirs(str(victim))
    (victim / "storage" / "uploads" / "keep.png").write_bytes(b"neighbour upload")
    # The tenant swaps its storage folder for a link to the neighbour's.
    os.rename(alpha / "storage", alpha / "storage.old")
    os.symlink("../victim/storage", str(alpha / "storage"))
    _as_instance(monkeypatch, "alpha", status="running")
    instance_environment.set_instance_plugin_quarantine("a", True)
    monkeypatch.setattr(instance_manager, "stop_instance", lambda _id: (True, ""))
    monkeypatch.setattr(instance_manager, "restart_instance", lambda _id: (True, ""))
    monkeypatch.setattr(instance_manager, "_seed_instance_db", lambda *_a, **_k: True)

    ok, _password = instance_manager.reset_wiki("a")
    assert ok
    assert (victim / "storage" / "uploads" / "keep.png").read_bytes() == b"neighbour upload"
    assert not os.path.islink(alpha / "storage")
    assert os.path.isdir(alpha / "storage" / "uploads")
    # A reset started by the tenant's owner does not lift the operator's quarantine.
    assert instance_environment.instance_plugins_quarantined("a")


def test_reset_password_refuses_symlinked_db(host, monkeypatch):
    alpha = _tenant(host, "alpha")
    beta = _tenant(host, "beta", "beta", users=[("b1", "admin", "admin")])
    os.symlink("../beta/bananawiki.db", str(alpha / "bananawiki.db"))
    _as_instance(monkeypatch, "alpha", status="running")
    ok, message = instance_manager.reset_instance_password("a")
    assert not ok
    assert "not safe" in message.lower()
    assert _query(str(beta / "bananawiki.db"), "SELECT password FROM users") == [("h",)]


def test_list_users_refuses_symlinked_db(host, monkeypatch):
    alpha = _tenant(host, "alpha")
    _tenant(host, "beta", "beta", users=[("b1", "beta-admin", "admin")])
    os.symlink("../beta/bananawiki.db", str(alpha / "bananawiki.db"))
    _as_instance(monkeypatch, "alpha", status="running")
    assert instance_manager.list_instance_users_page("a") == ([], 0)


def test_prepare_database_refuses_symlinked_db_without_migrating(host, monkeypatch):
    alpha = _tenant(host, "alpha")
    _tenant(host, "beta", "BETA")
    os.symlink("../beta/bananawiki.db", str(alpha / "bananawiki.db"))

    def _no_migrations(*_a, **_k):
        raise AssertionError("migrations must not run on a planted link")

    monkeypatch.setattr(instance_database, "_run_instance_db_migrations", _no_migrations)
    assert instance_database._prepare_instance_database(str(alpha)) is False


def test_prepare_database_leaves_migrations_to_a_running_container(host, monkeypatch):
    alpha = _tenant(host, "alpha", "alpha", gpu=(1, "http://gpu:8787", PLATFORM_GPU_TOKEN))
    monkeypatch.setattr(hosting_config, "HOSTING_INSTANCE_RUNTIME", "docker")
    monkeypatch.setattr(container_runtime, "container_is_running", lambda _d: True)

    def _no_migrations(*_a, **_k):
        raise AssertionError("the host must not migrate a live container's DB")

    monkeypatch.setattr(instance_database, "_run_instance_db_migrations", _no_migrations)
    assert instance_database._prepare_instance_database(str(alpha)) is True
    # The GPU token is still cleared through the checked connection.
    assert _query(str(alpha / "bananawiki.db"),
                  "SELECT tts_gpu_auth_token FROM site_settings") == [("",)]


def test_external_plugin_ids_skip_links_and_oversized_or_special_manifests(host):
    alpha = _tenant(host, "alpha")
    ext = alpha / "external_plugins"
    (ext / "honest").mkdir(parents=True)
    (ext / "honest" / "plugin.json").write_text('{"id": "renamed"}')
    (ext / "huge").mkdir()
    (ext / "huge" / "plugin.json").write_text('{"id": "x", "pad": "' + "a" * 100000 + '"}')
    (ext / "piped").mkdir()
    os.mkfifo(str(ext / "piped" / "plugin.json"))
    (ext / "linked_manifest").mkdir()
    os.symlink("../../../../.secret_key", str(ext / "linked_manifest" / "plugin.json"))
    os.symlink("../../beta", str(ext / "linked_dir"))
    ids = instance_manager._external_plugin_ids(str(alpha))
    assert set(ids) == {"honest", "renamed", "huge", "piped", "linked_manifest"}


# ---------------------------------------------------------------------------
# A#15 / A#17: kill switch by code location, in force across restarts.
# ---------------------------------------------------------------------------


def test_quarantine_disables_by_location_and_persists(host, monkeypatch):
    alpha = _tenant(
        host, "alpha", "alpha",
        plugins=[("hostile", 1, 1), ("honest_ext", 1, 0), ("chat", 1, 1)],
    )
    # A plugin that has run, promoted its own row to builtin=1 and planted a
    # trigger that silently cancels any UPDATE of the plugins table.
    conn = sqlite3.connect(str(alpha / "bananawiki.db"))
    conn.execute(
        "CREATE TRIGGER keep_me BEFORE UPDATE ON plugins "
        "BEGIN SELECT RAISE(IGNORE); END"
    )
    conn.commit()
    conn.close()
    hostile = alpha / "external_plugins" / "hostile"
    hostile.mkdir(parents=True)
    (hostile / "plugin.json").write_text('{"id": "hostile"}')
    _as_instance(monkeypatch, "alpha")

    ok, _message = instance_manager.quarantine_instance_external_plugins("a")
    assert ok
    rows = dict(_query(str(alpha / "bananawiki.db"), "SELECT id, enabled FROM plugins"))
    assert rows == {"hostile": 0, "honest_ext": 0, "chat": 1}
    # The quarantine persists host-side and forces external plugins off.
    assert instance_environment.instance_plugins_quarantined("a") is True
    env = instance_environment._instance_env(str(alpha), 6001, {
        "id": "a", "subdomain": "alpha", "account_id": None,
    })
    assert env["BW_ALLOW_EXTERNAL_PLUGINS"] == "0"
    assert env["BW_MANAGED_PLUGIN_QUARANTINE"] == "1"
    docker_env = container_runtime._container_env(env, str(alpha), 5001)
    assert docker_env["BW_ALLOW_EXTERNAL_PLUGINS"] == "0"
    # The marker is outside the tenant's data dir.
    marker_dir = instance_environment.platform_state_dir("a")
    assert not marker_dir.startswith(os.path.realpath(host / "instances"))


def test_quarantine_restarts_running_tenant_with_data_dir(host, monkeypatch):
    alpha = _tenant(host, "alpha", "alpha", plugins=[("ext", 1, 0)])
    _as_instance(monkeypatch, "alpha", status="running")
    started = []

    def _fake_start(inst, data_dir, **_kwargs):
        # A signature that requires data_dir catches the old _start_process(inst) bug.
        started.append(data_dir)
        return True

    monkeypatch.setattr(instance_manager, "_start_process", _fake_start)
    ok, _message = instance_manager.quarantine_instance_external_plugins("a")
    assert ok
    assert started == [str(alpha)]


def test_lift_quarantine_clears_marker(host, monkeypatch):
    _tenant(host, "alpha", "alpha", plugins=[("ext", 1, 0)])
    instance_environment.set_instance_plugin_quarantine("a", True)
    _as_instance(monkeypatch, "alpha")
    ok, _message = instance_manager.lift_instance_plugin_quarantine("a")
    assert ok
    assert instance_environment.instance_plugins_quarantined("a") is False
    ok, _message = instance_manager.lift_instance_plugin_quarantine("a")
    assert not ok


def test_container_env_honours_quarantine():
    base = {"BW_DATABASE_PATH": "/x/bananawiki.db", "BW_MANAGED_PLUGIN_QUARANTINE": "1"}
    env = container_runtime._container_env(base, "/x", 5001)
    assert env["BW_ALLOW_EXTERNAL_PLUGINS"] == "0"
    env2 = container_runtime._container_env({"BW_DATABASE_PATH": "/x/bananawiki.db"}, "/x", 5001)
    assert env2["BW_ALLOW_EXTERNAL_PLUGINS"] == "1"


def test_platform_state_dir_rejects_bad_ids_and_tenant_reachable_roots(host, monkeypatch):
    with pytest.raises(ValueError):
        instance_environment.platform_state_dir("../alpha")
    monkeypatch.setattr(
        hosting_config, "HOSTING_PLATFORM_STATE_DIR", str(host / "instances" / "state")
    )
    with pytest.raises(RuntimeError):
        instance_environment.platform_state_dir("a")


# ---------------------------------------------------------------------------
# A#18: restore trusts only platform-taken, host-stored snapshots.
# ---------------------------------------------------------------------------


def test_restore_uses_only_host_owned_snapshot(host, monkeypatch):
    alpha = _tenant(host, "alpha", "current")
    live = alpha / "bananawiki.db"
    _as_instance(monkeypatch, "alpha")
    ok, _msg = instance_manager.capture_plugin_safety_snapshot("a")
    assert ok
    # The tenant then changes its DB and plants a "newer" snapshot in its own
    # writable snapshot folder, with a far-future mtime.
    conn = sqlite3.connect(str(live))
    conn.execute("UPDATE marker SET value='TAMPERED'")
    conn.commit()
    conn.close()
    tenant_snaps = alpha / "plugin_safety_snapshots"
    tenant_snaps.mkdir()
    planted = tenant_snaps / "20990101T000000Z-evil.db"
    _marker_db(str(planted), "ATTACKER-CONTENT")
    os.utime(planted, (4102444800, 4102444800))

    ok, _msg = instance_manager.restore_latest_plugin_safety_snapshot("a")
    assert ok
    assert _read_marker(str(live)) == "current"
    # The DB as it was before the restore is kept, and plugins stay off.
    kept = [s for s in instance_manager.list_plugin_safety_snapshots("a")
            if s["label"] == "before-restore"]
    assert len(kept) == 1
    snap_dir = instance_environment.plugin_snapshot_dir("a")
    assert _read_marker(os.path.join(snap_dir, kept[0]["name"])) == "TAMPERED"
    assert instance_environment.instance_plugins_quarantined("a")


def test_restore_never_picks_quarantine_or_before_restore_copies(host, monkeypatch):
    alpha = _tenant(host, "alpha", "good", plugins=[("ext", 1, 0)])
    live = str(alpha / "bananawiki.db")
    _as_instance(monkeypatch, "alpha")
    assert instance_manager.capture_plugin_safety_snapshot("a")[0]
    conn = sqlite3.connect(live)
    conn.execute("UPDATE marker SET value='after-plugin'")
    conn.commit()
    conn.close()
    assert instance_manager.quarantine_instance_external_plugins("a")[0]
    assert instance_manager.restore_latest_plugin_safety_snapshot("a")[0]
    assert _read_marker(live) == "good"
    # A second restore still goes back to the pre-enable copy, not to the
    # before-restore copy the first restore saved.
    assert instance_manager.restore_latest_plugin_safety_snapshot("a")[0]
    assert _read_marker(live) == "good"


def test_restore_refuses_without_platform_snapshot(host, monkeypatch):
    alpha = _tenant(host, "alpha", "current")
    tenant_snaps = alpha / "plugin_safety_snapshots"
    tenant_snaps.mkdir()
    _marker_db(str(tenant_snaps / "20990101T000000Z-evil.db"), "ATTACKER-CONTENT")
    _as_instance(monkeypatch, "alpha")
    ok, message = instance_manager.restore_latest_plugin_safety_snapshot("a")
    assert not ok
    assert "saved by the platform" in message
    assert _read_marker(str(alpha / "bananawiki.db")) == "current"


def test_restore_rejects_tampered_and_corrupt_snapshots_without_stopping(host, monkeypatch):
    alpha = _tenant(host, "alpha", "current")
    _as_instance(monkeypatch, "alpha", status="running")
    stopped = []
    monkeypatch.setattr(instance_manager, "_stop_process", lambda *_a, **_k: stopped.append(True))
    assert instance_manager.capture_plugin_safety_snapshot("a")[0]
    snap = instance_manager.list_plugin_safety_snapshots("a")[0]
    snap_path = os.path.join(instance_environment.plugin_snapshot_dir("a"), snap["name"])

    conn = sqlite3.connect(snap_path)
    conn.execute("UPDATE marker SET value='edited'")
    conn.commit()
    conn.close()
    ok, message = instance_manager.restore_latest_plugin_safety_snapshot("a")
    assert not ok
    assert "checksum" in message

    with open(snap_path, "wb") as fh:
        fh.write(b"not sqlite")
    index = instance_manager._read_snapshot_index("a")
    index[snap["name"]]["sha256"] = instance_manager._sha256_file(snap_path)
    instance_manager._write_snapshot_index("a", index)
    ok, message = instance_manager.restore_latest_plugin_safety_snapshot("a")
    assert not ok
    assert "unreadable" in message.lower() or "integrity" in message.lower()
    assert stopped == []
    assert _read_marker(str(alpha / "bananawiki.db")) == "current"


def test_restore_named_snapshot_and_pruning(host, monkeypatch):
    alpha = _tenant(host, "alpha", "v0")
    live = str(alpha / "bananawiki.db")
    _as_instance(monkeypatch, "alpha")
    for version in range(1, 8):
        conn = sqlite3.connect(live)
        conn.execute("UPDATE marker SET value=?", (f"v{version}",))
        conn.commit()
        conn.close()
        assert instance_manager.capture_plugin_safety_snapshot("a")[0]
    snaps = instance_manager.list_plugin_safety_snapshots("a")
    assert len(snaps) == instance_manager._PLATFORM_SNAPSHOT_KEEP
    on_disk = [n for n in os.listdir(instance_environment.plugin_snapshot_dir("a"))
               if n.endswith(".db")]
    assert len(on_disk) == instance_manager._PLATFORM_SNAPSHOT_KEEP
    oldest_kept = snaps[-1]["name"]
    ok, _msg = instance_manager.restore_latest_plugin_safety_snapshot(
        "a", snapshot_name=oldest_kept
    )
    assert ok
    assert _read_marker(live) == "v3"
    ok, message = instance_manager.restore_latest_plugin_safety_snapshot(
        "a", snapshot_name="../../hosting.db"
    )
    assert not ok
    assert "not available" in message


# ---------------------------------------------------------------------------
# B#8: the portal reads a bounded page of tenant users plus a count.
# ---------------------------------------------------------------------------


def test_list_instance_users_is_bounded_and_counted(host, monkeypatch):
    users = [(f"u{i:03d}", f"user{i:03d}", "user") for i in range(250)]
    _tenant(host, "alpha", "alpha", users=users)
    _as_instance(monkeypatch, "alpha", status="running")
    page, total = instance_manager.list_instance_users_page("a")
    assert total == 250
    assert len(page) == instance_manager._INSTANCE_USER_LIST_LIMIT
    page, total = instance_manager.list_instance_users_page("a", limit=10, offset=245)
    assert [row["username"] for row in page] == [f"user{i:03d}" for i in range(245, 250)]
    assert len(instance_manager.list_instance_users("a", limit=100000)) == (
        instance_manager._INSTANCE_USER_LIST_LIMIT
    )


# ---------------------------------------------------------------------------
# B#4: portal password resets revoke the reset user's wiki API tokens.
# ---------------------------------------------------------------------------


def test_reset_password_revokes_only_the_reset_users_tokens(host, monkeypatch):
    from hosting.db import create_account, create_instance

    aid = create_account("tokenowner", "hash")
    inst = create_instance(aid, "tokenwiki", admin_username="admin", admin_password="pw")
    data_dir = host / "instances" / "tokenwiki"
    data_dir.mkdir(exist_ok=True)
    db_path = str(data_dir / "bananawiki.db")
    _marker_db(
        db_path, "wiki",
        users=[("uid-admin", "admin", "admin"), ("uid-other", "coadmin", "admin")],
        tokens=[(1, "uid-admin", 1), (2, "uid-admin", 1), (3, "uid-other", 1)],
    )
    # A trigger that would silently keep tokens active is not honoured.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TRIGGER keep_tokens BEFORE UPDATE ON api_service__tokens "
        "BEGIN SELECT RAISE(IGNORE); END"
    )
    conn.commit()
    conn.close()

    ok, _pw = instance_manager.reset_instance_password(inst["id"])
    assert ok
    rows = dict(_query(db_path, "SELECT id, active FROM api_service__tokens"))
    assert rows == {1: 0, 2: 0, 3: 1}
    assert _query(db_path, "SELECT force_password_change FROM users WHERE id='uid-admin'") == [(1,)]


def test_reset_password_falls_back_to_all_admins(host, monkeypatch):
    alpha = _tenant(
        host, "alpha", "alpha",
        users=[("o1", "someone", "owner"), ("a1", "other", "admin"), ("u1", "plain", "user")],
        tokens=[(1, "o1", 1), (2, "a1", 1), (3, "u1", 1)],
    )
    row = _as_instance(monkeypatch, "alpha", status="running")
    # The provisioned admin account no longer exists in the wiki.
    row["admin_username"] = "gone"
    ok, _pw = instance_manager.reset_instance_password("a")
    assert ok
    rows = dict(_query(str(alpha / "bananawiki.db"), "SELECT id, active FROM api_service__tokens"))
    assert rows == {1: 0, 2: 0, 3: 1}


def test_set_instance_user_password_revokes_that_users_tokens(host, monkeypatch):
    alpha = _tenant(
        host, "alpha", "alpha",
        users=[("u1", "alice", "user"), ("u2", "bob", "user")],
        tokens=[(1, "u1", 1), (2, "u2", 1)],
    )
    _as_instance(monkeypatch, "alpha", status="running")
    ok, _reason = instance_manager.set_instance_user_password("a", "alice", "new-password")
    assert ok
    rows = dict(_query(str(alpha / "bananawiki.db"), "SELECT id, active FROM api_service__tokens"))
    assert rows == {1: 0, 2: 1}


# ---------------------------------------------------------------------------
# B#0 / A#35: platform GPU token never kept in a tenant DB; injected via env.
# ---------------------------------------------------------------------------


def test_safety_defaults_clear_copied_gpu_token(host):
    alpha = _tenant(host, "alpha", "alpha", gpu=(1, "http://gpu.internal:8787", PLATFORM_GPU_TOKEN))
    db_path = str(alpha / "bananawiki.db")
    assert instance_database._apply_hosted_db_safety_defaults(db_path) is True
    assert _query(
        db_path,
        "SELECT tts_gpu_enabled, tts_gpu_url, tts_gpu_auth_token FROM site_settings WHERE id=1",
    ) == [(0, "", "")]


def test_prepare_database_clears_gpu_token_even_if_migrations_fail(host, monkeypatch):
    alpha = _tenant(host, "alpha", "alpha", gpu=(1, "http://gpu.internal:8787", PLATFORM_GPU_TOKEN))
    monkeypatch.setattr(instance_database, "_run_instance_db_migrations", lambda *_a: False)
    assert instance_database._prepare_instance_database(str(alpha)) is False
    assert _query(str(alpha / "bananawiki.db"),
                  "SELECT tts_gpu_auth_token FROM site_settings") == [("",)]


def test_instance_env_injects_gpu_only_for_allowed(host):
    from hosting.db import create_account, create_instance, get_instance, update_hosting_settings

    update_hosting_settings(
        global_tts_gpu_enabled=1,
        global_tts_gpu_url="http://gpu.internal:8787/",
        global_tts_gpu_auth_token=PLATFORM_GPU_TOKEN,
        global_tts_gpu_timeout=45,
        global_tts_enabled=1,
        global_tts_mode="blacklist",
        global_tts_list="blocked",
    )
    aid = create_account("gpuowner", "hash")
    allowed = create_instance(aid, "allowed")
    blocked = create_instance(aid, "blocked")
    instances = host / "instances"

    env_ok = instance_environment._instance_env(
        str(instances / "allowed"), allowed["port"], dict(get_instance(allowed["id"]))
    )
    assert env_ok["BW_TTS_BACKEND"] == "remote-gpu"
    assert env_ok["BW_TTS_REMOTE_GPU_URL"] == "http://gpu.internal:8787"
    assert env_ok["BW_TTS_REMOTE_GPU_AUTH_TOKEN"] == PLATFORM_GPU_TOKEN
    assert env_ok["BW_TTS_REMOTE_GPU_TIMEOUT"] == "45"

    env_blocked = instance_environment._instance_env(
        str(instances / "blocked"), blocked["port"], dict(get_instance(blocked["id"]))
    )
    assert not any(key.startswith("BW_TTS_REMOTE_GPU") for key in env_blocked)
    assert "BW_TTS_BACKEND" not in env_blocked
    assert env_blocked.get("BW_MANAGED_TTS_DISABLED") == "1"


# ---------------------------------------------------------------------------
# A#19: operator denylist forwarded into tenants.
# ---------------------------------------------------------------------------


def test_instance_env_forwards_plugin_denylist(host, monkeypatch):
    from hosting.db import create_account, create_instance, get_instance

    monkeypatch.setattr(hosting_config, "HOSTING_TENANT_PLUGIN_DENYLIST", "banana_ai,file_manager")
    aid = create_account("denyowner", "hash")
    inst = create_instance(aid, "denywiki")
    env = instance_environment._instance_env(
        str(host / "instances" / "denywiki"), inst["port"], dict(get_instance(inst["id"]))
    )
    assert env["BW_MANAGED_PLUGIN_DENYLIST"] == "banana_ai,file_manager"
    docker_env = container_runtime._container_env(env, str(host / "instances" / "denywiki"), 5001)
    assert docker_env["BW_MANAGED_PLUGIN_DENYLIST"] == "banana_ai,file_manager"


# ---------------------------------------------------------------------------
# A#20: outbound tenant networking is warned about; onion cannot be isolated.
# ---------------------------------------------------------------------------


def test_outbound_network_warning(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(container_runtime, "_OUTBOUND_WARNING_EMITTED", False)
    monkeypatch.setattr(container_runtime.config, "HOSTING_TENANT_NETWORK_OUTBOUND", True)
    monkeypatch.setattr(container_runtime.config, "HOSTING_MODE", "onion")
    with caplog.at_level(logging.WARNING, logger="hosting.container_runtime"):
        assert container_runtime.warn_if_tenant_network_outbound() is True
        assert container_runtime.warn_if_tenant_network_outbound() is False
    warnings = [r for r in caplog.records if "outbound network access" in r.message]
    assert len(warnings) == 1
    assert "DOCKER-USER" in warnings[0].message

    monkeypatch.setattr(container_runtime, "_OUTBOUND_WARNING_EMITTED", False)
    monkeypatch.setattr(container_runtime.config, "HOSTING_TENANT_NETWORK_OUTBOUND", False)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hosting.container_runtime"):
        assert container_runtime.warn_if_tenant_network_outbound() is False
    assert not caplog.records


def test_portal_startup_recovery_warns_about_outbound_networking(monkeypatch):
    calls = []
    monkeypatch.setattr(container_runtime, "warn_if_tenant_network_outbound",
                        lambda: calls.append(True))
    monkeypatch.setattr(instance_manager, "get_all_active_instances", lambda: [])
    instance_manager.recover_running_instances()
    assert calls == [True]


def _import_config(tmp_path, **env):
    full_env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("HOSTING_", "INSTANCES_DIR"))
    }
    full_env.update({
        "HOSTING_DATABASE_PATH": str(tmp_path / "hosting.db"),
        "INSTANCES_DIR": str(tmp_path / "instances"),
        "HOSTING_SECRET_KEY": "x" * 64,
    })
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", "import hosting.config"],
        cwd=REPO_ROOT, env=full_env, capture_output=True, text=True, timeout=60,
    )


def test_config_refuses_isolated_onion_and_state_dir_inside_instances(tmp_path):
    result = _import_config(
        tmp_path, HOSTING_MODE="onion", HOSTING_TENANT_NETWORK="isolated",
        HOSTING_INSTANCE_RUNTIME="docker",
    )
    assert result.returncode != 0
    assert "Isolated tenants require subdomain hosting" in result.stderr

    result = _import_config(
        tmp_path, HOSTING_MODE="onion", HOSTING_INSTANCE_RUNTIME="docker",
    )
    assert result.returncode == 0, result.stderr

    result = _import_config(
        tmp_path, HOSTING_PLATFORM_STATE_DIR=str(tmp_path / "instances" / "state"),
    )
    assert result.returncode != 0
    assert "HOSTING_PLATFORM_STATE_DIR" in result.stderr


# ---------------------------------------------------------------------------
# Admin routes for plugin safety: they work end to end and are recorded.
# ---------------------------------------------------------------------------


def test_plugin_safety_routes_are_recorded_in_instance_events(host, monkeypatch):
    from hosting.app import create_hosting_app
    from hosting.db import create_instance, get_account_by_username
    from hosting.db._events import list_events

    app = create_hosting_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/signup", data={
        "username": "platformadmin", "password": "password123",
        "confirm_password": "password123",
    }, follow_redirects=True)
    client.post("/login", data={"username": "platformadmin", "password": "password123"})
    admin = get_account_by_username("platformadmin")
    assert admin and admin["is_admin"]
    inst = create_instance(admin["id"], "routewiki")
    data_dir = host / "instances" / "routewiki"
    data_dir.mkdir(exist_ok=True)
    live = str(data_dir / "bananawiki.db")
    _marker_db(live, "good", plugins=[("ext", 1, 0)])
    monkeypatch.setattr(instance_manager, "_start_process", lambda *_a, **_k: True)
    base = f"/admin/instances/{inst['id']}"

    assert client.post(f"{base}/capture-plugin-snapshot").status_code == 302
    snaps = instance_manager.list_plugin_safety_snapshots(inst["id"])
    assert [s["label"] for s in snaps] == ["pre-enable"]
    conn = sqlite3.connect(live)
    conn.execute("UPDATE marker SET value='changed'")
    conn.commit()
    conn.close()
    assert client.post(f"{base}/quarantine-plugins").status_code == 302
    assert instance_environment.instance_plugins_quarantined(inst["id"])
    page = client.get(f"{base}/manage")
    assert page.status_code == 200
    assert client.post(
        f"{base}/restore-plugin-snapshot", data={"snapshot": snaps[0]["name"]}
    ).status_code == 302
    assert _read_marker(live) == "good"
    assert client.post(f"{base}/lift-plugin-quarantine").status_code == 302
    assert not instance_environment.instance_plugins_quarantined(inst["id"])

    actions = [row["action"] for row in list_events("instance", inst["id"])]
    for action in ("plugins.snapshot_saved", "plugins.quarantined",
                   "plugins.snapshot_restored", "plugins.quarantine_lifted"):
        assert action in actions
    rows = list_events("instance", inst["id"])
    assert all(row["actor_id"] == admin["id"] for row in rows if row["action"].startswith("plugins."))


# ---------------------------------------------------------------------------
# Diagnostics, platform backups and the proxy read tenant files without
# following links.
# ---------------------------------------------------------------------------


def test_diagnostics_do_not_follow_tenant_links_or_block_on_fifos(host, monkeypatch):
    from hosting import instance_diagnostics

    alpha = _tenant(host, "alpha")
    os.symlink("../../hosting.db", str(alpha / "error.log"))
    os.symlink("../../.secret_key", str(alpha / ".boot_id"))
    os.mkfifo(str(alpha / "bananawiki.pid"))
    (alpha / "real.txt").write_bytes(b"0123456789")
    os.symlink("../../hosting.db", str(alpha / "big.bin"))
    row = _as_instance(monkeypatch, "alpha")
    monkeypatch.setattr(instance_diagnostics, "get_instance", lambda _id: dict(row))

    content, _path, size, error = instance_diagnostics.read_instance_log("a")
    assert content == "" and size == 0 and error
    assert instance_diagnostics._read_stored_boot_id(str(alpha)) is None
    started = time.monotonic()
    assert instance_diagnostics._read_pid(str(alpha)) is None
    assert time.monotonic() - started < 2
    assert instance_diagnostics.get_storage_bytes(str(alpha), force_refresh=True) == 10


def test_analytics_refuse_a_database_linked_to_another_tenant(host, monkeypatch):
    from hosting import instance_diagnostics

    alpha = _tenant(host, "alpha")
    _tenant(host, "beta", "BETA-PRIVATE")
    os.symlink("../beta/bananawiki.db", str(alpha / "bananawiki.db"))
    row = _as_instance(monkeypatch, "alpha")
    monkeypatch.setattr(instance_diagnostics, "get_instance", lambda _id: dict(row))

    summary, error = instance_diagnostics.read_instance_analytics("a")
    assert summary is None and error


def test_platform_backup_skips_tenant_links_and_special_files(host):
    from hosting.backups import build_full_backup

    alpha = _tenant(host, "alpha", "alpha")
    beta = _tenant(host, "beta")
    uploads = alpha / "uploads"
    uploads.mkdir()
    os.symlink("../../../.backup_encryption_key", str(uploads / "k.png"))
    os.symlink("../../beta", str(uploads / "beta_dir"))
    os.mkfifo(str(uploads / "pipe.png"))
    (uploads / "real.png").write_bytes(b"real bytes")
    os.symlink("../alpha/bananawiki.db", str(beta / "bananawiki.db"))

    [path] = build_full_backup()
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            assert zf.read("instances/alpha/uploads/real.png") == b"real bytes"
            assert "instances/alpha/bananawiki.db" in names
            assert not [n for n in names if n.startswith("instances/beta/")]
            for leaked in ("k.png", "pipe.png"):
                assert f"instances/alpha/uploads/{leaked}" not in names
            assert not [n for n in names if "beta_dir" in n]
            tenant_blobs = b"".join(zf.read(n) for n in names if n.startswith("instances/"))
    finally:
        os.unlink(path)
    assert b"K" * 32 not in tenant_blobs


def test_proxy_ignores_a_linked_starting_lock(host):
    from hosting import _subdomain_proxy

    alpha = _tenant(host, "alpha")
    (host / "fresh").write_text("")
    os.symlink("../../fresh", str(alpha / ".starting"))
    assert not _subdomain_proxy._is_instance_starting("alpha")
    os.unlink(str(alpha / ".starting"))
    (alpha / ".starting").write_text("")
    assert _subdomain_proxy._is_instance_starting("alpha")


def test_admin_manage_page_offers_the_plugin_safety_controls(host, monkeypatch):
    from helpers._passwords import generate_password_hash
    from hosting.app import create_hosting_app
    from hosting.db import create_account, create_instance
    from hosting.routes import dashboard_instance_admin

    admin = create_account("root", generate_password_hash("password123"), is_admin=True)
    inst = create_instance(admin, "alpha", "wikiadmin", "one-time-secret")
    app = create_hosting_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = app.test_client()
    assert client.post("/login", data={"username": "root", "password": "password123"}).status_code == 302
    monkeypatch.setattr(dashboard_instance_admin, "list_instance_users_page", lambda _id: (
        [{"username": "first", "role": "user", "created_at": "2026-01-01"}], 250,
    ))
    monkeypatch.setattr(dashboard_instance_admin, "list_plugin_safety_snapshots", lambda _id: [{
        "name": "pre-enable-1.db", "label": "pre-enable", "created_at": "2026-09-01T10:00:00+00:00",
        "size": 2048, "restorable": True,
    }])
    manage = f"/admin/instances/{inst['id']}/manage"

    monkeypatch.setattr(dashboard_instance_admin, "instance_plugins_quarantined", lambda _id: True)
    page = client.get(manage).get_data(as_text=True)
    assert "/capture-plugin-snapshot" in page
    assert "/lift-plugin-quarantine" in page
    assert "/quarantine-plugins" not in page
    assert 'name="snapshot"' in page and 'value="pre-enable-1.db"' in page
    assert '<span class="count-badge">250</span>' in page
    assert "Showing the first 1 of 250 users." in page

    monkeypatch.setattr(dashboard_instance_admin, "instance_plugins_quarantined", lambda _id: False)
    page = client.get(manage).get_data(as_text=True)
    assert "/quarantine-plugins" in page
    assert "/lift-plugin-quarantine" not in page
