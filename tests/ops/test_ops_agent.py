"""The hosting runtime agent: validation, the docker command it builds, the socket protocol."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import threading
from pathlib import Path

import pytest
from hosting.quota_fakes import FakeProjectQuota

from bananawiki.ops.agent_client import AgentClient, connect
from bananawiki.ops.files import write_environment, write_json
from bananawiki.ops.runtime_agent import (
    SECCOMP_PROFILE,
    AgentError,
    AgentServer,
    TenantRuntime,
    container_name,
    handle_line,
)


class Docker:
    """Records docker invocations and keeps a copy of every env file at the time of `docker run`."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.env_files: list[str] = []
        self.mounts: list[tuple[str, int]] = []

    def __call__(self, command, **_):
        self.calls.append(command)
        if command[1] == "run":
            path = command[command.index("--env-file") + 1]
            self.env_files.append(Path(path).read_text())
            mount = next(part for part in command if part.startswith("type=bind,src="))
            source = mount.split("src=", 1)[1].split(",", 1)[0]
            self.mounts.append((source, Path(source).stat().st_ino))
        if command[1:3] == ["network", "inspect"]:
            if "{{.EnableIPv6}}" in command:
                return subprocess.CompletedProcess(command, 0, "false", "")
            return subprocess.CompletedProcess(command, 1, "", "No such network")
        if command[1] == "inspect" and "--format" in command:
            return subprocess.CompletedProcess(command, 0, "1", "")
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 1, "", "No such container")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def runtime(tmp_path):
    instances = tmp_path / "instances"
    (instances / "acme").mkdir(parents=True)
    if os.geteuid() == 0:
        os.chown(instances / "acme", 4242, 4242)
    owner = (instances / "acme").stat()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    docker = Docker()
    config = {"service": "svc", "service_uid": owner.st_uid, "service_gid": owner.st_gid,
              "instances_dir": str(instances), "image": "bananawiki-tenant:" + "a" * 40,
              "limits": {"memory_mb": 2048, "cpus": 2.0, "pids": 1024, "nofile": 4096}}
    runtime = TenantRuntime(config, runner=docker, private_dir=private, quota=FakeProjectQuota(instances))
    runtime._verify_mount_descriptor = lambda pid, witness: None
    runtime.docker_calls = docker  # type: ignore[attr-defined]
    return runtime


def test_start_builds_a_locked_down_container(runtime, tmp_path):
    runtime.call("tenant.start", {"tenant": "acme", "env": {"BW_SITE_NAME": "Acme", "BW_HOST": "127.0.0.9"},
                                  "limits": {"memory_mb": 1024, "cpus": 1.5}})
    docker = runtime.docker_calls
    run = next(call for call in docker.calls if call[1] == "run")
    real = str((tmp_path / "instances/acme").resolve())
    for flag in ("--read-only", "--cap-drop", "no-new-privileges:true",
                 "org.bananawiki.role=tenant", f"org.bananawiki.data-dir={real}", "max-size=10m", "1024m"):
        assert flag in run
    assert docker.mounts[0][0] == real
    assert docker.mounts[0][1] == (tmp_path / "instances/acme").stat().st_ino
    assert "seccomp=" + str(SECCOMP_PROFILE.resolve()) in run
    assert "net.ipv6.conf.all.disable_ipv6=1" in run and "net.ipv6.conf.default.disable_ipv6=1" in run
    assert "bananawiki-tenant:" + "a" * 40 in run
    assert run[-2] == "server" and len(run[-1]) == 32
    assert "bananawiki.ops.tenant_guard" in run
    owner = (tmp_path / "instances/acme").stat()
    assert run[run.index("--user") + 1] == f"{owner.st_uid}:{owner.st_gid}"
    assert run[run.index("--name") + 1] == container_name(real)
    assert not any("Acme" in part for part in run), "variables never appear on argv"
    env = docker.env_files[0]
    assert "BW_SITE_NAME=Acme\n" in env and "BW_HOST=0.0.0.0\n" in env and "BW_MANAGED_HOSTING=1\n" in env
    assert not list(Path(runtime.private_dir).iterdir()), "the env file is removed after docker run"
    assert ["docker", "network", "create", "--driver", "bridge", "--ipv6=false", "--internal", "--label",
            "org.bananawiki.role=tenant", container_name(real) + "-net"] in docker.calls


