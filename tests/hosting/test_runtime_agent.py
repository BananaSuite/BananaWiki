"""The production runtime: lifecycle, environment, status, tenant database tools, plugin safety, routing."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from bananawiki.hosting.runtime import OAuthClient, RuntimeFailure, TenantPolicy, TtsGpu, load_runtime
from bananawiki.hosting.runtime.agent import AgentRuntime, tenant_environment
from bananawiki.ops.runtime_agent import AgentError, TenantRuntime

from .agent_fakes import WIKI_PASSWORD, make_runtime, make_spec, provision


@pytest.fixture
def setup(tmp_path):
    return make_runtime(tmp_path)


def _db(runtime: AgentRuntime, name: str = "acme") -> sqlite3.Connection:
    conn = sqlite3.connect(Path(runtime._cfg().instances_dir) / name / "bananawiki.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_backend_is_the_default():
    assert isinstance(load_runtime("agent"), AgentRuntime)


def test_provision_creates_the_1x_layout_seeds_inside_the_sandbox_and_starts(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    root = Path(runtime._cfg().instances_dir) / "acme"
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    for name in ("uploads", "attachments", "chat_attachments", "kanban_attachments", "custom_page_files"):
        assert (root / "storage" / name).is_dir()
        assert os.readlink(root / name) == f"storage/{name}", "relative alias like 1.4 (C12)"
    assert agent.tasks() == ["seed"]
    started = agent.ops("tenant.start")[0]
    assert started["tenant"] == "acme" and started["network"] == "isolated" and "publish_port" not in started
    assert started["limits"] == {"memory_mb": 768, "cpus": 1.0, "pids": 256, "nofile": 1024, "storage_bytes": 0}
    with _db(runtime) as conn:
        owner = conn.execute("SELECT * FROM users").fetchone()
        assert owner["username"] == "owner1" and owner["role"] == "owner" and owner["force_password_change"] == 1
        assert owner["password"] != WIKI_PASSWORD
        assert conn.execute("SELECT setup_done FROM site_settings").fetchone()[0] == 1
    with pytest.raises(RuntimeFailure) as error:
        provision(runtime, spec)
    assert error.value.code == "data_exists"


def test_start_is_idempotent_and_recreates_on_policy_change(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    runtime.start(spec)
    assert len(agent.ops("tenant.start")) == 1, "an up-to-date container is left alone"
    runtime.start(replace(spec, policy=TenantPolicy(easy_wiki=True)))
    assert len(agent.ops("tenant.start")) == 2
    assert agent.ops("tenant.start")[-1]["env"]["BW_EASY_WIKI"] == "1"


@pytest.mark.parametrize("key,value", [("quota_protected", False), ("quota_protected", None),
                                     ("ipv6_disabled", False), ("ipv6_disabled", None)])
def test_start_replaces_outdated_sandbox_even_when_image_and_policy_match(setup, key, value):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    agent.containers[spec.data_dir_name][key] = value
    runtime.start(spec)
    assert len(agent.ops("tenant.start")) == 2
    assert agent.containers[spec.data_dir_name][key] is True


def test_start_clears_stale_1x_state_and_restores_missing_aliases(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    root = Path(runtime._cfg().instances_dir) / "acme"
    (root / "bananawiki.pid").write_text("123")
    (root / "uploads").unlink()
    runtime.restart(spec)
    assert not (root / "bananawiki.pid").exists()
    assert os.readlink(root / "uploads") == "storage/uploads"


def test_layout_repair_does_not_follow_a_planted_storage_link(setup, tmp_path):
    """A tenant that swaps ``storage`` for a link must not make the portal create folders elsewhere."""
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    root = Path(runtime._cfg().instances_dir) / "acme"
    elsewhere = tmp_path / "other-tenant"
    elsewhere.mkdir()
    (root / "storage").rename(root / "moved")
    (root / "storage").symlink_to(elsewhere)
    with pytest.raises(RuntimeFailure) as error:
        runtime.restart(spec)
    assert error.value.code == "db_unsafe"
    assert list(elsewhere.iterdir()) == []


def test_port_mode_publishes_on_loopback(tmp_path):
    runtime, agent = make_runtime(tmp_path, BASE_DOMAIN="", HOSTING_MODE="port")
    provision(runtime, make_spec(hostnames=()))
    started = agent.ops("tenant.start")[0]
    assert started["network"] == "outbound" and started["publish_port"] == 6001


def test_outbound_network_is_opt_in(tmp_path):
    runtime, agent = make_runtime(tmp_path, HOSTING_TENANT_NETWORK="outbound")
    provision(runtime, make_spec())
    assert agent.ops("tenant.start")[0]["network"] == "outbound"


def test_environment_maps_the_policy_and_keeps_portal_secrets_out(tmp_path):
    runtime, _agent = make_runtime(tmp_path, HOSTING_ALLOW_TENANT_PLUGINS="1")
    policy = TenantPolicy(
        easy_wiki=True, forbid_public_mode=False, storage_limit_bytes=5 * 1024 * 1024, upload_max_bytes=32 * 1024 ** 2,
        max_request_bytes=16 * 1024 ** 2, blocked_extensions=("exe", "bat"), expires_at="2027-01-01 00:00:00",
        tts_gpu=TtsGpu("https://gpu.example", "gpu-token", 90), plugin_denylist=("evil",),
        oauth=OAuthClient("bw_client", "client-secret", "https://portal.example", "inst1"),
    )
    env = tenant_environment(make_spec(policy=policy), runtime._cfg(), quarantined=False)
    assert env["BW_EASY_WIKI"] == "1" and env["BW_FORBID_PUBLIC_MODE"] == "0"
    assert env["BW_STORAGE_LIMIT_BYTES"] == str(5 * 1024 * 1024)
    assert env["BW_MAX_CONTENT_LENGTH_BYTES"] == str(32 * 1024 ** 2)
    assert env["BW_PLATFORM_UPLOAD_BLACKLIST"] == "exe,bat"
    assert env["BW_TTS_BACKEND"] == "remote-gpu" and env["BW_TTS_REMOTE_GPU_AUTH_TOKEN"] == "gpu-token"
    assert env["BW_PLATFORM_OAUTH_LINK_STATUS_URL"] == "https://portal.example/oauth/link-status"
    assert env["BW_PLATFORM_OAUTH_CLIENT_SECRET"] == "client-secret" and env["BW_PLATFORM_INSTANCE_ID"] == "inst1"
    assert env["BW_ALLOW_EXTERNAL_PLUGINS"] == "1" and env["BW_SESSION_COOKIE_NAME"] == "bw_session_acme"
    assert all(key.startswith("BW_") for key in env)
    assert runtime._cfg().secret_key not in env.values()
    assert tenant_environment(make_spec(), runtime._cfg(), quarantined=True)["BW_ALLOW_EXTERNAL_PLUGINS"] == "0"


def test_hosted_third_party_plugins_require_operator_opt_in(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    spec = make_spec()
    provision(runtime, spec)
    assert agent.ops("tenant.start")[-1]["env"]["BW_ALLOW_EXTERNAL_PLUGINS"] == "0"
    plugin_dir = Path(runtime._cfg().instances_dir) / spec.data_dir_name / "external_plugins" / "custom"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("# preserved on upgrade")
    runtime._state(runtime._cfg(), spec).set_quarantine(True)
    runtime.lift_plugin_quarantine(spec)
    assert agent.ops("tenant.start")[-1]["env"]["BW_ALLOW_EXTERNAL_PLUGINS"] == "0"
    assert (plugin_dir / "plugin.py").read_text() == "# preserved on upgrade"


def test_stop_destroy_and_relocate(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    runtime.stop(spec)
    runtime.stop(spec)
    assert "acme" not in agent.containers
    runtime.relocate(spec.instance_id, "acme", "acme--terminated-idacme")
    base = Path(runtime._cfg().instances_dir)
    assert (base / "acme--terminated-idacme" / "bananawiki.db").is_file()
    with pytest.raises(RuntimeFailure) as error:
        runtime.relocate(spec.instance_id, "missing", "other")
    assert error.value.code == "not_found"
    (base / "taken").mkdir()
    with pytest.raises(RuntimeFailure) as error:
        runtime.relocate(spec.instance_id, "acme--terminated-idacme", "taken")
    assert error.value.code == "data_exists"
    with pytest.raises(RuntimeFailure):
        runtime.relocate(spec.instance_id, "acme--terminated-idacme", "../escape")
    moved = replace(spec, data_dir_name="acme--terminated-idacme")
    runtime._state(runtime._cfg(), moved).set_quarantine(True)
    runtime.destroy(moved)
    runtime.destroy(moved)
    assert not (base / "acme--terminated-idacme").exists()
    assert not runtime._state(runtime._cfg(), moved).root.exists()


def test_destroy_does_not_follow_planted_links(setup, tmp_path):
    runtime, _agent = setup
    spec = make_spec()
    provision(runtime, spec)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("host file")
    (Path(runtime._cfg().instances_dir) / "acme" / "storage" / "uploads" / "link").symlink_to(outside)
    runtime.destroy(spec)
    assert (outside / "keep.txt").read_text() == "host file"


def test_status_reports_health_and_caches(setup):
    runtime, agent = setup
    spec = make_spec()
    assert runtime.status(spec).state == "missing"
    provision(runtime, spec)
    assert runtime.status(spec).state == "running"
    runtime.health["ok"] = False
    assert runtime.status(spec).state == "running", "positive results are cached"
    runtime._forget("acme")
    assert runtime.status(spec).state == "unhealthy", "started long ago and not answering"
    runtime.stop(spec)
    assert runtime.status(spec).state == "stopped"
    agent.failures["tenant.list"] = AgentError("unavailable", "down")
    runtime._forget("acme")
    assert runtime.status(spec).state == "unknown"


def test_upstream_is_the_running_containers_bridge_address(setup):
    runtime, agent = setup
    spec = make_spec()
    assert runtime.upstream(spec) is None
    provision(runtime, spec)
    item = agent.containers["acme"]
    assert runtime.upstream(spec) == (item["address"], item["internal_port"])
    item["address"] = "127.0.0.1"
    runtime._forget("acme")
    assert runtime.upstream(spec) is None, "never a loopback address"
    runtime.stop(spec)
    assert runtime.upstream(spec) is None


def test_usage_counts_files_without_following_links(setup, tmp_path):
    runtime, _agent = setup
    spec = make_spec()
    provision(runtime, spec)
    root = Path(runtime._cfg().instances_dir) / "acme"
    base = runtime.usage(spec)
    (root / "storage" / "uploads" / "big.bin").write_bytes(b"x" * 100_000)
    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"y" * 500_000)
    (root / "storage" / "uploads" / "link.bin").symlink_to(huge)
    runtime._forget("acme")
    assert 100_000 <= runtime.usage(spec) - base < 200_000
    assert runtime.usage(make_spec("nothing")) == 0


def test_usage_deadline_also_bounds_a_single_large_directory(tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import tenantfs

    root = tmp_path / "tenant"
    root.mkdir()
    for number in range(6):
        (root / str(number)).write_bytes(b"a" * 10)
    ticks = iter(index / 4 for index in range(100))
    monkeypatch.setattr(tenantfs.time, "monotonic", lambda: next(ticks))
    assert tenantfs.usage(root, deadline_seconds=1) <= 20


def test_logs_split_access_and_error_lines(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    agent.log_lines = ['172.30.0.1 - - [01/Jan/2026:00:00:00 +0000] "GET / HTTP/1.1" 200 12 "-" "curl"',
                       "[2026-01-01 00:00:00 +0000] [7] [ERROR] boom"]
    assert "GET /" in runtime.logs(spec, "access.log").content
    error_log = runtime.logs(spec, "error.log", max_bytes=10)
    assert error_log.truncated and error_log.content.endswith("boom\n")
    with pytest.raises(RuntimeFailure) as error:
        runtime.logs(spec, "../../etc/passwd")
    assert error.value.code == "invalid"


def test_recover_starts_missing_containers_and_skips_missing_data(setup):
    runtime, agent = setup
    specs = [make_spec("one"), make_spec("two"), make_spec("three")]
    for spec in specs:
        provision(runtime, spec)
    agent.containers.clear()
    assert runtime.recover([*specs, make_spec("gone")]) == 3
    assert set(agent.containers) == {"one", "two", "three"}
    assert runtime.recover(specs) == 0, "healthy tenants are left alone"


def test_recover_restarts_stuck_containers(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    runtime.health["ok"] = False
    assert runtime.recover([spec]) == 1
    assert len(agent.ops("tenant.start")) == 2


def test_agent_errors_become_runtime_failures(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    agent.failures["tenant.start"] = AgentError("docker_failed", "no image")
    with pytest.raises(RuntimeFailure) as error:
        runtime.restart(spec)
    assert error.value.code == "start_failed"
    agent.failures["tenant.stop"] = AgentError("unavailable", "socket")
    with pytest.raises(RuntimeFailure) as error:
        runtime.stop(spec)
    assert error.value.code == "unavailable"
    agent.failures["tenant.task"] = AgentError("unknown_operation", "old agent")
    with pytest.raises(RuntimeFailure) as error:
        runtime.list_users(spec)
    assert error.value.code == "unavailable"


# ── Tenant database tools ─────────────────────────────────────────────────────


def test_user_management_runs_as_tenant_tasks(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    runtime.set_user_password(spec, "editor1", "editor-password", "editor")
    users, total = runtime.list_users(spec)
    assert total == 2 and [(u.username, u.role) for u in users] == [("owner1", "owner"), ("editor1", "editor")]
    with _db(runtime) as conn:
        user_id = conn.execute("SELECT id FROM users WHERE username = 'editor1'").fetchone()[0]
        conn.execute("INSERT INTO api_service__tokens (user_id, token_hash) VALUES (?, 'h1')", (user_id,))
        conn.execute("INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at) "
                     "VALUES ('s1', 't1', ?, '2026-01-01', '2026-01-01', '2099-01-01')", (user_id,))
    runtime.set_user_password(spec, "editor1", "another-password", "admin")
    with _db(runtime) as conn:
        assert conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()[0] == "admin"
        assert conn.execute("SELECT active FROM api_service__tokens").fetchone()[0] == 0
        assert conn.execute("SELECT revoked_at FROM user_sessions").fetchone()[0]
    with pytest.raises(RuntimeFailure) as error:
        runtime.remove_user(spec, "owner1")
    assert error.value.code == "protected_user"
    with pytest.raises(RuntimeFailure) as error:
        runtime.remove_user(spec, "nobody")
    assert error.value.code == "user_not_found"
    runtime.remove_user(spec, "editor1")
    assert runtime.list_users(spec)[1] == 1
    with pytest.raises(RuntimeFailure) as error:
        runtime.set_user_password(spec, "x", "short", "user")
    assert error.value.code == "invalid"
    assert set(agent.tasks()) <= {"seed", "set_password", "list_users", "remove_user"}


def test_passwords_never_reach_an_argument_list(setup):
    runtime, agent = setup
    spec = make_spec()
    provision(runtime, spec)
    runtime.reset_admin_password(spec, "owner1", "fresh-password-9")
    for op, args in agent.calls:
        if op != "tenant.task":
            assert "fresh-password-9" not in repr(args) and WIKI_PASSWORD not in repr(args)


def test_reset_admin_password_named_or_every_admin(setup):
    runtime, _agent = setup
    spec = make_spec()
    provision(runtime, spec)
    assert runtime.reset_admin_password(spec, "owner1", "fresh-password-9") == "owner1"
    runtime.set_user_password(spec, "admin2", "admin-password", "admin")
    assert runtime.reset_admin_password(spec, "renamed-away", "fresh-password-8") == "owner1"
    with _db(runtime) as conn:
        flags = {row[0]: row[1] for row in conn.execute("SELECT username, force_password_change FROM users")}
    assert flags == {"owner1": 1, "admin2": 1}


def test_database_tools_need_a_database(setup):
    runtime, _agent = setup
    spec = make_spec()
    provision(runtime, spec)
    root = Path(runtime._cfg().instances_dir) / "acme"
    for name in ("bananawiki.db", "bananawiki.db-wal", "bananawiki.db-shm", "bananawiki.db.initialized"):
        (root / name).unlink(missing_ok=True)
    with pytest.raises(RuntimeFailure) as error:
        runtime.list_users(spec)
    assert error.value.code == "db_missing"
    with pytest.raises(RuntimeFailure) as error:
        runtime.list_users(make_spec("absent"))
    assert error.value.code == "not_found"


def test_reset_content_wipes_data_and_keeps_the_layout(setup, tmp_path):
    runtime, _agent = setup
    spec = make_spec()
    provision(runtime, spec)
    root = Path(runtime._cfg().instances_dir) / "acme"
    (root / "storage" / "uploads" / "photo.png").write_bytes(b"png")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("host")
    (root / "attachments").unlink()
    (root / "attachments").symlink_to(outside)
    runtime.set_user_password(spec, "someone", "someone-password", "user")
    runtime.stop(spec)
    runtime.reset_content(spec, admin_username="fresh", admin_password="fresh-password-1")
    assert not (root / "storage" / "uploads" / "photo.png").exists()
    assert os.readlink(root / "attachments") == "storage/attachments"
    assert (outside / "keep.txt").read_text() == "host"
    assert [(u.username, u.role) for u in runtime.list_users(spec)[0]] == [("fresh", "owner")]


def test_apply_limits_and_analytics(setup):
    runtime, _agent = setup
    spec = replace(make_spec(), policy=TenantPolicy(upload_max_bytes=20 * 1024 ** 2, blocked_extensions=("exe",)))
    provision(runtime, spec)
    with _db(runtime) as conn:
        conn.execute("UPDATE site_settings SET tts_gpu_url = 'https://evil.example', tts_gpu_enabled = 1")
        conn.execute("INSERT INTO analytics_daily (day, kind, count) VALUES (date('now'), 'request', 7)")
    runtime.apply_limits(spec)
    with _db(runtime) as conn:
        row = conn.execute("SELECT upload_max_size_mb, platform_upload_blacklist, tts_gpu_url, tts_gpu_enabled "
                           "FROM site_settings").fetchone()
    assert tuple(row) == (20, "exe", "", 0)
    summary = runtime.analytics(spec, 7)
    assert summary["window_days"] == 7 and summary["totals"]["request"] == 7 and len(summary["daily"]) == 7


# ── Plugin safety ─────────────────────────────────────────────────────────────


def _add_plugin(runtime: AgentRuntime, plugin_id: str, *, builtin: int, folder: bool) -> None:
    root = Path(runtime._cfg().instances_dir) / "acme"
    if folder:
        (root / "external_plugins" / plugin_id).mkdir(parents=True)
    with _db(runtime) as conn:
        conn.execute("INSERT INTO plugins (id, name, version, builtin, enabled) VALUES (?, ?, '1', ?, 1)",
                     (plugin_id, plugin_id, builtin))


def test_quarantine_disables_external_plugins_and_survives_restarts(tmp_path):
    runtime, agent = make_runtime(tmp_path, HOSTING_ALLOW_TENANT_PLUGINS="1")
    spec = make_spec()
    provision(runtime, spec)
    _add_plugin(runtime, "sneaky", builtin=1, folder=True)
    _add_plugin(runtime, "honest", builtin=0, folder=False)
    _add_plugin(runtime, "core", builtin=1, folder=False)
    summary = runtime.quarantine_plugins(spec)
    assert "2 plugin(s)" in summary and runtime.plugins_quarantined(spec)
    with _db(runtime) as conn:
        enabled = {row[0]: row[1] for row in conn.execute("SELECT id, enabled FROM plugins")}
    assert enabled == {"sneaky": 0, "honest": 0, "core": 1}
    assert agent.ops("tenant.start")[-1]["env"]["BW_ALLOW_EXTERNAL_PLUGINS"] == "0"
    assert [item["label"] for item in runtime.list_plugin_snapshots(spec)] == ["operator-quarantine"]
    runtime.lift_plugin_quarantine(spec)
    assert not runtime.plugins_quarantined(spec)
    assert agent.ops("tenant.start")[-1]["env"]["BW_ALLOW_EXTERNAL_PLUGINS"] == "1"


def test_plugin_snapshots_restore_only_platform_copies(setup):
    runtime, _agent = setup
    spec = make_spec()
    provision(runtime, spec)
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_plugin_snapshot(spec)
    assert error.value.code == "not_found"
    name = runtime.capture_plugin_snapshot(spec)
    runtime.set_user_password(spec, "intruder", "intruder-password", "admin")
    summary = runtime.restore_plugin_snapshot(spec)
    assert name in summary and runtime.plugins_quarantined(spec)
    assert [u.username for u in runtime.list_users(spec)[0]] == ["owner1"]
    labels = sorted(item["label"] for item in runtime.list_plugin_snapshots(spec))
    assert labels == ["before-restore", "pre-enable"]
    state = runtime._state(runtime._cfg(), spec)
    (state.snapshot_dir / name).write_bytes(b"tampered")
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_plugin_snapshot(spec, name)
    assert error.value.code == "db_unsafe"


# ── Routing ───────────────────────────────────────────────────────────────────


def test_sync_routes_sends_hostnames_to_the_agent(setup):
    runtime, agent = setup
    runtime.sync_routes([make_spec("one", hostnames=("one-hosting.wiki.test", "Docs.Example.org")),
                         make_spec("two", hostnames=())])
    assert agent.routes == [{"tenant": "one", "hosts": ["docs.example.org", "one-hosting.wiki.test"]}]


def test_one_bad_or_shared_hostname_does_not_stop_routing_every_wiki(setup):
    """The agent refuses a whole table with one invalid or duplicate host; the portal filters first."""
    runtime, agent = setup
    runtime.sync_routes([
        make_spec("one", hostnames=("one-hosting.wiki.test", "shared.wiki.test")),
        make_spec("two", hostnames=("two-hosting.wiki.test", "shared.wiki.test", "bad_host.example.org")),
        make_spec("three", hostnames=("three-hosting.wiki.test",)),
    ])
    assert agent.routes == [
        {"tenant": "one", "hosts": ["one-hosting.wiki.test"]},
        {"tenant": "two", "hosts": ["two-hosting.wiki.test"]},
        {"tenant": "three", "hosts": ["three-hosting.wiki.test"]},
    ]
    # What the real agent does with the unfiltered table: nothing is routed at all.
    real = TenantRuntime({"routes_dir": str(Path(runtime._cfg().instances_dir).parent / "routes")},
                         runner=lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""))
    raw = [{"tenant": "one", "hosts": ["one-hosting.wiki.test", "shared.wiki.test"]},
           {"tenant": "two", "hosts": ["shared.wiki.test", "two-hosting.wiki.test"]}]
    with pytest.raises(AgentError):
        real.call("proxy.routes", {"routes": raw})
    assert real.call("proxy.routes", {"routes": agent.routes})["changed"] is True


def test_sync_routes_is_a_no_op_in_port_mode_and_tolerates_old_agents(tmp_path, caplog):
    runtime, agent = make_runtime(tmp_path, BASE_DOMAIN="", HOSTING_MODE="port")
    runtime.sync_routes([make_spec()])
    assert agent.routes is None
    runtime, agent = make_runtime(tmp_path / "b")
    agent.failures["proxy.routes"] = AgentError("not_configured", "no routes dir")
    runtime.sync_routes([make_spec()])
    agent.failures["proxy.routes"] = AgentError("invalid_request", "bad host")
    with pytest.raises(RuntimeFailure):
        runtime.sync_routes([make_spec()])


# ── Wired into the portal ─────────────────────────────────────────────────────


def test_portal_lifecycle_on_the_production_runtime(tmp_path):
    from bananawiki.hosting.db import connection_scope

    from .agent_fakes import FakeAgent
    from .hosting_support import PASSWORD, build_portal

    agent = FakeAgent(tmp_path / "instances")
    runtime = AgentRuntime(client=agent, probe=lambda _address, _port: True)
    portal = build_portal(tmp_path, runtime=runtime)  # type: ignore[arg-type]
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import accounts, instances

        owner = accounts.create("owner", PASSWORD)
        inst, username, password = instances.create(owner, "team")
        assert agent.routes == [{"tenant": "team", "hosts": ["team-hosting.wiki.test"]}]
        assert instances.describe(inst)["health"] == "running"
        instances.stop(inst, actor_id=None)
        assert "team" not in agent.containers and agent.routes == []
        instances.terminate(instances.get(inst["id"]), actor_id=None)
        terminated = instances.get(inst["id"])
        assert (tmp_path / "instances" / terminated["subdomain"]).is_dir(), "data kept for the grace period"
        instances.hard_delete(terminated, actor_id=owner["id"])
        assert not any((tmp_path / "instances").iterdir())
    assert username and password
