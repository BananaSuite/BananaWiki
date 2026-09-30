"""The hosting runtime agent: the only component allowed to drive Docker.

1.4 put the portal's service account in the ``docker`` group, which made any
bug in the internet-facing portal a root compromise of the host. In 1.6 the
portal is unprivileged and asks this small agent (``<name>-agent.service``,
root, no network, sandboxed) to start and stop tenant containers over a local
UNIX socket. The command set is narrow and every argument is validated: the
portal cannot choose the image, mounts, capabilities, user or network mode
of a container, only which tenant directory to run and within which limits.

The protocol is documented in ``RUNTIME_AGENT.md`` next to this file. The
same :class:`TenantRuntime` also runs in-process for a portal that still has
Docker access (a release installed by the 1.4 updater, before its units
are converged), so both paths share one implementation.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import pwd
import re
import secrets
import signal
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .files import read_environment, read_json

PROTOCOL = 1
MAX_REQUEST = 256 * 1024
TENANT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}(?:__apex)?")
ENV_KEY = re.compile(r"BW_[A-Z0-9_]{1,100}")
FORBIDDEN_ENV = frozenset({"BW_SECRET_KEY", "BW_SETUP_TOKEN"})
FORCED_ENV = {
    "BW_HOST": "0.0.0.0",  # noqa: S104 - inside the container's own network namespace
    "BW_PROXY_MODE": "1", "BW_MANAGED_HOSTING": "1", "BW_PLUGIN_ISOLATION": "container", "BW_ENV": "production",
    "BW_INSTANCE_DIR": "/data", "BW_DATABASE_PATH": "/data/bananawiki.db",
    "BW_MAINTENANCE_FILE": "/data/.banana-maintenance", "BW_EXTERNAL_PLUGINS_DIR": "/data/external_plugins",
}
LABEL_ROLE = "org.bananawiki.role=tenant"
LABEL_TASK = "org.bananawiki.role=tenant-task"
TASK_MODULE = "bananawiki.ops.tenant_task"
# The tenant can write the container's /tmp, which is $HOME in the image: without -s a
# user site-packages ``.pth`` planted there by its plugins would run inside the task and
# see the passwords the portal sends. -E ignores PYTHON* variables for the same reason.
TASK_COMMAND = ("python", "-E", "-s", "-m", TASK_MODULE)
TASK_ACTIONS = frozenset({
    "seed", "migrate", "list_users", "set_password", "remove_user", "reset_admin", "analytics", "apply_policy",
    "quarantine", "snapshot", "restore_db", "discard",
})
MAX_TASK_REQUEST = 64 * 1024
MAX_TASK_RESPONSE = 2 * 1024 * 1024
HOSTNAME = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_ROUTES = 10_000
MAX_HOSTS_PER_TENANT = 16
ROUTES_FILE = "tenants.caddy"
ROUTES_WANTED = "routes.json"

log = logging.getLogger("bananawiki.agent")
Runner = Callable[..., subprocess.CompletedProcess]


class AgentError(Exception):
    """A refused or failed request; ``code`` is stable for the portal to branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:28] or "tenant"


def container_name(data_dir: str) -> str:
    """Identical to 1.4 ``hosting.container_runtime.container_name`` so existing containers are recognised."""
    real = os.path.realpath(data_dir)
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:12]
    return f"bananawiki-{_safe_slug(os.path.basename(real))}-{digest}"


