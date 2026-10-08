"""A stand-in for the root runtime agent, and helpers for the production runtime tests.

:class:`FakeAgent` keeps containers in memory, but runs ``tenant.task``
requests for real: :mod:`bananawiki.ops.tenant_task` is executed in-process
against the tenant directory, with the environment the tenant container
would have, so seeding, migrations and user management are exercised on
genuine SQLite databases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from bananawiki.hosting.config import load_config
from bananawiki.hosting.runtime import TenantPolicy, TenantSpec
from bananawiki.hosting.runtime.agent import AgentRuntime
from bananawiki.ops import tenant_task
from bananawiki.ops.runtime_agent import AgentError

from .hosting_support import portal_environ
from .quota_fakes import FakeProjectQuota

WIKI_PASSWORD = "wiki-password-1"
IMAGE = "bananawiki-tenant:test"


class FakeAgent:
    def __init__(self, instances_dir: Path):
        self.instances_dir = Path(instances_dir)
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, AgentError] = {}
        self.routes: list[dict[str, Any]] | None = None
        self.log_lines: list[str] = []
        self.started = 0
        self.quota = FakeProjectQuota(self.instances_dir)

    def call(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        self.calls.append((op, args))
        if op in self.failures:
            raise self.failures.pop(op)
        return getattr(self, op.replace(".", "_"))(args)

    def ops(self, name: str) -> list[dict[str, Any]]:
        return [args for op, args in self.calls if op == name]

    def tasks(self) -> list[str]:
        return [args["request"]["action"] for args in self.ops("tenant.task")]

    def _dir(self, tenant: str) -> Path:
        path = self.instances_dir / tenant
        if path.is_symlink() or not path.is_dir():
            raise AgentError("unknown_tenant", tenant)
        return path

    def _status(self, tenant: str) -> dict[str, Any]:
        item = self.containers.get(tenant)
        if item is None:
            return {"tenant": tenant, "running": False, "exists": False}
        return {key: item.get(key) for key in ("tenant", "running", "address", "internal_port", "image", "started_at",
                                             "quota_protected", "ipv6_disabled", "storage_quota_verified", "storage_quota")}

    def ping(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"protocol": 1, "docker": True, "image": IMAGE}

    def tenant_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"tenants": [self._status(name) for name in self.containers]}

    def tenant_status(self, args: dict[str, Any]) -> dict[str, Any]:
        self._dir(args["tenant"])
        return self._status(args["tenant"])

    def tenant_quota(self, args: dict[str, Any]) -> dict[str, Any]:
        self._dir(args["tenant"])
        limits = args.get("limits", {})
        if args.get("prepare"):
            witness = self.quota.prepare(args["tenant"], byte_limit=limits.get("storage_bytes", 0) or 10 * 1024 ** 3,
                                         inode_limit=100_000, repair=True)
        else:
            witness = self.quota.verify(args["tenant"])
        return {"tenant": args["tenant"], "storage_quota_verified": True, "storage_quota": witness.to_dict()}

    def tenant_start(self, args: dict[str, Any]) -> dict[str, Any]:
        self._dir(args["tenant"])
        self.started += 1
        self.containers[args["tenant"]] = {
            "tenant": args["tenant"], "running": True, "address": f"172.30.0.{self.started}",
            "internal_port": args.get("internal_port", 5001), "image": IMAGE,
            "started_at": f"2026-01-01T00:00:{self.started:02d}.000000000Z", "args": args,
            "quota_protected": True, "ipv6_disabled": True, "storage_quota_verified": True,
            "storage_quota": self.quota.verify(args["tenant"]).to_dict(),
        }
        return self._status(args["tenant"])

    def tenant_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        self._dir(args["tenant"])
        self.containers.pop(args["tenant"], None)
        return {"tenant": args["tenant"], "stopped": True}

    def tenant_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"tenant": args["tenant"], "lines": self.log_lines[-args.get("lines", 200):]}

    def tenant_task(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self._dir(args["tenant"])
        environment = {
            "BW_ENV": "test", "BW_INSTANCE_DIR": str(root), "BW_DATABASE_PATH": str(root / "bananawiki.db"),
            "BW_EXTERNAL_PLUGINS_DIR": str(root / "external_plugins"), "BW_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000",
        }
        with mock.patch.dict(os.environ, environment):
            result = tenant_task.run(json.dumps(args["request"]))
        return {"tenant": args["tenant"], "result": result}

    def proxy_routes(self, args: dict[str, Any]) -> dict[str, Any]:
        self.routes = args["routes"]
        return {"routes": len(self.routes), "changed": True, "reloaded": True}


def runtime_config(tmp_path: Path, **extra: str):
    environ = portal_environ(tmp_path, HOSTING_CONTAINER_IMAGE=IMAGE, **extra)
    return load_config(environ, secret_key="runtime-test-secret-" + "k" * 32)


def make_runtime(tmp_path: Path, *, healthy: bool = True, **extra: str) -> tuple[AgentRuntime, FakeAgent]:
    cfg = runtime_config(tmp_path, **extra)
    agent = FakeAgent(Path(cfg.instances_dir))
    health = {"ok": healthy}
    runtime = AgentRuntime(cfg, client=agent, probe=lambda _address, _port: health["ok"])
    runtime.health = health  # type: ignore[attr-defined]
    return runtime, agent


def make_spec(name: str = "acme", *, port: int | None = 6001, policy: TenantPolicy | None = None,
              hostnames: tuple[str, ...] | None = None, instance_id: str | None = None) -> TenantSpec:
    return TenantSpec(
        instance_id=instance_id or f"id{name.replace('-', '')}"[:16], slug=name, domain_mode="hosting",
        data_dir_name=name, port=port, url=f"https://{name}-hosting.wiki.test",
        hostnames=(f"{name}-hosting.wiki.test",) if hostnames is None else hostnames,
        policy=policy or TenantPolicy(),
    )


def provision(runtime: AgentRuntime, spec: TenantSpec, username: str = "owner1") -> None:
    runtime.provision(spec, admin_username=username, admin_password=WIKI_PASSWORD, force_password_change=True)
