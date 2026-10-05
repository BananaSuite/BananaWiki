"""Hosted wikis: the ``instances`` rows and every lifecycle decision.

This module owns the database side of a wiki's life (status, expiry,
suspension windows, grace periods, names, quotas, entitlements) and calls the
:class:`~bananawiki.hosting.runtime.Runtime` for everything that touches the
tenant itself. Refusals raise :class:`~bananawiki.hosting.errors.ServiceError`
with a translation key; runtime failures become ``hosting.runtime.<code>``.

Statuses are those of 1.4: ``running``, ``stopped`` (paused by the owner),
``suspended`` (by an administrator; the expiry clock is frozen while
``suspended_at`` is set and the owner cannot act) and ``terminated`` (row
kept, name archived as ``<slug>--terminated-<id8>``; data retained until
``data_retained_until`` when a grace period is configured).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import socket
import sqlite3
import string
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from flask import current_app

from ..core.timeutil import is_past, now_sql, parse, sql_in, to_sql, utcnow
from . import accounts, events, oauth, settings, urls
from .config import HostingConfig
from .db import db
from .errors import ServiceError
from .runtime import Runtime, RuntimeFailure, TenantPolicy, TenantSpec, TtsGpu

log = logging.getLogger("bananawiki.hosting.instances")

STATUSES = ("running", "stopped", "suspended", "terminated")
FEATURES = {"public_access": "public_wiki_allowed", "page_builder": "page_builder_allowed"}
ADMIN_EXPIRY_DAYS = 365 * 100
MAX_EXTENSION_SECONDS = 10 * 365 * 86400
LOG_NAMES = ("error.log", "access.log")
_ID_ALPHABET = string.ascii_lowercase + string.digits
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def cfg() -> HostingConfig:
    return current_app.config["HOSTING"]


def runtime() -> Runtime:
    return current_app.extensions["bananawiki.hosting.runtime"]


def get(instance_id: str | None) -> dict[str, Any] | None:
    if not instance_id or len(instance_id) > 64:
        return None
    return db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))


def by_slug(slug: str, domain_mode: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM instances WHERE subdomain = ? COLLATE NOCASE AND domain_mode = ? "
                  "AND status != 'terminated'", (slug, domain_mode))


def name_holder(slug: str, domain_mode: str) -> dict[str, Any] | None:
    """The live wiki already using this name: the same slug and mode, or the same public host.

    An apex wiki named ``team-hosting`` and a hosting wiki named ``team`` both
    answer at ``team-hosting.<base>`` (and without a suffix every apex name
    matches a hosting one), so a slug free in its own mode can still be taken.
    """
    found = by_slug(slug, domain_mode)
    if found:
        return found
    host = urls.platform_host({"subdomain": slug, "domain_mode": domain_mode})
    if not host:
        return None
    for row in db.all("SELECT * FROM instances WHERE status != 'terminated' AND domain_mode != ?", (domain_mode,)):
        if urls.platform_host(row) == host:
            return row
    return None


def owned_by(account_id: str) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM instances WHERE account_id = ? AND status != 'terminated' ORDER BY created_at DESC",
                  (account_id,))


def generate_password(length: int = 12) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _fail(error: RuntimeFailure) -> ServiceError:
    log.warning("Runtime operation failed: %s", error)
    return ServiceError(f"hosting.runtime.{error.code}")


def _each(rows: list[dict[str, Any]], action: Callable[[dict[str, Any]], Any], failure: str) -> int:
    """Apply *action* to every row on its own and count the rows it handled (False skips one).

    A failing row is logged and the rest still run: a wiki that keeps failing
    (a refusal, a runtime or file error) is retried on the next maintenance
    pass instead of holding up every wiki after it.
    """
    count = 0
    for row in rows:
        try:
            if action(row) is not False:
                count += 1
        except ServiceError as error:
            log.warning(failure, row["id"], error.key)
        except Exception as error:  # noqa: BLE001 - one wiki must not block the others
            log.exception(failure, row["id"], error)
    return count


# ── Policy and specs ──────────────────────────────────────────────────────────


def owner_is_admin(inst: dict[str, Any]) -> bool:
    return bool(db.scalar("SELECT is_admin FROM accounts WHERE id = ?", (inst["account_id"],), default=0))


def storage_limit_mb(inst: dict[str, Any], admin_owner: bool | None = None) -> int | None:
    """Effective cap in MB, or None for unlimited (admin owners, apex wikis, 0)."""
    if admin_owner is None:
        admin_owner = owner_is_admin(inst)
    if admin_owner or (inst.get("domain_mode") or "hosting") == "apex":
        return None
    value = inst.get("storage_limit_mb")
    if value is None:
        return cfg().limits.storage_limit_mb
    return int(value) if int(value) > 0 else None


def _extensions(text: str | None) -> list[str]:
    items = []
    for item in (text or "").replace("\n", ",").split(","):
        item = item.strip().lower().lstrip(".")
        if item and item.isalnum() and item not in items:
            items.append(item)
    return items


def upload_policy(inst: dict[str, Any]) -> dict[str, Any]:
    """Platform default merged with the per-wiki override (blocked lists add up)."""
    default_mb = int(settings.get("global_wiki_upload_max_size_mb", 100) or 100)
    size = inst.get("upload_max_size_mb")
    size_mb = max(1, min(2048, int(size))) if size not in (None, "") else default_mb
    blocked = _extensions(settings.get("global_wiki_blocked_extensions"))
    blocked += [e for e in _extensions(inst.get("upload_blocked_extensions")) if e not in blocked]
    return {"upload_max_size_mb": size_mb, "blocked_extensions": blocked, "inherits_size": size in (None, "")}


def _tts_allowed(inst: dict[str, Any]) -> bool:
    if not settings.flag("global_tts_enabled", True):
        return False
    mode = settings.get("global_tts_mode", "all")
    listed = {item.strip().lower() for item in (settings.get("global_tts_list") or "").replace("\n", ",").split(",")
              if item.strip()}
    names = {inst["id"].lower(), inst["subdomain"].lower()}
    if mode == "whitelist":
        return bool(names & listed)
    if mode == "blacklist":
        return not names & listed
    return True


TENANT_TTS_PREFIX = "bwt1"


def tenant_tts_token(master: str, instance_id: str) -> str:
    """The GPU speech token of one wiki: ``bwt1.<instance id>.<HMAC-SHA256(master, id)>``.

    Hosted wikis may run plugins, so anything in their environment must be
    assumed readable by the wiki's administrators. Each wiki therefore gets its
    own token instead of the platform's master token; the GPU server
    (``contrib/tts-gpu-server``) recomputes it from the master token and can
    refuse single wikis (``TTS_REVOKED_TENANTS``). Keep in sync with that server.
    """
    digest = hmac.new(master.encode("utf-8"), f"bananawiki-tts-tenant:{instance_id}".encode(), hashlib.sha256)
    return f"{TENANT_TTS_PREFIX}.{instance_id}.{digest.hexdigest()}"


def policy(inst: dict[str, Any]) -> TenantPolicy:
    config = cfg()
    admin_owner = owner_is_admin(inst)
    forbid_public = (settings.flag("forbid_non_admin_public_wikis", True) and not admin_owner
                     and not inst.get("public_wiki_allowed"))
    forbid_builder = (settings.flag("forbid_non_admin_page_builder", True) and not admin_owner
                      and not inst.get("page_builder_allowed"))
    uploads = upload_policy(inst)
    limit_mb = storage_limit_mb(inst, admin_owner)
    tts_allowed = _tts_allowed(inst)
    gpu = None
    token = settings.tts_gpu_token()
    if tts_allowed and settings.flag("global_tts_gpu_enabled") and settings.get("global_tts_gpu_url") and token:
        if config.tts_gpu_tenant_tokens:
            token = tenant_tts_token(token, inst["id"])
        gpu = TtsGpu(url=str(settings.get("global_tts_gpu_url")).strip().rstrip("/"), token=token,
                     timeout=int(settings.get("global_tts_gpu_timeout", 120) or 120))
    federation = config.federation_instances
    return TenantPolicy(
        easy_wiki=bool(inst.get("easy_wiki")),
        forbid_public_mode=forbid_public,
        forbid_page_builder=forbid_builder,
        forbid_public_builder=forbid_public or forbid_builder,
        storage_limit_bytes=(limit_mb or 0) * 1024 * 1024,
        upload_max_bytes=uploads["upload_max_size_mb"] * 1024 * 1024,
        max_request_bytes=config.limits.max_upload_mb * 1024 * 1024,
        blocked_extensions=tuple(uploads["blocked_extensions"]),
        expires_at=inst.get("expires_at"),
        federation_enabled="*" in federation or inst["id"] in federation,
        tts_disabled=not tts_allowed,
        tts_gpu=gpu,
        oauth=oauth.client_for(inst),
        plugin_denylist=config.tenant_plugin_denylist,
        global_tour=settings.flag("global_tour_enabled"),
        memory_mb=config.limits.memory_limit_mb,
        cpu_limit=config.limits.cpu_limit,
        pids_limit=config.limits.pids_limit,
        nofile_limit=config.limits.nofile_limit,
    )


def hostnames(inst: dict[str, Any]) -> tuple[str, ...]:
    names = [urls.platform_host(inst)]
    domain = db.scalar("SELECT domain FROM instance_custom_domains WHERE instance_id = ? AND verified_at IS NOT NULL "
                       "AND verified_until > ?", (inst["id"], now_sql()))
    if domain:
        names.append(domain)
    return tuple(name for name in names if name)


def spec(inst: dict[str, Any], *, with_policy: bool = True) -> TenantSpec:
    mode = inst.get("domain_mode") or "hosting"
    return TenantSpec(
        instance_id=inst["id"], slug=inst["subdomain"], domain_mode=mode,
        data_dir_name=urls.data_dir_name(inst["subdomain"], mode), port=inst.get("port"),
        url=urls.instance_url(inst), hostnames=hostnames(inst) if inst["status"] != "terminated" else (),
        policy=policy(inst) if with_policy else TenantPolicy(),
    )


def sync_routes() -> None:
    """Publish the routing table of every wiki that may serve (best effort)."""
    from . import domains

    try:
        rows = db.all("SELECT * FROM instances WHERE status = 'running'")
        runtime().sync_routes([spec(row, with_policy=False) for row in rows if domains.may_serve(row["id"])])
    except (RuntimeFailure, sqlite3.Error, ValueError) as error:
        log.warning("Could not publish tenant routes: %s", error)


# ── Creation ──────────────────────────────────────────────────────────────────


def _port_free(port: int) -> bool:
    for address in ("0.0.0.0", "127.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((address, port))
        except OSError:
            return False
    return True


def _allocate_port() -> int:
    """Lowest port in range not held by any live wiki (suspended ones keep theirs)."""
    used = set(db.column("SELECT port FROM instances WHERE status != 'terminated' AND port IS NOT NULL"))
    start, end = cfg().port_range
    for port in range(start, end):
        if port not in used and _port_free(port):
            return port
    raise ServiceError("hosting.instances.no_ports")


def _new_id() -> str:
    while True:
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(16))
        if not db.scalar("SELECT 1 FROM instances WHERE id = ?", (candidate,)):
            return candidate


def platform_usage_mb() -> float:
    total = 0
    for row in db.all("SELECT * FROM instances WHERE status != 'terminated'"):
        total += usage_bytes(row)
    return total / (1024 * 1024)


def usage_bytes(inst: dict[str, Any]) -> int:
    try:
        return int(runtime().usage(spec(inst, with_policy=False)))
    except (RuntimeFailure, ValueError):
        return 0


def _check_capacity(account: dict[str, Any]) -> None:
    if account["is_admin"]:
        return
    active = db.scalar("SELECT COUNT(*) FROM instances WHERE account_id = ? AND status IN ('running', 'stopped')",
                       (account["id"],), default=0)
    if active >= cfg().limits.max_per_account:
        raise ServiceError("hosting.instances.account_limit", max=cfg().limits.max_per_account)
    if settings.flag("global_limit_enabled", True):
        total = db.scalar("SELECT COUNT(*) FROM instances WHERE status IN ('running', 'stopped')", default=0)
        if total >= int(settings.get("global_limit_max_instances", 50)):
            raise ServiceError("hosting.instances.platform_full")
        if platform_usage_mb() >= int(settings.get("global_limit_max_storage_mb", 5000)):
            raise ServiceError("hosting.instances.platform_storage_full")


def _insert(account: dict[str, Any], slug: str, domain_mode: str, *, admin_username: str, custom_credentials: bool,
            easy_wiki: bool, use_case: str) -> dict[str, Any]:
    if name_holder(slug, domain_mode):
        raise ServiceError("hosting.instances.name_taken")
    admin_owner = bool(account["is_admin"])
    if admin_owner:
        expires = sql_in(days=ADMIN_EXPIRY_DAYS) if domain_mode != "apex" else None
    else:
        expires = sql_in(days=cfg().limits.duration_days)
    instance_id = _new_id()
    try:
        with db.transaction():
            db.insert("instances", {
                "id": instance_id, "account_id": account["id"], "subdomain": slug, "status": "running",
                "port": _allocate_port(), "admin_username": admin_username, "admin_password_plain": None,
                "storage_limit_mb": 0 if admin_owner else None, "created_at": now_sql(), "expires_at": expires,
                "domain_mode": domain_mode, "custom_credentials": 1 if custom_credentials else 0,
                "easy_wiki": 1 if easy_wiki else 0, "declared_use_case": use_case[:2000],
                "tos_compliance_declared_at": now_sql(),
            })
    except sqlite3.IntegrityError as error:
        raise ServiceError("hosting.instances.name_taken") from error
    return get(instance_id)  # type: ignore[return-value]


def create(account: dict[str, Any], slug: str, *, domain_mode: str = "hosting", admin_username: str = "",
           admin_password: str = "", easy_wiki: bool = False, use_case: str = "") -> tuple[dict[str, Any], str, str]:
    """Provision a wiki. Returns ``(instance, admin_username, admin_password)``.

    The initial password is returned to be shown once; it is never stored.
    """
    slug = urls.validate_slug(slug, account_is_admin=bool(account["is_admin"]), domain_mode=domain_mode)
    _check_capacity(account)
    custom = bool(admin_password)
    username = (admin_username or "").strip() or f"admin_{secrets.token_hex(3)}"
    if custom:
        accounts.check_new_password(admin_password)
        accounts.check_username(username)
    password = admin_password if custom else generate_password()
    inst = _insert(account, slug, domain_mode, admin_username=username, custom_credentials=custom,
                   easy_wiki=easy_wiki, use_case=use_case)
    try:
        runtime().provision(spec(inst), admin_username=username, admin_password=password,
                            force_password_change=not custom)
    except RuntimeFailure as error:
        _discard_failed(inst, error)
        raise _fail(error) from error
    events.record("instance", inst["id"], "instance.created", account["id"], slug)
    sync_routes()
    return get(inst["id"]), username, password  # type: ignore[return-value]


def _discard_failed(inst: dict[str, Any], error: RuntimeFailure) -> None:
    """Remove a wiki whose provisioning failed: terminate the row, drop the data it created.

    ``data_exists`` means the runtime refused before writing anything: the
    directory is someone else's (data a failed move left behind, a manual
    copy), so it is left untouched for an administrator to look at.
    """
    if error.code == "data_exists":
        log.error("Wiki %s not created: its data directory already exists and was left untouched", inst["id"])
    else:
        try:
            runtime().destroy(spec(inst, with_policy=False))
        except RuntimeFailure as cleanup:
            log.warning("Could not clean up failed wiki %s: %s", inst["id"], cleanup)
    db.update("instances", {"status": "terminated", "terminated_at": now_sql(), "port": None,
                            "subdomain": urls.archived_slug(inst["id"], inst["subdomain"]),
                            "terminated_reason": "provisioning_failed", "data_retained_until": None},
              "id = ?", (inst["id"],))


def _data_dir(inst: dict[str, Any]) -> str:
    return urls.data_dir_name(inst["subdomain"], inst.get("domain_mode") or "hosting")


def _settle_data(instance_id: str, moved_to: str) -> None:
    """After a row update failed, move data already moved to *moved_to* back where the row expects it.

    Data must never sit under a name its row does not hold: the next wiki
    created with that name would find it (and fail), and the wiki that owns
    it could no longer reach it.
    """
    current = get(instance_id)
    if current is None:
        return
    expected = _data_dir(current)
    if expected == moved_to:
        return
    try:
        runtime().relocate(instance_id, moved_to, expected)
    except RuntimeFailure as error:
        log.error("Data of wiki %s is stuck in %s (expected %s): %s", instance_id, moved_to, expected, error)


def _undo_move(inst: dict[str, Any], moved_to: str, was_running: bool) -> None:
    """Put a wiki back as it was when its row could not take the name its data moved to."""
    _settle_data(inst["id"], moved_to)
    if not was_running:
        return
    try:
        runtime().start(spec(inst))
    except RuntimeFailure as error:
        log.warning("Wiki %s stays stopped: %s", inst["id"], error)
        db.update("instances", {"status": "stopped", "stopped_at": now_sql()}, "id = ?", (inst["id"],))


# ── Pause, resume, restart ────────────────────────────────────────────────────


def _cas(instance_id: str, allowed: tuple[str, ...], values: dict[str, Any]) -> bool:
    marks = ", ".join("?" for _ in allowed)
    return db.update("instances", values, f"id = ? AND status IN ({marks})", (instance_id, *allowed)) == 1


def stop(inst: dict[str, Any], *, actor_id: str | None, allow_suspended: bool = False) -> None:
    allowed = ("running", "suspended") if allow_suspended else ("running",)
    if inst["status"] not in allowed:
        raise ServiceError("hosting.instances.not_running")
    try:
        runtime().stop(spec(inst, with_policy=False))
    except RuntimeFailure as error:
        raise _fail(error) from error
    if not _cas(inst["id"], (inst["status"],), {"status": "stopped", "stopped_at": now_sql()}):
        raise ServiceError("hosting.instances.changed_state")
    events.record("instance", inst["id"], "instance.stopped", actor_id)
    sync_routes()


def start(inst: dict[str, Any], *, actor_id: str | None, allow_suspended: bool = False) -> None:
    """Resume a paused wiki (administrators may also start a suspended one)."""
    if inst["status"] == "running":
        raise ServiceError("hosting.instances.already_running")
    if not allow_suspended and (inst["status"] == "suspended" or inst.get("suspended_at")):
        raise ServiceError("hosting.instances.suspended")
    if inst["status"] not in ("stopped", "suspended"):
        raise ServiceError("hosting.instances.not_stopped")
    if not _cas(inst["id"], (inst["status"],), {"status": "running", "stopped_at": None}):
        raise ServiceError("hosting.instances.changed_state")
    try:
        runtime().start(spec(get(inst["id"])))  # type: ignore[arg-type]
    except RuntimeFailure as error:
        _cas(inst["id"], ("running",), {"status": "stopped", "stopped_at": now_sql()})
        raise _fail(error) from error
    events.record("instance", inst["id"], "instance.started", actor_id)
    sync_routes()


def restart(inst: dict[str, Any], *, actor_id: str | None) -> None:
    """Restart a running wiki with its current policy (clears stale state)."""
    if inst["status"] != "running":
        raise ServiceError("hosting.instances.not_running")
    try:
        runtime().restart(spec(inst), force=True)
    except RuntimeFailure as error:
        raise _fail(error) from error
    events.record("instance", inst["id"], "instance.restarted", actor_id)


def apply_policy(inst: dict[str, Any], *, actor_id: str | None) -> bool:
    """Push a changed policy: sync in-DB limits, restart a running wiki.

    Returns False when the restart failed; a wiki that cannot pick up a more
    restrictive policy is stopped rather than left running with the old one.
    """
    inst = get(inst["id"]) or inst
    if inst["status"] == "terminated":
        return True
    try:
        runtime().apply_limits(spec(inst))
        if inst["status"] == "running":
            runtime().restart(spec(inst), force=True)
    except RuntimeFailure as error:
        log.warning("Policy for %s not applied: %s", inst["id"], error)
        if inst["status"] == "running":
            try:
                stop(inst, actor_id=actor_id)
            except ServiceError:
                pass
        return False
    return True


# ── Suspension ────────────────────────────────────────────────────────────────


def suspend(inst: dict[str, Any], *, actor_id: str | None, until: str | None = None, reason: str = "",
            reason_visible: bool = False, time_visible: bool = False, duration_label: str = "permanent") -> None:
    """Suspend (idempotent: an existing suspension window is not reset)."""
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    reason = (reason or "").strip()[:500]
    with db.transaction():
        if inst["status"] != "suspended":
            db.update("instances", {"status": "suspended", "stopped_at": now_sql()}, "id = ? AND status != 'terminated'",
                      (inst["id"],))
            db.execute("UPDATE instances SET suspended_at = ? WHERE id = ? AND (suspended_at IS NULL OR suspended_at = '')",
                       (now_sql(), inst["id"]))
        db.update("instances", {"suspended_until": until, "suspend_reason": reason,
                                "suspend_reason_visible": 1 if reason_visible and reason else 0,
                                "suspend_time_visible": 1 if time_visible and until else 0}, "id = ?", (inst["id"],))
        _audit(inst["id"], "suspend", actor_id, reason or None, reason_visible and bool(reason),
               time_visible and bool(until), duration_label, until)
    try:
        runtime().stop(spec(inst, with_policy=False))
    except RuntimeFailure as error:
        log.error("Suspended wiki %s could not be stopped: %s", inst["id"], error)
    sync_routes()


def _audit(instance_id: str, action: str, actor_id: str | None, reason: str | None, reason_visible: bool,
           time_visible: bool, duration: str | None, until: str | None) -> None:
    actor = actor_id if actor_id and db.scalar("SELECT 1 FROM accounts WHERE id = ?", (actor_id,)) else None
    db.insert("instance_suspension_audit", {
        "instance_id": instance_id, "action": action, "reason": reason, "reason_visible": int(reason_visible),
        "time_visible": int(time_visible), "duration": duration, "suspended_until": until, "performed_by": actor,
        "created_at": now_sql(),
    })
    events.record("instance", instance_id, f"instance.{action}", actor_id, reason or "")


def unsuspend(inst: dict[str, Any], *, actor_id: str | None) -> None:
    """Lift a suspension, credit the frozen time to the expiry and start the wiki."""
    if inst["status"] != "suspended":
        raise ServiceError("hosting.instances.not_suspended")
    with db.transaction():
        started = parse(inst.get("suspended_at"))
        delta = max(0, int((utcnow() - started).total_seconds())) if started else 0
        expires = parse(inst.get("expires_at"))
        db.update("instances", {
            "suspended_at": None, "suspended_until": None, "suspend_reason": "", "suspend_reason_visible": 0,
            "suspend_time_visible": 0, "status": "stopped",
            "suspended_accumulated_seconds": int(inst.get("suspended_accumulated_seconds") or 0) + delta,
            "expires_at": to_sql(expires + timedelta(seconds=delta)) if expires else inst.get("expires_at"),
        }, "id = ?", (inst["id"],))
        _audit(inst["id"], "unsuspend", actor_id, None, False, False, None, None)
    start(get(inst["id"]), actor_id=actor_id)  # type: ignore[arg-type]


def suspension_active(inst: dict[str, Any]) -> bool:
    if inst.get("status") != "suspended":
        return False
    until = inst.get("suspended_until")
    return not (until and is_past(until))


def suspension_history(instance_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT s.*, a.username AS performed_by_name FROM instance_suspension_audit s "
        "LEFT JOIN accounts a ON a.id = s.performed_by WHERE s.instance_id = ? ORDER BY s.created_at DESC, s.id DESC",
        (instance_id,),
    )


# ── Termination, restore, deletion ────────────────────────────────────────────


def terminate(inst: dict[str, Any], *, actor_id: str | None, reason: str = "manual") -> None:
    """Stop the wiki, archive its name and keep its data for the grace period.

    The data moves to the archived name before the row gives up the original
    one. When the move fails the wiki keeps its name (stopped) and the
    termination can simply be retried; data left under a released name would
    make the next wiki of that name fail and could no longer be restored.
    Without a grace period the archived data is then deleted; if that fails
    it stays under the archived name until an administrator deletes the wiki.
    """
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    runtime_spec = spec(inst, with_policy=False)
    try:
        runtime().stop(runtime_spec)
    except RuntimeFailure as error:
        raise _fail(error) from error
    grace = settings.grace_period_days()
    archived = urls.archived_slug(inst["id"], inst["subdomain"])
    archived_dir = urls.data_dir_name(archived, runtime_spec.domain_mode)
    moved = False
    try:
        runtime().relocate(inst["id"], runtime_spec.data_dir_name, archived_dir)
        moved = True
    except RuntimeFailure as error:
        if error.code != "not_found":
            if inst["status"] == "running":
                _cas(inst["id"], ("running",), {"status": "stopped", "stopped_at": now_sql()})
                sync_routes()
            raise _fail(error) from error
        log.warning("Terminated wiki %s had no data directory to keep", inst["id"])
    try:
        with db.transaction():
            # A rename since *inst* was read moved the data elsewhere (the move
            # above then found nothing): the row must not release that name.
            changed = db.update("instances", {
                "status": "terminated", "terminated_at": now_sql(), "port": None, "subdomain": archived,
                "data_retained_until": sql_in(days=grace) if grace > 0 else None, "terminated_reason": reason,
                "admin_password_plain": None, "grace_period_suspended": 0,
            }, "id = ? AND status != 'terminated' AND subdomain = ? AND domain_mode IS ?",
                (inst["id"], inst["subdomain"], inst.get("domain_mode")))
            if not changed:
                raise ServiceError("hosting.instances.changed_state")
            db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ?", (inst["id"],))
            db.execute("UPDATE instance_ownership_transfers SET status = 'cancelled', resolved_at = ? "
                       "WHERE instance_id = ? AND status = 'pending'", (now_sql(), inst["id"]))
            events.record("instance", inst["id"], f"instance.terminated.{reason}", actor_id)
    except (ServiceError, sqlite3.Error):
        if moved:
            _settle_data(inst["id"], archived_dir)
        raise
    if grace <= 0:
        try:
            runtime().destroy(spec({**inst, "subdomain": archived, "status": "terminated"}, with_policy=False))
        except RuntimeFailure as error:
            log.error("Data of terminated wiki %s not deleted: %s", inst["id"], error)
    sync_routes()


def restore(inst: dict[str, Any], *, actor_id: str, extend_days: int | None = None) -> dict[str, Any]:
    """Bring a terminated wiki back from its retained data."""
    if inst["status"] != "terminated":
        raise ServiceError("hosting.instances.not_terminated")
    original = urls.original_slug(inst["subdomain"])
    mode = inst.get("domain_mode") or "hosting"
    if not original or not inst.get("data_retained_until"):
        raise ServiceError("hosting.instances.no_retained_data")
    target = original
    for _attempt in range(8):
        if not name_holder(target, mode):
            break
        target = f"{original[:30]}-r{secrets.token_hex(2)}"
    else:
        raise ServiceError("hosting.instances.name_taken")
    days = extend_days if extend_days and extend_days > 0 else cfg().limits.duration_days
    old_dir = urls.data_dir_name(inst["subdomain"], mode)
    new_dir = urls.data_dir_name(target, mode)
    try:
        runtime().relocate(inst["id"], old_dir, new_dir)
    except RuntimeFailure as error:
        raise _fail(error) from error
    try:
        with db.transaction():
            db.update("instances", {
                "status": "stopped", "subdomain": target, "port": _allocate_port(), "stopped_at": now_sql(),
                "expires_at": None if mode == "apex" else sql_in(days=days), "terminated_at": None,
                "data_retained_until": None, "terminated_reason": None, "grace_period_suspended": 0,
            }, "id = ?", (inst["id"],))
            events.record("instance", inst["id"], "instance.restored", actor_id, target)
    except sqlite3.IntegrityError as error:
        _settle_data(inst["id"], new_dir)
        raise ServiceError("hosting.instances.name_taken") from error
    except (ServiceError, sqlite3.Error):
        _settle_data(inst["id"], new_dir)
        raise
    restored = get(inst["id"])
    try:
        start(restored, actor_id=actor_id)  # type: ignore[arg-type]
    except ServiceError:
        log.warning("Restored wiki %s is stopped: it did not start", inst["id"])
    return get(inst["id"])  # type: ignore[return-value]


def hard_delete(inst: dict[str, Any], *, actor_id: str) -> None:
    if inst["status"] != "terminated":
        raise ServiceError("hosting.instances.not_terminated")
    try:
        runtime().destroy(spec(inst, with_policy=False))
    except RuntimeFailure as error:
        raise _fail(error) from error
    db.execute("DELETE FROM instances WHERE id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.deleted", actor_id)


_GRACE_DUE = ("status = 'terminated' AND data_retained_until IS NOT NULL AND data_retained_until <= ? "
              "AND grace_period_suspended = 0")


def purge_expired_grace_periods() -> int:
    """Delete the data and rows of terminated wikis whose retention ended (paused countdowns wait)."""

    def purge(inst: dict[str, Any]) -> bool:
        # An administrator may have paused the countdown since the list was read.
        if not db.scalar(f"SELECT 1 FROM instances WHERE id = ? AND {_GRACE_DUE}", (inst["id"], now_sql())):
            return False
        try:
            runtime().destroy(spec(inst, with_policy=False))
        except RuntimeFailure as error:
            log.warning("Retained data of %s not deleted yet: %s", inst["id"], error)
            return False
        db.execute("DELETE FROM instances WHERE id = ?", (inst["id"],))
        return True

    rows = db.all(f"SELECT * FROM instances WHERE {_GRACE_DUE} ORDER BY data_retained_until, id", (now_sql(),))
    return _each(rows, purge, "Retained data of %s not deleted yet: %s")


def _grace_paused_since(inst: dict[str, Any]) -> datetime | None:
    """When the current pause of the deletion countdown began.

    That is the latest ``instance.grace_suspended`` event of this
    termination; a pause with no such event (set before 1.6 recorded one)
    counts from the termination, so resuming never shortens the retention.
    """
    terminated = parse(inst.get("terminated_at"))
    paused = parse(db.scalar(
        "SELECT created_at FROM hosting_events WHERE subject_type = 'instance' AND subject_id = ? "
        "AND action = 'instance.grace_suspended' ORDER BY id DESC LIMIT 1", (inst["id"],)))
    if paused and (terminated is None or paused >= terminated):
        return paused
    return terminated


def set_grace_suspended(inst: dict[str, Any], suspended: bool, *, actor_id: str) -> bool:
    """Pause or resume the deletion countdown of a terminated wiki's retained data.

    While paused the data is kept however long the pause lasts (the purge
    skips the wiki) and its owner cannot download it. Resuming pushes
    ``data_retained_until`` back by the paused time, so the countdown goes on
    from where it stopped. Asking for the current state again changes nothing
    and returns False.
    """
    if inst["status"] != "terminated" or not inst.get("data_retained_until"):
        raise ServiceError("hosting.instances.not_in_grace")
    paused = bool(inst.get("grace_period_suspended"))
    if suspended == paused:
        return False
    values: dict[str, Any] = {"grace_period_suspended": 1 if suspended else 0}
    if suspended:
        if not grace_active(inst):
            raise ServiceError("hosting.instances.not_in_grace")
    else:
        since = _grace_paused_since(inst)
        until = parse(inst["data_retained_until"])
        if since and until:
            values["data_retained_until"] = to_sql(until + max(timedelta(0), utcnow() - since))
    with db.transaction():
        changed = db.update("instances", values, "id = ? AND status = 'terminated' AND grace_period_suspended = ?",
                            (inst["id"], 1 if paused else 0))
        if not changed:
            raise ServiceError("hosting.instances.changed_state")
        events.record("instance", inst["id"], "instance.grace_" + ("suspended" if suspended else "unsuspended"),
                      actor_id)
    return True


def grace_active(inst: dict[str, Any]) -> bool:
    """Whether a terminated wiki's data is retained: until its date, or for as long as the countdown is paused."""
    until = inst.get("data_retained_until")
    if inst["status"] != "terminated" or not until:
        return False
    return bool(inst.get("grace_period_suspended")) or not is_past(until)


