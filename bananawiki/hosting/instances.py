"""Hosted wikis: the ``instances`` rows and every lifecycle decision.

This module owns the database side of a wiki's life (status, expiry,
suspension windows, grace periods, names, quotas, entitlements) and calls the
:class:`~bananawiki.hosting.runtime.Runtime` for everything that touches the
tenant itself. Refusals raise :class:`~bananawiki.hosting.errors.ServiceError`
with a translation key; runtime failures become ``hosting.runtime.<code>``.

Statuses are those of 1.4: ``running``, ``stopped`` (paused, or not started yet),
``suspended`` (by an administrator; the expiry clock is frozen while
``suspended_at`` is set and the owner cannot act) and ``terminated`` (row
kept, name archived as ``<slug>--terminated-<id8>``; data retained until
``data_retained_until`` when a grace period is configured).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import socket
import sqlite3
import string
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from flask import current_app

from ..core.i18n import FALLBACK
from ..core.timeutil import MAX_YEAR, is_past, now_sql, parse, sql_in, to_sql, utcnow
from ..core.web import one_line
from . import accounts, certwarm, events, oauth, settings, urls
from .config import HostingConfig
from .db import db
from .errors import ServiceError
from .i18n import t_lang
from .runtime import Runtime, RuntimeFailure, TenantPolicy, TenantSpec, TtsGpu

log = logging.getLogger("bananawiki.hosting.instances")

STATUSES = ("running", "stopped", "suspended", "terminated")
FEATURES = {"public_access": "public_wiki_allowed", "page_builder": "page_builder_allowed"}
ADMIN_EXPIRY_DAYS = 365 * 100
RENEWAL_WINDOW_DAYS = 7
MAX_EXTENSION_SECONDS = 10 * 365 * 86400
LOG_NAMES = ("error.log", "access.log")
# Wiki lists ask several wikis for their health at once and accept storage measured up to a minute ago.
PROBE_WORKERS = 16
LISTED_USAGE_AGE = 60.0
_ID_ALPHABET = string.ascii_lowercase + string.digits
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_MARKER_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def provisioning_ready(inst: dict[str, Any]) -> bool:
    """Only completed data may be launched, modified or served."""
    return inst.get("provisioning_state", "ready") == "ready"


def require_ready(inst: dict[str, Any]) -> None:
    if not provisioning_ready(get(inst["id"]) or inst):
        raise ServiceError("hosting.instances.provisioning_incomplete")


@contextmanager
def _provisioning_lock(inst: dict[str, Any], *, wait: bool = False) -> Iterator[None]:
    """A crashed creator releases this host-only lock, but its row stays pending.

    Fixed stripes bound lock-file growth and coordinate independent portal/CLI
    workers. A busy stripe refuses cancellation rather than waiting on seed.
    """
    folder = Path(cfg().platform_state_dir) / ".provisioning-locks"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stripe = int(hashlib.sha256(inst["id"].encode()).hexdigest()[:8], 16) % 64
    lock = FileLock(str(folder / f"{stripe}.lock"), timeout=600 if wait else 0, mode=0o600)
    try:
        lock.acquire()
    except Timeout:
        raise ServiceError("hosting.instances.provisioning_busy") from None
    try:
        yield
    finally:
        lock.release()


PROVISIONING_STARTED = "instance.provisioning_started"
PROVISIONING_REFUSED = "instance.provisioning_refused"


def _refusal_marker(instance_id: str) -> Path:
    """The file that records, outside ``hosting.db``, that the runtime refused to create this wiki's data.

    It sits next to the provisioning lock stripes and is never removed: it
    is a few bytes per refused creation, keyed by the wiki's unique id, and
    it still protects the directory if ``hosting.db`` is restored from a
    backup taken before the refused row was terminated.
    """
    name = instance_id if _MARKER_NAME.fullmatch(instance_id) else hashlib.sha256(instance_id.encode()).hexdigest()
    return Path(cfg().platform_state_dir) / ".provisioning-refused" / name


def _mark_refused(inst: dict[str, Any], code: str) -> None:
    """Write the refusal marker atomically and durably, before any database statement about the refusal.

    A failing write is logged and the refusal goes on to the database: the
    directory is then lost only if the database writes fail as well.
    """
    from ..ops.files import atomic_write  # POSIX only, like the hosting platform

    try:
        atomic_write(_refusal_marker(inst["id"]),
                     f"{code} {now_sql()} {inst['subdomain']} ({inst.get('domain_mode') or 'hosting'})\n")
    except (OSError, ValueError) as error:
        log.error("Could not record outside the database that the runtime refused to create wiki %s: %s",
                  inst["id"], error)


def _refusal_marked(instance_id: str) -> bool:
    """Whether the refusal marker exists.

    A check that fails is raised: the caller then neither deletes the data nor
    releases the reservation (its container may still run), and a later cancel
    retries.
    """
    try:
        os.lstat(_refusal_marker(instance_id))
    except FileNotFoundError:
        return False
    except OSError as error:
        log.error("Cannot check the refusal marker of wiki %s: %s", instance_id, error)
        raise
    return True


def _provision(inst: dict[str, Any], operation: Callable[[TenantSpec], None]) -> None:
    """Reserve initialization until all seed/copy/import and startup work is done."""
    with _provisioning_lock(inst, wait=True):
        fresh = get(inst["id"])
        if not fresh or fresh["status"] != "stopped" or fresh["provisioning_state"] != "pending":
            raise ServiceError("hosting.instances.changed_state")
        initial_spec = spec(fresh)
        # Recorded before the runtime may create anything under this wiki's
        # name: a creation cancelled without it never reached the runtime, so
        # whatever sits under the name is not its own (see _discard_failed).
        events.record("instance", inst["id"], PROVISIONING_STARTED)
        try:
            operation(initial_spec)
        except RuntimeFailure as error:
            _discard_failed(inst, error)
            raise _fail(error) from error
        except Exception as error:
            # The runtime contract counts any other exception as "failed".
            log.exception("Creating wiki %s failed unexpectedly", inst["id"])
            failure = RuntimeFailure("failed", str(error)[:300])
            _discard_failed(inst, failure)
            raise _fail(failure) from error
        try:
            # Global settings or the account's privileges may have changed
            # while the slow seed/copy ran. Complete with the current policy.
            settings.load(fresh=True)
            current_spec = spec(get(inst["id"]) or inst)
            if current_spec.policy != initial_spec.policy:
                runtime().apply_limits(current_spec)
                runtime().restart(current_spec, force=True)
        except RuntimeFailure as error:
            # The wiki was created and started: whatever this step reports,
            # the data under its name is its own.
            _discard_failed(inst)
            raise _fail(error) from error
        with db.transaction():
            owner = accounts.get(inst["account_id"])
            allowed = (owner and not owner.get("deleted_at") and not owner.get("suspended")
                       and owner.get("approval_status") == "approved")
            changed = allowed and db.update(
                "instances", {"status": "running", "stopped_at": None, "provisioning_state": "ready"},
                "id = ? AND status = 'stopped' AND provisioning_state = 'pending'", (inst["id"],))
        if not changed:
            _discard_failed(inst)
            raise ServiceError("hosting.instances.changed_state")


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


def address_conflicts(suffix: str) -> list[dict[str, Any]]:
    """The live wikis that would lose their address with *suffix* (``""``: none) as the instance suffix.

    Their host would also be another wiki's, or their name would be the
    suffix, which is reserved.
    """
    rows = db.all("SELECT * FROM instances WHERE status != 'terminated' ORDER BY created_at, id")
    hosts = {row["id"]: urls.platform_host(row, suffix=suffix) for row in rows}
    taken = Counter(hosts.values())
    return [row for row in rows if hosts[row["id"]]
            and (taken[hosts[row["id"]]] > 1 or (suffix and row["subdomain"].lower() == suffix))]


def owned_by(account_id: str) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM instances WHERE account_id = ? AND status != 'terminated' ORDER BY created_at DESC",
                  (account_id,))


def generate_password(length: int = 12) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _fail(error: RuntimeFailure) -> ServiceError:
    log.warning("Runtime operation failed: %s", error)
    return ServiceError(f"hosting.runtime.{error.code}")


def _each(rows: list[dict[str, Any]], action: Callable[[dict[str, Any]], Any], failure: str,
          step: str | None = None) -> int:
    """Apply *action* to every row on its own and count the rows it handled (False skips one).

    A failing row is logged and the rest still run: a wiki that keeps failing
    (a refusal, a runtime or file error) is retried on a later maintenance
    pass instead of holding up every wiki after it. With *step*, a row that
    failed several passes in a row waits between attempts (see
    :func:`maintenance_due`).
    """
    count = 0
    for row in rows:
        failures = maintenance_due("instance", row["id"], step) if step else 0
        if failures is None:
            continue
        try:
            handled = action(row) is not False
        except Exception as error:  # noqa: BLE001 - one wiki must not block the others
            if isinstance(error, (ServiceError, RuntimeFailure)):
                log.warning(failure, row["id"], getattr(error, "key", error))
            else:
                log.exception(failure, row["id"], error)
            if step:
                maintenance_result("instance", row["id"], step, failures, error)
            continue
        if handled:
            count += 1
        if step:
            maintenance_result("instance", row["id"], step, failures)
    return count


RETRY_AFTER_FAILURES = 3
RETRY_WAIT = timedelta(minutes=15)
RETRY_WAIT_MAX = timedelta(days=1)


def retry_wait(failures: int) -> timedelta:
    """How long a maintenance row waits after *failures* failures in a row."""
    if failures < RETRY_AFTER_FAILURES:
        return timedelta(0)
    return min(RETRY_WAIT_MAX, RETRY_WAIT * 2 ** min(failures - RETRY_AFTER_FAILURES, 10))


def maintenance_due(subject_type: str, subject_id: str, step: str) -> int | None:
    """The failures in a row of maintenance *step* on this row, or None while it waits.

    After ``RETRY_AFTER_FAILURES`` failures in a row the row is tried again
    only after :func:`retry_wait` (15 minutes, doubling up to a day), so a
    row that fails on every pass stops costing every pass its runtime
    timeouts and log lines. The count lives in the moderation log, where
    administrators see it: ``maintenance.<step>.failed`` events since the
    last ``maintenance.<step>.recovered``.
    """
    row = db.one(
        "SELECT COUNT(*) AS failures, MAX(created_at) AS latest FROM hosting_events "
        "WHERE subject_type = ? AND subject_id = ? AND action = ? AND id > COALESCE((SELECT MAX(id) "
        "FROM hosting_events WHERE subject_type = ? AND subject_id = ? AND action = ?), 0)",
        (subject_type, subject_id, f"maintenance.{step}.failed",
         subject_type, subject_id, f"maintenance.{step}.recovered"),
    )
    failures = int(row["failures"] or 0) if row else 0
    latest = parse(row["latest"]) if row else None
    if latest is not None and utcnow() - latest < retry_wait(failures):
        return None
    return failures


def maintenance_result(subject_type: str, subject_id: str, step: str, failures: int,
                       error: BaseException | None = None) -> None:
    """Record a failed attempt of maintenance *step* on this row, or the success that ends a run of them."""
    try:
        if error is not None:
            events.record(subject_type, subject_id, f"maintenance.{step}.failed", None,
                          getattr(error, "key", None) or f"{type(error).__name__}: {error}")
            if failures + 1 >= RETRY_AFTER_FAILURES:
                log.warning("Maintenance step %s failed %d times in a row for %s %s; next attempt in %s", step,
                            failures + 1, subject_type, subject_id, retry_wait(failures + 1))
        elif failures:
            events.record(subject_type, subject_id, f"maintenance.{step}.recovered")
    except sqlite3.Error:
        log.exception("Could not record the maintenance result of %s %s", subject_type, subject_id)


# Policy and specs


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
        rows = db.all("SELECT * FROM instances WHERE status = 'running' AND provisioning_state = 'ready'")
        result = runtime().sync_routes([spec(row, with_policy=False) for row in rows if domains.may_serve(row["id"])])
    except (RuntimeFailure, sqlite3.Error, ValueError) as error:
        log.warning("Could not publish tenant routes: %s", error)
        return
    if isinstance(result, dict) and result.get("changed") and not result.get("reloaded"):
        log.warning("Tenant routes were written but Caddy did not reload; new wikis stay unrouted until it does.")


# Creation


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


def on_probation(account: dict[str, Any]) -> bool:
    """Whether *account* is a new self-service account, held to the smaller limits for new accounts.

    That is an account created on the public sign-up form (it accepted the
    terms there), never approved by an administrator, and younger than
    ``HOSTING_NEW_ACCOUNT_DAYS``. Administrators, accounts an administrator
    created or approved, and older accounts use the normal limits.
    """
    days = cfg().limits.new_account_days
    if account["is_admin"] or days <= 0 or account.get("approved_by") or not account.get("terms_accepted_at"):
        return False
    created = parse(account.get("created_at"))
    return created is None or utcnow() - created < timedelta(days=days)


def account_limit(account: dict[str, Any]) -> int:
    """How many live wikis *account* may own (``MAX_INSTANCES_PER_ACCOUNT``, less while on probation)."""
    limits = cfg().limits
    if on_probation(account):
        return min(limits.max_per_account, limits.new_account_max_instances)
    return limits.max_per_account


def check_account_capacity(account: dict[str, Any]) -> None:
    """Refuse one more wiki for *account* (created or received) once it holds its limit.

    Suspended wikis count: suspending a wiki must not free a place for
    another one. Run it inside the transaction that adds the wiki.
    """
    if account["is_admin"]:
        return
    held = db.scalar("SELECT COUNT(*) FROM instances WHERE account_id = ? "
                     "AND status IN ('running', 'stopped', 'suspended')", (account["id"],), default=0)
    limit = account_limit(account)
    if held >= limit:
        if limit < cfg().limits.max_per_account:
            raise ServiceError("hosting.instances.new_account_limit", max=limit,
                               days=cfg().limits.new_account_days)
        raise ServiceError("hosting.instances.account_limit", max=limit)


def _check_capacity(account: dict[str, Any]) -> None:
    """Check count limits while holding the transaction that reserves a new row.

    With the platform limit on, accounts on probation (see
    :func:`on_probation`) share at most ``HOSTING_NEW_ACCOUNT_SHARE_PERCENT``
    of it: a burst of throwaway sign-ups can never fill the whole platform.
    """
    if account["is_admin"]:
        return
    settings.load(fresh=True)  # preflight may have cached policy before this write lock
    check_account_capacity(account)
    if settings.flag("global_limit_enabled", True):
        maximum = int(settings.get("global_limit_max_instances", 50))
        total = db.scalar("SELECT COUNT(*) FROM instances WHERE status IN ('running', 'stopped')", default=0)
        if total >= maximum:
            log.warning("The platform's wiki limit (%d) is reached; refusing a new wiki", maximum)
            raise ServiceError("hosting.instances.platform_full")
        limits = cfg().limits
        if on_probation(account) and limits.new_account_days > 0:
            share = maximum * limits.new_account_share_percent // 100
            newcomers = db.scalar(
                "SELECT COUNT(*) FROM instances i JOIN accounts a ON a.id = i.account_id "
                "WHERE i.status IN ('running', 'stopped') AND a.is_admin = 0 AND a.approved_by IS NULL "
                "AND a.terms_accepted_at IS NOT NULL AND a.created_at > ?",
                (sql_in(days=-limits.new_account_days),), default=0)
            if newcomers >= share:
                log.warning("New accounts hold %d of the %d wikis they may share; refusing another", newcomers,
                            share)
                raise ServiceError("hosting.instances.new_accounts_full")


def _check_storage_capacity(account: dict[str, Any]) -> None:
    """Sample storage outside the write lock; host quotas enforce the hard boundary."""
    if (not account["is_admin"] and settings.flag("global_limit_enabled", True)
            and platform_usage_mb() >= int(settings.get("global_limit_max_storage_mb", 5000))):
        raise ServiceError("hosting.instances.platform_storage_full")


def default_expiry() -> str | None:
    """The expiry of a customer's wiki from now: ``INSTANCE_DURATION_DAYS``, or None when that is 0 (never)."""
    days = cfg().limits.duration_days
    return sql_in(days=days) if days > 0 else None