def _bounded_int(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise AgentError("invalid_request", f"{key} must be an integer between {low} and {high}.")
    return value


class TenantRuntime:
    """Validated Docker operations for tenant containers.

    ``config`` holds what only the operator decides: ``instances_dir``,
    ``image``, ``service_uid``/``service_gid`` and the limit ceilings. It is
    re-read from ``config/app.env`` for every request by :func:`load_config`.
    """

    def __init__(self, config: dict[str, Any], *, runner: Runner | None = None,
                 private_dir: Path | None = None):
        self.config = config
        self.runner = runner or subprocess.run
        self.private_dir = private_dir
        self.lock = threading.Lock()

    # Plumbing -------------------------------------------------------------

    def docker(self, *args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        result = self.runner(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
        if check and result.returncode:
            detail = (result.stderr or "").strip().splitlines()[-1:] or ["no detail"]
            raise AgentError("docker_failed", f"docker {args[0]} failed: {detail[0][:300]}")
        return result

    def tenant_dir(self, tenant: Any) -> Path:
        if not isinstance(tenant, str) or not TENANT.fullmatch(tenant):
            raise AgentError("invalid_tenant", "Tenant names are lowercase letters, digits and hyphens.")
        base = Path(self.config["instances_dir"]).resolve()
        path = base / tenant
        if path.is_symlink() or not path.is_dir():
            raise AgentError("unknown_tenant", f"No tenant directory {tenant!r}.")
        real = path.resolve()
        if real.parent != base:
            raise AgentError("invalid_tenant", "Tenant directories must be direct children of the instances directory.")
        return real

    # Commands -------------------------------------------------------------

    def ping(self, args: dict[str, Any]) -> dict[str, Any]:
        available = self.docker("version", "--format", "{{.Server.Version}}", timeout=10, check=False)
        return {"protocol": PROTOCOL, "docker": available.returncode == 0, "image": self.config["image"]}

    def image_status(self, args: dict[str, Any]) -> dict[str, Any]:
        present = self.docker("image", "inspect", self.config["image"], timeout=20, check=False).returncode == 0
        return {"image": self.config["image"], "present": present}

    def _inspect(self, names: list[str]) -> list[dict[str, Any]]:
        if not names:
            return []
        result = self.docker("inspect", "--type", "container", *names, timeout=30, check=False)
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        base = Path(self.config["instances_dir"]).resolve()
        output = []
        for item in items:
            labels = (item.get("Config") or {}).get("Labels") or {}
            data_dir = Path(labels.get("org.bananawiki.data-dir", "/"))
            if labels.get("org.bananawiki.role") != "tenant" or data_dir.parent != base:
                continue
            networks = (item.get("NetworkSettings") or {}).get("Networks") or {}
            addresses = [network["IPAddress"] for network in networks.values() if network.get("IPAddress")]
            state = item.get("State") or {}
            output.append({
                "tenant": data_dir.name, "container": item.get("Name", "").lstrip("/"),
                "running": bool(state.get("Running")), "address": addresses[0] if addresses else None,
                "internal_port": int(labels.get("org.bananawiki.internal-port", "5001")),
                "data_dir": str(data_dir), "image": (item.get("Config") or {}).get("Image"),
                "started_at": state.get("StartedAt"),
            })
        return output

    def tenant_list(self, args: dict[str, Any]) -> dict[str, Any]:
        ids = self.docker("ps", "--all", "--quiet", "--filter", "label=" + LABEL_ROLE).stdout.split()
        return {"tenants": self._inspect(ids)}

    def tenant_status(self, args: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.tenant_dir(args.get("tenant"))
        found = self._inspect([container_name(str(data_dir))])
        return found[0] if found else {"tenant": data_dir.name, "running": False, "exists": False}

    def _network(self, data_dir: Path, mode: str) -> str:
        name = container_name(str(data_dir)) + "-net"
        internal = mode == "isolated"
        inspected = self.docker("network", "inspect", "--format", "{{.Internal}}", name, timeout=15, check=False)
        if inspected.returncode == 0 and inspected.stdout.strip().lower() != str(internal).lower():
            # Never keep an older, more permissive network.
            self.docker("network", "rm", name, timeout=20)
            inspected = self.docker("network", "inspect", name, timeout=15, check=False)
        if inspected.returncode != 0:
            self.docker("network", "create", "--driver", "bridge", *(["--internal"] if internal else []),
                        "--label", LABEL_ROLE, name, timeout=30)
        return name

    def _environment(self, args: dict[str, Any], internal_port: int) -> dict[str, str]:
        supplied = args.get("env", {})
        if not isinstance(supplied, dict) or len(supplied) > 200:
            raise AgentError("invalid_request", "env must be an object with at most 200 entries.")
        output = {}
        for key, value in supplied.items():
            if not isinstance(key, str) or not ENV_KEY.fullmatch(key) or key in FORBIDDEN_ENV:
                raise AgentError("invalid_env", f"Environment variable {str(key)[:60]!r} is not allowed.")
            if not isinstance(value, str) or len(value) > 8192 or any(char in value for char in "\r\n\0"):
                raise AgentError("invalid_env", f"Invalid value for {key}.")
            output[key] = value
        output.update(FORCED_ENV)
        output["BW_PORT"] = str(internal_port)
        return output

    def _user(self, data_dir: Path) -> str:
        info = data_dir.stat()
        allowed = {self.config["service_uid"]}
        if info.st_uid == 0 or info.st_gid == 0 or info.st_uid not in allowed:
            raise AgentError("invalid_owner", "The tenant directory must belong to the hosting service account.")
        return f"{info.st_uid}:{info.st_gid}"

    def _limit_flags(self, args: dict[str, Any]) -> list[str]:
        limits = args.get("limits", {})
        if not isinstance(limits, dict):
            raise AgentError("invalid_request", "limits must be an object.")
        ceilings = self.config["limits"]
        memory = _bounded_int(limits, "memory_mb", 512, 128, ceilings["memory_mb"])
        pids = _bounded_int(limits, "pids", 256, 32, ceilings["pids"])
        nofile = _bounded_int(limits, "nofile", 1024, 128, ceilings["nofile"])
        cpus = limits.get("cpus", 1)
        if type(cpus) not in (int, float) or not 0.1 <= cpus <= ceilings["cpus"]:
            raise AgentError("invalid_request", f"cpus must be between 0.1 and {ceilings['cpus']}.")
        return ["--pids-limit", str(pids), "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
                "--cpus", f"{float(cpus):g}", "--ulimit", f"nofile={nofile}:{nofile}"]

    def _sandbox_flags(self, data_dir: Path, env_file: Path) -> list[str]:
        """What every tenant container gets, whatever the portal asked for."""
        return [
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",  # noqa: S108 - container tmpfs
            "--tmpfs", "/run:rw,nosuid,nodev,size=16m,mode=1777",
            "--user", self._user(data_dir), "--mount", f"type=bind,src={data_dir},dst=/data",
            "--env-file", str(env_file),
        ]

    def build_run(self, args: dict[str, Any], data_dir: Path, env_file: Path, network: str) -> list[str]:
        limits = self._limit_flags(args)
        internal_port = _bounded_int(args, "internal_port", 5001, 1024, 65535)
        command = [
            "run", "--detach", "--name", container_name(str(data_dir)), "--network", network,
            *self._sandbox_flags(data_dir, env_file), *limits,
            "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "--restart", "no",
            "--label", LABEL_ROLE, "--label", f"org.bananawiki.data-dir={data_dir}",
            "--label", f"org.bananawiki.internal-port={internal_port}",
        ]
        publish = args.get("publish_port")
        if publish is not None:
            port = _bounded_int(args, "publish_port", 0, 1024, 65535)
            command += ["--publish", f"127.0.0.1:{port}:{internal_port}"]
        return [*command, self.config["image"]]

    @contextmanager
    def _env_file(self, environment: dict[str, str]) -> Iterator[Path]:
        """A 0600 env file in the agent's private directory, removed afterwards."""
        with tempfile.TemporaryDirectory(prefix="env-", dir=self.private_dir) as directory:
            env_file = Path(directory) / "tenant.env"
            descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.writelines(f"{key}={value}\n" for key, value in sorted(environment.items()))
            yield env_file

    def tenant_start(self, args: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.tenant_dir(args.get("tenant"))
        network_mode = args.get("network", "isolated")
        if network_mode not in {"isolated", "outbound"}:
            raise AgentError("invalid_request", "network must be 'isolated' or 'outbound'.")
        if network_mode == "isolated" and args.get("publish_port") is not None:
            raise AgentError("invalid_request", "Isolated tenants cannot publish ports; use the bridge address.")
        environment = self._environment(args, _bounded_int(args, "internal_port", 5001, 1024, 65535))
        with self.lock:
            self.docker("rm", "--force", container_name(str(data_dir)), timeout=30, check=False)
            network = self._network(data_dir, network_mode)
            with self._env_file(environment) as env_file:
                self.docker(*self.build_run(args, data_dir, env_file, network), timeout=120)
        self._refresh_routes()
        return self.tenant_status({"tenant": data_dir.name})

    def tenant_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.tenant_dir(args.get("tenant"))
        timeout = _bounded_int(args, "timeout", 15, 1, 120)
        name = container_name(str(data_dir))
        with self.lock:
            self.docker("stop", "--time", str(timeout), name, timeout=timeout + 30, check=False)
            removed = self.docker("rm", "--force", name, timeout=30, check=False)
            if removed.returncode and "No such container" not in (removed.stderr or ""):
                raise AgentError("docker_failed", "The tenant container could not be removed.")
            self.docker("network", "rm", name + "-net", timeout=30, check=False)
        self._refresh_routes()
        return {"tenant": data_dir.name, "stopped": True}

    def tenant_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.tenant_dir(args.get("tenant"))
        lines = _bounded_int(args, "lines", 200, 1, 1000)
        result = self.docker("logs", "--tail", str(lines), container_name(str(data_dir)), timeout=30, check=False)
        text = ((result.stdout or "") + (result.stderr or ""))[-256 * 1024:]
        return {"tenant": data_dir.name, "lines": text.splitlines()[-lines:]}

    def tenant_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run :mod:`bananawiki.ops.tenant_task` in the tenant's sandbox and return its answer.

        While the tenant runs this is a ``docker exec`` in its container;
        otherwise a one-shot container with the same image, user, mount and
        hardening, and no network. The request (which may hold a password)
        goes to the task's stdin, never onto a command line.
        """
        data_dir = self.tenant_dir(args.get("tenant"))
        request = args.get("request")
        if not isinstance(request, dict) or request.get("action") not in TASK_ACTIONS:
            raise AgentError("invalid_request", "request must be an object with a known action.")
        payload = json.dumps(request)
        if len(payload) > MAX_TASK_REQUEST:
            raise AgentError("too_large", "The task request is too large.")
        timeout = _bounded_int(args, "timeout", 120, 5, 600)
        name = container_name(str(data_dir))
        found = self._inspect([name])
        if found and found[0]["running"]:
            result = self._run_task(["exec", "-i", name, *TASK_COMMAND], payload, timeout)
        else:
            environment = self._environment(args, 5001)
            limits = self._limit_flags(args)
            with self._env_file(environment) as env_file:
                command = [
                    "run", "--rm", "-i", "--name", name + "-task", "--network", "none",
                    *self._sandbox_flags(data_dir, env_file), *limits, "--label", LABEL_TASK,
                    "--entrypoint", TASK_COMMAND[0], self.config["image"], *TASK_COMMAND[1:],
                ]
                result = self._run_task(command, payload, timeout)
        return {"tenant": data_dir.name, "result": result}

    def _run_task(self, command: list[str], payload: str, timeout: int) -> dict[str, Any]:
        try:
            completed = self.runner(["docker", *command], input=payload, capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise AgentError("timeout", "The tenant task did not finish in time.") from None
        lines = [line for line in (completed.stdout or "")[-MAX_TASK_RESPONSE:].splitlines() if line.strip()]
        try:
            answer = json.loads(lines[-1]) if lines else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
            detail = (completed.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise AgentError("docker_failed", f"The tenant task failed: {detail[0][:300]}")
        return answer

    def proxy_routes(self, args: dict[str, Any]) -> dict[str, Any]:
        """Publish Caddy site blocks that send each tenant's hostnames straight to its container.

        The portal names tenants and hostnames only; upstream addresses come
        from Docker, and the file is written by the agent, so a compromised
        portal cannot inject Caddy directives or route to arbitrary hosts.
        """
        directory = self.config.get("routes_dir")
        if not directory:
            raise AgentError("not_configured", "This installation has no routes directory.")
        routes = args.get("routes")
        if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
            raise AgentError("invalid_request", f"routes must be a list of at most {MAX_ROUTES} entries.")
        wanted: dict[str, list[str]] = {}
        seen: set[str] = set()
        for route in routes:
            if not isinstance(route, dict):
                raise AgentError("invalid_request", "Each route is an object.")
            tenant, hosts = route.get("tenant"), route.get("hosts")
            if not isinstance(tenant, str) or not TENANT.fullmatch(tenant) or tenant in wanted:
                raise AgentError("invalid_tenant", "Routes name each tenant once, by its directory name.")
            if not isinstance(hosts, list) or not 0 < len(hosts) <= MAX_HOSTS_PER_TENANT:
                raise AgentError("invalid_request", f"Each tenant has 1 to {MAX_HOSTS_PER_TENANT} hostnames.")
            for host in hosts:
                if not isinstance(host, str) or not HOSTNAME.fullmatch(host) or host in seen:
                    raise AgentError("invalid_request", f"Invalid or duplicate hostname {str(host)[:80]!r}.")
                seen.add(host)
            wanted[tenant] = hosts
        with self.lock:
            _write_file(Path(directory) / ROUTES_WANTED, json.dumps(wanted, sort_keys=True), 0o600)
        return self._publish_routes(wanted)

    def _publish_routes(self, wanted: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Render the wanted routes with the containers' current addresses; reload Caddy on change.

        Called by ``proxy.routes`` and after every start and stop, so a
        container that comes back with a new address is routed at once.
        """
        directory = Path(self.config["routes_dir"])
        if wanted is None:
            try:
                wanted = json.loads((directory / ROUTES_WANTED).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"routes": 0, "changed": False, "reloaded": False}
        upstreams = {}
        for item in self.tenant_list({})["tenants"]:
            if item["running"] and item["address"] and item["tenant"] in wanted:
                address = ipaddress.ip_address(item["address"])
                if address.is_private and not address.is_loopback:
                    upstreams[item["tenant"]] = f"{item['address']}:{int(item['internal_port'])}"
        text = render_routes({tenant: (hosts, upstreams[tenant]) for tenant, hosts in wanted.items()
                              if tenant in upstreams})
        target = directory / ROUTES_FILE
        with self.lock:
            changed = not target.is_file() or target.read_text(encoding="utf-8") != text
            reloaded = False
            if changed:
                _write_file(target, text, 0o644)
                reload = self.runner(["systemctl", "reload", "caddy"], capture_output=True, text=True,
                                     timeout=60, check=False)
                reloaded = reload.returncode == 0
                if not reloaded:
                    log.warning("Caddy did not reload after a routing change: %s", (reload.stderr or "")[-300:])
        return {"routes": len(upstreams), "changed": changed, "reloaded": reloaded}

    def _refresh_routes(self) -> None:
        if not self.config.get("routes_dir"):
            return
        try:
            self._publish_routes()
        except (AgentError, OSError, subprocess.SubprocessError, ValueError) as error:
            log.warning("Could not refresh tenant routes: %s", error)

    OPERATIONS = {
        "ping": "ping", "image.status": "image_status", "tenant.list": "tenant_list",
        "tenant.status": "tenant_status", "tenant.start": "tenant_start", "tenant.stop": "tenant_stop",
        "tenant.logs": "tenant_logs", "tenant.task": "tenant_task", "proxy.routes": "proxy_routes",
    }

    def call(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        method = self.OPERATIONS.get(op) if isinstance(op, str) else None
        if method is None:
            raise AgentError("unknown_operation", f"Unknown operation {str(op)[:40]!r}.")
        if args is not None and not isinstance(args, dict):
            raise AgentError("invalid_request", "args must be an object.")
        return getattr(self, method)(args or {})


# Routes ----------------------------------------------------------------------


def render_routes(routes: dict[str, tuple[list[str], str]]) -> str:
    """Caddy site blocks, imported by the managed Caddyfile (``bananawiki.ops.caddy``)."""
    parts = ["# Managed by the BananaWiki runtime agent; rewritten on every routing change.\n"]
    for tenant in sorted(routes):
        hosts, upstream = routes[tenant]
        parts.append(
            f"\n# tenant {tenant}\n{', '.join(hosts)} {{\n\ttls {{\n\t\ton_demand\n\t}}\n"
            '\theader Strict-Transport-Security "max-age=31536000"\n'
            f"\treverse_proxy {upstream} {{\n\t\tflush_interval -1\n\t\theader_up -X-Forwarded-Prefix\n\t}}\n}}\n"
        )
    return "".join(parts)


def _write_file(target: Path, text: str, mode: int) -> None:
    """Atomically replace *target* (the routes file is 0644: Caddy runs as its own user)."""
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(dir=target.parent, prefix=".routes-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(pending, mode)
        os.replace(pending, target)
    finally:
        Path(pending).unlink(missing_ok=True)


# Configuration ---------------------------------------------------------------


def routes_dir(service: str) -> str:
    """The agent's state directory for Caddy routes (``StateDirectory=<service>-routes``)."""
    return f"/var/lib/{service}-routes"


def load_config(root: Path) -> dict[str, Any]:
    """What the operator configured: read fresh for every request (updates change the image)."""
    installation = read_json(root / "config/installation.json") or {}
    environment = read_environment(root / "config/app.env")
    service = installation.get("service", "")
    try:
        entry = pwd.getpwnam(service)
    except KeyError:
        raise AgentError("not_configured", "The hosting service account does not exist.") from None

    def ceiling(key: str, default: float) -> float:
        try:
            return float(environment.get(key, default))
        except ValueError:
            return default

    return {
        "service": service, "service_uid": entry.pw_uid, "service_gid": entry.pw_gid,
        "instances_dir": environment.get("INSTANCES_DIR", str(root / "data/instances")),
        "routes_dir": routes_dir(service),
        "image": environment.get("HOSTING_CONTAINER_IMAGE", "bananawiki-tenant:" + installation.get("revision", "")),
        "limits": {
            "memory_mb": int(ceiling("HOSTING_AGENT_MAX_MEMORY_MB", 4096)),
            "cpus": ceiling("HOSTING_AGENT_MAX_CPUS", 4.0),
            "pids": int(ceiling("HOSTING_AGENT_MAX_PIDS", 4096)),
            "nofile": int(ceiling("HOSTING_AGENT_MAX_NOFILE", 65536)),
        },
    }


# Socket server ---------------------------------------------------------------


def peer_uid(connection: socket.socket) -> int:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def handle_line(raw: bytes, runtime_factory: Callable[[], TenantRuntime]) -> dict[str, Any]:
    request_id = None
    try:
        if len(raw) > MAX_REQUEST:
            raise AgentError("too_large", "Request too large.")
        try:
            request = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AgentError("invalid_request", "Requests are one JSON object per line.") from None
        if not isinstance(request, dict) or request.get("v") != PROTOCOL:
            raise AgentError("unsupported_protocol", f"Use protocol version {PROTOCOL}.")
        request_id = request.get("id") if isinstance(request.get("id"), (str, int)) else None
        result = runtime_factory().call(request.get("op"), request.get("args"))
        return {"v": PROTOCOL, "id": request_id, "ok": True, "result": result}
    except AgentError as error:
        return {"v": PROTOCOL, "id": request_id, "ok": False, "error": {"code": error.code, "message": error.message}}
    except Exception as error:  # noqa: BLE001 - always answer; a crashed handler leaves the portal hanging
        log.exception("Agent request failed")
        return {"v": PROTOCOL, "id": request_id, "ok": False,
                "error": {"code": "internal", "message": type(error).__name__}}


class _Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:
        server: AgentServer = self.server  # type: ignore[assignment]
        uid = peer_uid(self.connection)
        try:
            allowed = server.allowed_uids()
        except AgentError as error:
            self.wfile.write(json.dumps({"v": PROTOCOL, "ok": False, "error": {
                "code": error.code, "message": error.message}}).encode() + b"\n")
            return
        if uid not in allowed:
            log.warning("Refused a connection from uid %s", uid)
            self.wfile.write(json.dumps({"v": PROTOCOL, "ok": False, "error": {
                "code": "forbidden", "message": "This account may not use the runtime agent."}}).encode() + b"\n")
            return
        raw = self.rfile.readline(MAX_REQUEST + 1)
        if not raw:
            return
        response = handle_line(raw, server.runtime)
        log.info("uid=%s op=%s ok=%s", uid, _op_name(raw), response["ok"])
        self.wfile.write(json.dumps(response).encode() + b"\n")


def _op_name(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("op"))[:40]
    except (ValueError, AttributeError):
        return "?"


class AgentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, root: Path, *, runner: Runner | None = None):
        self.root = root
        self.runner = runner
        self.private = path.parent / "private"
        self.private.mkdir(mode=0o700, exist_ok=True)
        self._runtime_lock = threading.Lock()
        self._shared: TenantRuntime | None = None
        path.unlink(missing_ok=True)
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o660)

    def allowed_uids(self) -> set[int]:
        return {0, load_config(self.root)["service_uid"]}

    def runtime(self) -> TenantRuntime:
        with self._runtime_lock:
            config = load_config(self.root)
            if self._shared is None:
                self._shared = TenantRuntime(config, runner=self.runner, private_dir=self.private)
            self._shared.config = config
            return self._shared


def serve(root: Path, socket_path: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    server = AgentServer(socket_path, root)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    log.info("Runtime agent listening on %s", socket_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)
    return 0


def new_request_id() -> str:
    return secrets.token_hex(8)
