"""The agent operations added for the hosting runtime: tenant tasks and Caddy routes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from bananawiki.ops import caddy, units
from bananawiki.ops.profile import ReleaseFeatures, services
from bananawiki.ops.runtime_agent import (
    MOUNT_INVENTORY_FORMAT,
    SECCOMP_PROFILE,
    AgentError,
    TenantRuntime,
    container_name,
    render_routes,
)

from .quota_fakes import FakeProjectQuota


class Docker:
    """Records commands; containers listed in ``running`` are up with the given address."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.running: dict[str, str] = {}
        self.task_output = '{"ok": true, "users": []}\n'
        self.env_files: list[str] = []
        self.security_options = ["seccomp=" + SECCOMP_PROFILE.read_text()]
        self.sysctls = {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"}

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "systemctl":
            return subprocess.CompletedProcess(command, 0, "", "")
        verb = command[1]
        if verb == "run" and "--env-file" in command:
            self.env_files.append(Path(command[command.index("--env-file") + 1]).read_text())
        if verb == "inspect" and "--format" in command:
            if MOUNT_INVENTORY_FORMAT in command:
                items = [{"id": hashlib.sha256(name.encode()).hexdigest(), "name": "/" + name,
                          "role": "tenant", "running": True, "pid": index + 1}
                         for index, name in enumerate(self.running)]
                return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(item) for item in items), "")
            return subprocess.CompletedProcess(command, 0, "1", "")
        if verb == "inspect":
            items = self._inspect(command[4:])
            return subprocess.CompletedProcess(command, 0 if items else 1, json.dumps(items), "")
        if verb == "ps" and "label=org.bananawiki.role=tenant-task" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "ps" and "label=org.bananawiki.role" in command:
            return subprocess.CompletedProcess(command, 0,
                                               "\n".join(hashlib.sha256(name.encode()).hexdigest()
                                                         for name in self.running), "")
        if verb == "ps" and "--filter" in command and "name=^/" in command[command.index("--filter") + 1]:
            wanted = command[command.index("--filter") + 1].removeprefix("name=^/").removesuffix("$")
            return subprocess.CompletedProcess(command, 0, wanted if wanted in self.running else "", "")
        if verb == "ps":
            return subprocess.CompletedProcess(command, 0, "\n".join(self.running), "")
        if verb in ("exec", "run") and "-i" in command:
            return subprocess.CompletedProcess(command, 0, "log line\n" + self.task_output, "")
        if command[1:3] == ["network", "inspect"]:
            if "{{.EnableIPv6}}" in command:
                return subprocess.CompletedProcess(command, 0, "false", "")
            return subprocess.CompletedProcess(command, 1, "", "No such network")
        return subprocess.CompletedProcess(command, 0, "", "")

    def _inspect(self, names):
        items = []
        for name in names:
            if name in self.running:
                data_dir, address = self.running[name].split("|")
                items.append({"Name": "/" + name, "Config": {"Labels": {
                    "org.bananawiki.role": "tenant", "org.bananawiki.data-dir": data_dir,
                    "org.bananawiki.internal-port": "5001",
                    **{f"org.bananawiki.quota-{key}": str(self.quota.verify(Path(data_dir).name).to_dict()[key])
                       for key in ("project_id", "root_inode", "root_generation")}}, "Image": "img"},
                    "State": {"Running": True, "Pid": 1, "StartedAt": "2026-01-01T00:00:00Z"},
                    "HostConfig": {"SecurityOpt": self.security_options, "Sysctls": self.sysctls},
                    "NetworkSettings": {"Networks": {"n": {"IPAddress": address}}}})
        return items

    def commands(self, verb):
        return [command for command, _ in self.calls if len(command) > 1 and command[1] == verb]