def _insert(account: dict[str, Any], slug: str, domain_mode: str, *, admin_username: str, custom_credentials: bool,
            easy_wiki: bool, use_case: str) -> dict[str, Any]:
    if name_holder(slug, domain_mode):
        raise ServiceError("hosting.instances.name_taken")
    admin_owner = bool(account["is_admin"])
    if admin_owner:
        expires = sql_in(days=ADMIN_EXPIRY_DAYS) if domain_mode != "apex" else None
    else:
        expires = default_expiry()
    instance_id = _new_id()
    try:
        with db.transaction():
            db.insert("instances", {
                "id": instance_id, "account_id": account["id"], "subdomain": slug, "status": "stopped",
                "provisioning_state": "pending", "stopped_at": now_sql(),
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
    _check_storage_capacity(account)
    custom = bool(admin_password)
    username = (admin_username or "").strip() or f"admin_{secrets.token_hex(3)}"
    if custom:
        accounts.check_new_password(admin_password)
        accounts.check_username(username)
    password = admin_password if custom else generate_password()
    # Count admission and insertion share one cross-worker SQLite write lock.
    # Commit before external provisioning: this row reserves capacity while the
    # runtime is busy, or if this process is interrupted after the commit.
    with db.transaction():
        _check_capacity(account)
        inst = _insert(account, slug, domain_mode, admin_username=username, custom_credentials=custom,
                       easy_wiki=easy_wiki, use_case=use_case)
    _provision(inst, lambda target: runtime().provision(target, admin_username=username, admin_password=password,
                                                       force_password_change=not custom))
    events.record("instance", inst["id"], "instance.created", account["id"], slug)
    sync_routes()
    certwarm.schedule(urls.platform_host(inst))
    return get(inst["id"]), username, password  # type: ignore[return-value]


def _discard_failed(inst: dict[str, Any], error: RuntimeFailure | None = None) -> None:
    """Remove a wiki whose provisioning failed: terminate the row, drop the data it created.

    *error* is the failure the runtime operation raised, or None when the
    creation was cancelled, its owner lost the right to it or a later step
    failed. Data under the wiki's name is destroyed only when it can be this
    wiki's own:

    * ``data_exists``: the runtime refused because the directory belongs to
      someone else.
    * Never started (no ``instance.provisioning_started`` event): the runtime
      was never asked to create anything.

    In both cases the directory is left alone and the row is terminated at
    once, giving up its name, port and capacity. Otherwise the runtime may
    have left a directory or container behind; while they cannot be
    destroyed, the row keeps its reservation.
    """
    terminated = {"status": "terminated", "terminated_at": now_sql(), "port": None,
                  "subdomain": urls.archived_slug(inst["id"], inst["subdomain"]),
                  "terminated_reason": "provisioning_failed", "data_retained_until": None}
    refused = error is not None and error.code == "data_exists"
    if refused:
        # Marker file first, then the event: either one keeps a later
        # cancellation or expiry from deleting the directory, so a failing
        # event or row update cannot turn the refusal into a deletion.
        _mark_refused(inst, error.code)
        events.record("instance", inst["id"], PROVISIONING_REFUSED, None, error.code)
    if refused or not _runtime_may_own_data(inst["id"]):
        log.error("Wiki %s not created: the data directory under its name, if any, is not its own and was left "
                  "untouched", inst["id"])
        db.update("instances", {"provisioning_state": "failed", **terminated}, "id = ?", (inst["id"],))
        return
    db.update("instances", {"provisioning_state": "failed"}, "id = ?", (inst["id"],))
    try:
        runtime().destroy(spec(inst, with_policy=False))
    except RuntimeFailure as cleanup:
        # A failing runtime may have started a container before reporting an
        # error. Do not release its port/name/capacity while resources remain.
        log.error("Could not clean up failed wiki %s; retaining its resource reservation: %s", inst["id"],
                  cleanup)
        try:
            runtime().stop(spec(inst, with_policy=False))
        except RuntimeFailure as stop_error:
            db.update("instances", {"status": "running"}, "id = ?", (inst["id"],))
            log.error("Failed wiki %s could not be verified stopped; its container may remain active: %s",
                      inst["id"], stop_error)
        else:
            db.update("instances", {"status": "stopped", "stopped_at": now_sql()}, "id = ?", (inst["id"],))
        return
    db.update("instances", terminated, "id = ?", (inst["id"],))


def _runtime_may_own_data(instance_id: str) -> bool:
    """Whether the runtime was asked to create this wiki's data and did not refuse because it existed.

    The refusal marker and the ``instance.provisioning_refused`` event count
    the same: either one means the data under the name is not this wiki's.
    """
    if _refusal_marked(instance_id):
        return False
    actions = set(db.column(
        "SELECT action FROM hosting_events WHERE subject_type = 'instance' AND subject_id = ? AND action IN (?, ?)",
        (instance_id, PROVISIONING_STARTED, PROVISIONING_REFUSED)))
    return PROVISIONING_STARTED in actions and PROVISIONING_REFUSED not in actions


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
        log.error("Data of wiki %s is stuck in %s (expected %s): %s", one_line(instance_id), one_line(moved_to),
                  one_line(expected), one_line(error))


def _undo_move(inst: dict[str, Any], moved_to: str, was_running: bool) -> None:
    """Put a wiki back as it was when its row could not take the name its data moved to."""
    _settle_data(inst["id"], moved_to)
    _start_again(inst, was_running)


def _start_again(inst: dict[str, Any], was_running: bool) -> None:
    """Start a wiki a failed rename stopped under its old name; one that does not start is recorded as stopped."""
    if not was_running:
        return
    try:
        runtime().start(spec(inst))
    except RuntimeFailure as error:
        log.warning("Wiki %s stays stopped: %s", inst["id"], error)
        db.update("instances", {"status": "stopped", "stopped_at": now_sql()}, "id = ?", (inst["id"],))
        sync_routes()


# Pause, resume, restart


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
    require_ready(inst)
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
    # A suspension (or stop) that landed while the container was starting
    # already stopped it, before this start finished: stop it again.
    current = get(inst["id"])
    if current is None or current["status"] != "running":
        try:
            runtime().stop(spec(current or inst, with_policy=False))
        except RuntimeFailure as error:
            log.error("Wiki %s changed state while starting and could not be stopped: %s", inst["id"], error)
        sync_routes()
        raise ServiceError("hosting.instances.changed_state")
    events.record("instance", inst["id"], "instance.started", actor_id)
    sync_routes()


def restart(inst: dict[str, Any], *, actor_id: str | None) -> None:
    """Restart a running wiki with its current policy (clears stale state)."""
    require_ready(inst)
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
    if not provisioning_ready(inst):
        return False
    if inst["status"] == "terminated":
        return True
    try:
        runtime().apply_limits(spec(inst))
        if inst["status"] == "running":
            # Not an explicit start: a wiki an update isolated stays stopped.
            runtime().restart(spec(inst), force=True, policy_change=True)
    except RuntimeFailure as error:
        log.warning("Policy for %s not applied: %s", inst["id"], error)
        if inst["status"] == "running":
            try:
                stop(inst, actor_id=actor_id)
            except ServiceError:
                pass
        return False
    return True


# Suspension


def suspend(inst: dict[str, Any], *, actor_id: str | None, until: str | None = None, reason: str = "",
            reason_visible: bool = False, time_visible: bool = False, duration_label: str = "permanent") -> None:
    """Suspend (idempotent: an existing suspension window is not reset)."""
    require_ready(inst)
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
    require_ready(inst)
    if inst["status"] != "suspended":
        raise ServiceError("hosting.instances.not_suspended")
    with db.transaction():
        started = parse(inst.get("suspended_at"))
        delta = max(0, int((utcnow() - started).total_seconds())) if started else 0
        expires = parse(inst.get("expires_at"))
        # An expiry at the end of datetime's range keeps its date.
        credited = _shifted(expires, delta) if expires else None
        expires_at = to_sql(credited) if credited else inst.get("expires_at")
        db.update("instances", {
            "suspended_at": None, "suspended_until": None, "suspend_reason": "", "suspend_reason_visible": 0,
            "suspend_time_visible": 0, "status": "stopped",
            "suspended_accumulated_seconds": int(inst.get("suspended_accumulated_seconds") or 0) + delta,
            "expires_at": expires_at,
            # An end date an administrator brought forward moves with the credit and stays theirs.
            "admin_expiry": expires_at if renewal_locked(inst) else inst.get("admin_expiry"),
        }, "id = ?", (inst["id"],))
        _audit(inst["id"], "unsuspend", actor_id, None, False, False, None, None)
    start(get(inst["id"]), actor_id=actor_id)  # type: ignore[arg-type]


def suspension_history(instance_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT s.*, a.username AS performed_by_name FROM instance_suspension_audit s "
        "LEFT JOIN accounts a ON a.id = s.performed_by WHERE s.instance_id = ? ORDER BY s.created_at DESC, s.id DESC",
        (instance_id,),
    )


# The duration label of a wiki suspended together with its owner's account.
ACCOUNT_LABEL = "account"


def suspended_with_account(inst: dict[str, Any]) -> bool:
    """Whether the wiki's latest suspension is the one that came with its owner's account."""
    if inst["status"] != "suspended":
        return False
    latest = db.one("SELECT action, duration FROM instance_suspension_audit WHERE instance_id = ? "
                    "ORDER BY id DESC LIMIT 1", (inst["id"],))
    return latest is not None and latest["action"] == "suspend" and latest["duration"] == ACCOUNT_LABEL


def suspend_with_account(account_id: str, *, actor_id: str | None, until: str | None, reason: str,
                         reason_visible: bool, time_visible: bool) -> int:
    """Suspend an account's wikis with the account itself, until the same end date.

    A wiki already suspended on its own keeps that suspension; one suspended
    with an earlier account suspension takes the new end date.
    """
    count = 0
    for inst in owned_by(account_id):
        if not provisioning_ready(inst):
            continue
        if inst["status"] in ("running", "stopped") or suspended_with_account(inst):
            suspend(inst, actor_id=actor_id, until=until, reason=reason, reason_visible=reason_visible,
                    time_visible=time_visible, duration_label=ACCOUNT_LABEL)
            count += 1
    return count


def unsuspend_with_account(account_id: str, *, actor_id: str | None) -> tuple[int, int, int]:
    """Reopen the wikis suspended with the account: ``(restarted, still suspended, held for storage)``.

    Wikis an administrator suspended on their own stay suspended. Those the
    storage protection holds stay too, and reopen by themselves once they fit
    within their limit.
    """
    restarted = kept = held = 0
    for inst in owned_by(account_id):
        if inst["status"] != "suspended":
            continue
        if storage_suspended(inst):
            held += 1
            continue
        if not suspended_with_account(inst):
            kept += 1
            continue
        try:
            unsuspend(inst, actor_id=actor_id)
        except ServiceError:
            continue
        restarted += 1
    return restarted, kept, held


# Termination, restore, deletion


def terminate(inst: dict[str, Any], *, actor_id: str | None, reason: str = "manual") -> None:
    """Stop the wiki, archive its name and keep its data for the grace period.

    A wiki whose creation never finished (pending or failed) is cancelled
    instead: its partial data is destroyed, never kept as retained data, and
    its name, port and capacity are released only once that cleanup is done.
    Data under its name that the runtime never created for it (it was never
    asked to, or refused because the directory existed) is left untouched.

    Otherwise the data moves to the archived name before the row gives up the
    original one. When the move fails the wiki keeps its name (stopped) and
    the termination can simply be retried; data left under a released name
    would make the next wiki of that name fail and could no longer be
    restored. Without a grace period the archived data is then deleted; if
    that fails it stays under the archived name until an administrator
    deletes the wiki.
    """
    if not provisioning_ready(inst):
        with _provisioning_lock(inst):
            fresh = get(inst["id"])
            if not fresh or fresh["status"] == "terminated":
                raise ServiceError("hosting.instances.changed_state")
            if provisioning_ready(fresh):
                terminate(fresh, actor_id=actor_id, reason=reason)
                return
            # Cleanup must finish before an incomplete tenant releases its name,
            # port or capacity. It is never offered as retained recovery data.
            _discard_failed(fresh)
            if get(inst["id"])["status"] != "terminated":
                raise ServiceError("hosting.instances.provisioning_cleanup_failed")
        sync_routes()
        return
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
            db.execute("DELETE FROM instance_domain_claims WHERE instance_id = ?", (inst["id"],))
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


def deletable_wikis(account_id: str, *, transfer: bool = False) -> list[dict[str, Any]]:
    """The live wikis of an account about to be deleted, once none of them can stop the deletion half-way.

    Callers check this before they terminate or transfer any wiki, so a
    refusal leaves every wiki as it was: an unfinished creation cannot be
    transferred, and one still running cannot be cancelled until it ends.
    """
    owned = owned_by(account_id)
    for inst in owned:
        if provisioning_ready(inst):
            continue
        if transfer:
            raise ServiceError("hosting.accounts.wiki_unfinished", slug=inst["subdomain"])
        with _provisioning_lock(inst):  # refuses while its creator holds the lock
            pass
    return owned


def terminate_all(account_id: str, *, actor_id: str | None, reason: str = "account_deleted") -> None:
    """Terminate every wiki of an account that is being deleted.

    Unfinished creations are cancelled first: one whose cleanup fails stops
    the deletion before any live wiki has been terminated.
    """
    for inst in sorted(deletable_wikis(account_id), key=provisioning_ready):
        terminate(inst, actor_id=actor_id, reason=reason)


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
    if extend_days and extend_days > 0:
        expires = sql_in(days=extend_days)
    else:
        expires = sql_in(days=ADMIN_EXPIRY_DAYS) if owner_is_admin(inst) else default_expiry()
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
                "expires_at": None if mode == "apex" else expires, "terminated_at": None,
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
        runtime().destroy(spec(inst, with_policy=False))
        db.execute("DELETE FROM instances WHERE id = ?", (inst["id"],))
        return True

    rows = db.all(f"SELECT * FROM instances WHERE {_GRACE_DUE} ORDER BY data_retained_until, id", (now_sql(),))
    return _each(rows, purge, "Retained data of %s not deleted yet: %s", "purge_retained_data")


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


# Expiry


def set_expiry(inst: dict[str, Any], expires_at: str | None, *, actor_id: str) -> None:
    """Set (or with None remove) the expiry; a date already past terminates now.

    An administrator's decision (the portal and the command line come here).
    An end date brought forward, or set on a wiki that had none, is kept in
    ``admin_expiry``: while the wiki still ends then, nobody can renew
    it (:func:`renewal_locked`). A later date, or none, gives renewal back.
    """
    require_ready(inst)
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if (inst.get("domain_mode") or "hosting") == "apex" and expires_at is not None:
        raise ServiceError("hosting.instances.apex_perpetual")
    previous, moved = parse(inst.get("expires_at")), parse(expires_at)
    brought_forward = moved is not None and (previous is None or moved < previous)
    db.update("instances", {"expires_at": expires_at, "admin_expiry": expires_at if brought_forward else None},
              "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.expiry_changed", actor_id, expires_at or "")
    if expires_at and is_past(expires_at):
        terminate(get(inst["id"]), actor_id=actor_id, reason="expired")  # type: ignore[arg-type]


def _shifted(moment: datetime, seconds: int) -> datetime | None:
    """*moment* moved by *seconds*, or None past the ends of ``datetime``."""
    try:
        return moment + timedelta(seconds=seconds)
    except OverflowError:
        return None


def shift_expiry(inst: dict[str, Any], seconds: int, *, actor_id: str) -> None:
    """Extend (positive) or shorten (negative) the expiry by *seconds*.

    An extension past the year ``MAX_YEAR`` is refused rather than clamped:
    an expiry already that far (earlier releases accepted any year) keeps its
    date and the administrator is told why. Shortening it still works.
    """
    if (inst.get("domain_mode") or "hosting") == "apex":
        raise ServiceError("hosting.instances.apex_perpetual")
    seconds = max(-MAX_EXTENSION_SECONDS, min(MAX_EXTENSION_SECONDS, int(seconds)))
    base = parse(inst.get("expires_at")) or utcnow()
    if seconds > 0 and base < utcnow():
        base = utcnow()
    moved = _shifted(base, seconds)
    if moved is None or (seconds > 0 and parse(moved, bounded=True) is None):
        raise ServiceError("hosting.instances.expiry_out_of_range", max=MAX_YEAR)
    set_expiry(inst, to_sql(moved), actor_id=actor_id)


def terminate_expired() -> int:
    rows = db.all("SELECT * FROM instances WHERE status IN ('running', 'stopped') AND expires_at IS NOT NULL "
                  "AND expires_at <= ? ORDER BY expires_at, id", (now_sql(),))

    def expire(inst: dict[str, Any]) -> None:
        terminate(inst, actor_id=None, reason="expired")
        _notify_expired(get(inst["id"]) or inst)

    return _each(rows, expire, "Expired wiki %s not terminated: %s", "terminate_expired")


def _notify_owner(inst: dict[str, Any], kind: str, texts: Callable[[str], dict[str, str]] | None = None,
                  **values: Any) -> bool | None:
    """Email the wiki's owner in their own language (best effort; maintenance).

    *texts* returns the values that are sentences in that language. Returns
    whether the message went out, or None when there is nobody to email.
    """
    from . import notifications

    owner = accounts.get(inst["account_id"])
    if not owner or not owner.get("email") or owner.get("deleted_at") or not notifications.configured():
        return None
    lang = owner.get("language") or FALLBACK
    slug = urls.original_slug(inst["subdomain"]) or inst["subdomain"]
    return notifications.send(owner["email"], kind, username=owner["username"], lang=lang, slug=slug,
                              **(texts(lang) if texts else {}), **values)[0]


def _notify_expired(inst: dict[str, Any]) -> None:
    retained = inst.get("data_retained_until")

    def texts(lang: str) -> dict[str, str]:
        if retained:
            return {"retention": t_lang(lang, "email.instance_expired.retained", date=f"{retained[:16]} UTC",
                                        contact=cfg().contact_email)}
        return {"retention": t_lang(lang, "email.instance_expired.deleted")}

    _notify_owner(inst, "instance_expired", texts)


def warn_expiring() -> int:
    """Email owners ``HOSTING_EXPIRY_WARNING_DAYS`` days before their wiki expires (maintenance).

    Each threshold is announced once per expiry date: the date warned about
    and the smallest number of days announced are stored on the row, claimed
    before sending and given back when the message could not be sent (the
    next pass tries again). A new expiry date, after an extension or a
    renewal, starts over.
    """
    thresholds = cfg().limits.expiry_warning_days
    if not thresholds:
        return 0
    rows = db.all("SELECT * FROM instances WHERE status IN ('running', 'stopped') AND provisioning_state = 'ready' "
                  "AND expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ? ORDER BY expires_at, id",
                  (now_sql(), sql_in(days=max(thresholds))))

    def warn(inst: dict[str, Any]) -> bool:
        expires = parse(inst["expires_at"])
        if expires is None:
            return False
        left = expires - utcnow()
        due = min(days for days in thresholds if left <= timedelta(days=days))
        warned = inst.get("expiry_warning_days") if inst.get("expiry_warning_for") == inst["expires_at"] else None
        if warned is not None and warned <= due:
            return False
        claimed = db.execute("UPDATE instances SET expiry_warning_for = ?, expiry_warning_days = ? "
                             "WHERE id = ? AND expires_at = ?", (inst["expires_at"], due, inst["id"],
                                                                 inst["expires_at"])).rowcount
        if not claimed:
            return False
        renewal = renewal_offered() and not renewal_locked(inst)

        def texts(lang: str) -> dict[str, str]:
            if renewal:
                return {"keep": t_lang(lang, "email.expiry_warning.renew", days=cfg().limits.duration_days)}
            return {"keep": t_lang(lang, "email.expiry_warning.ask", contact=cfg().contact_email)}

        date = f"{inst['expires_at'][:16]} UTC"
        sent = _notify_owner(inst, "expiry_warning", texts, date=date, detail_value=date,
                             action_url=urls.external_url("dashboard.instance_detail", instance_id=inst["id"]))
        if sent is False:
            db.execute("UPDATE instances SET expiry_warning_for = ?, expiry_warning_days = ? WHERE id = ? "
                       "AND expiry_warning_for = ? AND expiry_warning_days = ?",
                       (inst.get("expiry_warning_for"), inst.get("expiry_warning_days"), inst["id"],
                        inst["expires_at"], due))
        return bool(sent)

    return _each(rows, warn, "Expiry warning for wiki %s not sent: %s")


def renewal_offered() -> bool:
    """Owners, and collaborators who may start and stop a wiki, can renew it (platform setting, and wikis expire at all)."""
    return settings.flag("owner_renewal_enabled") and cfg().limits.duration_days > 0


def renewal_locked(inst: dict[str, Any]) -> bool:
    """An administrator brought the wiki's end date forward and it still ends then (see :func:`set_expiry`)."""
    return bool(inst.get("admin_expiry")) and inst.get("admin_expiry") == inst.get("expires_at")


def renewable(inst: dict[str, Any]) -> bool:
    """The wiki may be renewed now: it expires within ``RENEWAL_WINDOW_DAYS``."""
    expires = parse(inst.get("expires_at"))
    return (renewal_offered() and expires is not None and provisioning_ready(inst)
            and inst["status"] in ("running", "stopped") and not renewal_locked(inst)
            and expires - utcnow() <= timedelta(days=RENEWAL_WINDOW_DAYS))


def may_renew(inst: dict[str, Any], account: dict[str, Any]) -> bool:
    """The owner, an administrator, or a collaborator allowed to start and stop the wiki."""
    from .collaborators import can

    return can(inst, account, "start_stop")


def renew(inst: dict[str, Any], *, actor_id: str) -> None:
    """Give the wiki a fresh ``INSTANCE_DURATION_DAYS`` from now (when the platform allows owners to).

    Only in the last ``RENEWAL_WINDOW_DAYS`` before expiry: a wiki stays
    alive while someone looks after it, and an abandoned one still expires.
    Never an end date an administrator brought forward (:func:`renewal_locked`).
    """
    require_ready(inst)
    if not renewal_offered() or not inst.get("expires_at"):
        raise ServiceError("hosting.instances.renewal_disabled")
    if inst["status"] not in ("running", "stopped"):
        raise ServiceError("hosting.instances.changed_state")
    if renewal_locked(inst):
        raise ServiceError("hosting.instances.renewal_admin_set")
    if not renewable(inst):
        raise ServiceError("hosting.instances.renewal_not_due", days=RENEWAL_WINDOW_DAYS)
    expires = default_expiry()
    changed = db.execute("UPDATE instances SET expires_at = ? WHERE id = ? AND expires_at = ? "
                         "AND status IN ('running', 'stopped')", (expires, inst["id"], inst["expires_at"])).rowcount
    if not changed:
        raise ServiceError("hosting.instances.changed_state")
    events.record("instance", inst["id"], "instance.renewed", actor_id, expires or "")


def lift_expired_suspensions() -> int:
    rows = db.all("SELECT * FROM instances WHERE status = 'suspended' AND suspended_until IS NOT NULL "
                  "AND suspended_until <= ? ORDER BY suspended_until, id", (now_sql(),))
    return _each(rows, lambda inst: unsuspend(inst, actor_id=None), "Timed suspension of %s not lifted: %s",
                 "lift_suspension")


# The automatic storage protection: the reason it gives (1.4 wrote the same words) and its duration label.
STORAGE_REASON = "Automatic storage protection:"
STORAGE_LABEL = "storage"
STORAGE_OVERRUN_RATIO = 1.10
_MB = 1024 * 1024


def storage_overrun(inst: dict[str, Any], overrun_ratio: float = STORAGE_OVERRUN_RATIO) -> tuple[float, int] | None:
    """``(MB used, MB allowed)`` while the wiki uses more than *overrun_ratio* times its limit, else None."""
    limit = storage_limit_mb(inst)
    if limit is None:
        return None
    used = usage_bytes(inst)
    if used <= limit * _MB * overrun_ratio:
        return None
    return used / _MB, limit


def enforce_storage_quotas(overrun_ratio: float = STORAGE_OVERRUN_RATIO) -> int:
    """Suspend non-admin wikis that materially exceed their storage cap, and tell their owners."""

    def check(inst: dict[str, Any]) -> bool:
        overrun = storage_overrun(inst, overrun_ratio)
        if overrun is None:
            return False
        used, limit = overrun
        suspend(inst, actor_id=None, reason=f"{STORAGE_REASON} {used:.1f} MB used, {limit} MB allowed.",
                reason_visible=True, duration_label=STORAGE_LABEL)
        _notify_owner(inst, "instance_storage_suspended", used=f"{used:.1f}", limit=str(limit))
        return True

    # An unfinished creation cannot be suspended; it is cancelled, never enforced.
    rows = db.all("SELECT * FROM instances WHERE status IN ('running', 'stopped') AND provisioning_state = 'ready' "
                  "ORDER BY created_at, id")
    return _each(rows, check, "Storage quota of %s not enforced: %s")


def storage_suspended(inst: dict[str, Any]) -> bool:
    """Whether the wiki's suspension is the automatic storage protection and nothing else.

    That is its latest suspension record: made by no one, labelled
    ``storage`` (or, from 1.4, giving the storage reason), with no end date.
    Any later suspension (by an administrator, of the owner's account) takes
    its place, and is never lifted by the storage check.
    """
    if inst["status"] != "suspended" or inst.get("suspended_until"):
        return False
    latest = db.one("SELECT action, duration, reason, performed_by FROM instance_suspension_audit "
                    "WHERE instance_id = ? ORDER BY id DESC LIMIT 1", (inst["id"],))
    return (latest is not None and latest["action"] == "suspend" and not latest["performed_by"]
            and (latest["duration"] == STORAGE_LABEL or (latest["reason"] or "").startswith(STORAGE_REASON)))


def lift_storage_suspension(inst: dict[str, Any], *, actor_id: str | None = None) -> bool:
    """Reopen a wiki suspended only for storage once it fits within its limit, and tell its owner.

    Within the limit itself, not the 10% margin, so that a wiki at the margin
    does not flap; a raised or removed limit counts. Nothing reopens while the
    owner's account is suspended, and a wiki whose usage cannot be measured
    stays suspended (the error is raised).
    """
    if not storage_suspended(inst):
        return False
    owner = accounts.get(inst["account_id"])
    if owner is None or owner.get("deleted_at") or accounts.is_suspended(owner):
        return False
    limit = storage_limit_mb(inst)
    used = int(runtime().usage(spec(inst, with_policy=False)))
    if limit is not None and used > limit * _MB:
        return False
    unsuspend(inst, actor_id=actor_id)

    def texts(lang: str) -> dict[str, str]:
        if limit is None:
            return {"usage": t_lang(lang, "email.instance_storage_reopened.unlimited", used=f"{used / _MB:.1f}")}
        return {"usage": t_lang(lang, "email.instance_storage_reopened.usage", used=f"{used / _MB:.1f}",
                                limit=str(limit))}

    _notify_owner(inst, "instance_storage_reopened", texts)
    return True


def lift_storage_suspensions() -> int:
    """Maintenance: reopen the wikis suspended for storage that fit within their limits again."""
    rows = db.all("SELECT * FROM instances WHERE status = 'suspended' AND provisioning_state = 'ready' "
                  "AND suspended_until IS NULL ORDER BY suspended_at, id")
    return _each(rows, lift_storage_suspension, "Storage suspension of %s not lifted: %s", "lift_storage_suspension")


# Names, owners and quotas


def rename(inst: dict[str, Any], slug: str, domain_mode: str, *, actor_id: str, auto_suffix: bool = False) -> dict[str, Any]:
    """Change a wiki's name and/or URL mode, moving its data directory."""
    require_ready(inst)
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
    except RuntimeFailure as error:
        raise _fail(error) from error
    try:
        runtime().relocate(inst["id"], old_spec.data_dir_name, new_dir)
    except RuntimeFailure as error:
        _start_again(inst, was_running)
        raise _fail(error) from error
    values: dict[str, Any] = {"subdomain": slug, "domain_mode": domain_mode}
    if domain_mode == "apex":
        values.update(expires_at=None, storage_limit_mb=0)
    elif old_mode == "apex":
        values["expires_at"] = sql_in(days=ADMIN_EXPIRY_DAYS) if admin_owner else default_expiry()
    try:
        db.update("instances", values, "id = ?", (inst["id"],))
    except sqlite3.Error as error:
        _undo_move(inst, new_dir, was_running)
        if isinstance(error, sqlite3.IntegrityError):
            raise ServiceError("hosting.instances.name_taken") from error
        raise
    db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ? AND verified_at IS NULL", (inst["id"],))
    db.execute("DELETE FROM instance_domain_claims WHERE instance_id = ?", (inst["id"],))
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
    require_ready(inst)
    if inst["status"] == "terminated":
        return
    if admin_owner:
        expires = None if (inst.get("domain_mode") or "hosting") == "apex" else sql_in(days=ADMIN_EXPIRY_DAYS)
        db.update("instances", {"expires_at": expires, "storage_limit_mb": 0}, "id = ?", (inst["id"],))
        return
    limit = inst.get("storage_limit_mb")
    db.update("instances", {
        "expires_at": default_expiry(),
        "storage_limit_mb": limit if limit and int(limit) > 0 else None,
    }, "id = ?", (inst["id"],))


def apply_owner_role(account_id: str, admin_owner: bool, *, actor_id: str | None) -> int:
    """After an account gains or loses administrator rights: its wikis' quotas and policies.

    Apex names are for administrators, so a former administrator's apex
    wikis move to hosting mode first. Returns how many moved.
    """
    renamed = 0
    for inst in owned_by(account_id):
        if not admin_owner and (inst.get("domain_mode") or "hosting") == "apex":
            inst = rename_for_non_admin(inst, actor_id=actor_id)
            renamed += 1
        apply_owner_quota(inst, admin_owner)
        apply_policy(inst, actor_id=actor_id)
    return renamed


def move_to_owner(inst: dict[str, Any], target: dict[str, Any], *, actor_id: str,
                  check_capacity: bool = False) -> list[str]:
    """Give a wiki to another account, applying that account's privileges.

    With *check_capacity* (an owner's transfer, not an administrator's move)
    the new owner's wiki limit applies, checked again in the transaction
    that changes the owner.

    Returns notes for the caller: ``"renamed"`` when an apex wiki had to move
    to hosting mode because the new owner is not an administrator.
    """
    require_ready(inst)
    if check_capacity:
        check_account_capacity(target)
    notes = []
    target_admin = bool(target["is_admin"])
    previous_admin = owner_is_admin(inst)
    if (inst.get("domain_mode") or "hosting") == "apex" and not target_admin:
        inst = rename_for_non_admin(inst, actor_id=actor_id)
        notes.append("renamed")
    with db.transaction():
        if check_capacity:
            check_account_capacity(target)
        db.update("instances", {"account_id": target["id"]}, "id = ?", (inst["id"],))
        db.execute("DELETE FROM instance_collaborators WHERE instance_id = ? AND account_id = ?",
                   (inst["id"], target["id"]))
        if previous_admin != target_admin:
            apply_owner_quota(get(inst["id"]), target_admin)  # type: ignore[arg-type]
        events.record("instance", inst["id"], "instance.owner_changed", actor_id, target["username"])
    apply_policy(get(inst["id"]), actor_id=actor_id)  # type: ignore[arg-type]
    return notes


def rename_for_non_admin(inst: dict[str, Any], *, actor_id: str | None) -> dict[str, Any]:
    """Move an apex wiki to hosting mode (apex names are for administrators)."""
    require_ready(inst)
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
    except RuntimeFailure as error:
        raise _fail(error) from error
    try:
        runtime().relocate(inst["id"], old_spec.data_dir_name, new_dir)
    except RuntimeFailure as error:
        _start_again(inst, inst["status"] == "running")
        raise _fail(error) from error
    try:
        db.update("instances", {"subdomain": slug, "domain_mode": "hosting",
                                "expires_at": default_expiry()}, "id = ?", (inst["id"],))
    except sqlite3.Error as error:
        _undo_move(inst, new_dir, inst["status"] == "running")
        if isinstance(error, sqlite3.IntegrityError):
            raise ServiceError("hosting.instances.name_taken") from error
        raise
    events.record("instance", inst["id"], "instance.renamed", actor_id, f"{base} (apex) -> {slug} (hosting)")
    return get(inst["id"])  # type: ignore[return-value]


def set_storage_limit(inst: dict[str, Any], limit_mb: int | None, *, actor_id: str) -> bool:
    require_ready(inst)
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if (inst.get("domain_mode") or "hosting") == "apex":
        limit_mb = 0
    db.update("instances", {"storage_limit_mb": limit_mb}, "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.storage_limit", actor_id, str(limit_mb))
    return apply_policy(inst, actor_id=actor_id)


def set_upload_policy(inst: dict[str, Any], size_mb: int | None, blocked: str, *, actor_id: str) -> bool:
    require_ready(inst)
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if size_mb is not None and not 1 <= size_mb <= 2048:
        raise ServiceError("hosting.instances.invalid_upload_size")
    db.update("instances", {"upload_max_size_mb": size_mb, "upload_blocked_extensions": ",".join(_extensions(blocked))},
              "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.upload_policy", actor_id)
    return apply_policy(inst, actor_id=actor_id)


def set_easy_wiki(inst: dict[str, Any], enable: bool, *, actor_id: str) -> None:
    require_ready(inst)
    if inst["status"] in ("terminated", "suspended"):
        raise ServiceError("hosting.instances.cannot_modify")
    if bool(inst.get("easy_wiki")) == enable:
        raise ServiceError("hosting.instances.mode_unchanged")
    db.update("instances", {"easy_wiki": 1 if enable else 0}, "id = ?", (inst["id"],))
    events.record("instance", inst["id"], "instance.easy_wiki." + ("on" if enable else "off"), actor_id)
    if not apply_policy(inst, actor_id=actor_id):
        raise ServiceError("hosting.instances.restart_failed")


def set_use_case(inst: dict[str, Any], text: str) -> None:
    require_ready(inst)
    text = (text or "").strip()
    if not 20 <= len(text) <= 2000:
        raise ServiceError("hosting.instances.use_case_length")
    db.update("instances", {"declared_use_case": text, "tos_compliance_declared_at": now_sql()}, "id = ?",
              (inst["id"],))


# Wiki recovery tools


def _live(inst: dict[str, Any]) -> None:
    require_ready(inst)
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")


def reset_admin_password(inst: dict[str, Any], *, actor_id: str) -> tuple[str, str]:
    """Return ``(username, password)`` to show once; nothing is stored.

    The wiki also turns off that account's two-step verification and ends its
    sessions (see ``reset_admin`` in :mod:`bananawiki.ops.tenant_task`): this
    is the owner's way back in when the authenticator app is lost as well.
    """
    _live(inst)
    password = generate_password()
    try:
        username = runtime().reset_admin_password(spec(inst, with_policy=False), inst.get("admin_username") or "admin",
                                                  password)
    except RuntimeFailure as error:
        raise _fail(error) from error
    events.record("instance", inst["id"], "instance.admin_password_reset", actor_id,
                  f"{username}: new password; two-step verification turned off; signed out everywhere")
    return username, password


def reset_content(inst: dict[str, Any], *, actor_id: str) -> tuple[str, str]:
    """Factory reset. Returns the new ``(username, password)`` to show once."""
    require_ready(inst)
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
    """The copy's row, with ``two_step_off``: the accounts whose two-step verification the copy turned off."""
    _live(inst)
    slug = urls.validate_slug(slug, account_is_admin=bool(owner["is_admin"]), domain_mode=domain_mode)
    copy = _insert(owner, slug, domain_mode, admin_username=inst.get("admin_username") or "admin",
                   custom_credentials=bool(inst.get("custom_credentials")), easy_wiki=bool(inst.get("easy_wiki")),
                   use_case=inst.get("declared_use_case") or "")
    two_step_off: list[str] = []
    _provision(copy, lambda target: two_step_off.extend(
        runtime().duplicate(spec(inst, with_policy=False), target) or ()))
    events.record("instance", copy["id"], "instance.duplicated", actor_id, inst["id"])
    sync_routes()
    return {**get(copy["id"]), "two_step_off": two_step_off}  # type: ignore[dict-item]


def import_archive(owner: dict[str, Any], slug: str, domain_mode: str, archive_path: Any, *,
                   actor_id: str) -> dict[str, Any]:
    """The new wiki's row, with ``two_step_off`` as for :func:`duplicate`."""
    slug = urls.validate_slug(slug, account_is_admin=bool(owner["is_admin"]), domain_mode=domain_mode)
    inst = _insert(owner, slug, domain_mode, admin_username="admin", custom_credentials=True, easy_wiki=False,
                   use_case="")
    two_step_off: list[str] = []
    _provision(inst, lambda target: two_step_off.extend(runtime().import_archive(target, archive_path) or ()))
    events.record("instance", inst["id"], "instance.imported", actor_id)
    sync_routes()
    return {**get(inst["id"]), "two_step_off": two_step_off}  # type: ignore[dict-item]


# Views for dashboards


def _runtime_spec(inst: dict[str, Any]) -> TenantSpec | None:
    try:
        return spec(inst, with_policy=False)
    except ValueError:
        return None


def _used(runtime_spec: TenantSpec | None, max_age: float | None = None) -> int:
    if runtime_spec is None:
        return 0
    try:
        return int(runtime().usage(runtime_spec, max_age=max_age))
    except (RuntimeFailure, ValueError):
        return 0


def _health(inst: dict[str, Any], runtime_spec: TenantSpec | None) -> str:
    """The runtime's state of a running wiki; ``unknown`` for any other."""
    if runtime_spec is None or inst["status"] != "running":
        return "unknown"
    try:
        return runtime().status(runtime_spec).state
    except (RuntimeFailure, ValueError):
        return "unknown"


def _describe_all(rows: list[dict[str, Any]], admin_owner: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """:func:`describe` of each row, asking up to ``PROBE_WORKERS`` wikis for their health at once.

    An unresponsive wiki holds up its health probe for the whole timeout: one
    after another, a few of them would add up to more than a request may take.
    Storage is measured meanwhile, one wiki after another (walking files gains
    nothing from threads), and may be up to ``LISTED_USAGE_AGE`` seconds old.
    """
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    specs = [_runtime_spec(row) for row in rows]

    def health(row: dict[str, Any], runtime_spec: TenantSpec | None) -> str:
        with app.app_context():
            return _health(row, runtime_spec)

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="probe") as pool:
        states = [pool.submit(health, row, runtime_spec) for row, runtime_spec in zip(rows, specs, strict=True)]
        used = [_used(runtime_spec, LISTED_USAGE_AGE) for runtime_spec in specs]
        return [describe(row, admin_owner=admin_owner(row), observed=(size, state.result()))
                for row, size, state in zip(rows, used, states, strict=True)]


def describe(inst: dict[str, Any], *, admin_owner: bool | None = None, with_status: bool = True,
             observed: tuple[int, str] | None = None) -> dict[str, Any]:
    """The row plus URL, time left, grace information, health and storage (*observed*: already measured)."""
    item = dict(inst)
    if inst["status"] != "terminated" and not provisioning_ready(inst):
        item["status"] = "provisioning" if inst["provisioning_state"] == "pending" else "provisioning_failed"
    item["url"] = urls.instance_url(inst) if inst["status"] != "terminated" and provisioning_ready(inst) else ""
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
    item["storage_suspended"] = inst["status"] == "suspended" and storage_suspended(inst)
    item["original_slug"] = urls.original_slug(inst["subdomain"]) or inst["subdomain"]
    limit = storage_limit_mb(inst, admin_owner)
    item["storage_limit_effective_mb"] = limit
    item["health"] = "unknown"
    item["storage_bytes"] = 0
    if observed is not None:
        item["storage_bytes"], item["health"] = observed
    elif with_status:
        runtime_spec = _runtime_spec(inst)
        item["storage_bytes"], item["health"] = _used(runtime_spec), _health(inst, runtime_spec)
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
    items = [{**item, "role": "owner"} for item in _describe_all(rows, lambda _row: bool(account["is_admin"]))]
    shared = db.all(
        "SELECT i.*, c.role AS collab_role, c.permissions AS collab_permissions, a.username AS owner_username, "
        "a.is_admin AS owner_admin FROM instance_collaborators c JOIN instances i ON i.id = c.instance_id "
        "JOIN accounts a ON a.id = i.account_id WHERE c.account_id = ? AND i.status != 'terminated' "
        "ORDER BY i.created_at DESC", (account["id"],),
    )
    items += [{**item, "role": "collaborator"} for item in _describe_all(shared, lambda row: bool(row["owner_admin"]))]
    return items


def admin_list() -> list[dict[str, Any]]:
    rows = db.all("SELECT i.*, a.username AS owner_username, a.is_admin AS owner_admin FROM instances i "
                  "JOIN accounts a ON a.id = i.account_id ORDER BY i.created_at DESC")
    return _describe_all(rows, lambda row: bool(row["owner_admin"]))


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
