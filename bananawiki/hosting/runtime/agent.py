"""The production :class:`~bananawiki.hosting.runtime.Runtime` (``HOSTING_RUNTIME_BACKEND=agent``).

It runs inside the portal and maintenance processes as the unprivileged
service account and splits the work three ways:

* **Docker** (containers, networks, logs, Caddy routes) only through the root
  runtime agent, ``bananawiki.ops.agent_client.connect().call(op, args)``
  (contract: ``bananawiki/ops/RUNTIME_AGENT.md``). The portal never needs
  Docker or root.
* **Tenant databases** only inside the tenant's own sandbox, through the
  agent's ``tenant.task`` operation (:mod:`bananawiki.ops.tenant_task`):
  seeding, migrations, user management, analytics, policy and plugin
  quarantine run as the tenant, never host-side. Decision and reason: the
  database file is written by the tenant and its plugins, so opening it in
  the portal (1.4 did, and migrated it host-side with the portal's
  environment) would let a tenant attack the portal through SQLite; in the
  sandbox a hostile database can only hurt itself. Running tenants get a
  ``docker exec``, stopped ones a one-shot container, so every tool works in
  both states and never races the wiki's own writes outside SQLite's locking.
* **Files** (data directories, archives, backups, snapshots) directly, as the
  service account that owns them, with the no-follow helpers of
  :mod:`.tenantfs`.

Routing: the agent turns the portal's hostname table into Caddy site blocks
that proxy each wiki's hostnames straight to its container (and refreshes
them whenever a container starts or stops), so tenant traffic no longer
passes through the portal (1.4's in-portal proxy had 8 slots for the whole
platform). In port mode the agent publishes each wiki on
``127.0.0.1:<port>``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app

from ...core.env import Env
from ...ops.agent_client import AgentError, connect
from ...ops.runtime_agent import HOSTNAME as AGENT_HOSTNAME
from ...ops.runtime_agent import MAX_HOSTS_PER_TENANT
from ...ops.runtime_agent import TENANT as AGENT_TENANT
from ..config import LEGACY_DATA_DIR, HostingConfig
from . import (
    DomainCheck,
    LogTail,
    RuntimeFailure,
    TenantSpec,
    TenantStatus,
    WikiUser,
    crypto,
    dnscheck,
    gdrive,
    platform_backup,
    tenantfs,
)
from . import archives as tenant_archives
from .plugin_safety import PluginState

log = logging.getLogger("bananawiki.hosting.runtime.agent")

MIB = 1024 * 1024
TASK_TIMEOUT = 140
RECOVER_PARALLEL = 4
HEALTH_TTL = 10.0
NEGATIVE_TTL = 3.0
USAGE_TTL = 5.0
CONTAINERS_TTL = 3.0
LOG_LINES = 1000
LEFTOVER_SECONDS = 24 * 3600
LEGACY_LOG_SECONDS = 7 * 24 * 3600
GDRIVE_RETRY_SECONDS = 3600
LEGACY_LOGS = ("access.log", "error.log", "bananawiki.log")
_ACCESS_LINE = re.compile(r'^\S+ \S+ \S+ \[[^\]]+\] "')
_AGENT_CODES = {
    "unavailable": "unavailable", "protocol": "unavailable", "forbidden": "unavailable",
    "unsupported_protocol": "unavailable", "unknown_operation": "unavailable", "invalid_tenant": "invalid",
    "invalid_env": "invalid", "invalid_request": "invalid", "invalid_owner": "invalid",
    "unknown_tenant": "not_found", "not_configured": "not_configured", "too_large": "too_large", "timeout": "timeout",
}

Probe = Callable[[str, int], bool]


def http_health(address: str, port: int) -> bool:
    """``GET /health`` on the container's bridge address."""
    connection = http.client.HTTPConnection(address, port, timeout=2.0)
    try:
        connection.request("GET", "/health", headers={"Host": "localhost"})
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _started_at(value: str | None) -> float | None:
    """Docker's ``StartedAt`` (``2026-01-01T10:00:00.123456789Z``) as a POSIX time."""
    try:
        return datetime.fromisoformat((value or "")[:19] + "+00:00").timestamp()
    except ValueError:
        return None


def _flag(value: bool) -> str:
    return "1" if value else "0"