@pytest.mark.parametrize(("args", "code"), [
    ({"tenant": "../etc"}, "invalid_tenant"),
    ({"tenant": "Acme"}, "invalid_tenant"),
    ({"tenant": "missing"}, "unknown_tenant"),
    ({"tenant": "acme", "env": {"PATH": "/usr/bin"}}, "invalid_env"),
    ({"tenant": "acme", "env": {"BW_SECRET_KEY": "x"}}, "invalid_env"),
    ({"tenant": "acme", "env": {"BW_X": "a\nBW_Y=b"}}, "invalid_env"),
    ({"tenant": "acme", "limits": {"memory_mb": 999999}}, "invalid_request"),
    ({"tenant": "acme", "limits": {"cpus": 64}}, "invalid_request"),
    ({"tenant": "acme", "network": "host"}, "invalid_request"),
    ({"tenant": "acme", "publish_port": 8080}, "invalid_request"),
    ({"tenant": "acme", "internal_port": 22}, "invalid_request"),
])
def test_start_rejects_unsafe_requests(runtime, args, code):
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.start", args)
    assert error.value.code == code
    assert not any(call[1] == "run" for call in runtime.docker_calls.calls)
    assert not any(call[1] == "rm" for call in runtime.docker_calls.calls), "invalid settings must keep the live wiki"


def test_symlinked_or_root_owned_tenants_are_refused(runtime, tmp_path):
    (tmp_path / "instances/evil").symlink_to("/")
    with pytest.raises(AgentError):
        runtime.call("tenant.start", {"tenant": "evil"})
    runtime.config["service_uid"] += 1
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.start", {"tenant": "acme"})
    assert error.value.code == "invalid_owner"


def test_outbound_tenants_may_publish_on_loopback(runtime):
    runtime.call("tenant.start", {"tenant": "acme", "network": "outbound", "publish_port": 6001})
    run = next(call for call in runtime.docker_calls.calls if call[1] == "run")
    assert run[run.index("--publish") + 1] == "127.0.0.1:6001:5001"


@pytest.mark.parametrize("existing", [True, False])
def test_ipv6_network_defaults_are_refused_for_existing_and_new_bridges(runtime, existing):
    original = runtime.runner

    def runner(command, **kwargs):
        if command[1:3] == ["network", "inspect"]:
            if "{{.EnableIPv6}}" in command:
                return subprocess.CompletedProcess(command, 0, "true", "")
            if existing:
                return subprocess.CompletedProcess(command, 0, "true", "")
        return original(command, **kwargs)

    runtime.runner = runner
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.start", {"tenant": "acme"})
    assert error.value.code == "network_unavailable"
    assert not any(call[1] == "run" for call in runtime.docker_calls.calls)
    assert any(call[1:3] == ["network", "create"] for call in runtime.docker_calls.calls) is not existing


@pytest.mark.parametrize("contents", [None, '{"defaultAction":"SCMP_ACT_ALLOW"}'])
def test_unavailable_or_changed_seccomp_keeps_the_running_tenant(runtime, tmp_path, monkeypatch, contents):
    from bananawiki.ops import runtime_agent

    profile = tmp_path / "profile.json"
    if contents is not None:
        profile.write_text(contents)
    monkeypatch.setattr(runtime_agent, "SECCOMP_PROFILE", profile)
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.start", {"tenant": "acme"})
    assert error.value.code == "sandbox_unavailable"
    assert not any(call[1] in {"rm", "run"} for call in runtime.docker_calls.calls)


def test_quota_seccomp_retains_the_complete_upstream_allowlist():
    import hashlib

    baseline = SECCOMP_PROFILE.with_name("default-docker-29.8.2.json")
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == \
        "536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74"
    upstream = json.loads(baseline.read_text())
    protected = json.loads(SECCOMP_PROFILE.read_text())
    assert protected["defaultAction"] == "SCMP_ACT_ERRNO"
    assert protected["defaultErrnoRet"] == 1
    assert protected["archMap"] == upstream["archMap"][:1]
    assert {k: v for k, v in protected.items() if k not in {"syscalls", "archMap"}} == \
        {k: v for k, v in upstream.items() if k not in {"syscalls", "archMap"}}
    # Preserve every upstream condition and syscall except generic ioctl ALLOW.
    upstream["syscalls"][0]["names"].remove("ioctl")
    baseline_count = len(upstream["syscalls"])
    assert protected["syscalls"][:baseline_count] == upstream["syscalls"]
    intervals = []
    for rule in protected["syscalls"][baseline_count:]:
        assert rule["names"] == ["ioctl"] and rule["action"] == "SCMP_ACT_ALLOW"
        assert set(rule) == {"names", "action", "args"} and len(rule["args"]) == 1
        argument = rule["args"][0]
        assert argument["index"] == 1 and argument["op"] == "SCMP_CMP_MASKED_EQ"
        mask, prefix = argument["value"], argument["valueTwo"]
        assert 0 < mask <= 0xFFFFFFFF and prefix & mask == prefix
        suffix = mask ^ 0xFFFFFFFF
        assert suffix & (suffix + 1) == 0  # contiguous low-bit suffix, hence one interval
        intervals.append((prefix, prefix | suffix))
    # Prove all 2**32 commands are covered exactly once except these three gaps.
    next_command, forbidden = 0, {0x401C5820, 0x40086602, 0x40046602}
    excluded = set()
    for first, last in sorted(intervals):
        assert first >= next_command  # no overlapping generic rule
        assert first - next_command <= 1
        excluded.update(range(next_command, first))
        next_command = last + 1
    excluded.update(range(next_command, 1 << 32))
    assert excluded == forbidden