# ── Expiry ────────────────────────────────────────────────────────────────────


def set_expiry(inst: dict[str, Any], expires_at: str | None, *, actor_id: str) -> None:
    """Set (or with None remove) the expiry; a date already past terminates now."""
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if (inst.get("domain_mode") or "hosting") == "apex" and expires_at is not None:
        raise ServiceError("hosting.instances.apex_perpetual")
    db.update("instances", {"expires_at": expires_at}, "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.expiry_changed", actor_id, expires_at or "")
    if expires_at and is_past(expires_at):
        terminate(get(inst["id"]), actor_id=actor_id, reason="expired")  # type: ignore[arg-type]


def shift_expiry(inst: dict[str, Any], seconds: int, *, actor_id: str) -> None:
    """Extend (positive) or shorten (negative) the expiry by *seconds*."""
    if (inst.get("domain_mode") or "hosting") == "apex":
        raise ServiceError("hosting.instances.apex_perpetual")
    seconds = max(-MAX_EXTENSION_SECONDS, min(MAX_EXTENSION_SECONDS, int(seconds)))
    base = parse(inst.get("expires_at")) or utcnow()
    if seconds > 0 and base < utcnow():
        base = utcnow()
    set_expiry(inst, to_sql(base + timedelta(seconds=seconds)), actor_id=actor_id)