def tenant_environment(spec: TenantSpec, cfg: HostingConfig, *, quarantined: bool) -> dict[str, str]:
    """The ``BW_*`` environment of one wiki (the agent adds the paths and managed-mode flags)."""
    policy = spec.policy
    cookie = re.sub(r"[^a-z0-9]+", "_", spec.data_dir_name.lower()).strip("_") or "tenant"
    env = {
        "BW_SOURCE_URL": cfg.source_url,
        "BW_SESSION_COOKIE_NAME": f"bw_session_{cookie}",
        "BW_PREFERRED_URL_SCHEME": "https" if spec.url.startswith("https://") else "http",
        "BW_EASY_WIKI": _flag(policy.easy_wiki),
        "BW_FORBID_PUBLIC_MODE": _flag(policy.forbid_public_mode),
        "BW_FORBID_PAGE_BUILDER": _flag(policy.forbid_page_builder),
        "BW_FORBID_PUBLIC_BUILDER_PAGES": _flag(policy.forbid_public_builder),
        "BW_STORAGE_LIMIT_BYTES": str(max(0, policy.storage_limit_bytes)),
        "BW_MAX_ATTACHMENT_SIZE_BYTES": str(policy.upload_max_bytes),
        "BW_MAX_CONTENT_LENGTH_BYTES": str(max(policy.max_request_bytes, policy.upload_max_bytes)),
        "BW_PLATFORM_UPLOAD_BLACKLIST": ",".join(policy.blocked_extensions),
        "BW_FEDERATION_ENABLED": _flag(policy.federation_enabled),
        "BW_MEMORY_LIMIT_MB": str(policy.memory_mb),
        "BW_NOFILE_LIMIT": str(policy.nofile_limit),
        "BW_INSTANCE_STARTUP_TIMEOUT_SECONDS": str(cfg.limits.startup_timeout_seconds),
        "BW_ALLOW_EXTERNAL_PLUGINS": _flag(not quarantined),
        "BW_MANAGED_PLUGIN_QUARANTINE": _flag(quarantined),
        "BW_PLATFORM_OAUTH_ENABLED": "0",
    }
    if policy.expires_at:
        env["BW_INSTANCE_EXPIRES_AT"] = policy.expires_at
    if policy.tts_disabled:
        env["BW_MANAGED_TTS_DISABLED"] = "1"
    if policy.tts_gpu is not None:
        env.update(BW_TTS_BACKEND="remote-gpu", BW_TTS_REMOTE_GPU_URL=policy.tts_gpu.url,
                   BW_TTS_REMOTE_GPU_AUTH_TOKEN=policy.tts_gpu.token,
                   BW_TTS_REMOTE_GPU_TIMEOUT=str(policy.tts_gpu.timeout))
    if policy.plugin_denylist:
        env["BW_MANAGED_PLUGIN_DENYLIST"] = ",".join(policy.plugin_denylist)
    if policy.oauth is not None:
        urls = policy.oauth.urls()
        env.update({
            "BW_PLATFORM_OAUTH_ENABLED": "1", "BW_PLATFORM_OAUTH_CLIENT_ID": policy.oauth.client_id,
            "BW_PLATFORM_OAUTH_CLIENT_SECRET": policy.oauth.client_secret,
            "BW_PLATFORM_OAUTH_PORTAL_BASE": policy.oauth.portal_base.rstrip("/"),
            "BW_PLATFORM_INSTANCE_ID": policy.oauth.instance_id,
            **{f"BW_PLATFORM_OAUTH_{name.upper()}_URL": url for name, url in urls.items()},
        })
    return env


def _limits(spec: TenantSpec) -> dict[str, Any]:
    try:
        cpus = float(spec.policy.cpu_limit)
    except ValueError:
        cpus = 1.0
    return {"memory_mb": spec.policy.memory_mb, "cpus": cpus, "pids": spec.policy.pids_limit,
            "nofile": spec.policy.nofile_limit}


