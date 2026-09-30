"""The hosting runtime agent: validation, the docker command it builds, the socket protocol."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import threading
from pathlib import Path

import pytest

from bananawiki.ops.agent_client import AgentClient, connect
from bananawiki.ops.files import write_environment, write_json
from bananawiki.ops.runtime_agent import AgentError, AgentServer, TenantRuntime, container_name, handle_line


class Docker:
    """Records docker invocations and keeps a copy of every env file at the time of `docker run`."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.env_files: list[str] = []

    def __call__(self, command, **_):
        self.calls.append(command)
        if command[1] == "run":
            path = command[command.index("--env-file") + 1]
            self.env_files.append(Path(path).read_text())
        if command[1:3] == ["network", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "No such network")
        return subprocess.CompletedProcess(command, 0, "[]" if command[1] == "inspect" else "", "")


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
    runtime = TenantRuntime(config, runner=docker, private_dir=private)
    runtime.docker_calls = docker  # type: ignore[attr-defined]
    return runtime


def test_start_builds_a_locked_down_container(runtime, tmp_path):
    runtime.call("tenant.start", {"tenant": "acme", "env": {"BW_SITE_NAME": "Acme", "BW_HOST": "127.0.0.9"},
                                  "limits": {"memory_mb": 1024, "cpus": 1.5}})
    docker = runtime.docker_calls
    run = next(call for call in docker.calls if call[1] == "run")
    real = str((tmp_path / "instances/acme").resolve())
    for flag in ("--read-only", "--cap-drop", "no-new-privileges:true", f"type=bind,src={real},dst=/data",
                 "org.bananawiki.role=tenant", f"org.bananawiki.data-dir={real}", "max-size=10m", "1024m"):
        assert flag in run
    assert run[-1] == "bananawiki-tenant:" + "a" * 40
    owner = (tmp_path / "instances/acme").stat()
    assert run[run.index("--user") + 1] == f"{owner.st_uid}:{owner.st_gid}"
    assert run[run.index("--name") + 1] == container_name(real)
    assert not any("Acme" in part for part in run), "variables never appear on argv"
    env = docker.env_files[0]
    assert "BW_SITE_NAME=Acme\n" in env and "BW_HOST=0.0.0.0\n" in env and "BW_MANAGED_HOSTING=1\n" in env
    assert not list(Path(runtime.private_dir).iterdir()), "the env file is removed after docker run"
    assert ["docker", "network", "create", "--driver", "bridge", "--internal", "--label",
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