def terminate_expired() -> int:
    rows = db.all("SELECT * FROM instances WHERE status IN ('running', 'stopped') AND expires_at IS NOT NULL "
                  "AND expires_at <= ? ORDER BY expires_at, id", (now_sql(),))
    return _each(rows, lambda inst: terminate(inst, actor_id=None, reason="expired"),
                 "Expired wiki %s not terminated: %s")


def lift_expired_suspensions() -> int:
    rows = db.all("SELECT * FROM instances WHERE status = 'suspended' AND suspended_until IS NOT NULL "
                  "AND suspended_until <= ? ORDER BY suspended_until, id", (now_sql(),))
    return _each(rows, lambda inst: unsuspend(inst, actor_id=None), "Timed suspension of %s not lifted: %s")


def enforce_storage_quotas(overrun_ratio: float = 1.10) -> int:
    """Suspend non-admin wikis that materially exceed their storage cap."""

    def check(inst: dict[str, Any]) -> bool:
        limit = storage_limit_mb(inst)
        if limit is None:
            return False
        used = usage_bytes(inst)
        if used <= limit * 1024 * 1024 * overrun_ratio:
            return False
        suspend(inst, actor_id=None, reason=f"Automatic storage protection: {used / 1048576:.1f} MB used, "
                f"{limit} MB allowed.", reason_visible=True, duration_label="storage")
        return True

    rows = db.all("SELECT * FROM instances WHERE status IN ('running', 'stopped') ORDER BY created_at, id")
    return _each(rows, check, "Storage quota of %s not enforced: %s")