@pytest.fixture
def agent(tmp_path):
    instances = tmp_path / "instances"
    for name in ("acme", "beta"):
        (instances / name).mkdir(parents=True)
        if os.geteuid() == 0:
            os.chown(instances / name, 4242, 4242)
    owner = (instances / "acme").stat()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    docker = Docker()
    config = {"service": "svc", "service_uid": owner.st_uid, "service_gid": owner.st_gid,
              "instances_dir": str(instances), "image": "bananawiki-tenant:abc", "routes_dir": str(tmp_path / "routes"),
              "limits": {"memory_mb": 2048, "cpus": 2.0, "pids": 1024, "nofile": 4096}}
    quota = FakeProjectQuota(instances)
    for name in ("acme", "beta"):
        quota.prepare(name, byte_limit=10 * 1024 ** 3, inode_limit=100_000)
    docker.quota = quota
    runtime = TenantRuntime(config, runner=docker, private_dir=private, quota=quota)
    runtime._verify_mount_descriptor = lambda pid, witness: None
    def mounted_identity(pid):
        path = list(docker.running.values())[pid - 1].split("|")[0]
        info = Path(path).stat()
        return info.st_dev, info.st_ino
    runtime._mounted_identity = mounted_identity
    runtime.fake = docker  # type: ignore[attr-defined]
    runtime.base = instances.resolve()  # type: ignore[attr-defined]
    return runtime


def _run_up(agent, name, address):
    real = str(agent.base / name)
    agent.fake.running[container_name(real)] = f"{real}|{address}"


def test_task_in_a_stopped_tenant_uses_a_locked_down_one_shot_container(agent):
    request = {"action": "set_password", "username": "bob", "password": "top-secret-pw", "role": "user"}
    result = agent.call("tenant.task", {"tenant": "acme", "request": request})
    assert result == {"tenant": "acme", "result": {"ok": True, "users": []}}
    command = agent.fake.commands("run")[0]
    for flag in ("--detach", "--read-only", "no-new-privileges:true", "ALL", "org.bananawiki.role=tenant-task"):
        assert flag in command
    assert command[command.index("--network") + 1] == "none"
    assert "seccomp=" + str(SECCOMP_PROFILE.resolve()) in command
    assert "net.ipv6.conf.all.disable_ipv6=1" in command and "net.ipv6.conf.default.disable_ipv6=1" in command
    assert command[command.index("--entrypoint") + 1] == "python"
    assert command[-3] == "task" and len(command[-2]) == 32 and command[-1] == "150"
    executed, kwargs = next((c, k) for c, k in agent.fake.calls if c[1] == "exec")
    assert executed[4:7] == ["/usr/bin/timeout", "--signal=TERM", "--kill-after=5s"]
    assert 0 < float(executed[7][:-1]) <= 120 and executed[8] == "python"
    assert executed[-4:] == ["-E", "-s", "-m", "bananawiki.ops.tenant_task"]
    assert "org.bananawiki.role=tenant" not in command
    assert not any("top-secret-pw" in part for part in command + executed)
    assert json.loads(kwargs["input"]) == request
    assert not list(Path(agent.private_dir).iterdir())
    assert agent.fake.commands("rm")[-1][3] == executed[3]


def test_task_in_a_running_tenant_uses_exec(agent):
    _run_up(agent, "acme", "172.20.0.2")
    agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
    command, kwargs = next((c, k) for c, k in agent.fake.calls if c[1] == "exec")
    assert command[1:7] == ["exec", "-i", container_name(str(agent.base / "acme")), "/usr/bin/timeout", "--signal=TERM",
                           "--kill-after=5s"]
    assert 0 < float(command[7][:-1]) <= 120
    assert command[8:] == ["python", "-E", "-s", "-m", "bananawiki.ops.tenant_task"]
    assert "input" in kwargs
    assert 10 < kwargs["timeout"] <= 130


@pytest.mark.parametrize("security_options", [[], ["seccomp=builtin"], ["seccomp=unconfined"],
                                              ['seccomp={"defaultAction":"SCMP_ACT_ALLOW"}']])