def _upload_request(spec: TenantSpec) -> dict[str, Any]:
    return {"upload_max_mb": max(1, min(2048, spec.policy.upload_max_bytes // MIB)),
            "blocked_extensions": [item for item in spec.policy.blocked_extensions if item.isalnum()]}


def _token() -> str:
    return secrets.token_hex(8)


def routing_table(specs: Sequence[TenantSpec]) -> list[dict[str, Any]]:
    """The ``proxy.routes`` table: only hostnames the agent accepts, each owned by exactly one wiki.

    The agent refuses the whole table when one entry is invalid, so a single
    odd custom domain, or two wikis ending up with the same hostname (a
    changed URL suffix next to an apex wiki), would stop routing every wiki.
    Such hostnames are left out (a shared one for every claimant: routing it
    to either wiki would be a takeover) and logged instead.
    """
    claims: dict[str, list[str]] = {}
    wanted: dict[str, list[str]] = {}
    for spec in specs:
        if not AGENT_TENANT.fullmatch(spec.data_dir_name):
            continue
        hosts = sorted({host.lower().rstrip(".") for host in spec.hostnames})
        wanted[spec.data_dir_name] = hosts
        for host in hosts:
            claims.setdefault(host, []).append(spec.data_dir_name)
    table = []
    for tenant, hosts in wanted.items():
        usable = [host for host in hosts if AGENT_HOSTNAME.fullmatch(host) and len(claims[host]) == 1]
        dropped = sorted(set(hosts) - set(usable))
        if dropped:
            log.warning("Not routing %s for %s: invalid or claimed by several wikis", ", ".join(dropped), tenant)
        if usable:
            table.append({"tenant": tenant, "hosts": usable[:MAX_HOSTS_PER_TENANT]})
    return table


class AgentRuntime:
    """See the module docstring. *config* defaults to the current portal's configuration."""

    def __init__(self, config: HostingConfig | None = None, *, client: Any = None, probe: Probe | None = None):
        self._config = config
        self._client = client
        self._probe = probe or http_health
        self._lock = threading.Lock()
        self._status: dict[str, tuple[float, TenantStatus]] = {}
        self._usage: dict[str, tuple[float, int]] = {}
        self._containers: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._routes_warned = False

    # ── Plumbing ─────────────────────────────────────────────────────────

    def _cfg(self) -> HostingConfig:
        return self._config or current_app.config["HOSTING"]

    def _call(self, op: str, args: dict[str, Any], failure: str = "failed") -> dict[str, Any]:
        if self._client is None:
            self._client = connect()
        try:
            return self._client.call(op, args)
        except AgentError as error:
            raise RuntimeFailure(_AGENT_CODES.get(error.code, failure), f"{op}: {error.message}") from None

    def _root(self, cfg: HostingConfig, name: str) -> Path:
        return tenantfs.tenant_path(cfg.instances_dir, name)

    def _existing(self, cfg: HostingConfig, name: str) -> Path:
        root = self._root(cfg, name)
        if not tenantfs.is_tenant_dir(root):
            raise RuntimeFailure("not_found", name)
        return root

    def _task(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        """Run one :mod:`bananawiki.ops.tenant_task` action for tenant directory *name*."""
        answer = self._call("tenant.task", {"tenant": name, "request": request, "timeout": TASK_TIMEOUT})
        result = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(result, dict):
            raise RuntimeFailure("failed", "the tenant task gave no answer")
        if not result.get("ok"):
            raise RuntimeFailure(str(result.get("error") or "failed"), str(result.get("detail") or "")[:300])
        return result

    def _state(self, cfg: HostingConfig, spec: TenantSpec) -> PluginState:
        return PluginState(cfg.platform_state_dir, spec.instance_id)

    def _forget(self, name: str) -> None:
        with self._lock:
            self._status.pop(name, None)
            self._usage.pop(name, None)
            self._containers = None

    def _container_map(self, *, fresh: bool = False) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            cached = self._containers
        if cached and not fresh and now - cached[0] < CONTAINERS_TTL:
            return cached[1]
        listing = self._call("tenant.list", {})
        found = {item["tenant"]: item for item in listing.get("tenants", []) if isinstance(item, dict)}
        with self._lock:
            self._containers = (now, found)
        return found

    def _record_path(self, cfg: HostingConfig, spec: TenantSpec) -> Path:
        return self._state(cfg, spec).root / "runtime.json"

    # ── Lifecycle ────────────────────────────────────────────────────────

    def provision(self, spec: TenantSpec, *, admin_username: str, admin_password: str,
                  force_password_change: bool) -> None:
        cfg = self._cfg()
        Path(cfg.instances_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        tenantfs.create(self._root(cfg, spec.data_dir_name))
        self._task(spec.data_dir_name, {
            "action": "seed", "username": admin_username, "password": admin_password,
            "force_password_change": force_password_change, "onboarding": spec.policy.global_tour,
            **_upload_request(spec),
        })
        self._start(cfg, spec)

    def start(self, spec: TenantSpec) -> None:
        self._start(self._cfg(), spec)

    def _start(self, cfg: HostingConfig, spec: TenantSpec) -> None:
        root = self._existing(cfg, spec.data_dir_name)
        tenantfs.clear_stale_state(root)
        tenantfs.ensure_layout(root)
        args: dict[str, Any] = {
            "tenant": spec.data_dir_name, "limits": _limits(spec), "internal_port": cfg.container_internal_port,
            "env": tenant_environment(spec, cfg, quarantined=self._state(cfg, spec).quarantined()),
        }
        if cfg.hosting_mode == "subdomain":
            args["network"] = cfg.tenant_network
        else:
            args["network"] = "outbound"
            if spec.port:
                args["publish_port"] = spec.port
        digest = hashlib.sha256((json.dumps(args, sort_keys=True) + cfg.container_image).encode()).hexdigest()
        record_path = self._record_path(cfg, spec)
        current = self._call("tenant.status", {"tenant": spec.data_dir_name})
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        if (current.get("running") and record.get("digest") == digest
                and record.get("started_at") == current.get("started_at")
                and current.get("image") == cfg.container_image):
            return
        status = self._call("tenant.start", args, "start_failed")
        if not status.get("running"):
            raise RuntimeFailure("start_failed", f"{spec.data_dir_name}: the container exited at once")
        record_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record_path.write_text(json.dumps({"digest": digest, "started_at": status.get("started_at")}),
                               encoding="utf-8")
        self._forget(spec.data_dir_name)

    def stop(self, spec: TenantSpec) -> None:
        self._stop(self._cfg(), spec)

    def _stop(self, cfg: HostingConfig, spec: TenantSpec) -> None:
        self._forget(spec.data_dir_name)
        if not tenantfs.is_tenant_dir(self._root(cfg, spec.data_dir_name)):
            return
        try:
            self._call("tenant.stop", {"tenant": spec.data_dir_name, "timeout": 15}, "stop_failed")
        except RuntimeFailure as error:
            if error.code != "not_found":
                raise
        self._record_path(cfg, spec).unlink(missing_ok=True)

    def restart(self, spec: TenantSpec, *, force: bool = False) -> None:
        cfg = self._cfg()
        self._stop(cfg, spec)
        if force:
            with self._lock:
                self._status.clear()
                self._containers = None
        self._start(cfg, spec)

    def recover(self, specs: Sequence[TenantSpec]) -> int:
        """Start the wikis whose containers are missing or stuck, a few at a time (audit C10)."""
        cfg = self._cfg()
        containers = self._container_map(fresh=True)
        pending = []
        for spec in specs:
            if not tenantfs.is_tenant_dir(self._root(cfg, spec.data_dir_name)):
                log.warning("Not recovering %s: its data directory %s is missing", spec.instance_id,
                            spec.data_dir_name)
                continue
            item = containers.get(spec.data_dir_name)
            if item and item.get("running") and self._judge(cfg, item).state in ("running", "starting"):
                continue
            pending.append(spec)

        def attempt(spec: TenantSpec) -> bool:
            try:
                self._stop(cfg, spec)
                self._start(cfg, spec)
                return True
            except RuntimeFailure as error:
                log.error("Could not recover %s: %s", spec.data_dir_name, error)
                return False

        with ThreadPoolExecutor(max_workers=RECOVER_PARALLEL, thread_name_prefix="recover") as pool:
            return sum(pool.map(attempt, pending))

    def relocate(self, instance_id: str, old_dir_name: str, new_dir_name: str) -> None:
        cfg = self._cfg()
        tenantfs.relocate(cfg.instances_dir, old_dir_name, new_dir_name)
        self._forget(old_dir_name)
        self._forget(new_dir_name)

    def destroy(self, spec: TenantSpec) -> None:
        cfg = self._cfg()
        self._stop(cfg, spec)
        tenantfs.remove_tree(self._root(cfg, spec.data_dir_name))
        tenantfs.remove_tree(self._state(cfg, spec).root)

    # ── Observation ──────────────────────────────────────────────────────

    def _judge(self, cfg: HostingConfig, item: dict[str, Any]) -> TenantStatus:
        address, port = item.get("address"), int(item.get("internal_port") or cfg.container_internal_port)
        if address and self._probe(str(address), port):
            return TenantStatus("running")
        started = _started_at(item.get("started_at"))
        if started is not None and time.time() - started < cfg.limits.startup_timeout_seconds:
            return TenantStatus("starting")
        return TenantStatus("unhealthy", "the wiki does not answer /health")

    def status(self, spec: TenantSpec) -> TenantStatus:
        cfg = self._cfg()
        name = spec.data_dir_name
        now = time.monotonic()
        with self._lock:
            cached = self._status.get(name)
        if cached and cached[0] > now:
            return cached[1]
        if not tenantfs.is_tenant_dir(self._root(cfg, name)):
            result = TenantStatus("missing")
        else:
            try:
                item = self._container_map().get(name)
            except RuntimeFailure as error:
                return TenantStatus("unknown", error.code)
            result = self._judge(cfg, item) if item and item.get("running") else TenantStatus("stopped")
        with self._lock:
            self._status[name] = (now + (HEALTH_TTL if result.healthy else NEGATIVE_TTL), result)
        return result

    def usage(self, spec: TenantSpec) -> int:
        cfg = self._cfg()
        name = spec.data_dir_name
        now = time.monotonic()
        with self._lock:
            cached = self._usage.get(name)
        if cached and cached[0] > now:
            return cached[1]
        root = self._root(cfg, name)
        total = tenantfs.usage(root) if tenantfs.is_tenant_dir(root) else 0
        with self._lock:
            self._usage[name] = (now + USAGE_TTL, total)
        return total

    def logs(self, spec: TenantSpec, name: str, max_bytes: int = 256 * 1024) -> LogTail:
        """Container output (Docker rotates it): ``access.log`` shows request lines, ``error.log`` the rest."""
        if name not in ("error.log", "access.log"):
            raise RuntimeFailure("invalid", "unknown log")
        cfg = self._cfg()
        self._existing(cfg, spec.data_dir_name)
        lines = self._call("tenant.logs", {"tenant": spec.data_dir_name, "lines": LOG_LINES}).get("lines", [])
        wanted = [str(line) for line in lines if bool(_ACCESS_LINE.match(str(line))) == (name == "access.log")]
        data = ("\n".join(wanted) + ("\n" if wanted else "")).encode("utf-8", "replace")
        tail = data[-max(0, max_bytes):]
        return LogTail(tail.decode("utf-8", "replace"), len(data), len(tail) < len(data))

    def analytics(self, spec: TenantSpec, days: int) -> dict[str, Any]:
        self._existing(self._cfg(), spec.data_dir_name)
        result = self._task(spec.data_dir_name, {"action": "analytics", "days": max(1, min(365, int(days)))})
        return {key: result.get(key) for key in ("window_days", "totals", "daily")}

    # ── Tenant database operations ───────────────────────────────────────

    def list_users(self, spec: TenantSpec, *, limit: int = 200, offset: int = 0) -> tuple[list[WikiUser], int]:
        self._existing(self._cfg(), spec.data_dir_name)
        result = self._task(spec.data_dir_name, {"action": "list_users", "limit": max(1, min(1000, limit)),
                                                 "offset": max(0, offset)})
        users = [WikiUser(str(row.get("username", ""))[:200], str(row.get("role", ""))[:200],
                          str(row.get("created_at", ""))[:200])
                 for row in result.get("users", []) if isinstance(row, dict)]
        total = result.get("total")
        return users, total if type(total) is int else len(users)

    def set_user_password(self, spec: TenantSpec, username: str, password: str, role: str) -> None:
        self._existing(self._cfg(), spec.data_dir_name)
        self._task(spec.data_dir_name, {"action": "set_password", "username": username, "password": password,
                                        "role": role})

    def remove_user(self, spec: TenantSpec, username: str) -> None:
        self._existing(self._cfg(), spec.data_dir_name)
        self._task(spec.data_dir_name, {"action": "remove_user", "username": username})

    def reset_admin_password(self, spec: TenantSpec, admin_username: str, new_password: str) -> str:
        self._existing(self._cfg(), spec.data_dir_name)
        result = self._task(spec.data_dir_name, {"action": "reset_admin", "username": admin_username,
                                                 "password": new_password})
        return str(result.get("username") or admin_username)[:200]

    def reset_content(self, spec: TenantSpec, *, admin_username: str, admin_password: str) -> None:
        cfg = self._cfg()
        root = self._existing(cfg, spec.data_dir_name)
        tenantfs.reset_content(root)
        self._task(spec.data_dir_name, {
            "action": "seed", "username": admin_username, "password": admin_password, "force_password_change": True,
            "onboarding": spec.policy.global_tour, **_upload_request(spec),
        })

    def apply_limits(self, spec: TenantSpec) -> None:
        self._existing(self._cfg(), spec.data_dir_name)
        self._task(spec.data_dir_name, {"action": "apply_policy", **_upload_request(spec)})

    # ── Copies and archives ──────────────────────────────────────────────

    def _snapshot(self, name: str, *, portable: bool = False) -> str:
        """A fresh database copy inside the tenant directory; returns its relative path."""
        file_name = f"snap-{_token()}.db"
        self._task(name, {"action": "snapshot", "name": file_name, "portable": portable})
        return f"{tenantfs.EXCHANGE}/{file_name}"

    def export_archive(self, spec: TenantSpec, destination_dir: Path) -> Path:
        cfg = self._cfg()
        root = self._existing(cfg, spec.data_dir_name)
        copy = self._snapshot(spec.data_dir_name, portable=True)
        try:
            return tenant_archives.export(root, copy, Path(destination_dir), spec.slug.split("--terminated-")[0],
                                   cfg.archives)
        finally:
            tenantfs.unlink(root, copy)

    def import_archive(self, spec: TenantSpec, archive: Path) -> None:
        cfg = self._cfg()
        root = self._root(cfg, spec.data_dir_name)
        if os.path.lexists(root):
            raise RuntimeFailure("data_exists", spec.data_dir_name)
        base = Path(cfg.instances_dir)
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=base))
        try:
            tenant_archives.unpack(Path(archive), staging, cfg.archives)
            tenantfs.ensure_layout(staging)
            os.rename(staging, root)
        except BaseException:
            tenantfs.remove_tree(staging)
            raise
        self._task(spec.data_dir_name, {"action": "migrate", "imported": True, "policy": _upload_request(spec)})
        self._start(cfg, spec)

    def duplicate(self, source: TenantSpec, target: TenantSpec) -> None:
        cfg = self._cfg()
        source_root = self._existing(cfg, source.data_dir_name)
        target_root = self._root(cfg, target.data_dir_name)
        if os.path.lexists(target_root):
            raise RuntimeFailure("data_exists", target.data_dir_name)
        copy = self._snapshot(source.data_dir_name)
        try:
            tenantfs.create(target_root)
            tenantfs.copy_out(source_root, copy, target_root / "bananawiki.db")
        finally:
            tenantfs.unlink(source_root, copy)
        for folder in tenantfs.ASSET_FOLDERS:
            for origin in (f"storage/{folder}", folder):
                tenantfs.copy_tree(source_root, origin, target_root / "storage" / folder)
        for folder in ("favicons", "translations"):
            tenantfs.copy_tree(source_root, folder, target_root / folder)
        self._task(target.data_dir_name, {"action": "migrate", "revoke_credentials": True,
                                          "policy": _upload_request(target)})
        self._start(cfg, target)

    # ── Plugin safety ────────────────────────────────────────────────────

    def plugins_quarantined(self, spec: TenantSpec) -> bool:
        try:
            return self._state(self._cfg(), spec).quarantined()
        except RuntimeFailure:
            return False

    def _running(self, spec: TenantSpec) -> bool:
        item = self._container_map(fresh=True).get(spec.data_dir_name)
        return bool(item and item.get("running"))

    def _capture(self, cfg: HostingConfig, spec: TenantSpec, label: str) -> str:
        root = self._existing(cfg, spec.data_dir_name)
        state = self._state(cfg, spec)
        copy = self._snapshot(spec.data_dir_name)
        state.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        incoming = state.root / f".incoming-{_token()}.db"
        try:
            tenantfs.copy_out(root, copy, incoming)
            return state.add(incoming, label)
        finally:
            tenantfs.unlink(root, copy)
            incoming.unlink(missing_ok=True)

    def quarantine_plugins(self, spec: TenantSpec) -> str:
        """Kill switch: the marker first (it is what keeps plugin code off), then the database rows."""
        cfg = self._cfg()
        self._existing(cfg, spec.data_dir_name)
        self._state(cfg, spec).set_quarantine(True)
        was_running = self._running(spec)
        self._stop(cfg, spec)
        try:
            self._capture(cfg, spec, "operator-quarantine")
        except RuntimeFailure as error:
            log.warning("No forensic snapshot for %s: %s", spec.data_dir_name, error)
        disabled = int(self._task(spec.data_dir_name, {"action": "quarantine"}).get("disabled") or 0)
        if was_running:
            self._start(cfg, spec)
        return f"External plugins quarantined; {disabled} plugin(s) disabled; a copy of the database was kept."

    def lift_plugin_quarantine(self, spec: TenantSpec) -> str:
        cfg = self._cfg()
        self._state(cfg, spec).set_quarantine(False)
        if self._running(spec):
            self._stop(cfg, spec)
            self._start(cfg, spec)
        return "Plugin quarantine lifted; plugins the quarantine disabled stay off until an administrator enables them."

    def list_plugin_snapshots(self, spec: TenantSpec) -> list[dict[str, Any]]:
        try:
            return self._state(self._cfg(), spec).listing()
        except RuntimeFailure:
            return []

    def capture_plugin_snapshot(self, spec: TenantSpec) -> str:
        return self._capture(self._cfg(), spec, "pre-enable")

    def restore_plugin_snapshot(self, spec: TenantSpec, name: str | None = None) -> str:
        """Roll back to a platform-held ``pre-enable`` copy; plugins stay quarantined afterwards."""
        cfg = self._cfg()
        root = self._existing(cfg, spec.data_dir_name)
        state = self._state(cfg, spec)
        chosen, path = state.restorable(name)
        was_running = self._running(spec)
        self._stop(cfg, spec)
        self._capture(cfg, spec, "before-restore")
        incoming = f"restore-{_token()}.db"
        tenantfs.ensure_exchange(root)
        tenantfs.write_into(root, tenantfs.EXCHANGE, incoming, path)
        try:
            self._task(spec.data_dir_name, {"action": "restore_db", "name": incoming})
        finally:
            tenantfs.unlink(root, f"{tenantfs.EXCHANGE}/{incoming}")
        state.set_quarantine(True)
        if was_running:
            self._start(cfg, spec)
        return f"Snapshot {chosen} restored; external plugins stay quarantined until the quarantine is lifted."

    # ── Routing and custom domains ───────────────────────────────────────

    def check_domain(self, domain: str, token: str) -> DomainCheck:
        cfg = self._cfg()
        return dnscheck.check(domain, token, cfg.custom_domain_target, cfg.custom_domain_ips)

    def sync_routes(self, specs: Sequence[TenantSpec]) -> None:
        if self._cfg().hosting_mode != "subdomain":
            return
        routes = routing_table(specs)
        try:
            self._call("proxy.routes", {"routes": routes})
        except RuntimeFailure as error:
            if error.code != "not_configured":
                raise
            if not self._routes_warned:
                log.warning("The runtime agent cannot publish tenant routes (%s); run 'bananawiki update' so the "
                            "agent and the Caddy configuration are installed.", error.detail)
                self._routes_warned = True

    # ── Platform backups ─────────────────────────────────────────────────

    def backup_key(self) -> bytes:
        return crypto.load_key(os.environ.get("HOSTING_BACKUP_ENCRYPTION_KEY", ""), self._cfg().backup_key_path)

    def _backup_copy(self, tenant: str) -> str | None:
        try:
            return self._snapshot(tenant)
        except RuntimeFailure as error:
            if error.code in ("db_missing", "db_unsafe"):
                log.warning("Backing up %s without its database: %s", tenant, error)
                return None
            raise

    def export_platform(self, destination_dir: Path) -> Path:
        cfg = self._cfg()
        key = self.backup_key()
        return platform_backup.export(cfg, Path(destination_dir), key, self._backup_copy,
                               lambda tenant, copy: tenantfs.unlink(self._root(cfg, tenant), copy))

    def restore_platform(self, archives: Sequence[Path]) -> None:
        cfg = self._cfg()
        try:
            running = [name for name, item in self._container_map(fresh=True).items() if item.get("running")]
        except RuntimeFailure:
            running = []
        for name in running:
            try:
                self._call("tenant.stop", {"tenant": name, "timeout": 15}, "stop_failed")
            except RuntimeFailure as error:
                log.warning("Could not stop %s before the restore: %s", name, error)
        secret_path = Env().path("HOSTING_SECRET_KEY_PATH", str(LEGACY_DATA_DIR / ".secret_key"))
        platform_backup.restore(cfg, [Path(item) for item in archives], self.backup_key, secret_path)

    # ── Google Drive ─────────────────────────────────────────────────────

    def _drive_settings(self) -> gdrive.DriveSettings:
        from .. import settings

        return gdrive.DriveSettings.from_row(settings.load())

    def _drive_state_path(self, cfg: HostingConfig) -> Path:
        return Path(cfg.platform_state_dir) / "gdrive.json"

    def _drive_state(self, cfg: HostingConfig) -> dict[str, str]:
        try:
            data = json.loads(self._drive_state_path(cfg).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_drive_state(self, cfg: HostingConfig, **values: str) -> None:
        path = self._drive_state_path(cfg)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({**self._drive_state(cfg), **values}), encoding="utf-8")

    def gdrive_test(self) -> str:
        settings = self._drive_settings()
        return f"Folder: {gdrive.test(gdrive.service(settings), settings)}"

    def gdrive_backup_now(self) -> str:
        cfg = self._cfg()
        settings = self._drive_settings()
        client = gdrive.service(settings)
        self._save_drive_state(cfg, last_attempt=datetime.now().isoformat())
        root = Path(cfg.archives.export_temp_dir)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="bwh-gdrive-", dir=root))
        try:
            name = gdrive.upload(client, settings, self.export_platform(work))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._save_drive_state(cfg, last_success=datetime.now().isoformat())
        removed = gdrive.prune(client, settings)
        return f"{name} uploaded; {removed} old backup(s) removed."

    def store_gdrive_credentials(self, content: bytes) -> str:
        path = Path(self._cfg().database_path).parent / ".gdrive_credentials.json"
        return str(gdrive.store_credentials(content, path))

    def _scheduled_drive_backup(self, cfg: HostingConfig) -> None:
        settings = self._drive_settings()
        if not settings.enabled:
            return
        state = self._drive_state(cfg)
        now = datetime.now()

        def moment(key: str) -> datetime | None:
            try:
                return datetime.fromisoformat(state[key])
            except (KeyError, ValueError):
                return None

        attempt = moment("last_attempt")
        if not gdrive.due(settings, moment("last_success"), now) or (
                attempt and (now - attempt).total_seconds() < GDRIVE_RETRY_SECONDS):
            return
        try:
            log.info("Scheduled Google Drive backup: %s", self.gdrive_backup_now())
        except RuntimeFailure as error:
            log.error("Scheduled Google Drive backup failed: %s", error)

    # ── Maintenance ──────────────────────────────────────────────────────

    def maintenance_tick(self) -> None:
        """Periodic runtime work: leftovers, legacy 1.4 logs, scheduled Drive backups."""
        cfg = self._cfg()
        self._prune_leftovers(cfg)
        self._scheduled_drive_backup(cfg)

    def _prune_leftovers(self, cfg: HostingConfig) -> None:
        base = Path(cfg.instances_dir)
        if not base.is_dir():
            return
        now = time.time()
        for entry in base.iterdir():
            try:
                if entry.name.startswith(".import-") and now - entry.lstat().st_mtime > LEFTOVER_SECONDS:
                    tenantfs.remove_tree(entry)
                elif tenantfs.DATA_DIR_NAME.fullmatch(entry.name) and tenantfs.is_tenant_dir(entry):
                    self._prune_tenant(entry, now)
            except OSError as error:
                log.warning("Could not tidy %s: %s", entry.name, error)

    @staticmethod
    def _prune_tenant(root: Path, now: float) -> None:
        """1.4 logs nothing writes any more (1.6 logs to Docker) and stale exchange copies."""
        for name in LEGACY_LOGS:
            path = root / name
            if path.is_file() and not path.is_symlink() and now - path.lstat().st_mtime > LEGACY_LOG_SECONDS:
                tenantfs.unlink(root, name)
        exchange = root / tenantfs.EXCHANGE
        if exchange.is_dir() and not exchange.is_symlink():
            for item in exchange.iterdir():
                if now - item.lstat().st_mtime > LEFTOVER_SECONDS:
                    tenantfs.unlink(root, f"{tenantfs.EXCHANGE}/{item.name}")


def create_runtime(config: HostingConfig | None = None) -> AgentRuntime:
    return AgentRuntime(config)