# ── Names, owners and quotas ──────────────────────────────────────────────────


def rename(inst: dict[str, Any], slug: str, domain_mode: str, *, actor_id: str, auto_suffix: bool = False) -> dict[str, Any]:
    """Change a wiki's name and/or URL mode, moving its data directory."""
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    admin_owner = owner_is_admin(inst)
    slug = urls.validate_slug(slug, account_is_admin=admin_owner, domain_mode=domain_mode)
    if auto_suffix:
        base, number = slug, 2
        while (other := name_holder(slug, domain_mode)) and other["id"] != inst["id"]:
            slug = f"{base[: cfg().subdomain_max_length - len(str(number)) - 1].rstrip('-')}-{number}"
            number += 1
    else:
        other = name_holder(slug, domain_mode)
        if other and other["id"] != inst["id"]:
            raise ServiceError("hosting.instances.name_taken")
    old_mode = inst.get("domain_mode") or "hosting"
    if slug == inst["subdomain"] and domain_mode == old_mode:
        return inst
    was_running = inst["status"] == "running"
    old_spec = spec(inst, with_policy=False)
    new_dir = urls.data_dir_name(slug, domain_mode)
    try:
        if was_running:
            runtime().stop(old_spec)
        runtime().relocate(inst["id"], old_spec.data_dir_name, new_dir)
    except RuntimeFailure as error:
        raise _fail(error) from error
    values: dict[str, Any] = {"subdomain": slug, "domain_mode": domain_mode}
    if domain_mode == "apex":
        values.update(expires_at=None, storage_limit_mb=0)
    elif old_mode == "apex":
        values["expires_at"] = sql_in(days=ADMIN_EXPIRY_DAYS if admin_owner else cfg().limits.duration_days)
    try:
        db.update("instances", values, "id = ?", (inst["id"],))
    except sqlite3.Error as error:
        _undo_move(inst, new_dir, was_running)
        if isinstance(error, sqlite3.IntegrityError):
            raise ServiceError("hosting.instances.name_taken") from error
        raise
    db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ? AND verified_at IS NULL", (inst["id"],))
    events.record("instance", inst["id"], "instance.renamed", actor_id,
                  f"{inst['subdomain']} ({old_mode}) -> {slug} ({domain_mode})")
    renamed = get(inst["id"])
    if was_running:
        try:
            runtime().start(spec(renamed))  # type: ignore[arg-type]
        except RuntimeFailure as error:
            db.update("instances", {"status": "stopped", "stopped_at": now_sql()}, "id = ?", (inst["id"],))
            raise _fail(error) from error
    sync_routes()
    return get(inst["id"])  # type: ignore[return-value]


