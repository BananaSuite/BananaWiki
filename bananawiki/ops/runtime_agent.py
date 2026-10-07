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
import platform
import pwd
import re
import secrets
import selectors
import signal
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .caddy_blocks import HSTS, addresses, https_policy, parse_version, proxy, supports_client_ip
from .files import read_environment, read_json
from .project_quota import ProjectQuota, QuotaError

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
MOUNT_INVENTORY_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"role":{{json (index .Config.Labels "org.bananawiki.role")}},'
    '"running":{{json .State.Running}},"pid":{{json .State.Pid}}}'
)
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
MAX_CONCURRENT_REQUESTS = 16
HOSTNAME = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_ROUTES = 10_000
MAX_HOSTS_PER_TENANT = 16
ROUTES_FILE = "tenants.caddy"
ROUTES_WANTED = "routes.json"
ROUTES_RELOADED = "routes.reloaded.sha256"
RETIRED_NETWORKS = "retired-networks.json"
NETWORK_NAME = re.compile(r"bananawiki-[a-z0-9-]{1,28}-[a-f0-9]{12}-net")
SECCOMP_PROFILE = Path(__file__).with_name("seccomp") / "tenant.json"
SECCOMP_SHA256 = "c8e22680a0b52e62c96b342ac9e0605f98375a93ada11ad39e3ed2afe664a06e"
SECCOMP_CANONICAL_SHA256 = "66bc2d57c34a9d5ee26d6ce93a9b7f8caebfdea3233559f82e59c531c4a4dc31"
IPV6_SYSCTLS = {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"}
DEFAULT_STORAGE_BYTES = 10 * 1024 ** 3
DEFAULT_STORAGE_INODES = 100_000
DEFAULT_STORAGE_RESERVE = 256 * 1024 ** 2

log = logging.getLogger("bananawiki.agent")
Runner = Callable[..., subprocess.CompletedProcess]


class AgentError(Exception):
    """A refused or failed request; ``code`` is stable for the portal to branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def seccomp_profile() -> Path:
    """Require the pinned default allowlist plus quota-changing ioctl denials."""
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise AgentError("sandbox_unavailable", "Quota-protected hosting requires Linux x86_64.")
    try:
        contents = SECCOMP_PROFILE.read_bytes()
    except OSError:
        raise AgentError("sandbox_unavailable", "The tenant seccomp profile is unavailable.") from None
    if hashlib.sha256(contents).hexdigest() != SECCOMP_SHA256:
        raise AgentError("sandbox_unavailable", "The tenant seccomp profile does not match this release.")
    return SECCOMP_PROFILE.resolve()


def _quota_protected(options: Any) -> bool:
    """Docker stores the actual policy in SecurityOpt, including for later execs."""
    if not isinstance(options, list):
        return False
    for option in options:
        if not isinstance(option, str) or not option.startswith("seccomp="):
            continue
        try:
            profile = json.loads(option.removeprefix("seccomp="))
        except (ValueError, RecursionError):
            continue
        canonical = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(canonical).hexdigest() == SECCOMP_CANONICAL_SHA256:
            return True
    return False


def _task_process(command: list[str], payload: str, timeout: float) -> subprocess.CompletedProcess:
    """Drain untrusted task pipes within byte and time limits, then reap the Docker client."""
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0) as process:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        pending = memoryview(payload.encode("utf-8"))
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for ready, _events in selector.select(remaining):
                        if ready.data == "stdin":
                            try:
                                sent = os.write(ready.fd, pending[:65536]) if pending else 0
                                pending = pending[sent:]
                            except BrokenPipeError:
                                pending = pending[:0]
                            except BlockingIOError:
                                continue
                            if not pending:
                                selector.unregister(ready.fileobj)
                                ready.fileobj.close()
                            continue
                        chunk = os.read(ready.fd, 65536)
                        if not chunk:
                            selector.unregister(ready.fileobj)
                            ready.fileobj.close()
                            continue
                        buffer = output[ready.data]
                        if len(buffer) + len(chunk) > MAX_TASK_RESPONSE:
                            raise AgentError("too_large", "The tenant task response exceeded its byte limit.")
                        buffer.extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        return subprocess.CompletedProcess(command, process.returncode,
                                           output["stdout"].decode("utf-8", "replace"),
                                           output["stderr"].decode("utf-8", "replace"))


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
                 private_dir: Path | None = None, quota: Any = None):
        self.config = config
        self.runner = runner or subprocess.run
        self.private_dir = private_dir
        self._injected_quota = quota
        self.lock = threading.RLock()
        self._tenant_locks_lock = threading.Lock()
        self._tenant_locks: dict[str, threading.Lock] = {}

    def _tenant_lock(self, data_dir: Path) -> threading.Lock:
        """Keep a tenant's start, stop and database tasks from overlapping."""
        with self._tenant_locks_lock:
            return self._tenant_locks.setdefault(str(data_dir), threading.Lock())

    def _stop_task_containers(self, data_dir: Path) -> None:
        """Remove this tenant's bounded tasks left by an interrupted agent.

        A process-local lock cannot cover a previous agent generation. Prove
        these containers absent before treating a tree as stopped for repair.
        """
        pattern = f"name=^/{container_name(str(data_dir))}-task-[0-9a-f]{{12}}$"
        query = ("ps", "--all", "--filter", "label=" + LABEL_TASK, "--filter", pattern, "--format", "{{.ID}}")
        identifiers = self.docker(*query, timeout=30).stdout.split()
        if len(identifiers) > 256 or any(not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in identifiers):
            raise AgentError("docker_failed", "The tenant task container inventory is invalid or excessive.")
        if identifiers:
            self.docker("rm", "--force", *identifiers, timeout=30)
            if self.docker(*query, timeout=30).stdout.strip():
                raise AgentError("docker_failed", "The tenant task containers could not be confirmed removed.")

    def _mounted_identity(self, pid: int) -> tuple[int, int]:
        """Read only a daemon-reported process's actual mounted directory."""
        if type(pid) is not int or not 0 < pid <= 2 ** 31 - 1:
            raise AgentError("quota_unavailable", "A live tenant mount process is unavailable.")
        try:
            descriptor = os.open(f"/proc/{pid}/root/data",
                                 os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(descriptor)
                return info.st_dev, info.st_ino
            finally:
                os.close(descriptor)
        except OSError as error:
            raise AgentError("quota_unavailable", f"A live tenant mount could not be inspected: {error}") from None

    def _quiesce_mount(self, descriptor: int, *, allowed_main: str | None = None) -> None:
        """Find live mounts by inode even after the portal renames a directory.

        The caller holds the global admission lock and the target FD. Labels
        and path-derived names cannot prove that an inode is stopped. Inspect
        actual live mounts, remove matching tasks and refuse an aliased server.
        """
        info = os.fstat(descriptor)
        target = info.st_dev, info.st_ino
        query = ("ps", "--filter", "label=org.bananawiki.role", "--format", "{{.ID}}")
        listed = self.docker(*query, timeout=30).stdout
        identifiers = listed.split()
        if (len(listed) > 1024 * 1024 or len(identifiers) > MAX_ROUTES
                or len(set(identifiers)) != len(identifiers)
                or any(not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in identifiers)):
            raise AgentError("docker_failed", "The live tenant mount inventory is invalid or excessive.")
        tasks = []
        for offset in range(0, len(identifiers), 128):
            batch = identifiers[offset:offset + 128]
            inspected = self.docker("inspect", "--type", "container", "--format", MOUNT_INVENTORY_FORMAT,
                                    *batch, timeout=30).stdout
            if len(inspected) > 1024 * 1024:
                raise AgentError("docker_failed", "The live tenant mount inspection is excessive.")
            try:
                items = [json.loads(line) for line in inspected.splitlines() if line.strip()]
            except (ValueError, RecursionError):
                raise AgentError("docker_failed", "The live tenant mount inspection is invalid.") from None
            seen = set()
            if len(items) != len(batch):
                raise AgentError("docker_failed", "The live tenant mount inventory changed during inspection.")
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", item["id"])
                        or item["id"] in seen
                        or sum(item["id"].startswith(value) for value in batch) != 1
                        or not isinstance(item.get("name"), str)
                        or type(item.get("running")) is not bool):
                    raise AgentError("docker_failed", "The live tenant mount identity is invalid.")
                seen.add(item["id"])
                if not item["running"] or item.get("role") not in {"tenant", "tenant-task"}:
                    continue
                if self._mounted_identity(item.get("pid")) != target:
                    continue
                if item["role"] == "tenant":
                    if item["name"].lstrip("/") != allowed_main:
                        raise AgentError("tenant_running", "Stop the server mounted on this tenant directory "
                                         "before preparing or launching it under another name.")
                else:
                    tasks.append(item["id"])
        if tasks:
            self.docker("rm", "--force", *tasks, timeout=30)
            # Successful removal must be independently confirmed by the daemon.
            listed = self.docker("ps", "--all", "--no-trunc", "--filter", "label=org.bananawiki.role",
                                 "--format", "{{.ID}}", timeout=30).stdout
            remaining = listed.split()
            if (len(listed) > 1024 * 1024 or len(remaining) > MAX_ROUTES
                    or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in remaining)
                    or any(value in remaining for value in tasks)):
                raise AgentError("docker_failed", "The aliased tenant tasks could not be confirmed removed.")

    def _quota_config(self) -> dict[str, Any]:
        value = self.config.get("quota")
        if not isinstance(value, dict):
            if self._injected_quota is None:
                raise AgentError("quota_unavailable", "Hard tenant storage quotas are not configured.")
            return {"max_bytes": DEFAULT_STORAGE_BYTES, "max_inodes": DEFAULT_STORAGE_INODES}
        return value

    def _quotas(self) -> ProjectQuota:
        if self._injected_quota is not None:
            return self._injected_quota
        config = self._quota_config()
        try:
            return ProjectQuota(Path(self.config["instances_dir"]), Path(config["state_dir"]),
                                max_bytes=config["max_bytes"], max_inodes=config["max_inodes"],
                                project_start=config["project_start"],
                                storage_reserve_bytes=config["storage_reserve_bytes"])
        except (QuotaError, OSError) as error:
            raise AgentError("quota_unavailable", str(error)) from None

    def _storage_limits(self, args: dict[str, Any]) -> tuple[int, int]:
        limits = args.get("limits", {})
        if not isinstance(limits, dict):
            raise AgentError("invalid_request", "limits must be an object.")
        config = self._quota_config()
        # A portal policy of zero means its application estimate is unlimited;
        # it never disables the finite, operator-owned host ceiling.
        requested = _bounded_int(limits, "storage_bytes", 0, 0, config["max_bytes"])
        return requested or config["max_bytes"], config["max_inodes"]

    def _verify_storage(self, data_dir: Path, args: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            expected = {}
            if args is not None:
                byte_limit, inode_limit = self._storage_limits(args)
                expected = {"byte_limit": byte_limit, "inode_limit": inode_limit}
            return self._quotas().verify(data_dir.name, **expected).to_dict()
        except (QuotaError, OSError) as error:
            raise AgentError("quota_unavailable", str(error)) from None

    def _prepare_storage(self, data_dir: Path, args: dict[str, Any], *, running: bool) -> dict[str, Any]:
        byte_limit, inode_limit = self._storage_limits(args)
        try:
            descriptor = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                self._quiesce_mount(descriptor, allowed_main=container_name(str(data_dir)) if running else None)
                return self._quotas().prepare(data_dir.name, byte_limit=byte_limit, inode_limit=inode_limit,
                                              repair=not running, expected_descriptor=descriptor).to_dict()
            finally:
                os.close(descriptor)
        except (QuotaError, OSError) as error:
            raise AgentError("quota_unavailable", str(error)) from None

    @contextmanager
    def _pinned_storage(self, data_dir: Path, args: dict[str, Any] | None = None,
                        *, allowed_main: str | None = None):
        """Pin the expected inode until Docker's actual mount is verified.

        Docker starts only a trusted inert guard. Its mounted root is checked
        against this witness before the server or a tenant task can execute.
        """
        descriptor = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self._quiesce_mount(descriptor, allowed_main=allowed_main)
            witness = self._verify_storage(data_dir, args)
            try:
                self._quotas().verify_descriptor(descriptor, witness)
            except (QuotaError, OSError) as error:
                raise AgentError("quota_unavailable", str(error)) from None
            yield str(data_dir), witness
        finally:
            os.close(descriptor)

    def _verify_mount_descriptor(self, pid: int, witness: dict[str, Any]) -> None:
        if type(pid) is not int or not 0 < pid <= 2 ** 31 - 1:
            raise AgentError("quota_unavailable", "The tenant mount process is unavailable.")
        try:
            descriptor = os.open(f"/proc/{pid}/root/data",
                                 os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                self._quotas().verify_descriptor(descriptor, witness)
            finally:
                os.close(descriptor)
        except (QuotaError, OSError) as error:
            raise AgentError("quota_unavailable", f"The actual tenant mount could not be verified: {error}") from None

    def _verify_started_mount(self, name: str, witness: dict[str, Any], *, timeout: float = 30) -> None:
        inspected = self.docker("inspect", "--type", "container", "--format", "{{.State.Pid}}", name,
                                timeout=timeout, check=False)
        value = inspected.stdout.strip()
        if inspected.returncode or not value.isascii() or not value.isdigit() or len(value) > 10:
            raise AgentError("quota_unavailable", "The actual tenant mount process could not be inspected.")
        self._verify_mount_descriptor(int(value), witness)

    def tenant_quota(self, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a stopped tenant, or verify an existing tenant's kernel limits.

        Project identifiers and filesystem paths are chosen only by the agent.
        This operation must succeed before the portal writes imported/copied
        files or asks for its first seed task.
        """
        data_dir = self.tenant_dir(args.get("tenant"))
        self._user(data_dir)
        prepare = args.get("prepare", False)
        if type(prepare) is not bool:
            raise AgentError("invalid_request", "prepare must be a boolean.")
        if prepare:
            self._storage_limits(args)
        with self._tenant_lock(data_dir), self.lock:
            if prepare:
                found = self._inspect([container_name(str(data_dir))], strict=True)
                if not found or not found[0]["running"]:
                    self._stop_task_containers(data_dir)
                witness = self._prepare_storage(data_dir, args, running=bool(found and found[0]["running"]))
            else:
                witness = self._verify_storage(data_dir, args if "limits" in args else None)
        return {"tenant": data_dir.name, "storage_quota_verified": True, "storage_quota": witness}

    # Plumbing -------------------------------------------------------------

    def _caddy_client_ip(self) -> bool:
        """Whether the installed Caddy understands ``{client_ip}`` (2.7+; unknown: yes)."""
        try:
            result = self.runner(["caddy", "version"], capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            return True
        return supports_client_ip(parse_version(result.stdout or "") if result.returncode == 0 else None)

    def docker(self, *args: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
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

    def _inspect(self, names: list[str], *, timeout: float = 30, strict: bool = False) -> list[dict[str, Any]]:
        if not names:
            return []
        result = self.docker("inspect", "--type", "container", *names, timeout=timeout, check=False)
        if strict and result.returncode:
            # Inspect errors do not prove that the named container is stopped.
            # Confirm absence with a successful independent daemon query.
            absent = self.docker("ps", "--all", "--filter", f"name=^/{names[0]}$",
                                 "--format", "{{.ID}}", timeout=timeout, check=False)
            if absent.returncode or absent.stdout.strip():
                raise AgentError("unavailable", "The tenant container state could not be confirmed.")
            return []
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            if strict:
                raise AgentError("unavailable", "The tenant container inspection is invalid.") from None
            return []
        if strict and (not isinstance(items, list) or len(items) != 1
                       or not isinstance(items[0], dict)
                       or type((items[0].get("State") or {}).get("Running")) is not bool):
            raise AgentError("unavailable", "The tenant container state could not be confirmed.")
        base = Path(self.config["instances_dir"]).resolve()
        output = []
        for item in items:
            labels = (item.get("Config") or {}).get("Labels") or {}
            data_dir = Path(labels.get("org.bananawiki.data-dir", "/"))
            if labels.get("org.bananawiki.role") != "tenant" or data_dir.parent != base:
                if strict:
                    raise AgentError("sandbox_outdated", "The named container has unexpected ownership labels.")
                continue
            if strict and (container_name(str(data_dir)) != names[0]
                           or item.get("Name", "").lstrip("/") != names[0]):
                raise AgentError("sandbox_outdated", "The named container has unexpected tenant identity.")
            networks = (item.get("NetworkSettings") or {}).get("Networks") or {}
            addresses = [network["IPAddress"] for network in networks.values() if network.get("IPAddress")]
            state = item.get("State") or {}
            sysctls = (item.get("HostConfig") or {}).get("Sysctls") or {}
            try:
                quota = self._verify_storage(self.tenant_dir(data_dir.name))
            except AgentError:
                quota = None
            if quota is not None and any(labels.get(f"org.bananawiki.quota-{key}") != str(quota[key])
                                         for key in ("project_id", "root_inode", "root_generation")):
                quota = None
            if quota is not None and state.get("Running"):
                try:
                    self._verify_mount_descriptor(state.get("Pid"), quota)
                except AgentError:
                    quota = None
            output.append({
                "tenant": data_dir.name, "container": item.get("Name", "").lstrip("/"),
                "running": bool(state.get("Running")), "address": addresses[0] if addresses else None,
                "internal_port": int(labels.get("org.bananawiki.internal-port", "5001")),
                "data_dir": str(data_dir), "image": (item.get("Config") or {}).get("Image"),
                "started_at": state.get("StartedAt"),
                "quota_protected": _quota_protected((item.get("HostConfig") or {}).get("SecurityOpt")),
                "storage_quota_verified": quota is not None, "storage_quota": quota,
                "ipv6_disabled": isinstance(sysctls, dict) and all(sysctls.get(k) == v for k, v in IPV6_SYSCTLS.items()),
            })
        return output

    def tenant_list(self, args: dict[str, Any]) -> dict[str, Any]:
        ids = self.docker("ps", "--all", "--quiet", "--filter", "label=" + LABEL_ROLE).stdout.split()
        return {"tenants": self._inspect(ids)}

    def tenant_status(self, args: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.tenant_dir(args.get("tenant"))
        found = self._inspect([container_name(str(data_dir))], strict=True)
        return found[0] if found else {"tenant": data_dir.name, "running": False, "exists": False}

    def _network(self, data_dir: Path, mode: str) -> str:
        name = container_name(str(data_dir)) + "-net"
        internal = mode == "isolated"
        inspected = self.docker("network", "inspect", "--format", "{{.Internal}}", name, timeout=15, check=False)
        if inspected.returncode == 0 and inspected.stdout.strip().lower() != str(internal).lower():
            # Never keep an older, more permissive network.
            # Caddy must first forget its former upstream: removing the bridge
            # makes its subnet available for an unrelated tenant to reuse.
            if not self._refresh_routes():
                raise AgentError("routing_unavailable", "Caddy must reload before changing a tenant network.")
            removed = self.docker("network", "rm", name, timeout=20, check=False)
            if removed.returncode and "No such network" not in (removed.stderr or ""):
                raise AgentError("docker_failed", "The former tenant network could not be removed.")
            inspected = self.docker("network", "inspect", name, timeout=15, check=False)
        if inspected.returncode != 0:
            self.docker("network", "create", "--driver", "bridge", "--ipv6=false",
                        *(["--internal"] if internal else []),
                        "--label", LABEL_ROLE, name, timeout=30)
        ipv6 = self.docker("network", "inspect", "--format", "{{.EnableIPv6}}", name, timeout=15, check=False)
        if ipv6.returncode or ipv6.stdout.strip().lower() != "false":
            raise AgentError("network_unavailable", "Tenant bridges must have IPv6 disabled. "
                             "Review Docker network defaults before restarting this tenant.")
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

    def _sandbox_flags(self, data_dir: Path, env_file: Path, *, mount_source: str | None = None) -> list[str]:
        """What every tenant container gets, whatever the portal asked for."""
        return [
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--security-opt", "seccomp=" + str(seccomp_profile()),
            *(flag for key, value in IPV6_SYSCTLS.items() for flag in ("--sysctl", f"{key}={value}")),
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",  # noqa: S108 - container tmpfs
            "--tmpfs", "/run:rw,nosuid,nodev,size=16m,mode=1777",
            "--user", self._user(data_dir), "--mount", f"type=bind,src={mount_source or data_dir},dst=/data",
            "--env-file", str(env_file),
        ]

    def build_run(self, args: dict[str, Any], data_dir: Path, env_file: Path, network: str, *,
                  mount_source: str | None = None, quota: dict[str, Any] | None = None,
                  gate_token: str | None = None) -> list[str]:
        limits = self._limit_flags(args)
        internal_port = _bounded_int(args, "internal_port", 5001, 1024, 65535)
        command = [
            "run", "--detach", "--name", container_name(str(data_dir)), "--network", network,
            *self._sandbox_flags(data_dir, env_file, mount_source=mount_source), *limits,
            "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "--restart", "no",
            "--label", LABEL_ROLE, "--label", f"org.bananawiki.data-dir={data_dir}",
            "--label", f"org.bananawiki.internal-port={internal_port}",
        ]
        publish = args.get("publish_port")
        if publish is not None:
            port = _bounded_int(args, "publish_port", 0, 1024, 65535)
            command += ["--publish", f"127.0.0.1:{port}:{internal_port}"]
        if quota is not None:
            for key in ("project_id", "root_inode", "root_generation"):
                command += ["--label", f"org.bananawiki.quota-{key}={quota[key]}"]
        if gate_token is not None:
            return [*command, "--entrypoint", "python", self.config["image"], "-E", "-s", "-m",
                    "bananawiki.ops.tenant_guard", "server", gate_token]
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
        # Validate the whole request before removing the currently healthy wiki.
        self._limit_flags(args)
        self._storage_limits(args)
        self._user(data_dir)
        seccomp_profile()  # fail closed before removing an existing healthy container
        if args.get("publish_port") is not None:
            _bounded_int(args, "publish_port", 0, 1024, 65535)
        with self._tenant_lock(data_dir), self.lock:
            found = self._inspect([container_name(str(data_dir))], strict=True)
            self._stop_task_containers(data_dir)
            self._prepare_storage(data_dir, args, running=bool(found and found[0]["running"]))
            with self._pinned_storage(data_dir, args, allowed_main=container_name(str(data_dir))) as (mount_source, quota):
                self.docker("rm", "--force", container_name(str(data_dir)), timeout=30, check=False)
                network = self._network(data_dir, network_mode)
                gate_token = secrets.token_hex(16)
                name = container_name(str(data_dir))
                try:
                    with self._env_file(environment) as env_file:
                        self.docker(*self.build_run(args, data_dir, env_file, network,
                                                   mount_source=mount_source, quota=quota,
                                                   gate_token=gate_token), timeout=120)
                    self._verify_started_mount(name, quota)
                    self.docker("exec", name, "python", "-E", "-s", "-m", "bananawiki.ops.tenant_guard",
                                "release", gate_token, timeout=15)
                except BaseException:
                    self.docker("rm", "--force", name, timeout=30, check=False)
                    raise
        self._refresh_routes()
        return self.tenant_status({"tenant": data_dir.name})

    def tenant_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.tenant_dir(args.get("tenant"))
        timeout = _bounded_int(args, "timeout", 15, 1, 120)
        name = container_name(str(data_dir))
        with self._tenant_lock(data_dir), self.lock:
            self._stop_task_containers(data_dir)
            descriptor = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                self._quiesce_mount(descriptor, allowed_main=name)
            finally:
                os.close(descriptor)
            self.docker("stop", "--time", str(timeout), name, timeout=timeout + 30, check=False)
            removed = self.docker("rm", "--force", name, timeout=30, check=False)
            if removed.returncode and "No such container" not in (removed.stderr or ""):
                raise AgentError("docker_failed", "The tenant container could not be removed.")
            if self.config.get("routes_dir"):
                self._retire_network(name + "-net")
                self._refresh_routes()
            else:
                self.docker("network", "rm", name + "-net", timeout=30, check=False)
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
        seccomp_profile()
        name = container_name(str(data_dir))
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0.001:
                raise AgentError("timeout", "The tenant task could not start before its deadline.")
            return value

        lock = self._tenant_lock(data_dir)
        if not lock.acquire(timeout=remaining()):
            raise AgentError("timeout", "The tenant task could not start before its deadline.")
        try:
            self._user(data_dir)
            self._verify_storage(data_dir)
            found = self._inspect([name], timeout=min(30, remaining()), strict=True)
            if found and found[0]["running"]:
                if (not found[0]["quota_protected"] or not found[0]["ipv6_disabled"]
                        or not found[0]["storage_quota_verified"]):
                    raise AgentError("sandbox_outdated", "Restart this tenant to apply the current sandbox policy "
                                     "before running a task.")
                # An exec process survives a killed Docker CLI. Its deadline
                # must be enforced inside the container as well.
                budget = remaining()
                result = self._run_task(["exec", "-i", name, "/usr/bin/timeout", "--signal=TERM",
                                         "--kill-after=5s", f"{budget:.6f}s", *TASK_COMMAND], payload, budget + 10)
            else:
                environment = self._environment(args, 5001)
                limits = self._limit_flags(args)
                task_name = name + "-task-" + secrets.token_hex(6)
                launch_attempted = False
                with ExitStack() as contexts:
                    try:
                        # Serialize mount inventory, guard launch and actual-FD
                        # admission across names, but let unrelated tasks run in
                        # parallel after their mounts have been verified.
                        with self.lock:
                            self._stop_task_containers(data_dir)
                            mount_source, quota = contexts.enter_context(self._pinned_storage(data_dir))
                            env_file = contexts.enter_context(self._env_file(environment))
                            sandbox = self._sandbox_flags(data_dir, env_file, mount_source=mount_source)
                            budget = remaining()
                            command = [
                                "run", "--detach", "--name", task_name, "--network", "none",
                                *sandbox, *limits, "--label", LABEL_TASK,
                                "--label", f"org.bananawiki.data-dir={data_dir}",
                                "--entrypoint", "python", self.config["image"], "-E", "-s", "-m",
                                "bananawiki.ops.tenant_guard", "task", secrets.token_hex(16), str(timeout + 30),
                            ]
                            launch_attempted = True
                            self.docker(*command, timeout=min(30, budget))
                            self._verify_started_mount(task_name, quota, timeout=min(30, remaining()))
                        budget = remaining()
                        result = self._run_task(["exec", "-i", task_name, "/usr/bin/timeout", "--signal=TERM",
                                                 "--kill-after=5s", f"{budget:.6f}s", *TASK_COMMAND], payload, budget + 10)
                    finally:
                        # Task containers and inert guards have bounded lifetimes,
                        # including when an agent or Docker client is interrupted.
                        if launch_attempted:
                            self.docker("rm", "--force", task_name, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            raise AgentError("timeout", "The tenant task did not finish in time.") from None
        finally:
            lock.release()
        return {"tenant": data_dir.name, "result": result}

    def _run_task(self, command: list[str], payload: str, timeout: float) -> dict[str, Any]:
        try:
            if self.runner is subprocess.run:
                completed = _task_process(["docker", *command], payload, timeout)
            else:
                completed = self.runner(["docker", *command], input=payload, capture_output=True, text=True,
                                        timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise AgentError("timeout", "The tenant task did not finish in time.") from None
        if completed.returncode == 124:
            raise AgentError("timeout", "The tenant task did not finish in time.")
        if any(len((value or "").encode("utf-8")) > MAX_TASK_RESPONSE
               for value in (completed.stdout, completed.stderr)):
            raise AgentError("too_large", "The tenant task response exceeded its byte limit.")
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
        with self.lock:
            return self._publish_routes_locked(wanted)

    def _publish_routes_locked(self, wanted: dict[str, list[str]] | None) -> dict[str, Any]:
        """Read, render and publish one routing snapshot while holding ``self.lock``."""
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
                              if tenant in upstreams},
                             portal=self.config.get("portal"), client_ip=self._caddy_client_ip())
        target = directory / ROUTES_FILE
        with self.lock:
            changed = not target.is_file() or target.read_text(encoding="utf-8") != text
            reloaded = False
            if changed:
                _write_file(target, text, 0o644)
            digest = hashlib.sha256(text.encode()).hexdigest()
            acknowledged = directory / ROUTES_RELOADED
            try:
                previous_digest = acknowledged.read_text(encoding="ascii")
            except (OSError, UnicodeError):
                previous_digest = ""
            if changed or previous_digest != digest:
                reload = self.runner(["systemctl", "reload", "caddy"], capture_output=True, text=True,
                                     timeout=60, check=False)
                reloaded = reload.returncode == 0
                if reloaded:
                    _write_file(acknowledged, digest, 0o600)
                else:
                    log.warning("Caddy did not reload after a routing change: %s", (reload.stderr or "")[-300:])
            if reloaded or previous_digest == digest:
                self._clean_retired_networks()
        return {"routes": len(upstreams), "changed": changed, "reloaded": reloaded}

    def _retired_networks(self) -> list[str]:
        try:
            value = json.loads((Path(self.config["routes_dir"]) / RETIRED_NETWORKS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return list(dict.fromkeys(name for name in value if isinstance(name, str) and NETWORK_NAME.fullmatch(name))) \
            if isinstance(value, list) else []

    def _retire_network(self, name: str) -> None:
        pending = self._retired_networks()
        if name not in pending:
            pending.append(name)
        _write_file(Path(self.config["routes_dir"]) / RETIRED_NETWORKS, json.dumps(pending), 0o600)

    def _clean_retired_networks(self) -> None:
        """Release stopped bridges only once Caddy acknowledged the current table.

        The small durable queue survives agent restarts. Docker refuses to
        remove a bridge a restarted tenant is using; leave it queued until
        that tenant stops again. Rotate failed removals to the queue's end
        so active bridges cannot starve later entries. Bound work per sync
        when Docker is slow.
        """
        pending = self._retired_networks()
        before = list(pending)
        for name in before[:4]:
            pending.remove(name)
            try:
                result = self.docker("network", "rm", name, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError) as error:
                log.warning("Could not remove retired tenant network %s: %s", name, type(error).__name__)
                pending.append(name)
                continue
            if result.returncode != 0 and "No such network" not in (result.stderr or ""):
                pending.append(name)
        if pending != before:
            _write_file(Path(self.config["routes_dir"]) / RETIRED_NETWORKS, json.dumps(pending), 0o600)

    def _refresh_routes(self) -> bool:
        if not self.config.get("routes_dir"):
            return True
        try:
            self._publish_routes()
            directory = Path(self.config["routes_dir"])
            return (directory / ROUTES_RELOADED).read_text(encoding="ascii") == \
                hashlib.sha256((directory / ROUTES_FILE).read_bytes()).hexdigest()
        except (AgentError, OSError, subprocess.SubprocessError, ValueError) as error:
            log.warning("Could not refresh tenant routes: %s", error)
            return False

    OPERATIONS = {
        "ping": "ping", "image.status": "image_status", "tenant.list": "tenant_list",
        "tenant.status": "tenant_status", "tenant.start": "tenant_start", "tenant.stop": "tenant_stop",
        "tenant.logs": "tenant_logs", "tenant.task": "tenant_task", "tenant.quota": "tenant_quota",
        "proxy.routes": "proxy_routes",
    }

    def call(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        method = self.OPERATIONS.get(op) if isinstance(op, str) else None
        if method is None:
            raise AgentError("unknown_operation", f"Unknown operation {str(op)[:40]!r}.")
        if args is not None and not isinstance(args, dict):
            raise AgentError("invalid_request", "args must be an object.")
        return getattr(self, method)(args or {})


# Routes ----------------------------------------------------------------------


def render_routes(routes: dict[str, tuple[list[str], str]], *, portal: str | None = None,
                  client_ip: bool = True) -> str:
    """Caddy site blocks, imported by the managed Caddyfile (``bananawiki.ops.caddy``).

    Each wiki answers on HTTPS and plain HTTP (ACME HTTP-01, Cloudflare's
    "Flexible" mode; see :mod:`bananawiki.ops.caddy_blocks`). Caddy retries a
    container that is still starting for a few seconds, then hands the request
    to the *portal* (``127.0.0.1:<port>``), which shows the wiki's status page
    ("starting", refreshing by itself) instead of an empty 502 that Cloudflare
    would replace with its own "Bad gateway" page.
    """
    parts = ["# Managed by the BananaWiki runtime agent; rewritten on every routing change.\n"]
    retry = ("lb_try_duration 5s", "lb_try_interval 250ms")
    for tenant in sorted(routes):
        hosts, upstream = routes[tenant]
        fallback = ""
        if portal:
            fallback = "\thandle_errors {\n" + proxy(portal, streaming=False, client_ip=client_ip, indent="\t\t") + "\t}\n"
        parts.append(
            f"\n# tenant {tenant}\n{addresses(hosts)} {{\n\ttls {{\n\t\ton_demand\n\t}}\n"
            + https_policy() + HSTS
            + proxy(upstream, streaming=True, client_ip=client_ip, extra=retry if portal else ())
            + fallback + "}\n"
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


def _port(*candidates: Any) -> int:
    for value in candidates:
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return 5099


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

    def quota_number(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(environment.get(key, default))
        except (TypeError, ValueError):
            raise AgentError("not_configured", f"{key} must be a finite positive integer.") from None
        if not minimum <= value <= maximum:
            raise AgentError("not_configured", f"{key} must be between {minimum} and {maximum}.")
        return value

    return {
        "service": service, "service_uid": entry.pw_uid, "service_gid": entry.pw_gid,
        "instances_dir": environment.get("INSTANCES_DIR", str(root / "data/instances")),
        "routes_dir": routes_dir(service),
        "quota": {
            "state_dir": f"/var/lib/{service}-quotas",
            "max_bytes": quota_number("HOSTING_AGENT_MAX_STORAGE_BYTES", DEFAULT_STORAGE_BYTES, 512, 2 ** 50),
            "max_inodes": quota_number("HOSTING_AGENT_MAX_INODES", DEFAULT_STORAGE_INODES, 1, 10_000_000),
            "project_start": quota_number("HOSTING_AGENT_PROJECT_ID_START", 1_000_000, 1, 2 ** 31 - 1),
            "storage_reserve_bytes": quota_number("HOSTING_AGENT_STORAGE_RESERVE_BYTES", DEFAULT_STORAGE_RESERVE,
                                                 1, 2 ** 50),
        },
        # Wiki routes fall back to the portal's status pages while a container is unreachable.
        "portal": f"127.0.0.1:{_port(environment.get('HOSTING_PORT'), installation.get('port'))}",
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
        self._requests = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        path.unlink(missing_ok=True)
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o660)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._requests.acquire(blocking=False):
            try:
                # Consume the client's frame before closing: unread Unix
                # socket data can reset the connection and hide the busy reply.
                deadline = time.monotonic() + 1
                received = 0
                while received <= MAX_REQUEST:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    request.settimeout(remaining)
                    chunk = request.recv(min(65536, MAX_REQUEST + 1 - received))
                    if not chunk:
                        break
                    received += len(chunk)
                    if b"\n" in chunk:
                        break
                request.settimeout(1)
                request.sendall(json.dumps({"v": PROTOCOL, "ok": False, "error": {
                    "code": "busy", "message": "The runtime agent is busy; retry shortly."}}).encode() + b"\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._requests.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._requests.release()

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