@pytest.mark.parametrize("system,machine", [("Linux", "aarch64"), ("Linux", "ppc64le"), ("Darwin", "x86_64")])
def test_quota_seccomp_refuses_unverified_host_architectures(monkeypatch, system, machine):
    from bananawiki.ops import runtime_agent

    monkeypatch.setattr(runtime_agent.platform, "system", lambda: system)
    monkeypatch.setattr(runtime_agent.platform, "machine", lambda: machine)
    with pytest.raises(AgentError) as error:
        runtime_agent.seccomp_profile()
    assert error.value.code == "sandbox_unavailable"


def test_stop_status_logs_and_unknown_operations(runtime):
    assert runtime.call("tenant.stop", {"tenant": "acme"}) == {"tenant": "acme", "stopped": True}
    assert runtime.call("tenant.status", {"tenant": "acme"})["exists"] is False
    assert runtime.call("tenant.logs", {"tenant": "acme", "lines": 5})["lines"] == []
    with pytest.raises(AgentError) as error:
        runtime.call("container.exec", {})
    assert error.value.code == "unknown_operation"


def test_protocol_errors():
    assert handle_line(b"not json", lambda: None)["error"]["code"] == "invalid_request"
    assert handle_line(json.dumps({"v": 9, "op": "ping"}).encode(), lambda: None)["error"]["code"] == \
        "unsupported_protocol"


def test_socket_round_trip(tmp_path):
    root = tmp_path / "root"
    service = pwd.getpwuid(os.getuid()).pw_name
    (root / "data/instances/acme").mkdir(parents=True)
    write_json(root / "config/installation.json", {"service": service, "revision": "b" * 40})
    write_environment(root / "config/app.env", {"INSTANCES_DIR": str(root / "data/instances")})
    socket_path = tmp_path / "run/agent.sock"
    socket_path.parent.mkdir()
    docker = Docker()
    server = AgentServer(socket_path, root, runner=docker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert oct(socket_path.stat().st_mode & 0o777) == "0o660"
        client = connect({"BW_RUNTIME_AGENT_SOCKET": str(socket_path)})
        assert isinstance(client, AgentClient)
        assert client.call("ping")["image"] == "bananawiki-tenant:" + "b" * 40
        with pytest.raises(AgentError) as error:
            client.call("tenant.start", {"tenant": "../x"})
        assert error.value.code == "invalid_tenant"
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(AgentError) as error:
        AgentClient(socket_path, timeout=1).call("ping")
    assert error.value.code == "unavailable"


def test_connect_falls_back_to_in_process_runtime(tmp_path):
    runtime = connect({"INSTANCES_DIR": str(tmp_path), "HOSTING_CONTAINER_IMAGE": "img:1"})
    assert isinstance(runtime, TenantRuntime) and runtime.config["image"] == "img:1"


def test_unexpected_errors_still_get_an_answer(runtime):
    """A container label Docker reports as garbage used to kill the handler thread without a response."""
    def inspect(command, **_):
        labels = {"org.bananawiki.role": "tenant", "org.bananawiki.data-dir": str(Path(runtime.config["instances_dir"]).resolve() / "acme"),
                  "org.bananawiki.internal-port": "not-a-port"}
        output = json.dumps([{"Name": "/x", "Config": {"Labels": labels}, "State": {}}])
        return subprocess.CompletedProcess(command, 0, output if command[1] == "inspect" else "x\n", "")

    runtime.runner = inspect
    request = json.dumps({"v": 1, "id": "r1", "op": "tenant.list"}).encode()
    response = handle_line(request, lambda: runtime)
    assert response == {"v": 1, "id": "r1", "ok": False, "error": {"code": "internal", "message": "ValueError"}}