def apply_owner_quota(inst: dict[str, Any], admin_owner: bool) -> None:
    """Administrators' wikis have no expiry or storage cap; others get the defaults."""
    if inst["status"] == "terminated":
        return
    if admin_owner:
        expires = None if (inst.get("domain_mode") or "hosting") == "apex" else sql_in(days=ADMIN_EXPIRY_DAYS)
        db.update("instances", {"expires_at": expires, "storage_limit_mb": 0}, "id = ?", (inst["id"],))
        return
    limit = inst.get("storage_limit_mb")
    db.update("instances", {
        "expires_at": sql_in(days=cfg().limits.duration_days),
        "storage_limit_mb": limit if limit and int(limit) > 0 else None,
    }, "id = ?", (inst["id"],))


def move_to_owner(inst: dict[str, Any], target: dict[str, Any], *, actor_id: str) -> list[str]:
    """Give a wiki to another account, applying that account's privileges.

    Returns notes for the caller: ``"renamed"`` when an apex wiki had to move
    to hosting mode because the new owner is not an administrator.
    """
    notes = []
    target_admin = bool(target["is_admin"])
    previous_admin = owner_is_admin(inst)
    if (inst.get("domain_mode") or "hosting") == "apex" and not target_admin:
        inst = rename_for_non_admin(inst, actor_id=actor_id)
        notes.append("renamed")
    with db.transaction():
        db.update("instances", {"account_id": target["id"]}, "id = ?", (inst["id"],))
        db.execute("DELETE FROM instance_collaborators WHERE instance_id = ? AND account_id = ?",
                   (inst["id"], target["id"]))
        if previous_admin != target_admin:
            apply_owner_quota(get(inst["id"]), target_admin)  # type: ignore[arg-type]
        events.record("instance", inst["id"], "instance.owner_changed", actor_id, target["username"])
    apply_policy(get(inst["id"]), actor_id=actor_id)  # type: ignore[arg-type]
    return notes


