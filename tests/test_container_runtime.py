import os
import pytest

from hosting import container_runtime


def test_container_command_has_isolation_boundary(tmp_path):
    data_dir = tmp_path / "tenant"
    data_dir.mkdir()
    env = {
        "BW_INSTANCE_DIR": str(data_dir),
        "BW_DATABASE_PATH": str(data_dir / "bananawiki.db"),
        "BW_MAINTENANCE_FILE": str(data_dir / ".banana-maintenance"),
        "SECRET_KEY": "test-only-secret",
        "HOSTING_SECRET_KEY": "portal-session-secret",
        "GITHUB_TOKEN": "portal-repository-token",
        "BW_SETUP_TOKEN": "portal-setup-token",
        "BW_SOURCE_URL": "https://github.com/BananaSuite/BananaWiki",
    }
    cmd = container_runtime.build_run_command(
        data_dir=str(data_dir), host_port=6123, host_env=env,
        image="bananawiki-tenant:test", internal_port=5001,
        memory_mb=512, nofile_limit=512, cpu_limit="0.75", pids_limit=96,
        uid=1000, gid=1000, network="isolated-test-net",
    )
    joined = " ".join(cmd)
    assert "--read-only" in cmd
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges:true" in cmd
    assert "--network isolated-test-net" in joined
    assert "127.0.0.1:6123:5001" in cmd
    assert f"type=bind,src={os.path.realpath(data_dir)},dst=/data" in cmd
    assert "BW_PLUGIN_ISOLATION=container" in cmd
    assert "BW_ALLOW_EXTERNAL_PLUGINS=1" in cmd
    assert "BW_DATABASE_PATH=/data/bananawiki.db" in cmd
    assert "BW_MAINTENANCE_FILE=/data/.banana-maintenance" in cmd
    assert not any("docker.sock" in value for value in cmd)
    for secret in ("test-only-secret", "portal-session-secret", "portal-repository-token", "portal-setup-token"):
        assert secret not in joined
    assert "BW_SOURCE_URL=https://github.com/BananaSuite/BananaWiki" in cmd


def test_container_and_network_names_are_stable_and_separate(tmp_path):
    first = tmp_path / "alpha"
    second = tmp_path / "beta"
    first.mkdir()
    second.mkdir()
    assert container_runtime.container_name(first) == container_runtime.container_name(first)
    assert container_runtime.container_name(first) != container_runtime.container_name(second)
    assert container_runtime.network_name(first) != container_runtime.network_name(second)


def test_stop_container_removes_private_network(tmp_path, monkeypatch):
    tenant = tmp_path / "alpha"
    tenant.mkdir()
    calls = []
    from subprocess import CompletedProcess
    def docker(*args, **_kwargs):
        calls.append(args)
        return CompletedProcess(args, 1, "", "No such object") if args[0] == "inspect" else CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(container_runtime, "_docker", docker)
    assert container_runtime.stop_container(tenant)
    assert ("network", "rm", container_runtime.network_name(tenant)) in calls


@pytest.mark.parametrize("policy,internal", [("isolated", True), ("outbound", False)])
def test_tenant_egress_is_an_explicit_host_policy(tmp_path, monkeypatch, policy, internal):
    from subprocess import CompletedProcess
    calls = []
    def docker(*args, **_kwargs):
        calls.append(args)
        return CompletedProcess(args, 1 if args[:2] == ("network", "inspect") else 0, "", "")
    monkeypatch.setattr(container_runtime.config, "HOSTING_TENANT_NETWORK", policy)
    monkeypatch.setattr(container_runtime, "_docker", docker)
    container_runtime._ensure_private_network(tmp_path)
    create = next(args for args in calls if args[:2] == ("network", "create"))
    assert ("--internal" in create) is internal
