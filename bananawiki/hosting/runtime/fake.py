"""In-memory :class:`~bananawiki.hosting.runtime.Runtime` for tests and demos.

It keeps a dictionary of tenants keyed by data directory name, records every
call in :attr:`FakeRuntime.calls` and can be told to fail an operation with
:meth:`FakeRuntime.fail_next`. Nothing touches the filesystem except
``export_archive`` and ``export_platform``, which write small files into the
directory they are given.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import DomainCheck, LogTail, RuntimeFailure, TenantSpec, TenantStatus, WikiUser


@dataclass
class FakeTenant:
    spec: TenantSpec
    running: bool = False
    usage: int = 0
    users: dict[str, WikiUser] = field(default_factory=dict)
    passwords: dict[str, str] = field(default_factory=dict)
    quarantined: bool = False
    snapshots: list[str] = field(default_factory=list)


class FakeRuntime:
    def __init__(self) -> None:
        self.tenants: dict[str, FakeTenant] = {}
        self.calls: list[tuple[str, str]] = []
        self.routes: list[TenantSpec] = []
        self.domain_results: dict[str, DomainCheck] = {}
        self._failures: dict[str, RuntimeFailure] = {}

    # Test helpers ---------------------------------------------------------

    def fail_next(self, method: str, code: str = "failed") -> None:
        self._failures[method] = RuntimeFailure(code, "simulated")

    def _record(self, method: str, target: str = "") -> None:
        self.calls.append((method, target))
        failure = self._failures.pop(method, None)
        if failure is not None:
            raise failure

    def _tenant(self, spec: TenantSpec) -> FakeTenant:
        tenant = self.tenants.get(spec.data_dir_name)
        if tenant is None:
            raise RuntimeFailure("not_found", spec.data_dir_name)
        tenant.spec = spec
        return tenant

    def called(self, method: str) -> list[str]:
        return [target for name, target in self.calls if name == method]

    # Lifecycle ------------------------------------------------------------

    def provision(self, spec: TenantSpec, *, admin_username: str, admin_password: str,
                  force_password_change: bool) -> None:
        self._record("provision", spec.data_dir_name)
        if spec.data_dir_name in self.tenants:
            raise RuntimeFailure("data_exists", spec.data_dir_name)
        tenant = FakeTenant(spec, running=True)
        tenant.users[admin_username] = WikiUser(admin_username, "owner", "2026-01-01 00:00:00")
        tenant.passwords[admin_username] = admin_password
        self.tenants[spec.data_dir_name] = tenant

    def start(self, spec: TenantSpec) -> None:
        self._record("start", spec.data_dir_name)
        self._tenant(spec).running = True

    def stop(self, spec: TenantSpec) -> None:
        self._record("stop", spec.data_dir_name)
        tenant = self.tenants.get(spec.data_dir_name)
        if tenant:
            tenant.running = False

    def restart(self, spec: TenantSpec, *, force: bool = False) -> None:
        self._record("restart", spec.data_dir_name)
        self._tenant(spec).running = True

    def recover(self, specs: Sequence[TenantSpec]) -> int:
        self._record("recover")
        started = 0
        for spec in specs:
            tenant = self.tenants.get(spec.data_dir_name)
            if tenant and not tenant.running:
                tenant.running = True
                started += 1
        return started

    def relocate(self, instance_id: str, old_dir_name: str, new_dir_name: str) -> None:
        self._record("relocate", f"{old_dir_name}->{new_dir_name}")
        if old_dir_name == new_dir_name:
            return
        if new_dir_name in self.tenants:
            raise RuntimeFailure("data_exists", new_dir_name)
        tenant = self.tenants.pop(old_dir_name, None)
        if tenant is None:
            raise RuntimeFailure("not_found", old_dir_name)
        self.tenants[new_dir_name] = tenant

    def destroy(self, spec: TenantSpec) -> None:
        self._record("destroy", spec.data_dir_name)
        self.tenants.pop(spec.data_dir_name, None)

    # Observation ----------------------------------------------------------

    def status(self, spec: TenantSpec) -> TenantStatus:
        tenant = self.tenants.get(spec.data_dir_name)
        if tenant is None:
            return TenantStatus("missing")
        return TenantStatus("running" if tenant.running else "stopped")

    def usage(self, spec: TenantSpec) -> int:
        tenant = self.tenants.get(spec.data_dir_name)
        return tenant.usage if tenant else 0

    def logs(self, spec: TenantSpec, name: str, max_bytes: int = 256 * 1024) -> LogTail:
        self._record("logs", spec.data_dir_name)
        if name not in ("error.log", "access.log"):
            raise RuntimeFailure("invalid", name)
        return LogTail(f"{name} of {spec.slug}\n", 20, False)

    def analytics(self, spec: TenantSpec, days: int) -> dict[str, Any]:
        self._record("analytics", spec.data_dir_name)
        self._tenant(spec)
        daily = [{"day": "2026-01-01", "request": 10, "page_view": 7, "error": 1}]
        return {"window_days": days, "totals": {"request": 10, "page_view": 7, "error": 1}, "daily": daily}

    # Tenant database ------------------------------------------------------

    def list_users(self, spec: TenantSpec, *, limit: int = 200, offset: int = 0) -> tuple[list[WikiUser], int]:
        tenant = self._tenant(spec)
        users = list(tenant.users.values())
        return users[offset:offset + limit], len(users)

    def set_user_password(self, spec: TenantSpec, username: str, password: str, role: str) -> None:
        self._record("set_user_password", username)
        tenant = self._tenant(spec)
        tenant.users[username] = WikiUser(username, role, "2026-01-01 00:00:00")
        tenant.passwords[username] = password

    def remove_user(self, spec: TenantSpec, username: str) -> None:
        self._record("remove_user", username)
        tenant = self._tenant(spec)
        user = tenant.users.get(username)
        if user is None:
            raise RuntimeFailure("user_not_found", username)
        if user.role == "owner":
            raise RuntimeFailure("protected_user", username)
        del tenant.users[username]

    def reset_admin_password(self, spec: TenantSpec, admin_username: str, new_password: str) -> str:
        self._record("reset_admin_password", admin_username)
        tenant = self._tenant(spec)
        tenant.passwords[admin_username] = new_password
        return admin_username

    def reset_content(self, spec: TenantSpec, *, admin_username: str, admin_password: str) -> None:
        self._record("reset_content", spec.data_dir_name)
        tenant = self._tenant(spec)
        tenant.users = {admin_username: WikiUser(admin_username, "owner", "2026-01-01 00:00:00")}
        tenant.passwords = {admin_username: admin_password}

    def apply_limits(self, spec: TenantSpec) -> None:
        self._record("apply_limits", spec.data_dir_name)
        self._tenant(spec)

    # Archives -------------------------------------------------------------

    def export_archive(self, spec: TenantSpec, destination_dir: Path) -> Path:
        self._record("export_archive", spec.data_dir_name)
        self._tenant(spec)
        path = Path(destination_dir) / f"{spec.slug}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("bananawiki.json", '{"format": "bananawiki-site"}')
        return path

    def import_archive(self, spec: TenantSpec, archive: Path) -> None:
        self._record("import_archive", spec.data_dir_name)
        if not zipfile.is_zipfile(archive):
            raise RuntimeFailure("archive_invalid", str(archive))
        self.tenants[spec.data_dir_name] = FakeTenant(spec, running=True)

    def duplicate(self, source: TenantSpec, target: TenantSpec) -> None:
        self._record("duplicate", f"{source.data_dir_name}->{target.data_dir_name}")
        original = self._tenant(source)
        if target.data_dir_name in self.tenants:
            raise RuntimeFailure("data_exists", target.data_dir_name)
        self.tenants[target.data_dir_name] = FakeTenant(
            target, running=True, users=dict(original.users), passwords=dict(original.passwords)
        )

    # Plugin safety ----------------------------------------------------------

    def plugins_quarantined(self, spec: TenantSpec) -> bool:
        tenant = self.tenants.get(spec.data_dir_name)
        return bool(tenant and tenant.quarantined)

    def quarantine_plugins(self, spec: TenantSpec) -> str:
        self._record("quarantine_plugins", spec.data_dir_name)
        self._tenant(spec).quarantined = True
        return "quarantined"

    def lift_plugin_quarantine(self, spec: TenantSpec) -> str:
        self._record("lift_plugin_quarantine", spec.data_dir_name)
        self._tenant(spec).quarantined = False
        return "lifted"

    def list_plugin_snapshots(self, spec: TenantSpec) -> list[dict[str, Any]]:
        tenant = self.tenants.get(spec.data_dir_name)
        names = list(reversed(tenant.snapshots)) if tenant else []
        return [{"name": n, "label": "manual", "created_at": "2026-01-01 00:00:00", "size_bytes": 1} for n in names]

    def capture_plugin_snapshot(self, spec: TenantSpec) -> str:
        self._record("capture_plugin_snapshot", spec.data_dir_name)
        tenant = self._tenant(spec)
        name = f"snapshot-{len(tenant.snapshots) + 1}"
        tenant.snapshots.append(name)
        return name

    def restore_plugin_snapshot(self, spec: TenantSpec, name: str | None = None) -> str:
        self._record("restore_plugin_snapshot", spec.data_dir_name)
        tenant = self._tenant(spec)
        if not tenant.snapshots or (name and name not in tenant.snapshots):
            raise RuntimeFailure("not_found", name or "")
        return name or tenant.snapshots[-1]

    # Routing ----------------------------------------------------------------

    def check_domain(self, domain: str, token: str) -> DomainCheck:
        self._record("check_domain", domain)
        return self.domain_results.get(domain, DomainCheck(ownership=False, routing=False, dns_error=True))

    def sync_routes(self, specs: Sequence[TenantSpec]) -> None:
        self.calls.append(("sync_routes", str(len(specs))))
        self.routes = list(specs)

    # Platform backups -----------------------------------------------------------

    def export_platform(self, destination_dir: Path) -> Path:
        self._record("export_platform")
        path = Path(destination_dir) / "bananawiki_hosting_backup.zip.bwenc"
        path.write_bytes(b"encrypted")
        return path

    def restore_platform(self, archives: Sequence[Path]) -> None:
        self._record("restore_platform", str(len(archives)))

    def backup_key(self) -> bytes:
        return b"k" * 32

    def gdrive_test(self) -> str:
        self._record("gdrive_test")
        return "ok"

    def gdrive_backup_now(self) -> str:
        self._record("gdrive_backup_now")
        return "uploaded"

    def store_gdrive_credentials(self, content: bytes) -> str:
        self._record("store_gdrive_credentials")
        return "/nonexistent/fake-credentials.json"