def rename_for_non_admin(inst: dict[str, Any], *, actor_id: str) -> dict[str, Any]:
    """Move an apex wiki to hosting mode (apex names are for administrators)."""
    base = inst["subdomain"]
    slug, number = base, 2
    while (other := name_holder(slug, "hosting")) and other["id"] != inst["id"]:
        slug = f"{base[:30]}-{number}"
        number += 1
    old_spec = spec(inst, with_policy=False)
    new_dir = urls.data_dir_name(slug, "hosting")
    try:
        if inst["status"] == "running":
            runtime().stop(old_spec)
        runtime().relocate(inst["id"], old_spec.data_dir_name, new_dir)
    except RuntimeFailure as error:
        raise _fail(error) from error
    try:
        db.update("instances", {"subdomain": slug, "domain_mode": "hosting",
                                "expires_at": sql_in(days=cfg().limits.duration_days)}, "id = ?", (inst["id"],))
    except sqlite3.Error as error:
        _undo_move(inst, new_dir, inst["status"] == "running")
        if isinstance(error, sqlite3.IntegrityError):
            raise ServiceError("hosting.instances.name_taken") from error
        raise
    events.record("instance", inst["id"], "instance.renamed", actor_id, f"{base} (apex) -> {slug} (hosting)")
    return get(inst["id"])  # type: ignore[return-value]