def test_tasks_refuse_a_running_container_without_the_quota_profile(agent, security_options):
    _run_up(agent, "acme", "172.20.0.2")
    agent.fake.security_options = security_options
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
    assert error.value.code == "sandbox_outdated"
    assert not agent.fake.commands("exec") and not agent.fake.commands("run")


@pytest.mark.parametrize("sysctls", [{}, {"net.ipv6.conf.all.disable_ipv6": "1"},
                                   {"net.ipv6.conf.all.disable_ipv6": "0", "net.ipv6.conf.default.disable_ipv6": "1"}])
def test_tasks_refuse_a_running_container_without_ipv6_disabled(agent, sysctls):
    _run_up(agent, "acme", "172.20.0.2")
    agent.fake.sysctls = sysctls
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
    assert error.value.code == "sandbox_outdated"
    assert not agent.fake.commands("exec") and not agent.fake.commands("run")


def test_timed_out_one_shot_task_is_removed_and_later_tasks_can_run(agent):
    original_runner = agent.runner
    timed_out = []

    def runner(command, **kwargs):
        if command[1] == "run" and not timed_out:
            timed_out.append(command[command.index("--name") + 1])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original_runner(command, **kwargs)

    agent.runner = runner
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
    assert error.value.code == "timeout"
    assert ["docker", "rm", "--force", timed_out[0]] in [c for c, _ in agent.fake.calls]
    assert agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})["result"]["ok"]
    next_name = agent.fake.commands("run")[-1]
    assert next_name[next_name.index("--name") + 1] != timed_out[0]


