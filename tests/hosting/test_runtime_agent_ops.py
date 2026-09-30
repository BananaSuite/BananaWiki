"""The agent operations added for the hosting runtime: tenant tasks and Caddy routes."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bananawiki.ops import caddy, units
from bananawiki.ops.profile import ReleaseFeatures, services
from bananawiki.ops.runtime_agent import AgentError, TenantRuntime, container_name, render_routes


class Docker:
    """Records commands; containers listed in ``running`` are up with the given address."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.running: dict[str, str] = {}
        self.task_output = '{"ok": true, "users": []}\n'
        self.env_files: list[str] = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "systemctl":
            return subprocess.CompletedProcess(command, 0, "", "")
        verb = command[1]
        if verb == "run" and "--env-file" in command:
            self.env_files.append(Path(command[command.index("--env-file") + 1]).read_text())
        if verb == "inspect":
            return subprocess.CompletedProcess(command, 0, json.dumps(self._inspect(command[4:])), "")
        if verb == "ps":
            return subprocess.CompletedProcess(command, 0, "\n".join(self.running), "")
        if verb in ("exec", "run") and "-i" in command:
            return subprocess.CompletedProcess(command, 0, "log line\n" + self.task_output, "")
        if command[1:3] == ["network", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "No such network")
        return subprocess.CompletedProcess(command, 0, "", "")

    def _inspect(self, names):
        items = []
        for name in names:
            if name in self.running:
                data_dir, address = self.running[name].split("|")
                items.append({"Name": "/" + name, "Config": {"Labels": {
                    "org.bananawiki.role": "tenant", "org.bananawiki.data-dir": data_dir,
                    "org.bananawiki.internal-port": "5001"}, "Image": "img"},
                    "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"},
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
    runtime = TenantRuntime(config, runner=docker, private_dir=private)
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
    command, kwargs = next((c, k) for c, k in agent.fake.calls if c[1] == "run")
    for flag in ("--rm", "-i", "--read-only", "no-new-privileges:true", "ALL", "org.bananawiki.role=tenant-task"):
        assert flag in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--entrypoint") + 1] == "python"
    assert command[-4:] == ["-E", "-s", "-m", "bananawiki.ops.tenant_task"]
    assert "org.bananawiki.role=tenant" not in command, "the updater must not mistake it for a tenant"
    assert not any("top-secret-pw" in part for part in command), "passwords go to stdin only"
    assert json.loads(kwargs["input"]) == request
    assert not list(Path(agent.private_dir).iterdir()), "the env file is removed"


def test_task_in_a_running_tenant_uses_exec(agent):
    _run_up(agent, "acme", "172.20.0.2")
    agent.call("tenant.task", {"tenant": "acme", "request": {"action": "list_users"}})
    command, kwargs = next((c, k) for c, k in agent.fake.calls if c[1] == "exec")
    assert command[1:] == ["exec", "-i", container_name(str(agent.base / "acme")), "python", "-E", "-s", "-m",
                           "bananawiki.ops.tenant_task"]
    assert "input" in kwargs


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
    assert "StateDirectory=bananawiki-routes\nStateDirectoryMode=0755\n" in unit


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