def set_storage_limit(inst: dict[str, Any], limit_mb: int | None, *, actor_id: str) -> bool:
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if (inst.get("domain_mode") or "hosting") == "apex":
        limit_mb = 0
    db.update("instances", {"storage_limit_mb": limit_mb}, "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.storage_limit", actor_id, str(limit_mb))
    return apply_policy(inst, actor_id=actor_id)


def set_upload_policy(inst: dict[str, Any], size_mb: int | None, blocked: str, *, actor_id: str) -> bool:
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if size_mb is not None and not 1 <= size_mb <= 2048:
        raise ServiceError("hosting.instances.invalid_upload_size")
    db.update("instances", {"upload_max_size_mb": size_mb, "upload_blocked_extensions": ",".join(_extensions(blocked))},
              "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.upload_policy", actor_id)
    return apply_policy(inst, actor_id=actor_id)


def set_easy_wiki(inst: dict[str, Any], enable: bool, *, actor_id: str) -> None:
    if inst["status"] in ("terminated", "suspended"):
        raise ServiceError("hosting.instances.cannot_modify")
    if bool(inst.get("easy_wiki")) == enable:
        raise ServiceError("hosting.instances.mode_unchanged")
    db.update("instances", {"easy_wiki": 1 if enable else 0}, "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.easy_wiki." + ("on" if enable else "off"), actor_id)
    if not apply_policy(inst, actor_id=actor_id):
        raise ServiceError("hosting.instances.restart_failed")


def set_use_case(inst: dict[str, Any], text: str) -> None:
    text = (text or "").strip()
    if not 20 <= len(text) <= 2000:
        raise ServiceError("hosting.instances.use_case_length")
    db.update("instances", {"declared_use_case": text, "tos_compliance_declared_at": now_sql()}, "id = ?",
              (inst["id"],))


# ── Wiki recovery tools ───────────────────────────────────────────────────────


def _live(inst: dict[str, Any]) -> None:
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")


def reset_admin_password(inst: dict[str, Any], *, actor_id: str) -> tuple[str, str]:
    """Return ``(username, password)`` to show once; nothing is stored."""
    _live(inst)
    password = generate_password()
    try:
        username = runtime().reset_admin_password(spec(inst, with_policy=False), inst.get("admin_username") or "admin",
                                                  password)
    except RuntimeFailure as error:
        raise _fail(error) from error
    events.record("instance", inst["id"], "instance.admin_password_reset", actor_id)
    return username, password


def reset_content(inst: dict[str, Any], *, actor_id: str) -> tuple[str, str]:
    """Factory reset. Returns the new ``(username, password)`` to show once."""
    if inst["status"] in ("terminated", "suspended"):
        raise ServiceError("hosting.instances.cannot_modify")
    username = inst.get("admin_username") or "admin"
    password = generate_password()
    was_running = inst["status"] == "running"
    try:
        runtime().stop(spec(inst, with_policy=False))
        runtime().reset_content(spec(inst), admin_username=username, admin_password=password)
        if was_running:
            runtime().start(spec(inst))
    except RuntimeFailure as error:
        if was_running:
            db.update("instances", {"status": "stopped", "stopped_at": now_sql()}, "id = ?", (inst["id"],))
        raise _fail(error) from error
    events.record("instance", inst["id"], "instance.reset", actor_id)
    return username, password