def test_tasks_serialize_per_tenant_without_blocking_other_tenants(agent):
    started = threading.Event()
    release = threading.Event()
    concurrent = threading.Event()
    original_runner = agent.runner

    def runner(command, **kwargs):
        if command[1] == "exec" and "-i" in command:
            name = command[3]
            if name.startswith(container_name(str(agent.base / "acme"))):
                if started.is_set() and not release.is_set():
                    concurrent.set()
                started.set()
                assert release.wait(5)
        return original_runner(command, **kwargs)

    agent.runner = runner
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(agent.call, "tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
        assert started.wait(5)
        second = pool.submit(agent.call, "tenant.task", {"tenant": "acme", "request": {"action": "analytics"}})
        other = pool.submit(agent.call, "tenant.task", {"tenant": "beta", "request": {"action": "list_users"}})
        assert other.result(timeout=5)["result"]["ok"]
        assert not concurrent.is_set()
        release.set()
        assert first.result(timeout=5)["result"]["ok"] and second.result(timeout=5)["result"]["ok"]


@pytest.mark.skipif(not Path("/usr/bin/python3").is_file(), reason="needs a system Python (venvs have no user site)")
def test_task_python_ignores_a_user_site_planted_in_the_shared_tmp(tmp_path):
    """The image sets HOME=/tmp, which the running wiki (and its plugins) can write.

    A ``.pth`` there runs in every ``python`` started without ``-s``, so it
    would see the password a portal reset sends to ``tenant_task`` on stdin.
    """
    from bananawiki.ops.runtime_agent import TASK_COMMAND

    probe = subprocess.run(["/usr/bin/python3", "-c", "import site, sys; print(site.getusersitepackages())"],
                           env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                           check=True)
    user_site = Path(probe.stdout.strip())
    user_site.mkdir(parents=True)
    stolen = tmp_path / "stolen"
    (user_site / "evil.pth").write_text(f"import sys; open({str(stolen)!r}, 'w').write('hijacked')\n")
    environment = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run(["/usr/bin/python3", "-c", "pass"], env=environment, check=True)
    assert stolen.exists(), "the planted .pth runs in a plain python"
    stolen.unlink()
    flags = [part for part in TASK_COMMAND[1:] if part.startswith("-") and part != "-m"]
    subprocess.run(["/usr/bin/python3", *flags, "-c", "pass"], env=environment, check=True)
    assert not stolen.exists(), "tenant_task must not load the tenant's user site"


@pytest.mark.parametrize(("args", "code"), [
    ({"tenant": "acme", "request": {"action": "shell"}}, "invalid_request"),
    ({"tenant": "acme", "request": "list_users"}, "invalid_request"),
    ({"tenant": "../x", "request": {"action": "list_users"}}, "invalid_tenant"),
    ({"tenant": "acme", "request": {"action": "seed", "blob": "x" * 70_000}}, "too_large"),
    ({"tenant": "acme", "request": {"action": "seed"}, "timeout": 5000}, "invalid_request"),
])
def test_task_requests_are_validated(agent, args, code):
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", args)
    assert error.value.code == code


def test_task_output_is_untrusted(agent):
    agent.fake.task_output = "not json"
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
    assert error.value.code == "docker_failed"
    agent.fake.task_output = '["ok"]'
    with pytest.raises(AgentError):
        agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_real_task_pipe_limit_terminates_a_flooding_process(tmp_path, monkeypatch, stream):
    from bananawiki.ops import runtime_agent

    monkeypatch.setattr(runtime_agent, "MAX_TASK_RESPONSE", 1024)
    marker = tmp_path / "completed"
    code = (f"import sys,time; from pathlib import Path; sys.{stream}.buffer.write(b'x'*100000); "
            f"sys.{stream}.flush(); time.sleep(10); Path({str(marker)!r}).touch()")
    with pytest.raises(AgentError) as error:
        runtime_agent._task_process([sys.executable, "-c", code], "", 2)
    assert error.value.code == "too_large"
    assert not marker.exists()


def test_real_task_pipes_do_not_deadlock_when_output_precedes_reading_the_request():
    from bananawiki.ops.runtime_agent import _task_process

    code = "import sys; sys.stdout.write('x'*100000); sys.stdout.flush(); print(len(sys.stdin.read()))"
    completed = _task_process([sys.executable, "-c", code], "y" * 64000, 3)
    assert completed.returncode == 0 and completed.stdout.endswith("64000\n")


def test_real_task_process_deadline_terminates_the_client(tmp_path):
    from bananawiki.ops.runtime_agent import _task_process

    marker = tmp_path / "completed"
    code = f"import time; from pathlib import Path; time.sleep(10); Path({str(marker)!r}).touch()"
    with pytest.raises(subprocess.TimeoutExpired):
        _task_process([sys.executable, "-c", code], "", 0.1)
    assert not marker.exists()


def test_task_wait_deadline_does_not_execute_a_queued_task(agent, monkeypatch):
    class Occupied:
        def acquire(self, *, timeout):
            assert 0 < timeout <= 5
            return False

    monkeypatch.setattr(agent, "_tenant_lock", lambda _path: Occupied())
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", {"tenant": "acme", "timeout": 5, "request": {"action": "list_users"}})
    assert error.value.code == "timeout"
    assert agent.fake.calls == []


def test_task_does_not_start_when_container_inspection_exhausts_its_deadline(agent, monkeypatch):
    from bananawiki.ops import runtime_agent

    now = [0.0]
    monkeypatch.setattr(runtime_agent.time, "monotonic", lambda: now[0])
    original = agent.runner

    def runner(command, **kwargs):
        if command[1] == "inspect":
            assert kwargs["timeout"] <= 5
            now[0] = 6.0
        return original(command, **kwargs)

    agent.runner = runner
    _run_up(agent, "acme", "172.20.0.2")
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", {"tenant": "acme", "timeout": 5, "request": {"action": "set_password"}})
    assert error.value.code == "timeout"
    assert not agent.fake.commands("exec") and not agent.fake.commands("run")
    assert agent._tenant_lock(agent.base / "acme").acquire(blocking=False)
    agent._tenant_lock(agent.base / "acme").release()


def test_container_enforced_deadline_is_reported_as_a_timeout(agent):
    original = agent.runner

    def runner(command, **kwargs):
        if command[1] == "exec":
            return subprocess.CompletedProcess(command, 124, "", "")
        return original(command, **kwargs)

    agent.runner = runner
    with pytest.raises(AgentError) as error:
        agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
    assert error.value.code == "timeout"
    assert agent.fake.commands("rm")


def test_runtime_connection_limit_rejects_excess_requests_and_recovers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from bananawiki.ops import runtime_agent
    from bananawiki.ops.agent_client import AgentClient

    monkeypatch.setattr(runtime_agent, "MAX_CONCURRENT_REQUESTS", 2)
    entered = threading.Condition()
    release = threading.Event()
    calls = 0

    class BlockingRuntime:
        def call(self, _operation, _arguments):
            nonlocal calls
            with entered:
                calls += 1
                entered.notify_all()
            assert release.wait(3)
            return {"ready": True}

    path = tmp_path / "agent.sock"
    server = runtime_agent.AgentServer(path, tmp_path)
    monkeypatch.setattr(server, "allowed_uids", lambda: {os.geteuid()})
    monkeypatch.setattr(server, "runtime", BlockingRuntime)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = [pool.submit(AgentClient(path, timeout=3).call, "ping") for _ in range(2)]
            try:
                with entered:
                    assert entered.wait_for(lambda: calls == 2, timeout=2)
                with pytest.raises(AgentError) as error:
                    AgentClient(path, timeout=2).call("ping")
                assert error.value.code == "busy"
            finally:
                release.set()
            assert [result.result(timeout=2) for result in first] == [{"ready": True}] * 2
        assert AgentClient(path, timeout=2).call("ping") == {"ready": True}
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_routes_are_rendered_from_docker_addresses(agent, tmp_path):
    _run_up(agent, "acme", "172.20.0.2")
    result = agent.call("proxy.routes", {"routes": [
        {"tenant": "acme", "hosts": ["acme-hosting.example.com", "docs.acme.org"]},
        {"tenant": "beta", "hosts": ["beta-hosting.example.com"]},
    ]})
    assert result == {"routes": 1, "changed": True, "reloaded": True}
    text = (tmp_path / "routes" / "tenants.caddy").read_text()
    assert ("acme-hosting.example.com, docs.acme.org, http://acme-hosting.example.com, http://docs.acme.org {"
            in text)
    assert "reverse_proxy 172.20.0.2:5001" in text and "on_demand" in text
    assert "beta" not in text, "stopped tenants are not routed"
    assert oct((tmp_path / "routes" / "tenants.caddy").stat().st_mode & 0o777) == "0o644"
    assert oct((tmp_path / "routes" / "routes.json").stat().st_mode & 0o777) == "0o600"
    assert ["systemctl", "reload", "caddy"] in [command for command, _ in agent.fake.calls]
    reloads = len([c for c, _ in agent.fake.calls if c[0] == "systemctl"])
    assert agent.call("proxy.routes", {"routes": [{"tenant": "acme", "hosts": ["acme-hosting.example.com",
                                                                             "docs.acme.org"]},
                                                  {"tenant": "beta", "hosts": ["beta-hosting.example.com"]}]}
                      )["changed"] is False
    assert len([c for c, _ in agent.fake.calls if c[0] == "systemctl"]) == reloads, "no reload without a change"


def test_routes_follow_container_restarts(agent, tmp_path):
    _run_up(agent, "acme", "172.20.0.2")
    agent.call("proxy.routes", {"routes": [{"tenant": "acme", "hosts": ["acme-hosting.example.com"]}]})
    _run_up(agent, "acme", "172.20.0.9")
    agent.call("tenant.start", {"tenant": "acme"})
    assert "reverse_proxy 172.20.0.9:5001" in (tmp_path / "routes" / "tenants.caddy").read_text()
    agent.fake.running.clear()
    agent.call("tenant.stop", {"tenant": "acme"})
    assert "reverse_proxy" not in (tmp_path / "routes" / "tenants.caddy").read_text()


def test_failed_route_reload_is_retried_even_after_agent_restart(agent, tmp_path):
    _run_up(agent, "acme", "172.20.0.2")
    original_runner = agent.runner
    failures = [True]

    def runner(command, **kwargs):
        if command[:2] == ["systemctl", "reload"] and failures:
            failures.pop()
            return subprocess.CompletedProcess(command, 1, "", "Caddy temporarily unavailable")
        return original_runner(command, **kwargs)

    agent.runner = runner
    routes = {"routes": [{"tenant": "acme", "hosts": ["acme-hosting.example.com"]}]}
    assert agent.call("proxy.routes", routes) == {"routes": 1, "changed": True, "reloaded": False}
    restarted = TenantRuntime(agent.config, runner=runner, private_dir=agent.private_dir, quota=agent.fake.quota)
    assert restarted.call("proxy.routes", routes) == {"routes": 1, "changed": False, "reloaded": True}
    assert restarted.call("proxy.routes", routes) == {"routes": 1, "changed": False, "reloaded": False}


def test_stopped_tenant_subnet_is_kept_until_caddy_forgets_its_address(agent, tmp_path):
    _run_up(agent, "acme", "172.20.0.2")
    routes = {"routes": [{"tenant": "acme", "hosts": ["acme-hosting.example.com"]}]}
    agent.call("proxy.routes", routes)
    original_runner = agent.runner
    fail_reload = [True]

    def runner(command, **kwargs):
        if command[:2] == ["systemctl", "reload"] and fail_reload:
            return subprocess.CompletedProcess(command, 1, "", "temporarily unavailable")
        if command[:3] == ["docker", "rm", "--force"]:
            agent.fake.running.pop(command[3], None)
        return original_runner(command, **kwargs)

    agent.runner = runner
    name = container_name(str(agent.base / "acme")) + "-net"
    agent.call("tenant.stop", {"tenant": "acme"})
    assert ["docker", "network", "rm", name] not in [command for command, _ in agent.fake.calls]
    assert name in json.loads((tmp_path / "routes/retired-networks.json").read_text())
    fail_reload.clear()
    restarted = TenantRuntime(agent.config, runner=runner, private_dir=agent.private_dir, quota=agent.fake.quota)
    assert restarted.call("proxy.routes", routes)["reloaded"]
    commands = [command for command, _ in agent.fake.calls]
    assert ["docker", "network", "rm", name] in commands
    assert json.loads((tmp_path / "routes/retired-networks.json").read_text()) == []


def test_network_policy_change_waits_for_caddy_to_drop_the_old_upstream(agent):
    _run_up(agent, "acme", "172.20.0.2")
    agent.call("proxy.routes", {"routes": [{"tenant": "acme", "hosts": ["acme-hosting.example.com"]}]})
    original_runner = agent.runner

    def runner(command, **kwargs):
        if command[:3] == ["docker", "rm", "--force"]:
            agent.fake.running.pop(command[3], None)
        if command[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "false", "")
        if command[:2] == ["systemctl", "reload"]:
            return subprocess.CompletedProcess(command, 1, "", "temporarily unavailable")
        return original_runner(command, **kwargs)

    agent.runner = runner
    with pytest.raises(AgentError) as error:
        agent.call("tenant.start", {"tenant": "acme", "network": "isolated"})
    assert error.value.code == "routing_unavailable"
    assert not any(command[:3] == ["docker", "network", "rm"] for command, _ in agent.fake.calls)
    assert not agent.fake.commands("run")


def test_retired_network_cleanup_survives_active_bridges_and_agent_restart(agent, tmp_path):
    routes = {"routes": []}
    agent.call("proxy.routes", routes)
    names = [container_name(str(agent.base / f"tenant-{index}")) + "-net" for index in range(5)]
    for name in names:
        agent._retire_network(name)
    original_runner = agent.runner
    attempted = []

    def runner(command, **kwargs):
        if command[:3] == ["docker", "network", "rm"]:
            attempted.append(command[3])
            if command[3] in names[:4]:
                return subprocess.CompletedProcess(command, 1, "", "network has active endpoints")
        return original_runner(command, **kwargs)

    agent.runner = runner
    agent.call("proxy.routes", routes)
    assert attempted == names[:4], "cleanup bounds Docker calls per sync"
    restarted = TenantRuntime(agent.config, runner=runner, private_dir=agent.private_dir, quota=agent.fake.quota)
    restarted.call("proxy.routes", routes)
    assert attempted[4] == names[4], "a restart preserves fair progress beyond active bridges"
    assert names[4] not in json.loads((tmp_path / "routes/retired-networks.json").read_text())
    assert set(json.loads((tmp_path / "routes/retired-networks.json").read_text())) == set(names[:4])


@pytest.mark.parametrize("routes", [
    [{"tenant": "acme", "hosts": ["evil.com {\n\troot * /"]}],
    [{"tenant": "acme", "hosts": ["UPPER.example.com"]}],
    [{"tenant": "acme", "hosts": ["10.0.0.1"]}],
    [{"tenant": "acme", "hosts": []}],
    [{"tenant": "acme", "hosts": ["a.example.com"]}, {"tenant": "beta", "hosts": ["a.example.com"]}],
    [{"tenant": "acme", "hosts": ["a.example.com"]}, {"tenant": "acme", "hosts": ["b.example.com"]}],
    [{"tenant": "../etc", "hosts": ["a.example.com"]}],
    "acme",
])
def test_routes_are_validated(agent, routes):
    with pytest.raises(AgentError):
        agent.call("proxy.routes", {"routes": routes})


def test_routes_refuse_public_or_loopback_upstreams(agent, tmp_path):
    _run_up(agent, "acme", "8.8.8.8")
    _run_up(agent, "beta", "127.0.0.1")
    result = agent.call("proxy.routes", {"routes": [{"tenant": "acme", "hosts": ["a.example.com"]},
                                                    {"tenant": "beta", "hosts": ["b.example.com"]}]})
    assert result["routes"] == 0


def test_routes_need_a_routes_directory(agent):
    del agent.config["routes_dir"]
    with pytest.raises(AgentError) as error:
        agent.call("proxy.routes", {"routes": []})
    assert error.value.code == "not_configured"
    agent.call("tenant.stop", {"tenant": "acme"})


def test_render_routes_is_deterministic():
    text = render_routes({"b": (["b.example.com"], "172.1.0.2:5001"), "a": (["a.example.com"], "172.1.0.3:5001")})
    assert text.index("# tenant a") < text.index("# tenant b")
    assert "header_up -X-Forwarded-Prefix" in text and "Strict-Transport-Security" in text


def test_routes_serve_cloudflare_and_fall_back_to_the_portal(agent, tmp_path):
    """Behind Cloudflare: real client address, no redirect loop, and never an empty 502 for a wiki."""
    from bananawiki.core import cloudflare

    agent.config["portal"] = "127.0.0.1:5099"
    _run_up(agent, "acme", "172.20.0.2")
    agent.call("proxy.routes", {"routes": [{"tenant": "acme", "hosts": ["acme-hosting.example.com"]}]})
    text = (tmp_path / "routes" / "tenants.caddy").read_text()
    assert "acme-hosting.example.com, http://acme-hosting.example.com {" in text
    assert "\ttls {\n\t\ton_demand\n\t}\n" in text
    assert "@needs_https {\n\t\tprotocol http\n\t\tnot {\n" in text
    assert 'header CF-Visitor *"scheme":"https"*' in text
    assert "remote_ip " + " ".join(cloudflare.RANGES) in text
    assert "redir @needs_https https://{host}{uri} 308" in text
    assert "lb_try_duration 5s" in text and "lb_try_interval 250ms" in text
    assert "handle_errors {\n\t\treverse_proxy 127.0.0.1:5099 {" in text
    assert text.count("header_up X-Forwarded-For {client_ip}") == 2
    assert "reverse_proxy 172.20.0.2:5001 {" in text, "readiness checks look for the container's route"


def test_routes_for_a_caddy_without_client_ip(agent, tmp_path):
    runner = agent.runner

    def old_caddy(command, **kwargs):
        if command[:2] == ["caddy", "version"]:
            return subprocess.CompletedProcess(command, 0, "v2.6.2 h1:abc\n", "")
        return runner(command, **kwargs)

    agent.runner = old_caddy
    _run_up(agent, "acme", "172.20.0.2")
    agent.call("proxy.routes", {"routes": [{"tenant": "acme", "hosts": ["acme-hosting.example.com"]}]})
    text = (tmp_path / "routes" / "tenants.caddy").read_text()
    assert "{client_ip}" not in text and "reverse_proxy 172.20.0.2:5001 {" in text
    assert "handle_errors" not in text, "no portal address configured"


def test_render_routes_without_portal_keeps_plain_proxying():
    text = render_routes({"a": (["a.example.com"], "172.1.0.3:5001")})
    assert "handle_errors" not in text and "lb_try_duration" not in text
    assert "a.example.com, http://a.example.com {" in text


def test_agent_config_names_the_portal(tmp_path, monkeypatch):
    from bananawiki.ops import runtime_agent

    (tmp_path / "config").mkdir()
    (tmp_path / "config/installation.json").write_text(json.dumps({"service": "root", "port": 5100}))
    (tmp_path / "config/app.env").write_text("HOSTING_PORT=5123\n")
    assert runtime_agent.load_config(tmp_path)["portal"] == "127.0.0.1:5123"
    (tmp_path / "config/app.env").write_text("HOSTING_PORT=nonsense\n")
    assert runtime_agent.load_config(tmp_path)["portal"] == "127.0.0.1:5100"


def test_managed_caddyfile_imports_the_routes_and_the_unit_owns_them():
    text = caddy.render({"domain": "example.com", "portal_domain": "portal.example.com", "port": 5099,
                         "mode": "hosting", "root": "/opt/bananawiki", "service": "bananawiki"})
    assert "import /var/lib/bananawiki-routes/*.caddy" in text
    assert text.index("import /var/lib/bananawiki-routes") < text.index(":443, :80 {"), "specific hosts first"
    settings = {"root": "/opt/bananawiki", "service": "bananawiki", "mode": "hosting", "port": 5099}
    features = ReleaseFeatures(runtime_agent=True, hardened=True)
    agent_service = next(s for s in services(settings, features) if s.privileged)
    unit = units.service_unit(settings, agent_service, features)
    assert "StateDirectory=bananawiki-routes bananawiki-quotas\nStateDirectoryMode=0755\n" in unit


def test_tenant_task_module_reports_errors_as_json(tmp_path, monkeypatch):
    from bananawiki.ops import tenant_task

    monkeypatch.setenv("BW_ENV", "test")
    monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
    monkeypatch.setenv("BW_DATABASE_PATH", str(tmp_path / "bananawiki.db"))
    assert tenant_task.run("not json")["error"] == "invalid"
    assert tenant_task.run('{"action": "rm -rf"}')["error"] == "invalid"
    assert tenant_task.run('{"action": "list_users"}')["error"] == "db_missing"
    seeded = tenant_task.run(json.dumps({"action": "seed", "username": "boss", "password": "long-password"}))
    assert seeded == {"ok": True, "username": "boss"}
    assert tenant_task.run(json.dumps({"action": "seed", "username": "boss", "password": "long-password"}))[
        "error"] == "data_exists"
    (tmp_path / ".bw-host").mkdir(exist_ok=True)
    assert tenant_task.run(json.dumps({"action": "snapshot", "name": "../../x.db"}))["error"] == "invalid"
    assert tenant_task.run(json.dumps({"action": "restore_db", "name": "missing.db"}))["error"] == "not_found"