def take_legacy_password(inst: dict[str, Any]) -> str:
    """A 1.4 initial password not yet shown to the owner: return it once and erase it."""
    stored = inst.get("admin_password_plain") or ""
    if stored:
        db.update("instances", {"admin_password_plain": None}, "id = ?", (inst["id"],))
    return stored


def duplicate(inst: dict[str, Any], owner: dict[str, Any], slug: str, domain_mode: str, *, actor_id: str) -> dict[str, Any]:
    _live(inst)
    slug = urls.validate_slug(slug, account_is_admin=bool(owner["is_admin"]), domain_mode=domain_mode)
    copy = _insert(owner, slug, domain_mode, admin_username=inst.get("admin_username") or "admin",
                   custom_credentials=bool(inst.get("custom_credentials")), easy_wiki=bool(inst.get("easy_wiki")),
                   use_case=inst.get("declared_use_case") or "")
    try:
        runtime().duplicate(spec(inst, with_policy=False), spec(copy))
    except RuntimeFailure as error:
        _discard_failed(copy, error)
        raise _fail(error) from error
    events.record("instance", copy["id"], "instance.duplicated", actor_id, inst["id"])
    sync_routes()
    return get(copy["id"])  # type: ignore[return-value]


def import_archive(owner: dict[str, Any], slug: str, domain_mode: str, archive_path: Any, *,
                   actor_id: str) -> dict[str, Any]:
    slug = urls.validate_slug(slug, account_is_admin=bool(owner["is_admin"]), domain_mode=domain_mode)
    inst = _insert(owner, slug, domain_mode, admin_username="admin", custom_credentials=True, easy_wiki=False,
                   use_case="")
    try:
        runtime().import_archive(spec(inst), archive_path)
    except RuntimeFailure as error:
        _discard_failed(inst, error)
        raise _fail(error) from error
    events.record("instance", inst["id"], "instance.imported", actor_id)
    sync_routes()
    return get(inst["id"])  # type: ignore[return-value]


# ── Views for dashboards ──────────────────────────────────────────────────────


def describe(inst: dict[str, Any], *, admin_owner: bool | None = None, with_status: bool = True) -> dict[str, Any]:
    """The row plus URL, time left, grace information, health and storage."""
    item = dict(inst)
    item["url"] = urls.instance_url(inst) if inst["status"] != "terminated" else ""
    expires = parse(inst.get("expires_at"))
    if inst["status"] == "terminated" or expires is None:
        item["seconds_left"] = None
    else:
        item["seconds_left"] = int((expires - utcnow()).total_seconds())
        if inst["status"] == "suspended" and inst.get("suspended_at"):
            frozen = parse(inst["suspended_at"])
            if frozen:
                item["seconds_left"] = int((expires - frozen).total_seconds())
    item["days_left"] = max(0, item["seconds_left"] // 86400) if item["seconds_left"] is not None else None
    item["expired"] = item["seconds_left"] is not None and item["seconds_left"] <= 0
    item["grace_active"] = grace_active(inst)
    item["original_slug"] = urls.original_slug(inst["subdomain"]) or inst["subdomain"]
    limit = storage_limit_mb(inst, admin_owner)
    item["storage_limit_effective_mb"] = limit
    item["health"] = "unknown"
    item["storage_bytes"] = 0
    if with_status:
        try:
            runtime_spec = spec(inst, with_policy=False)
            item["storage_bytes"] = int(runtime().usage(runtime_spec))
            if inst["status"] == "running":
                item["health"] = runtime().status(runtime_spec).state
        except (RuntimeFailure, ValueError):
            item["health"] = "unknown"
    item["storage_mb"] = round(item["storage_bytes"] / 1048576, 1)
    item["storage_pct"] = min(100, round(item["storage_bytes"] / (limit * 1048576) * 100, 1)) if limit else 0
    return item


def dashboard_list(account: dict[str, Any]) -> list[dict[str, Any]]:
    """Own wikis (plus retained ones when owners may recover them) and shared wikis."""
    show_retained = bool(account["is_admin"]) or settings.flag("allow_owner_download_expired")
    rows = db.all(
        "SELECT * FROM instances WHERE account_id = ? AND (status != 'terminated' OR (? = 1 AND "
        "(data_retained_until > ? OR (data_retained_until IS NOT NULL AND grace_period_suspended = 1)))) "
        "ORDER BY created_at DESC", (account["id"], 1 if show_retained else 0, now_sql()),
    )
    items = [{**describe(row, admin_owner=bool(account["is_admin"])), "role": "owner"} for row in rows]
    shared = db.all(
        "SELECT i.*, c.role AS collab_role, c.permissions AS collab_permissions, a.username AS owner_username, "
        "a.is_admin AS owner_admin FROM instance_collaborators c JOIN instances i ON i.id = c.instance_id "
        "JOIN accounts a ON a.id = i.account_id WHERE c.account_id = ? AND i.status != 'terminated' "
        "ORDER BY i.created_at DESC", (account["id"],),
    )
    items += [{**describe(row, admin_owner=bool(row["owner_admin"])), "role": "collaborator"} for row in shared]
    return items


def admin_list() -> list[dict[str, Any]]:
    rows = db.all("SELECT i.*, a.username AS owner_username, a.is_admin AS owner_admin FROM instances i "
                  "JOIN accounts a ON a.id = i.account_id ORDER BY i.created_at DESC")
    return [describe(row, admin_owner=bool(row["owner_admin"])) for row in rows]


def platform_stats() -> dict[str, Any]:
    counts = {status: 0 for status in STATUSES}
    for row in db.all("SELECT status, COUNT(*) AS n FROM instances GROUP BY status"):
        counts[row["status"]] = row["n"]
    return {
        "accounts": db.scalar("SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL", default=0),
        "instances": sum(counts.values()), **counts,
        "pending_accounts": db.scalar("SELECT COUNT(*) FROM accounts WHERE approval_status = 'pending' "
                                      "AND deleted_at IS NULL", default=0),
    }
