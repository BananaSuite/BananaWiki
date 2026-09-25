"""Hosted instance lifecycle operations and compatibility exports.

Paths, environments, database preparation, archives, and read-only diagnostics
live in focused modules. This module coordinates provisioning and mutations.
"""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import hashlib
import json
import logging
import pathlib
import secrets
import shutil
import signal
import sqlite3
import stat
import string
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
import archive_format  # noqa: E402  (import after sys.path tweak)
from helpers._passwords import generate_password_hash  # noqa: E402
from helpers._translations import t  # noqa: E402
from . import config
from . import container_runtime
from .db import (
    create_instance as db_create_instance,
    get_instance,
    get_instances_for_account,
    count_active_instances,
    get_account_by_id,
    terminate_and_archive_instance,
    update_instance_status,
    get_expired_instances,
    get_all_active_instances,
    get_instance_by_subdomain,
    find_available_instance_subdomain,
    update_instance_identity,
    delete_instance,
    get_instances_with_expired_grace_period,
    restore_instance_in_place,
    get_grace_period_days,
    set_instance_upload_policy as db_set_instance_upload_policy,
    purge_deleted_account_tombstones,
)
from .subdomain import validate_subdomain
from .instance_paths import (
    _domain_mode_of as _domain_mode_of,
    _format_host_for_url as _format_host_for_url,
    _instance_bind_host as _instance_bind_host,
    _instance_db_path as _instance_db_path,
    _instance_dir as _instance_dir,
    _instance_dir_for as _instance_dir_for,
    _instance_url as _instance_url,
    _original_subdomain_from_archived as _original_subdomain_from_archived,
    _pid_file as _pid_file,
    _safe_instance_dir as _safe_instance_dir,
    _tts_worker_pid_file as _tts_worker_pid_file,
)
from .instance_environment import (
    _apply_upload_policy_to_instance_db as _apply_upload_policy_to_instance_db,
    connect_tenant_db as connect_tenant_db,
    guard_tenant_path as guard_tenant_path,
    instance_plugins_quarantined as instance_plugins_quarantined,
    iter_tenant_files as iter_tenant_files,
    open_tenant_dir as open_tenant_dir,
    platform_state_dir as platform_state_dir,
    plugin_snapshot_dir as plugin_snapshot_dir,
    read_tenant_file as read_tenant_file,
    set_instance_plugin_quarantine as set_instance_plugin_quarantine,
    snapshot_tenant_db as snapshot_tenant_db,
    write_tenant_file as write_tenant_file,
    _bounded_upload_size_mb as _bounded_upload_size_mb,
    _build_oauth_portal_base_url as _build_oauth_portal_base_url,
    _combined_upload_blocklist as _combined_upload_blocklist,
    _get_or_create_instance_oauth_credentials as _get_or_create_instance_oauth_credentials,
    _hosting_tts_allowed_for_instance as _hosting_tts_allowed_for_instance,
    _instance_env as _instance_env,
    _normalize_upload_extensions as _normalize_upload_extensions,
    _positive_float_env as _positive_float_env,
    _positive_int_env as _positive_int_env,
    _row_value as _row_value,
    get_effective_instance_upload_policy as get_effective_instance_upload_policy,
)
from .instance_database import (
    _HOSTING_DISABLE_TTS_AUTO_GENERATE_ON_START as _HOSTING_DISABLE_TTS_AUTO_GENERATE_ON_START,
    _INSTANCE_DB_PREPARE_TIMEOUT as _INSTANCE_DB_PREPARE_TIMEOUT,
    _apply_hosted_db_safety_defaults as _apply_hosted_db_safety_defaults,
    _attribute_wiki_db_pages_to_system as _attribute_wiki_db_pages_to_system,
    _instance_db_prepare_env as _instance_db_prepare_env,
    _prepare_instance_database as _prepare_instance_database,
    _reconstruct_wiki_db_from_json as _reconstruct_wiki_db_from_json,
    _seed_instance_db as _seed_instance_db,
    _seed_instance_db_raw as _seed_instance_db_raw,
)
from .instance_archives import (
    _SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES as _SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES,
    _archive_compress_type as _archive_compress_type,
    _archive_write_file as _archive_write_file,
    _drop_tts_cache_rows as _drop_tts_cache_rows,
    _hosting_export_compress_level as _hosting_export_compress_level,
    _hosting_export_store_extensions as _hosting_export_store_extensions,
    _hosting_export_store_threshold_bytes as _hosting_export_store_threshold_bytes,
    _hosting_import_limit as _hosting_import_limit,
    _hosting_import_work_dir as _hosting_import_work_dir,
    _validate_import_zip_members as _validate_import_zip_members,
    _zip_member_is_symlink as _zip_member_is_symlink,
    build_instance_archive as build_instance_archive,
)
from .instance_diagnostics import (
    _LOG_TAIL_BYTES as _LOG_TAIL_BYTES,
    _READABLE_INSTANCE_LOGS as _READABLE_INSTANCE_LOGS,
    _HEALTH_CACHE_TTL as _HEALTH_CACHE_TTL,
    _HEALTH_CHECK_HTTP_TIMEOUT as _HEALTH_CHECK_HTTP_TIMEOUT,
    _INSTANCE_OP_SEMAPHORE as _INSTANCE_OP_SEMAPHORE,
    _NEGATIVE_HEALTH_CACHE_TTL as _NEGATIVE_HEALTH_CACHE_TTL,
    _PROCESS_ALIVE_SOCKET_TIMEOUT as _PROCESS_ALIVE_SOCKET_TIMEOUT,
    _STORAGE_CACHE_TTL as _STORAGE_CACHE_TTL,
    _boot_id_path as _boot_id_path,
    _cache_lock as _cache_lock,
    _current_boot_id as _current_boot_id,
    _enrich_instance_for_dashboard as _enrich_instance_for_dashboard,
    _get_cached_health as _get_cached_health,
    _get_cached_storage as _get_cached_storage,
    _health_cache as _health_cache,
    _instance_accepts_connections as _instance_accepts_connections,
    _instance_http_ready as _instance_http_ready,
    _instance_storage_limit_mb as _instance_storage_limit_mb,
    _invalidate_instance_caches as _invalidate_instance_caches,
    _is_our_instance_process as _is_our_instance_process,
    _is_our_tts_worker_process as _is_our_tts_worker_process,
    _is_port_accepting as _is_port_accepting,
    _is_process_alive as _is_process_alive,
    _read_pid as _read_pid,
    _read_pid_path as _read_pid_path,
    _read_proc_cmdline as _read_proc_cmdline,
    _read_proc_environ as _read_proc_environ,
    _read_stored_boot_id as _read_stored_boot_id,
    _read_tts_worker_pid as _read_tts_worker_pid,
    _set_cached_health as _set_cached_health,
    _set_cached_storage as _set_cached_storage,
    _storage_cache as _storage_cache,
    _write_boot_id as _write_boot_id,
    check_instance_health as check_instance_health,
    check_instance_process_alive as check_instance_process_alive,
    get_admin_instances as get_admin_instances,
    get_dashboard_instances as get_dashboard_instances,
    get_instance_detail as get_instance_detail,
    get_platform_stats as get_platform_stats,
    get_storage_bytes as get_storage_bytes,
    read_instance_analytics as read_instance_analytics,
    read_instance_log as read_instance_log,
)


logger = logging.getLogger("hosting.instance_manager")


_STARTUP_TIMEOUT = float(config.INSTANCE_STARTUP_TIMEOUT_SECONDS)


_STARTUP_CHECK_INTERVAL = 0.25


_HTTP_READY_TIMEOUT = float(config.INSTANCE_STARTUP_TIMEOUT_SECONDS)


_HTTP_READY_INTERVAL = 0.5


_START_ATTEMPTS = 2


_PORT_RELEASE_TIMEOUT = 20.0


_INSTANCE_GUNICORN_WORKERS = _positive_int_env(
    "BW_HOSTING_INSTANCE_GUNICORN_WORKERS", 1, minimum=1, maximum=8,
)


_INSTANCE_GUNICORN_THREADS = _positive_int_env(
    "BW_HOSTING_INSTANCE_GUNICORN_THREADS", 4, minimum=1, maximum=32,
)


_RECOVERY_MAX_WORKERS = _positive_int_env(
    "BW_HOSTING_RECOVERY_MAX_WORKERS", 2, minimum=1, maximum=16,
)


_TTS_WORKER_POLL_INTERVAL = _positive_float_env(
    "BW_HOSTING_TTS_WORKER_POLL_INTERVAL_SECONDS", 30.0, minimum=1.0,
    maximum=300.0,
)


def _cleanup_failed_import_instance(inst, subdomain, domain_mode):
    """Best-effort hard rollback for an import that never completed."""
    if not inst:
        return
    data_dir = _instance_dir(subdomain, domain_mode)
    try:
        port = inst["port"]
    except (KeyError, TypeError):
        port = None
    try:
        _stop_process(data_dir, timeout=5, port=port)
    except Exception:
        logger.exception("Failed to stop partially imported instance %s", inst)
    _invalidate_instance_caches(subdomain)
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir, ignore_errors=True)
    try:
        delete_instance(inst["id"])
    except Exception:
        logger.exception("Failed to delete partial import row %s", inst)


def _generate_temp_password(length=12):
    """Generate a random password suitable for temporary credentials."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _generate_temp_username():
    """Generate a random admin username for a new instance."""
    return "admin"


def _create_instance_dirs(data_dir):
    """Create the directory tree for a new instance."""
    for subdir in ("", "storage"):
        os.makedirs(os.path.join(data_dir, subdir), exist_ok=True)
    for name in (
        "uploads",
        "attachments",
        "chat_attachments",
        "kanban_attachments",
        "custom_page_files",
    ):
        target = os.path.join("storage", name)
        target_abs = os.path.join(data_dir, target)
        os.makedirs(target_abs, exist_ok=True)
        legacy_abs = os.path.join(data_dir, name)
        if not os.path.lexists(legacy_abs):
            try:
                os.symlink(target, legacy_abs)
            except OSError:
                # Symlink can fail on some filesystems; fall back to a
                # normal directory to keep provisioning portable.
                os.makedirs(legacy_abs, exist_ok=True)


def _wait_for_port_free(port, timeout=5):
    """Wait for a released port without signalling an unidentified process."""
    import socket

    deadline = time.monotonic() + timeout
    while True:
        available = True
        for address in ("0.0.0.0", "127.0.0.1"):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind((address, port))
            except OSError:
                available = False
                break
        if available:
            return True
        if time.monotonic() >= deadline:
            logger.warning("Port %d is still occupied. Its process was not signalled; review the port assignment before restarting.", port)
            return False
        time.sleep(min(0.3, max(0, deadline - time.monotonic())))


def _spawn_post_restart_health_watch(inst, *, domain_mode="hosting", grace_seconds=None):
    """Probe readiness in the background, recovering after the startup deadline."""
    if grace_seconds is None:
        grace_seconds = _HTTP_READY_TIMEOUT
    try:
        inst_dict = dict(inst)
    except (TypeError, ValueError):
        return None
    try:
        subdomain = inst_dict["subdomain"]
        if inst_dict.get("port") is None:
            return None
    except (KeyError, TypeError):
        return None

    def _watch():
        try:
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                try:
                    if _instance_http_ready(inst_dict, timeout=2.0):
                        return
                except Exception:
                    pass
                time.sleep(1.0)
            # Still not ready: ask the subdomain proxy's lazy-recovery
            # worker to escalate (force-restart on next attempt).
            try:
                from ._subdomain_proxy import _trigger_lazy_recovery
                _trigger_lazy_recovery(subdomain, domain_mode)
                logger.warning(
                    "Post-restart instance '%s' was not HTTP-ready within "
                    "%.0f s; triggered lazy recovery.",
                    subdomain, grace_seconds,
                )
            except Exception:
                logger.exception(
                    "Failed to trigger lazy recovery for '%s' after restart",
                    subdomain,
                )
        except Exception:
            logger.exception(
                "Unexpected error in post-restart health watch for '%s'",
                inst_dict.get("subdomain"),
            )

    t = threading.Thread(
        target=_watch,
        name=f"post-restart-health-{subdomain}",
        daemon=True,
    )
    t.start()
    return t


def _clean_stale_pid(data_dir, port=None):
    """Remove the PID file if it does not belong to a live Gunicorn we own.

    Stale PID files arise in two scenarios:

    * The instance crashed or was ``SIGKILL``-ed and the PID file was
      never cleaned up.
    * The host VPS was rebooted.  After a reboot the kernel reuses low
      PIDs (systemd is PID 1, kthreadd is 2, dbus/sshd land in the
      double digits …) so the recorded numeric PID often happens to be
      alive but is no longer our Gunicorn.

    Both cases produce the same user-visible symptom: the instance row
    stays ``running``, the proxy classifies it as ``recovering``, and
    health checks return False forever because no Gunicorn is bound to
    the instance's port.  This routine deletes the PID file unless
    :func:`_is_our_instance_process` confirms the recorded PID still
    points at our Gunicorn.
    """
    pid = _read_pid(data_dir)
    if pid is None:
        return
    if _is_our_instance_process(pid, data_dir, port=port):
        return
    pid_path = _pid_file(data_dir)
    try:
        os.unlink(pid_path)
    except FileNotFoundError:
        pass
    # The boot-id sidecar is meaningful only as long as the PID file it
    # was written next to exists, so drop it as well to avoid leaking
    # stale state across recoveries.
    boot_id_path = _boot_id_path(data_dir)
    try:
        os.unlink(boot_id_path)
    except FileNotFoundError:
        pass
    logger.debug("Removed stale PID file for pid %d in %s", pid, data_dir)


def _clean_stale_tts_worker_pid(data_dir):
    """Remove the TTS worker PID file unless it points at this instance."""
    pid = _read_tts_worker_pid(data_dir)
    if pid is None:
        return
    if _is_our_tts_worker_process(pid, data_dir):
        return
    try:
        os.unlink(_tts_worker_pid_file(data_dir))
    except FileNotFoundError:
        pass
    logger.debug("Removed stale TTS worker PID file for pid %d in %s", pid, data_dir)


def _resolve_worker_tmp_dir():
    """Return a tmpfs path suitable for Gunicorn's heartbeat files.

    Gunicorn writes per-worker heartbeat files to ``--worker-tmp-dir``
    (default ``/tmp``).  When the underlying filesystem is slow, full,
    or mounted with ``noatime``/``relatime`` quirks, those writes can
    stall long enough that the arbiter believes a worker is dead and
    sends ``SIGKILL``, which the user observes as random 500s and
    long timeouts that recover "on their own".  Using ``/dev/shm``
    (tmpfs in RAM) avoids the disk entirely and is the standard
    Gunicorn-on-Linux deployment recommendation.
    """
    candidate = "/dev/shm"
    if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
        return candidate
    import sys
    print(
        "WARNING: /dev/shm not writable: Gunicorn heartbeats will use /tmp. "
        "Worker kills under disk I/O pressure are possible.",
        file=sys.stderr,
    )
    return None


def _start_process_once(inst, data_dir, *, await_http_ready=True, http_ready_timeout=None):
    """Spawn one Gunicorn process attempt for *inst*.

    Returns ``True`` on success.  The process PID is written to the
    instance data directory.  After the PID file appears, the function
    additionally waits for the instance to accept TCP connections so that
    callers can be confident the instance is actually ready to serve
    requests (not just that the master process is alive).

    ``await_http_ready`` controls whether Phase 2 (HTTP-readiness probe)
    runs at all.  Setting it to ``False`` returns success as soon as the
    Gunicorn master writes its PID: useful for the *create-instance*
    request path, where blocking the portal Gunicorn worker for the
    full HTTP-ready window (up to 30 s, plus retries) can exceed the
    portal worker timeout and surface as a bare ``502 Bad Gateway`` from
    nginx on the create POST itself.  The subdomain proxy's branded
    "Starting up…" splash (200 OK + auto-refresh) plus lazy recovery
    cover the gap between PID-alive and HTTP-ready in that case.
    ``http_ready_timeout`` (seconds) overrides ``_HTTP_READY_TIMEOUT``
    when ``await_http_ready=True``.

    *inst* may be a plain ``dict`` or a ``sqlite3.Row``.  The body uses
    ``.get(...)`` below, which ``sqlite3.Row`` does not support, so we
    normalise to a dict up-front.  Without this, ``restart_instance``,
    which passes the row straight through from ``get_instance``,
    crashed with ``AttributeError: 'sqlite3.Row' object has no
    attribute 'get'`` on every admin/user *restart instance* click.
    """
    if not isinstance(inst, dict):
        inst = dict(inst)

    port = int(inst["port"])
    if not (config.INSTANCE_PORT_START <= port < config.INSTANCE_PORT_END):
        logger.error("Port %d outside allowed range for instance %s", port, inst["id"])
        return False

    pid_path = _pid_file(data_dir)

    # Clean up stale PID files left over from a crash or VPS reboot so we
    # don't mistakenly return True when the recorded PID belongs to an
    # unrelated process.
    _clean_stale_pid(data_dir, port=port)

    # Check if already running.  ``_is_our_instance_process`` (rather than
    # the bare ``_is_process_alive``) is required here so that a stale PID
    # that happens to match an unrelated live process, e.g. systemd at
    # PID 1 after a reboot: does not cause us to skip the actual spawn
    # and leave the instance permanently unreachable.
    old_pid = _read_pid(data_dir)
    if _is_our_instance_process(old_pid, data_dir, port=port):
        _ensure_tts_worker_started(inst, data_dir)
        if not await_http_ready:
            return True

        # A live master/container is not necessarily a usable wiki.  In
        # particular, a wedged Gunicorn master can retain its PID while no
        # worker ever answers HTTP.  Returning True here used to make every
        # recovery attempt a no-op and left the proxy on "Starting up" until
        # an operator manually paused and resumed the instance.
        effective_timeout = (
            http_ready_timeout if http_ready_timeout is not None else _HTTP_READY_TIMEOUT
        )
        checks = max(1, int(effective_timeout / _HTTP_READY_INTERVAL))
        for _ in range(checks):
            if _instance_http_ready(inst):
                return True
            time.sleep(_HTTP_READY_INTERVAL)
        logger.warning(
            "Instance %s has a live process but did not become HTTP-ready "
            "within %ds; recycling it",
            inst["id"], effective_timeout,
        )
        return False

    subdomain = inst.get("subdomain")
    if subdomain:
        _invalidate_instance_caches(subdomain)

    env = _instance_env(data_dir, port, inst)

    if config.HOSTING_INSTANCE_RUNTIME == "docker":
        _wait_for_port_free(port, timeout=_PORT_RELEASE_TIMEOUT)
        try:
            pid = container_runtime.start_container(
                data_dir=data_dir,
                host_port=port,
                host_env=env,
                image=config.HOSTING_CONTAINER_IMAGE,
                internal_port=config.HOSTING_CONTAINER_INTERNAL_PORT,
                memory_mb=config.INSTANCE_MEMORY_LIMIT_MB,
                nofile_limit=config.INSTANCE_NOFILE_LIMIT,
                cpu_limit=config.INSTANCE_CPU_LIMIT,
                pids_limit=config.INSTANCE_PIDS_LIMIT,
            )
            if not pid:
                raise RuntimeError("tenant container did not expose a live init PID")
            # The container is already running here, so the tenant may have
            # planted a link under this name; write_tenant_file replaces the
            # entry instead of writing through it.
            write_tenant_file(data_dir, os.path.basename(pid_path), str(pid))
            _write_boot_id(data_dir)
        except Exception:
            logger.exception("Failed to start tenant container for instance %s", inst["id"])
            return False

        if not await_http_ready:
            return True
        effective_timeout = (
            http_ready_timeout if http_ready_timeout is not None else _HTTP_READY_TIMEOUT
        )
        checks = max(1, int(effective_timeout / _HTTP_READY_INTERVAL))
        for _ in range(checks):
            if not container_runtime.container_is_running(data_dir):
                return False
            if _instance_http_ready(inst):
                return True
            time.sleep(_HTTP_READY_INTERVAL)
        logger.error(
            "Tenant container for instance %s never became HTTP-ready within %ds",
            inst["id"], effective_timeout,
        )
        return False

    bind_host = _instance_bind_host()
    # Use gthread workers instead of the default sync workers.  The default
    # footprint is intentionally small for multi-tenant VPS hosting; operators
    # can raise BW_HOSTING_INSTANCE_GUNICORN_WORKERS / THREADS on larger hosts.
    cmd = [
        sys.executable, "-m", "gunicorn",
        "--config", "gunicorn.conf.py",
        "wsgi:app",
        "--bind", f"{bind_host}:{port}",
        "--reuse-port",
        "--workers", str(_INSTANCE_GUNICORN_WORKERS),
        "--worker-class", "gthread",
        "--threads", str(_INSTANCE_GUNICORN_THREADS),
        "--preload",
        # 120 s matches gunicorn.conf.py for the main wiki: needed for
        # PDF export of large pages and large attachment uploads.  At 30 s
        # those operations were getting SIGKILL'd, surfacing as the "page
        # took forever and then 500" symptom on managed wikis.
        "--timeout", "120",
        "--graceful-timeout", "10",
        # Recycle workers periodically so memory leaks don't make managed
        # wikis slow down over time.  Mirrors the per-process settings used
        # by the main wiki Gunicorn config.
        "--max-requests", "5000",
        "--max-requests-jitter", "500",
        "--pid", pid_path,
        "--daemon",
        "--access-logfile", os.path.join(data_dir, "access.log"),
        "--error-logfile", os.path.join(data_dir, "error.log"),
    ]
    worker_tmp_dir = _resolve_worker_tmp_dir()
    if worker_tmp_dir:
        cmd.extend(["--worker-tmp-dir", worker_tmp_dir])

    # Ensure the port is free before spawning Gunicorn.  After a stop/restart
    # the old process may still be shutting down, holding the TCP port open.
    # Wait for an owned previous process to release the port. A conflicting
    # listener belongs to the operator and must never be killed here.
    _wait_for_port_free(port, timeout=_PORT_RELEASE_TIMEOUT)

    try:
        subprocess.Popen(
            cmd,
            cwd=config.BW_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("Failed to start instance %s", inst["id"])
        return False

    # Phase 1: Wait for Gunicorn to write its PID file
    checks = int(_STARTUP_TIMEOUT / _STARTUP_CHECK_INTERVAL)
    pid_alive = False
    for _ in range(checks):
        time.sleep(_STARTUP_CHECK_INTERVAL)
        pid = _read_pid(data_dir)
        if pid and _is_process_alive(pid):
            pid_alive = True
            break

    if not pid_alive:
        logger.warning(
            "Instance %s Gunicorn did not write PID within %ds",
            inst["id"], _STARTUP_TIMEOUT,
        )
        return False

    # Persist the kernel boot id next to the freshly written PID file so
    # ``_is_our_instance_process`` can later detect PID files left over
    # from a previous boot (the recorded PID may match an unrelated
    # process after the reboot).
    _write_boot_id(data_dir)
    _ensure_tts_worker_started(inst, data_dir)

    # Phase 2: Wait for the instance to answer HTTP health checks.
    # The master process may be alive (PID written) but workers may not yet
    # have bound the listening socket, especially on first start when Python
    # bytecode compilation or schema migrations run inside --preload.
    #
    # Callers that cannot afford to block on this window (the synchronous
    # *create instance* request handler, in particular) pass
    # ``await_http_ready=False`` and rely on the subdomain proxy's
    # "Starting up…" splash (200 OK + auto-refresh) to bridge the gap.
    if not await_http_ready:
        return True

    effective_timeout = (
        http_ready_timeout if http_ready_timeout is not None else _HTTP_READY_TIMEOUT
    )
    checks = max(1, int(effective_timeout / _HTTP_READY_INTERVAL))
    for _ in range(checks):
        if _instance_http_ready(inst):
            return True
        time.sleep(_HTTP_READY_INTERVAL)

    logger.error(
        "Instance %s (port %d) PID is alive but the service never became "
        "reachable within %ds",
        inst["id"], port, effective_timeout,
    )
    return False


def _start_process(inst, data_dir, *, await_http_ready=True, http_ready_timeout=None):
    """Spawn a Gunicorn process for *inst*, retrying once on cold-start races.

    See :func:`_start_process_once` for the meaning of ``await_http_ready``
    and ``http_ready_timeout``.  These are forwarded unchanged so callers
    can opt into a PID-only "the master is alive" success contract on
    paths where blocking on HTTP readiness would exceed the portal
    Gunicorn worker timeout.
    """
    if not isinstance(inst, dict):
        inst = dict(inst)

    port = int(inst["port"])
    os.makedirs(data_dir, exist_ok=True)
    starting_lock = os.path.join(data_dir, ".starting")
    try:
        # Never open(starting_lock, "w"): a tenant can leave a link under
        # this name, and writing through it would truncate the file it
        # points at, such as the platform database.
        write_tenant_file(data_dir, ".starting", str(time.time()))
    except OSError:
        pass

    try:
        db_prep_ok = _prepare_instance_database(data_dir)
        if not db_prep_ok:
            logger.warning(
                "Instance %s database pre-flight failed; "
                "Gunicorn will attempt its own schema migrations on startup.",
                inst["id"],
            )
        policy = get_effective_instance_upload_policy(inst)
        try:
            _apply_upload_policy_to_instance_db(_instance_db_path(data_dir), policy)
        except Exception:
            if db_prep_ok:
                raise
            logger.warning(
                "Non-fatal: upload policy could not be applied to %s before start "
                "(DB pre-flight already failed; Gunicorn will retry on startup).",
                data_dir,
            )
        for attempt in range(1, _START_ATTEMPTS + 1):
            if _start_process_once(
                inst,
                data_dir,
                await_http_ready=await_http_ready,
                http_ready_timeout=http_ready_timeout,
            ):
                return True
            if _read_pid(data_dir) is None:
                return False
            _stop_process(data_dir, timeout=3, port=port)
            if attempt < _START_ATTEMPTS:
                logger.warning(
                    "Retrying instance %s startup after failed attempt %d/%d",
                    inst["id"], attempt, _START_ATTEMPTS,
                )
                time.sleep(1.0)
        return False
    finally:
        try:
            os.unlink(starting_lock)
        except OSError:
            pass


def _ensure_tts_worker_started(inst, data_dir):
    """Start the per-instance TTS worker if it is not already running."""
    if config.HOSTING_INSTANCE_RUNTIME == "docker":
        # The container entrypoint supervises its TTS worker in the same
        # cgroup and tenant boundary as the web process.
        return container_runtime.container_is_running(data_dir)
    if not isinstance(inst, dict):
        inst = dict(inst)
    _clean_stale_tts_worker_pid(data_dir)
    old_pid = _read_tts_worker_pid(data_dir)
    if _is_our_tts_worker_process(old_pid, data_dir):
        return True

    port = int(inst["port"])
    env = _instance_env(data_dir, port, inst)
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "scripts/tts_worker.py",
                "--poll-interval",
                str(_TTS_WORKER_POLL_INTERVAL),
            ],
            cwd=config.BW_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if hasattr(os, "setpriority") and hasattr(os, "PRIO_PROCESS"):
                os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
        except Exception:
            logger.debug("Could not lower TTS worker priority for pid %d", proc.pid, exc_info=True)
        write_tenant_file(
            data_dir, os.path.basename(_tts_worker_pid_file(data_dir)), str(proc.pid)
        )
        logger.info("Started TTS worker pid %d for instance %s", proc.pid, inst["id"])
        return True
    except Exception:
        logger.exception("Failed to start TTS worker for instance %s", inst.get("id"))
        return False


def _stop_tts_worker(data_dir, timeout=10):
    """Stop the per-instance TTS worker, if it is still running."""
    if config.HOSTING_INSTANCE_RUNTIME == "docker":
        return
    pid = _read_tts_worker_pid(data_dir)
    if pid is None:
        return
    if not _is_our_tts_worker_process(pid, data_dir):
        _clean_stale_tts_worker_pid(data_dir)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        _clean_stale_tts_worker_pid(data_dir)
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            break
        time.sleep(0.25)

    if _is_process_alive(pid) and _is_our_tts_worker_process(pid, data_dir):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.unlink(_tts_worker_pid_file(data_dir))
    except FileNotFoundError:
        pass


def _stop_process(data_dir, timeout=15, port=None):
    """Stop the Gunicorn process for an instance.

    Sends ``SIGTERM`` first.  If the process is still alive after
    *timeout* seconds, sends ``SIGKILL``.  After the process exits,
    cleans up the PID file and waits briefly for the OS to release the
    bound port so that the port can be re-allocated immediately.

    The PID is validated against :func:`_is_our_instance_process` before
    any signal is sent.  This is critical: after a VPS reboot or PID
    wrap-around the recorded PID may now belong to systemd, sshd,
    another wiki instance's Gunicorn master, or any other process
    owned by the hosting portal user.  Sending ``SIGTERM`` /
    ``SIGKILL`` to those unrelated processes would kill them instead
    of our instance: surfacing as random 500s on admin actions like
    "suspend instance" when the action collides with a stale PID file
    left over from before the reboot.
    """
    if config.HOSTING_INSTANCE_RUNTIME == "docker":
        if not container_runtime.stop_container(data_dir, timeout=timeout):
            raise RuntimeError("Could not confirm that the tenant container stopped. Check Docker before retrying.")
        for path in (_pid_file(data_dir), _boot_id_path(data_dir),
                     _tts_worker_pid_file(data_dir)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        if port is not None:
            _wait_for_port_free(int(port), timeout=_PORT_RELEASE_TIMEOUT)
        return

    _stop_tts_worker(data_dir)
    pid = _read_pid(data_dir)
    if pid is None:
        return
    if not _is_our_instance_process(pid, data_dir, port=port):
        # Either the process is dead or the PID has been reused by an
        # unrelated process; in both cases drop the stale sidecar files
        # without sending any signals.
        _clean_stale_pid(data_dir, port=port)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        _clean_stale_pid(data_dir, port=port)
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            break
        time.sleep(0.25)

    # Force kill if still running.  Re-validate ownership first so we
    # never escalate to SIGKILL against a process that started living
    # under this PID after we sent SIGTERM.
    if _is_process_alive(pid) and _is_our_instance_process(pid, data_dir, port=port):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # Wait briefly for the OS to reap the process and release its bound
    # ports.  SIGKILL is near-instantaneous, but worker processes that
    # inherited the listening socket may still hold it open for a moment.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            break
        time.sleep(0.1)

    _clean_stale_pid(data_dir, port=port)

    # Even after the master exits, worker processes may still hold the
    # listening socket for a short grace period.  Waiting here avoids
    # immediate restart races that surface as transient "Address already in
    # use" bind failures and short-lived downtime.
    if port is not None:
        bind_host = _instance_bind_host()
        connect_host = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
        if connect_host == "::":
            connect_host = "::1"
        deadline = time.monotonic() + _PORT_RELEASE_TIMEOUT
        while time.monotonic() < deadline:
            if not _is_port_accepting(connect_host, int(port), timeout=0.15):
                break
            time.sleep(0.1)


def _sha256_file(path):
    """Return the hex SHA-256 of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Platform plugin safety snapshots are copies of a tenant database that the
# portal itself takes and keeps under HOSTING_PLATFORM_STATE_DIR, outside the
# tenant mount.  An index.json next to them records each file's label,
# creation time and SHA-256, so a restore never depends on a file name or an
# mtime.  Labels:
#   pre-enable           taken on an operator's request while the wiki is in a
#                        known-good state; the only kind a restore uses
#   operator-quarantine  taken when plugins are quarantined, for forensics
#   before-restore       the database as it was just before a restore
_RESTORABLE_SNAPSHOT_LABELS = frozenset({"pre-enable"})
_SNAPSHOT_LABELS = _RESTORABLE_SNAPSHOT_LABELS | {"operator-quarantine", "before-restore"}
# Snapshots kept per label; older ones are deleted when a new one is taken.
_PLATFORM_SNAPSHOT_KEEP = 5
# A plugin.json larger than this is not read while quarantining.
_PLUGIN_MANIFEST_MAX_BYTES = 64 * 1024


def _snapshot_index_path(instance_id):
    return os.path.join(plugin_snapshot_dir(instance_id), "index.json")


def _snapshot_index_lock(instance_id):
    """Return a cross-process lock for one instance's snapshot index."""
    from filelock import FileLock

    snap_dir = plugin_snapshot_dir(instance_id)
    os.makedirs(snap_dir, mode=0o700, exist_ok=True)
    return FileLock(os.path.join(snap_dir, ".index.lock"), timeout=30)


def _read_snapshot_index(instance_id):
    """Return the host-owned snapshot index, or ``{}`` when absent/unreadable."""
    try:
        with open(_snapshot_index_path(instance_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_snapshot_index(instance_id, index):
    path = _snapshot_index_path(instance_id)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".index.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=1, sort_keys=True)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _capture_platform_snapshot(instance_id, db_path, data_dir, label):
    """Copy a tenant DB into the host-owned snapshot dir and record it.

    The copy is made by :func:`snapshot_tenant_db`, which refuses a planted
    link even while the tenant is running and checks the copy's integrity.
    Returns the snapshot's path.
    """
    if label not in _SNAPSHOT_LABELS:
        raise ValueError(f"Unknown snapshot label: {label}")
    snap_dir = plugin_snapshot_dir(instance_id)
    os.makedirs(snap_dir, mode=0o700, exist_ok=True)
    now = datetime.now(timezone.utc)
    fname = f"{now.strftime('%Y%m%dT%H%M%S_%fZ')}-{label}.db"
    snap_path = os.path.join(snap_dir, fname)
    snapshot_tenant_db(data_dir, db_path, snap_path)
    entry = {
        "sha256": _sha256_file(snap_path),
        "created_at": now.isoformat(),
        "label": label,
        "size": os.path.getsize(snap_path),
    }
    with _snapshot_index_lock(instance_id):
        index = _read_snapshot_index(instance_id)
        index[fname] = entry
        same_label = sorted(
            (meta.get("created_at") or "", name)
            for name, meta in index.items()
            if isinstance(meta, dict) and meta.get("label") == label
        )
        stale = [name for _created, name in same_label[:-_PLATFORM_SNAPSHOT_KEEP]]
        for name in stale:
            index.pop(name, None)
        _write_snapshot_index(instance_id, index)
    for name in stale:
        try:
            os.unlink(os.path.join(snap_dir, name))
        except OSError:
            pass
    return snap_path


def list_plugin_safety_snapshots(instance_id):
    """Return the platform snapshots recorded for an instance, newest first.

    Each item has ``name``, ``label``, ``created_at``, ``size`` and
    ``restorable``.  Only files that are still on disk are listed.
    """
    try:
        snap_dir = plugin_snapshot_dir(instance_id)
    except ValueError:
        return []
    items = []
    for name, meta in _read_snapshot_index(instance_id).items():
        if not isinstance(meta, dict):
            continue
        path = os.path.join(snap_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        items.append({
            "name": name,
            "label": meta.get("label") or "",
            "created_at": meta.get("created_at") or "",
            "size": meta.get("size") or 0,
            "restorable": meta.get("label") in _RESTORABLE_SNAPSHOT_LABELS,
        })
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items


def _external_plugin_ids(data_dir):
    """Return the plugin ids whose code sits under the tenant's external_plugins.

    Quarantine disables plugins by where their code lives, not by the
    tenant-writable ``builtin`` column, because a plugin that has run can set
    ``builtin=1`` on its own row.  Both the folder name and the id in its
    ``plugin.json`` count.  Links and special files are skipped, and a
    manifest is read without following links and only up to a small size,
    so a planted entry cannot make the portal read a host file or a huge one.
    """
    root = os.path.join(data_dir, "external_plugins")
    try:
        root_st = os.lstat(root)
    except OSError:
        return []
    if not stat.S_ISDIR(root_st.st_mode):
        return []
    ids = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for entry in entries:
        plugin_dir = os.path.join(root, entry)
        try:
            if not stat.S_ISDIR(os.lstat(plugin_dir).st_mode):
                continue
        except OSError:
            continue
        ids.append(entry)
        try:
            raw = read_tenant_file(
                data_dir,
                os.path.join(plugin_dir, "plugin.json"),
                max_bytes=_PLUGIN_MANIFEST_MAX_BYTES,
            )
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
            ids.append(data["id"])
    return list(dict.fromkeys(ids))


def capture_plugin_safety_snapshot(instance_id):
    """Save a trusted rollback point of a tenant DB on an operator's request.

    The copy is stored host-side, where the tenant cannot change it, so it is
    what :func:`restore_latest_plugin_safety_snapshot` restores.  Take it
    while the wiki is in a known-good state, before a tenant enables custom
    plugins.  The tenant keeps running.  Returns ``(ok, message)``.
    """
    inst_row = get_instance(instance_id)
    if not inst_row:
        return False, "Instance not found."
    inst = dict(inst_row)
    if inst.get("status") == "terminated":
        return False, t("hosting.instance_ops.snapshot_terminated", default="Cannot snapshot a terminated instance.")
    data_dir = _instance_dir_for(inst)
    db_path = _instance_db_path(data_dir)
    if not os.path.lexists(db_path):
        return False, t("hosting.instance_ops.db_missing", default="Instance database is missing.")
    try:
        _capture_platform_snapshot(instance_id, db_path, data_dir, "pre-enable")
    except ValueError:
        logger.warning(
            "Refusing to snapshot %s: the tenant DB is not a regular file "
            "inside its data directory", instance_id,
        )
        return False, t("hosting.instance_ops.db_path_unsafe", default="The instance database path is not safe to open.")
    except Exception:
        logger.exception("Failed to capture plugin safety snapshot for %s", instance_id)
        return False, t("hosting.instance_ops.snapshot_failed", default="Could not save the snapshot.")
    return True, t("hosting.instance_ops.snapshot_saved", default="A plugin safety snapshot was saved on the platform.")


def lift_instance_plugin_quarantine(instance_id):
    """Clear the operator's plugin quarantine and restart a running tenant.

    External plugins can load again from the next start.  Rows the
    quarantine disabled stay disabled until the wiki admin enables them.
    Returns ``(ok, message)``.
    """
    inst_row = get_instance(instance_id)
    if not inst_row:
        return False, "Instance not found."
    inst = dict(inst_row)
    if not instance_plugins_quarantined(instance_id):
        return False, t("hosting.instance_ops.not_quarantined", default="This instance is not under plugin quarantine.")
    was_running = inst.get("status") == "running"
    data_dir = _instance_dir_for(inst)
    if was_running:
        _stop_process(data_dir, port=inst.get("port"))
    set_instance_plugin_quarantine(instance_id, False)
    if was_running and not _start_process(inst, data_dir):
        return False, t("hosting.instance_ops.quarantine_lifted_restart_failed", default="The quarantine was lifted, but the tenant did not restart.")
    _invalidate_instance_caches(inst.get("subdomain"))
    return True, t(
        "hosting.instance_ops.quarantine_lifted",
        default="Plugin quarantine lifted. The wiki admin can enable custom plugins again.",
    )


def quarantine_instance_external_plugins(instance_id):
    """Operator kill switch for a crashing or hostile custom plugin.

    Stops the tenant, then records the quarantine host-side, outside the
    tenant mount.  While that marker exists every start runs the tenant with
    external plugins switched off (BW_ALLOW_EXTERNAL_PLUGINS=0), so the wiki
    does not even scan its external plugin folder and nothing the tenant
    writes to its own database can bring a plugin back.  The marker stays
    until an operator lifts it.

    With the tenant stopped, it also saves a forensic copy of the database in
    the host-owned snapshot area and disables the plugin rows: every id whose
    code sits under external_plugins, whatever its ``builtin`` column says,
    and every honest ``builtin=0`` row.  It restarts the tenant only if it
    was running.  Returns ``(ok, message)``.
    """
    inst_row = get_instance(instance_id)
    if not inst_row:
        return False, "Instance not found."
    inst = dict(inst_row)
    data_dir = _instance_dir_for(inst)
    db_path = _instance_db_path(data_dir)
    was_running = inst.get("status") == "running"
    _stop_process(data_dir, port=inst.get("port"))

    # The marker is the part that actually keeps plugin code from running, so
    # set it before touching any tenant file.
    set_instance_plugin_quarantine(instance_id, True)

    if not os.path.lexists(db_path):
        return False, t(
            "hosting.instance_ops.quarantine_db_missing",
            default="External plugins are blocked, but the instance database is "
            "missing. The tenant was not restarted.",
        )
    try:
        guard_tenant_path(data_dir, db_path)
    except (OSError, ValueError):
        logger.warning(
            "Quarantine of %s left the DB untouched: it is not a regular file "
            "inside the instance directory", instance_id,
        )
        return False, t(
            "hosting.instance_ops.quarantine_db_unsafe",
            default="External plugins are blocked, but the instance database path "
            "is not safe to open, so it was left untouched and the tenant was not "
            "restarted.",
        )

    try:
        _capture_platform_snapshot(instance_id, db_path, data_dir, "operator-quarantine")
    except Exception:
        # The copy is for forensics only; the quarantine itself is in force.
        logger.exception("Failed to capture quarantine snapshot for %s", instance_id)

    external_ids = _external_plugin_ids(data_dir)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = connect_tenant_db(data_dir, db_path)
    except (OSError, ValueError, sqlite3.Error):
        logger.exception("Could not open the tenant DB of %s for quarantine", instance_id)
        return False, t(
            "hosting.instance_ops.quarantine_rows_failed",
            default="External plugins are blocked, but their database rows could "
            "not be updated. The tenant was not restarted.",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "plugins" in tables:
            # A temporary table avoids SQLite's limit on bound parameters when
            # a tenant has planted a very large number of plugin folders.
            conn.execute("CREATE TEMP TABLE quarantine_ids (id TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT OR IGNORE INTO quarantine_ids (id) VALUES (?)",
                ((plugin_id,) for plugin_id in external_ids),
            )
            conn.execute(
                "UPDATE plugins SET enabled=0, disabled_at=? "
                "WHERE builtin=0 OR id IN (SELECT id FROM quarantine_ids)",
                (now_iso,),
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Could not disable plugin rows of %s", instance_id)
        return False, t(
            "hosting.instance_ops.quarantine_rows_failed",
            default="External plugins are blocked, but their database rows could "
            "not be updated. The tenant was not restarted.",
        )
    finally:
        conn.close()

    if was_running and not _start_process(inst, data_dir):
        return False, "Plugins were quarantined, but the tenant did not restart."
    _invalidate_instance_caches(inst.get("subdomain"))
    return True, t(
        "hosting.instance_ops.quarantined",
        default="External plugins quarantined. They stay off until the quarantine "
        "is lifted; a copy of the database was saved on the platform.",
    )


def restore_latest_plugin_safety_snapshot(instance_id, snapshot_name=None):
    """Roll a tenant DB back to a platform-taken pre-enable snapshot.

    Only snapshots the platform itself took and keeps outside the tenant
    mount are used: the newest ``pre-enable`` one by the creation time in the
    host-owned index, or *snapshot_name* when the operator picked one.  The
    file must still match its recorded SHA-256 and pass an integrity check.
    Copies the tenant writes into its own ``plugin_safety_snapshots`` folder
    are never used, since plugin code can rewrite them.

    The current database is saved first as a ``before-restore`` snapshot,
    and the plugin quarantine is switched on: the restored database predates
    the suspect plugin, but the plugin's code may still be on disk.
    Returns ``(ok, message)``.
    """
    inst_row = get_instance(instance_id)
    if not inst_row:
        return False, "Instance not found."
    inst = dict(inst_row)
    data_dir = _instance_dir_for(inst)
    db_path = _instance_db_path(data_dir)
    snap_dir = plugin_snapshot_dir(instance_id)
    index = _read_snapshot_index(instance_id)

    candidates = []
    for name, meta in index.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("label") not in _RESTORABLE_SNAPSHOT_LABELS:
            continue
        if snapshot_name is not None and name != snapshot_name:
            continue
        path = os.path.join(snap_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        candidates.append((meta.get("created_at") or "", name, path, meta))
    candidates.sort(reverse=True)
    if not candidates:
        if snapshot_name is not None:
            return False, t("hosting.instance_ops.snapshot_not_available", default="That plugin safety snapshot is not available.")
        return False, t(
            "hosting.instance_ops.no_platform_snapshot",
            default="No plugin safety snapshot saved by the platform is available. "
            "Save one before the tenant enables custom plugins, or restore from a "
            "platform backup.",
        )

    _created, _name, trusted_path, meta = candidates[0]
    if meta.get("sha256") != _sha256_file(trusted_path):
        return False, t("hosting.instance_ops.snapshot_checksum_mismatch", default="The plugin safety snapshot does not match its recorded checksum.")
    try:
        check = sqlite3.connect(
            pathlib.Path(trusted_path).as_uri() + "?mode=ro", uri=True, timeout=10
        )
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
    except sqlite3.Error as exc:
        logger.warning("Cannot validate plugin snapshot %s: %s", trusted_path, exc)
        return False, t("hosting.instance_ops.snapshot_unreadable", default="The plugin safety snapshot is unreadable.")
    if not result or result[0] != "ok":
        return False, t("hosting.instance_ops.snapshot_integrity_failed", default="The plugin safety snapshot failed integrity checks.")

    was_running = inst.get("status") == "running"
    _stop_process(data_dir, port=inst.get("port"))
    if not os.path.lexists(db_path):
        return False, "Instance database is missing."

    try:
        _capture_platform_snapshot(instance_id, db_path, data_dir, "before-restore")
    except ValueError:
        return False, t("hosting.instance_ops.db_path_unsafe", default="The instance database path is not safe to open.")
    except Exception:
        logger.exception("Failed to save emergency snapshot for %s", instance_id)
        return False, "Could not save an emergency snapshot; no changes were made."

    source = sqlite3.connect(
        pathlib.Path(trusted_path).as_uri() + "?mode=ro", uri=True, timeout=20
    )
    try:
        destination = connect_tenant_db(data_dir, db_path)
    except (OSError, ValueError, sqlite3.Error):
        source.close()
        logger.exception("Could not open the tenant DB of %s for restore", instance_id)
        return False, t("hosting.instance_ops.db_path_unsafe", default="The instance database path is not safe to open.")
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass

    set_instance_plugin_quarantine(instance_id, True)

    if was_running and not _start_process(inst, data_dir):
        return False, "Snapshot restored, but the tenant did not restart."
    _invalidate_instance_caches(inst.get("subdomain"))
    return True, t(
        "hosting.instance_ops.snapshot_restored",
        default="The plugin safety snapshot was restored. Custom plugins stay "
        "quarantined until the quarantine is lifted.",
    )


def _admin_unlimited_expiry_iso():
    """Return the standard far-future expiry used for admin-owned instances."""
    return (datetime.now(timezone.utc) + timedelta(days=365 * 100)).isoformat()


def apply_instance_owner_quota_policy(instance_id, owner_is_admin):
    """Apply quota policy for one instance based on whether its owner is admin."""
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return True, ""

    with get_hosting_db_context() as conn:
        if owner_is_admin:
            conn.execute(
                "UPDATE instances SET expires_at=?, storage_limit_mb=? WHERE id=?",
                (_admin_unlimited_expiry_iso(), 0, instance_id),
            )
        else:
            current_limit = inst["storage_limit_mb"]
            try:
                parsed_limit = (
                    int(current_limit) if current_limit is not None
                    else int(config.INSTANCE_STORAGE_LIMIT_MB)
                )
            except (TypeError, ValueError):
                parsed_limit = int(config.INSTANCE_STORAGE_LIMIT_MB)
            if parsed_limit <= 0:
                parsed_limit = int(config.INSTANCE_STORAGE_LIMIT_MB)
            conn.execute(
                "UPDATE instances SET expires_at=?, storage_limit_mb=? WHERE id=?",
                (
                    (datetime.now(timezone.utc) + timedelta(days=config.INSTANCE_DURATION_DAYS)).isoformat(),
                    parsed_limit,
                    instance_id,
                ),
            )
        conn.commit()
    return True, ""


def apply_account_owner_quota_policy(account_id, owner_is_admin):
    """Apply owner quota policy to all active instances for one account."""
    for row in get_instances_for_account(account_id):
        if row["status"] == "terminated":
            continue
        ok, reason = apply_instance_owner_quota_policy(
            row["id"], owner_is_admin=owner_is_admin,
        )
        if not ok:
            logger.warning(
                "Failed to apply quota policy for instance %s (account=%s): %s",
                row["id"], account_id, reason,
            )


def provision_instance(
    account_id,
    subdomain,
    *,
    domain_mode="hosting",
    account_is_admin=False,
    admin_username=None,
    admin_password=None,
    easy_wiki=False,
    declared_use_case="",
):
    """Create and start a new pre-activated BananaWiki instance.

    Returns ``(instance_dict, error)`` where *error* is ``None``
    on success or a human-readable message on failure.  The instance is
    shipped already set up with temporary admin credentials stored in the
    instance row.

    When *admin_username* and *admin_password* are both provided they
    are used as the wiki's initial admin credentials.  Otherwise random
    temporary credentials are generated and the admin user is forced to
    set a new password on first login (and may optionally change the
    username at the same time).

    ``domain_mode`` controls the URL format and may be ``"hosting"`` (the
    default, available to everyone) or ``"apex"`` (admin-only).  The
    caller must pass ``account_is_admin=True`` when requesting an apex
    instance, otherwise validation rejects the request.
    """
    ok, reason = validate_subdomain(
        subdomain,
        account_is_admin=account_is_admin,
        domain_mode=domain_mode,
    )
    if not ok:
        return None, reason

    # validate_subdomain already normalises to lowercase/stripped
    subdomain = subdomain.lower().strip()

    # Uniqueness is scoped to ``(subdomain, domain_mode)``: the same slug
    # may live in both apex and hosting namespaces because they render
    # at distinct public URLs (``wiki.example.com`` vs
    # ``wiki-hosting.example.com``).
    if get_instance_by_subdomain(subdomain, domain_mode=domain_mode):
        return None, "That name is already in use."

    if not account_is_admin:
        if count_active_instances(account_id) >= config.MAX_INSTANCES_PER_ACCOUNT:
            return None, (
                f"You can only create up to {config.MAX_INSTANCES_PER_ACCOUNT} instances."
            )

        # Check global capacity limits
        from .db import get_hosting_settings
        settings = get_hosting_settings()
        if settings and settings["global_limit_enabled"]:
            stats = get_platform_stats()

            # Check instance count limit
            if stats["running"] + stats["stopped"] >= settings["global_limit_max_instances"]:
                return None, "The platform has reached its maximum instance capacity. Please try again later."

            # Check storage limit
            if stats["total_storage_mb"] >= settings["global_limit_max_storage_mb"]:
                return None, "The platform has reached its maximum storage capacity. Please try again later."

    # Determine admin credentials: custom or auto-generated
    use_custom = bool(admin_password)
    if use_custom:
        final_username = (admin_username or _generate_temp_username()).strip()
        final_password = admin_password
        force_pw_change = False
    else:
        final_username = _generate_temp_username()
        final_password = _generate_temp_password()
        force_pw_change = False

    try:
        inst = db_create_instance(
            account_id, subdomain,
            admin_username=final_username,
            admin_password=final_password,
            domain_mode=domain_mode,
            custom_credentials=use_custom,
            easy_wiki=easy_wiki,
            declared_use_case=declared_use_case,
        )
    except ValueError as exc:
        if str(exc) == "subdomain_in_use":
            return None, "That name is already in use."
        raise
    if inst is None:
        return None, "No available ports. Please try again later."

    data_dir = _instance_dir(subdomain, domain_mode)
    seeded = False
    started = False
    failure_message = None
    try:
        _create_instance_dirs(data_dir)

        # Pre-create the secret key so the seeding subprocess, every Gunicorn
        # worker, and any recovery restart all read the SAME key.
        # BananaWiki's _load_secret_key() is evaluated at module import time; if
        # two workers both import config before the file exists they each generate
        # a different key, making session cookies unreadable across workers and
        # causing CSRF errors ("Your session has expired").  Writing the file here:
        # before any subprocess or Gunicorn process starts: eliminates the race.
        secret_key_path = os.path.join(data_dir, ".secret_key")
        if not os.path.exists(secret_key_path):
            key = secrets.token_hex(32)
            # Atomic write: temp file + rename prevents partial reads.
            fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=".secret_key_")
            try:
                os.write(fd, key.encode("utf-8"))
                os.close(fd)
                if os.name != "nt":
                    os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, secret_key_path)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        # Pre-activate: seed the instance database with an admin user
        # Only set onboarding_required if the hosting admin has enabled the global tour
        from .db import get_hosting_settings
        _hosting_settings = get_hosting_settings()
        _global_tour_enabled = bool(_hosting_settings and _hosting_settings.get("global_tour_enabled"))
        db_path = os.path.join(data_dir, "bananawiki.db")
        seeded = _seed_instance_db(db_path, final_username, final_password, force_password_change=force_pw_change, global_tour_enabled=_global_tour_enabled)
        if not seeded:
            logger.error("Failed to seed instance %s database", inst["id"])
            failure_message = "Failed to initialize the instance database. Please try again."


        # Start the BananaWiki process.  We deliberately do NOT wait for
        # the freshly-spawned wiki to answer ``/healthz`` here. That
        # second phase can take up to ~30 s on a cold VPS (bytecode
        # compilation + schema migrations inside Gunicorn ``--preload``),
        # and combined with the rest of provisioning (seed DB, install
        # database, retries on cold-start races) easily exceeds the
        # portal Gunicorn worker timeout (120 s).  When that happens the
        # portal worker gets ``SIGKILL``-ed and nginx returns a bare
        # ``502 Bad Gateway`` to the create POST itself: exactly the
        # "I deployed an instance and got Bad Gateway" symptom users
        # have been reporting.
        #
        # Confirming the Gunicorn master wrote its PID (Phase 1, ~PID
        # fsync latency) is enough to mark the row ``running``.  The
        # subdomain proxy's branded "Starting up…" splash (200 OK +
        # auto-refresh) bridges the brief window before workers bind
        # the listening socket, and lazy recovery handles the rare
        # case where the master crashes before the workers come up.
        if seeded:
            started = _start_process(inst, data_dir, await_http_ready=False)
        if seeded and not started:
            logger.error(
                "Instance %s (port %d) process failed to start: check %s",
                inst["id"], inst["port"],
                os.path.join(data_dir, "error.log"),
            )
            failure_message = (
                "The instance process did not become reachable. "
                "Please try again."
            )
        if seeded and started:
            # Invalidate the cache so the first proxy request after
            # create does a fresh health probe.  We deliberately do NOT
            # ``_set_cached_health(subdomain, True)`` here. The wiki
            # may still be finishing bytecode compilation and the cache
            # would mask the real state, causing the proxy to forward
            # a request to a not-yet-listening socket and surface a
            # connection-refused 502 instead of the splash.  Letting
            # the proxy probe live means the first request shows the
            # auto-refresh splash and the second request: 5 s later:
            # almost always succeeds.
            _invalidate_instance_caches(subdomain)

            # Allow cold startup to finish before attempting recovery.
            _spawn_post_restart_health_watch(
                dict(inst), domain_mode=domain_mode,
            )
    except Exception:
        logger.exception("Unexpected error provisioning instance %s", subdomain)
        terminate_and_archive_instance(inst["id"], subdomain)
        _invalidate_instance_caches(subdomain)
        if os.path.isdir(data_dir):
            shutil.rmtree(data_dir, ignore_errors=True)
        return None, "Failed to provision the instance. Please try again."

    if failure_message is not None:
        terminate_and_archive_instance(inst["id"], subdomain)
        _invalidate_instance_caches(subdomain)
        if os.path.isdir(data_dir):
            shutil.rmtree(data_dir, ignore_errors=True)
        return None, failure_message

    inst["url"] = _instance_url(inst)

    # Admin-owned instances are intentionally unlimited by policy.
    if account_is_admin:
        ok, reason = apply_instance_owner_quota_policy(
            inst["id"], owner_is_admin=True,
        )
        if not ok:
            logger.warning(
                "Failed to apply admin quota policy for instance %s: %s",
                inst["id"], reason,
            )
        refreshed = get_instance(inst["id"])
        if refreshed is not None:
            inst = dict(refreshed)
            inst["url"] = _instance_url(inst)

    # Notify the hosting sync module about the new instance
    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change(
            "instance_provisioned",
            f"Instance '{subdomain}' provisioned for account {account_id}",
        )
    except Exception:
        pass

    return inst, None


# Asset folders a wiki keeps in its data dir.  New instances hold them under
# storage/<name>, with a portal-made link <name> -> storage/<name>; older ones
# have a plain <name> folder.
_TENANT_ASSET_DIRS = (
    "uploads",
    "attachments",
    "chat_attachments",
    "kanban_attachments",
    "custom_page_files",
)


def _tenant_asset_relpath(data_dir, name):
    """Return where an asset folder really lives, relative to *data_dir*.

    Accepts a plain folder, or the exact ``<name> -> storage/<name>`` link the
    portal creates.  Anything else, such as a link the tenant pointed
    somewhere else, gives ``None``.  The result is only a choice of path:
    :func:`open_tenant_dir` still opens it without following links.
    """
    path = os.path.join(data_dir, name)
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return name
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return None
        if target == f"storage/{name}":
            return target
    return None


def _copy_tenant_tree(src_dir_fd, dest_dir):
    """Copy the regular files under *src_dir_fd* into *dest_dir*.

    The source belongs to a tenant that may be running, so it is read with
    :func:`iter_tenant_files`: links, FIFOs and other special files are
    skipped and nothing outside the source folder is ever opened.  Files are
    created with ``O_EXCL`` and ``O_NOFOLLOW`` in the destination.  Returns
    the number of files copied.
    """
    copied = 0
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for rel, fd, st in iter_tenant_files(src_dir_fd):
        with os.fdopen(fd, "rb") as src:
            dest = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            out_fd = os.open(
                dest,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                stat.S_IMODE(st.st_mode) & 0o777 or 0o644,
            )
            with os.fdopen(out_fd, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
        copied += 1
    return copied


def _copy_tenant_assets(source_dir, target_dir):
    """Copy a tenant's asset folders into a freshly provisioned instance."""
    for name in _TENANT_ASSET_DIRS:
        rel = _tenant_asset_relpath(source_dir, name)
        if rel is None:
            continue
        try:
            src_fd = open_tenant_dir(source_dir, rel)
        except OSError:
            continue
        try:
            dest = os.path.realpath(os.path.join(target_dir, name))
            target_real = os.path.realpath(target_dir)
            if not dest.startswith(target_real + os.sep):
                raise ValueError(f"Asset folder {name} of the clone escapes its directory")
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            os.makedirs(dest, exist_ok=True)
            _copy_tenant_tree(src_fd, dest)
        finally:
            os.close(src_fd)


def duplicate_instance(
    source_instance_id,
    target_account_id,
    target_subdomain,
    *,
    domain_mode="hosting",
    account_is_admin=False,
):
    """Duplicate an existing instance to a new subdomain/owner.

    Copies the source instance's SQLite database and asset folders (uploads,
    attachments, chat and kanban attachments, custom page files) into a
    freshly provisioned instance, then starts the clone.  The new instance
    receives new administrator credentials, and the API tokens copied from the
    source are revoked, since they were issued for the source wiki.

    The source may be running and its data dir is tenant-writable, so the
    database is read through :func:`connect_tenant_db` and the folders
    through a copy that skips links and special files.

    Parameters
    ----------
    source_instance_id : str
        ID of the instance to clone.
    target_account_id : str
        Account that will own the clone.
    target_subdomain : str
        Subdomain for the clone.
    domain_mode : str
        ``"hosting"`` (default) or ``"apex"``.
    account_is_admin : bool
        Whether the target account is an admin (allows apex mode and
        bypasses quota limits).

    Returns ``(instance_dict, None)`` on success or ``(None, reason)``
    on failure.
    """
    source = get_instance(source_instance_id)
    if source is None:
        return None, "Source instance not found."
    if source["status"] == "terminated":
        return None, "Cannot duplicate a terminated instance."

    source_dir = _instance_dir_for(source)
    source_db = os.path.join(source_dir, "bananawiki.db")
    if not os.path.lexists(source_db):
        return None, "Source instance database not found."
    # Refuse early, before provisioning anything, when the source DB is a
    # link or special file.  The copy below checks again as it opens it.
    try:
        guard_tenant_path(source_dir, source_db)
    except (OSError, ValueError):
        return None, t("hosting.instance_ops.source_db_path_unsafe", default="The source instance database path is not safe to open.")

    # Provision an empty instance (validates subdomain, assigns port, etc.)
    new_inst, error = provision_instance(
        target_account_id,
        target_subdomain,
        domain_mode=domain_mode,
        account_is_admin=account_is_admin,
    )
    if error:
        return None, error

    target_dir = _instance_dir_for(new_inst)
    target_db = os.path.join(target_dir, "bananawiki.db")

    try:
        # Stop the freshly-started clone so we can replace its DB safely.
        port = int(new_inst["port"]) if new_inst["port"] is not None else None
        _stop_process(target_dir, port=port)

        # Copy the source database (use sqlite3 backup for integrity).
        src_conn = connect_tenant_db(source_dir, source_db, read_only=True, timeout=30)
        try:
            dst_conn = connect_tenant_db(target_dir, target_db, timeout=30)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        _copy_tenant_assets(source_dir, target_dir)

        # Re-issue admin credentials so the clone owner has access.
        temp_username = _generate_temp_username()
        temp_password = _generate_temp_password()
        hashed_pw = generate_password_hash(temp_password)
        try:
            conn = connect_tenant_db(target_dir, target_db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "UPDATE users SET username=?, password=? "
                "WHERE role IN ('admin', 'owner') LIMIT 1",
                (temp_username, hashed_pw),
            )
            if _tenant_table_exists(conn, "api_service__tokens"):
                conn.execute("UPDATE api_service__tokens SET active=0 WHERE active=1")
            conn.commit()
            conn.close()
        except Exception:
            logger.exception(
                "Failed to set clone admin credentials for %s",
                new_inst["id"],
            )

        # Update the hosting DB with the new credentials.
        from .db import get_hosting_db_context
        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE instances SET admin_username=?, admin_password_plain=? "
                "WHERE id=?",
                (temp_username, temp_password, new_inst["id"]),
            )
            conn.commit()


        # Start the clone.
        started = _start_process(new_inst, target_dir, await_http_ready=False)
        if not started:
            logger.error(
                "Cloned instance %s failed to start", new_inst["id"],
            )
            return None, "Instance duplicated but failed to start. Try restarting it from the admin panel."

        _invalidate_instance_caches(target_subdomain)
        _spawn_post_restart_health_watch(dict(new_inst), domain_mode=domain_mode)

    except Exception:
        logger.exception("Failed to duplicate instance %s", source_instance_id)
        terminate_and_archive_instance(new_inst["id"], target_subdomain)
        _invalidate_instance_caches(target_subdomain)
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
        return None, "Failed to duplicate the instance. Please try again."

    refreshed = get_instance(new_inst["id"])
    if refreshed is not None:
        new_inst = dict(refreshed)
    new_inst["url"] = _instance_url(new_inst)

    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change(
            "instance_duplicated",
            f"Instance '{source['subdomain']}' duplicated to '{target_subdomain}'",
        )
    except Exception:
        pass

    return new_inst, None


def provision_instance_from_archive(
    account_id,
    subdomain,
    archive_path,
    *,
    domain_mode="hosting",
    account_is_admin=False,
):
    """Provision a new instance whose data dir is seeded from a wiki ZIP.

    The ZIP must follow the same structure produced by
    :func:`build_instance_archive`: a single top-level folder containing
    ``bananawiki.db`` plus ``uploads/``, ``attachments/``, etc.  This is
    an admin-only entry point (the caller enforces it); regular users
    have no UI surface for it.

    Returns ``(instance_dict, None)`` on success or ``(None, reason)`` on
    failure.  On failure the partially-extracted data dir is cleaned up.
    """
    import zipfile

    if not account_is_admin:
        return None, "Importing a wiki archive is restricted to admin accounts."

    ok, reason = validate_subdomain(
        subdomain,
        account_is_admin=account_is_admin,
        domain_mode=domain_mode,
    )
    if not ok:
        return None, reason

    subdomain = subdomain.lower().strip()

    # Uniqueness is scoped to ``(subdomain, domain_mode)`` so the apex
    # and hosting namespaces stay independent.  ``provision_instance``
    # uses the same scoping for the regular create path.
    if get_instance_by_subdomain(subdomain, domain_mode=domain_mode):
        return None, "That name is already in use."

    if not zipfile.is_zipfile(archive_path):
        return None, "The uploaded file is not a valid ZIP archive."

    # Extract into a temp dir first; only commit to the data dir once we
    # have validated the archive contents. Keep staging under the hosting
    # import temp root so stale crash leftovers are swept by the portal's
    # import cleanup.
    data_parent = os.path.realpath(config.INSTANCES_DIR)
    os.makedirs(data_parent, exist_ok=True)
    work_dir = _hosting_import_work_dir()
    staging_dir = tempfile.mkdtemp(prefix="bwh-import-", dir=work_dir)
    reconstructed_root = None
    inst = None
    try:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                namelist = zf.namelist()
                _size, validation_error = _validate_import_zip_members(
                    zf,
                    staging_parent=work_dir,
                    data_parent=data_parent,
                )
                if validation_error:
                    return None, validation_error

                layout = archive_format.detect_layout(namelist)

                # Validate manifest if one is present.
                if layout.get("has_manifest"):
                    try:
                        with zf.open(archive_format.MANIFEST_FILENAME) as mh:
                            archive_format.parse_manifest(mh.read())
                    except ValueError as exc:
                        return None, f"Invalid archive manifest: {exc}"

                for info in zf.infolist():
                    zf.extract(info, staging_dir)
        except zipfile.BadZipFile:
            return None, "Could not read the uploaded ZIP archive."
        except Exception:
            logger.exception("Failed to extract import archive %s", archive_path)
            return None, "Failed to extract the uploaded ZIP archive."

        # Locate the directory that contains the payload.  For unified
        # archives this is just ``staging_dir`` itself; for legacy hosting
        # archives it is the single top-level slug directory; for legacy
        # standalone archives it is ``staging_dir`` plus an ``assets/``
        # sub-tree that we will fold in later.
        db_root = None
        site_export_path = None
        if layout["layout"] == "unified":
            db_root = staging_dir
            if layout.get("has_raw_db") and os.path.isfile(
                os.path.join(db_root, archive_format.RAW_DB_FILENAME)
            ):
                pass  # raw DB present at root
            else:
                db_root = None  # may still be reconstructable from JSON
            if layout.get("has_site_export_json"):
                site_export_path = os.path.join(
                    staging_dir, archive_format.SITE_EXPORT_FILENAME,
                )
        elif layout["layout"] == "legacy_hosting":
            candidate = os.path.join(
                staging_dir, layout["root_prefix"].rstrip("/"),
            )
            if os.path.isfile(
                os.path.join(candidate, archive_format.RAW_DB_FILENAME)
            ):
                db_root = candidate
                jpath = os.path.join(
                    candidate, archive_format.SITE_EXPORT_FILENAME,
                )
                if os.path.isfile(jpath):
                    site_export_path = jpath
        elif layout["layout"] == "legacy_standalone":
            # Some legacy standalone archives ship ``site_export.json``
            # without a raw DB.  We will reconstruct the DB from JSON
            # below.
            jpath = os.path.join(
                staging_dir, archive_format.SITE_EXPORT_FILENAME,
            )
            if os.path.isfile(jpath):
                site_export_path = jpath
        else:
            # Last-ditch fallback: walk the staging dir looking for any
            # bananawiki.db anywhere.
            for root, _dirs, files in os.walk(staging_dir):
                if archive_format.RAW_DB_FILENAME in files:
                    db_root = root
                    break

        if db_root is None and site_export_path is None:
            return None, (
                "Archive does not contain a BananaWiki database or a "
                "site_export.json dump."
            )

        # If we only have a JSON dump, reconstruct the SQLite database
        # in a temp dir so the rest of the provisioning path can treat
        # it like a raw DB import.  This is how legacy standalone-wiki
        # archives (no raw ``bananawiki.db``) get imported into the
        # hosting platform.
        if db_root is None and site_export_path is not None:
            reconstructed_root = tempfile.mkdtemp(prefix="bwh-recon-", dir=work_dir)
            recon_db = os.path.join(reconstructed_root, archive_format.RAW_DB_FILENAME)
            try:
                with open(site_export_path, "rb") as fh:
                    json_payload = json.loads(fh.read())
                _reconstruct_wiki_db_from_json(recon_db, json_payload)
            except Exception as exc:
                logger.exception(
                    "Failed to reconstruct DB from site_export.json in %s",
                    archive_path,
                )
                return None, (
                    "Failed to reconstruct the database from the "
                    f"site_export.json dump: {exc}"
                )
            db_root = reconstructed_root

        # Allocate a row + port and lay down the data dir.
        try:
            inst = db_create_instance(
                account_id, subdomain,
                admin_username=None,
                admin_password=None,
                domain_mode=domain_mode,
            )
        except ValueError as exc:
            if str(exc) == "subdomain_in_use":
                return None, "That name is already in use."
            raise
        if inst is None:
            return None, "No available ports. Please try again later."

        data_dir = _instance_dir(subdomain, domain_mode)
        try:
            _create_instance_dirs(data_dir)

            # Pre-create the shared secret key so all workers agree.
            secret_key_path = os.path.join(data_dir, ".secret_key")
            if not os.path.exists(secret_key_path):
                key = secrets.token_hex(32)
                fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=".secret_key_")
                try:
                    os.write(fd, key.encode("utf-8"))
                    os.close(fd)
                    if os.name != "nt":
                        os.chmod(tmp_path, 0o600)
                    os.replace(tmp_path, secret_key_path)
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise

            # Copy the archive contents (everything except volatile
            # runtime files, metadata files, and bundled secrets) into
            # the data dir.  We pull from ``db_root`` for the SQLite DB
            # and, for unified / legacy_standalone archives, also fold
            # in the asset directories that live in ``staging_dir``.
            def _copy_payload_entries(source_root):
                for entry in os.listdir(source_root):
                    # Skip volatile runtime files + bundled secrets +
                    # the metadata files (manifest / site_export.json).
                    if (
                        entry in archive_format.VOLATILE_FILENAMES
                        or entry in archive_format.SECRET_FILENAMES
                        or entry == archive_format.MANIFEST_FILENAME
                        or entry == archive_format.SITE_EXPORT_FILENAME
                    ):
                        continue
                    src = os.path.join(source_root, entry)
                    dst = os.path.join(data_dir, entry)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)

            _copy_payload_entries(db_root)
            imported_db_path = os.path.join(data_dir, archive_format.RAW_DB_FILENAME)
            if os.path.isfile(imported_db_path):
                _attribute_wiki_db_pages_to_system(imported_db_path)

            # For legacy_standalone archives, asset directories live at
            # ``staging_dir/assets/<kind>/`` rather than at the archive
            # root.  Fold them into the data dir under their canonical
            # flat names.
            legacy_assets_root = os.path.join(staging_dir, "assets")
            if os.path.isdir(legacy_assets_root):
                for kind in os.listdir(legacy_assets_root):
                    src = os.path.join(legacy_assets_root, kind)
                    if not os.path.isdir(src):
                        continue
                    dst = os.path.join(data_dir, kind)
                    shutil.copytree(src, dst, dirs_exist_ok=True)


            # See ``provision_instance`` for the rationale behind
            # ``await_http_ready=False``: we cannot afford to block
            # the portal worker on the wiki's first ``/healthz`` while
            # bytecode compilation and schema migrations run.
            started = _start_process(inst, data_dir, await_http_ready=False)
            if not started:
                logger.error(
                    "Imported instance %s failed to start", inst["id"],
                )
                _cleanup_failed_import_instance(inst, subdomain, domain_mode)
                return None, "The imported instance failed to start."
            # Don't pre-warm the health cache here: the wiki may still
            # be finishing first-boot work and the proxy needs to see a
            # live ``False`` so the "Starting up…" splash renders.
            _invalidate_instance_caches(subdomain)
        except Exception:
            logger.exception("Failed to seed imported instance %s", inst["id"])
            _cleanup_failed_import_instance(inst, subdomain, domain_mode)
            return None, "Failed to seed the imported instance."

        inst = dict(get_instance(inst["id"]))
        if account_is_admin:
            ok, reason = apply_instance_owner_quota_policy(
                inst["id"], owner_is_admin=True,
            )
            if not ok:
                logger.warning(
                    "Failed to apply admin quota policy for imported instance %s: %s",
                    inst["id"], reason,
                )
            inst = dict(get_instance(inst["id"]))
        inst["url"] = _instance_url(inst)

        try:
            from .backups import notify_change as hosting_notify_change
            hosting_notify_change(
                "instance_imported",
                f"Instance '{subdomain}' imported by admin for account {account_id}",
            )
        except Exception:
            pass

        return inst, None
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if reconstructed_root:
            shutil.rmtree(reconstructed_root, ignore_errors=True)


def stop_instance(instance_id, *, allow_suspended=False):
    """Pause a running instance.  Returns ``(True, "")`` or ``(False, reason)``.

    Only admin paths pass *allow_suspended*, which also turns a suspended
    instance into a stopped one. Customer paths must never move a wiki out
    of ``suspended``.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "suspended" and not allow_suspended:
        return False, "Instance is suspended."
    if inst["status"] not in ("running", "suspended"):
        return False, "Instance is not running."

    data_dir = _instance_dir_for(inst)
    port = int(inst["port"]) if inst["port"] is not None else None
    _stop_process(data_dir, port=port)
    _invalidate_instance_caches(inst["subdomain"])

    # Compare-and-set, so a suspension that landed while the process was
    # stopping is not overwritten with ``stopped``.
    if not update_instance_status(instance_id, "stopped", expected_status=inst["status"]):
        return False, "The instance changed state while it was being stopped."

    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change("instance_stopped", f"Instance '{inst['subdomain']}' stopped")
    except Exception:
        pass

    return True, ""


def suspend_instance(instance_id, *, suspended_until=None, reason="",
                     reason_visible=False, time_visible=False, performed_by=None):
    """Stop the process and mark an instance as suspended by an admin.

    Records ``suspended_at = now`` so that:

    * the user-facing expiration timer can be frozen for the duration of
      the suspension (see :func:`unsuspend_instance`); and
    * downstream code (templates, dashboards, the expiry sweeper) can
      tell whether an instance is currently in a frozen state.

    Idempotent: re-suspending an already-suspended instance does NOT
    overwrite ``suspended_at``: that would silently reset the freeze
    window and reward a re-suspend with a longer effective trial.

    When *suspended_until* is provided (UTC ISO datetime), the instance
    will be auto-unsuspended once that time passes (timed suspension).
    When it is ``None`` (the default), the suspension is permanent.

    *reason*, *reason_visible*, *time_visible*, and *performed_by* are
    stored alongside the suspension for display and audit purposes.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "suspended":
        return True, ""
    if inst["status"] == "terminated":
        return False, "Instance is already terminated."

    data_dir = _instance_dir_for(inst)
    port = int(inst["port"]) if inst["port"] is not None else None
    # Mark the row first and stop whatever runs afterwards, whatever status
    # was read above. A resume that is starting the process at this moment
    # then fails its own compare-and-set and stops what it started, or has
    # already finished and its process is stopped here.
    update_instance_status(instance_id, "suspended")

    # Stamp the freeze marker and timed-suspension metadata.
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE instances SET suspended_at=?, suspended_until=?, "
                "suspend_reason=?, suspend_reason_visible=?, suspend_time_visible=? "
                "WHERE id=? AND (suspended_at IS NULL OR suspended_at='')",
                (now_iso, suspended_until, reason,
                 int(reason_visible), int(time_visible), instance_id),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to stamp suspension metadata for instance %s", instance_id)

    _stop_process(data_dir, port=port)
    _invalidate_instance_caches(inst["subdomain"])

    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change(
            "instance_suspended",
            f"Instance '{inst['subdomain']}' suspended",
        )
    except Exception:
        pass

    return True, ""


def _consume_suspension_window(instance_id):
    """Close out the active suspension window for ``instance_id``.

    Computes ``delta = now - suspended_at`` and:

    * shifts ``expires_at`` forward by ``delta`` (unless ``expires_at`` is
      ``NULL``: apex / perpetual instances simply do not have a timer);
    * adds ``delta`` to ``suspended_accumulated_seconds`` for diagnostics;
    * clears ``suspended_at``.

    Returns the integer ``delta`` (seconds) that was credited, or ``0``
    if no active suspension window was found.  Errors are logged and
    swallowed so the surrounding unsuspend flow always proceeds.
    """
    from .db import get_hosting_db_context

    try:
        with get_hosting_db_context() as conn:
            row = conn.execute(
                "SELECT suspended_at, expires_at, suspended_accumulated_seconds "
                "FROM instances WHERE id=?",
                (instance_id,),
            ).fetchone()
            if row is None or not row["suspended_at"]:
                return 0
            try:
                started = datetime.fromisoformat(
                    str(row["suspended_at"]).replace("Z", "+00:00")
                )
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                started = datetime.now(timezone.utc)
            now_dt = datetime.now(timezone.utc)
            delta = max(int((now_dt - started).total_seconds()), 0)

            new_expires = row["expires_at"]
            if new_expires:
                try:
                    exp = datetime.fromisoformat(
                        str(new_expires).replace("Z", "+00:00")
                    )
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    new_expires = (exp + timedelta(seconds=delta)).isoformat()
                except (ValueError, TypeError):
                    new_expires = row["expires_at"]

            current_accum = row["suspended_accumulated_seconds"] or 0
            conn.execute(
                "UPDATE instances SET suspended_at=NULL, "
                "suspended_accumulated_seconds=?, expires_at=? WHERE id=?",
                (int(current_accum) + delta, new_expires, instance_id),
            )
            conn.commit()
            return delta
    except Exception:
        logger.exception(
            "Failed to consume suspension window for instance %s", instance_id
        )
        return 0


def unsuspend_instance(instance_id):
    """Lift a suspension and restart the instance.

    On unsuspend we:

    1. shift ``expires_at`` forward by the time the instance spent in
       the suspended state (so users do not silently lose trial days);
    2. restart the Gunicorn process via :func:`restart_instance` so the
       user immediately gets a working wiki again.

    Returns ``(True, "")`` on success or ``(False, reason)`` if the
    restart leg failed.  The expiry shift always runs first so an
    instance that fails to come back up still has its timer corrected
    and the admin's next unsuspend attempt picks up the right state.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] != "suspended":
        return False, "Only suspended instances can be unsuspended."

    _consume_suspension_window(instance_id)

    return restart_instance(instance_id, allow_suspended=True)


def restart_instance(instance_id, *, allow_suspended=False):
    """Resume a stopped instance.  Returns ``(True, "")`` or ``(False, reason)``.

    Only admin paths pass *allow_suspended*, which also starts a suspended
    instance. Without it an instance that is suspended, or still carries a
    suspension marker, is refused.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if not allow_suspended and (inst["status"] == "suspended" or dict(inst).get("suspended_at")):
        return False, "Instance is suspended."

    data_dir = _instance_dir_for(inst)
    port = int(inst["port"]) if inst["port"] is not None else None

    if inst["status"] == "running":
        if check_instance_health(inst):
            return False, "Instance is already running."
        # Force-restart a running instance whose process is down or unhealthy.
        _stop_process(data_dir, port=port)
        _invalidate_instance_caches(inst["subdomain"])
        if not update_instance_status(instance_id, "stopped", expected_status="running"):
            return False, "The instance changed state while it was being restarted."
        inst = get_instance(instance_id)
        if inst is None:
            return False, "Instance not found."

    if inst["status"] not in ("stopped", "suspended"):
        return False, "Instance is not stopped."

    # Clean up any stale PID from a previous crash or reboot before
    # restarting.  Passing the port lets ``_clean_stale_pid`` reject a
    # PID that happens to be alive but does not match this instance.
    _clean_stale_pid(data_dir, port=port)

    try:
        started = _start_process(inst, data_dir)
    except Exception:
        logger.exception("Unhandled error starting instance %s", instance_id)
        return False, "An unexpected error occurred while starting the instance."

    if not started:
        return False, "The process did not start. Check the error log at the instance data directory."
    # Compare-and-set: if a suspension or a pause landed while the process
    # was starting, it wins and the process started here is stopped again.
    if not update_instance_status(instance_id, "running", expected_status=inst["status"]):
        logger.warning("Instance %s changed state while starting; stopping it again", instance_id)
        try:
            _stop_process(data_dir, port=port)
        except Exception:
            logger.exception("Could not stop instance %s after its state changed", instance_id)
        _invalidate_instance_caches(inst["subdomain"])
        return False, "The instance changed state while it was starting."
    _invalidate_instance_caches(inst["subdomain"])

    # Pre-warm the health cache so the first proxy request after restart
    # hits cache instead of doing a cold health check that could race with
    # Gunicorn worker initialisation.
    _set_cached_health(inst["subdomain"], True)

    # Check readiness without blocking the request or recycling a cold startup.
    _spawn_post_restart_health_watch(inst, domain_mode=_domain_mode_of(inst))

    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change("instance_started", f"Instance '{inst['subdomain']}' restarted")
    except Exception:
        pass

    return True, ""


def _move_dir_safely(src, dst):
    """Move ``src`` directory to ``dst``.

    If ``src`` does not exist, returns ``False``.  If ``dst`` already exists,
    ``src`` is removed (data already at the target path) and ``True`` is
    returned.  All errors are swallowed so the surrounding lifecycle flow can
    continue: the caller logs and the periodic cleanup will retry on the
    next tick.
    """
    if not os.path.isdir(src):
        return False
    try:
        if os.path.realpath(src) == os.path.realpath(dst):
            return True
    except OSError:
        pass
    if os.path.exists(dst):
        shutil.rmtree(src, ignore_errors=True)
        return True
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        return True
    except OSError:
        try:
            shutil.move(src, dst)
            return True
        except Exception:
            logger.exception("Failed to move data dir %s -> %s", src, dst)
            return False


def change_instance_identity(
    instance_id,
    subdomain,
    domain_mode,
    *,
    account_is_admin=False,
    auto_suffix=False,
):
    """Rename an instance and/or move it between apex and hosting URL modes."""
    if domain_mode not in ("hosting", "apex"):
        return None, "Invalid domain mode."

    inst = get_instance(instance_id)
    if inst is None:
        return None, "Instance not found."
    if inst["status"] == "terminated":
        return None, "Cannot rename a terminated instance."

    ok, reason = validate_subdomain(
        subdomain,
        account_is_admin=account_is_admin,
        domain_mode=domain_mode,
    )
    if not ok:
        return None, reason

    requested_subdomain = subdomain.lower().strip()
    if auto_suffix:
        try:
            target_subdomain = find_available_instance_subdomain(
                requested_subdomain,
                domain_mode,
                exclude_instance_id=instance_id,
            )
        except ValueError:
            return None, "No available subdomain could be found."
    else:
        target_subdomain = requested_subdomain
        other = get_instance_by_subdomain(target_subdomain, domain_mode=domain_mode)
        if other and other["id"] != instance_id:
            return None, "That name is already in use."

    old_subdomain = inst["subdomain"]
    old_mode = _domain_mode_of(inst)
    if old_subdomain == target_subdomain and old_mode == domain_mode:
        return dict(inst), None

    old_dir = _instance_dir(old_subdomain, old_mode)
    new_dir = _instance_dir(target_subdomain, domain_mode)
    if os.path.exists(new_dir) and os.path.realpath(old_dir) != os.path.realpath(new_dir):
        return None, "The target data directory already exists."

    was_running = inst["status"] == "running"
    port = int(inst["port"]) if inst["port"] is not None else None
    if was_running:
        _stop_process(old_dir, port=port)

    moved = _move_dir_safely(old_dir, new_dir)
    if not moved and os.path.isdir(old_dir):
        return None, "Failed to move the instance data directory."

    try:
        update_instance_identity(instance_id, target_subdomain, domain_mode)
    except ValueError as exc:
        if moved:
            _move_dir_safely(new_dir, old_dir)
        if str(exc) == "subdomain_in_use":
            return None, "That name is already in use."
        return None, "Failed to update the instance identity."

    _invalidate_instance_caches(old_subdomain)
    _invalidate_instance_caches(target_subdomain)

    refreshed = get_instance(instance_id)
    if refreshed is None:
        return None, "Instance not found."
    refreshed_dict = dict(refreshed)

    if was_running:
        started = _start_process(refreshed_dict, new_dir, await_http_ready=False)
        if not started:
            return refreshed_dict, (
                "The wiki was renamed, but the process did not restart. "
                "Check the instance logs."
            )

    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change(
            "instance_identity_changed",
            (
                f"Instance '{old_subdomain}' ({old_mode}) renamed to "
                f"'{target_subdomain}' ({domain_mode})"
            ),
        )
    except Exception:
        pass

    return refreshed_dict, None


def convert_account_apex_instances_to_hosting(account_id):
    """Convert an account's active apex instances to hosting mode safely."""
    converted = []
    for inst in get_instances_for_account(account_id):
        if inst["status"] == "terminated" or _domain_mode_of(inst) != "apex":
            continue
        new_inst, error = change_instance_identity(
            inst["id"],
            inst["subdomain"],
            "hosting",
            account_is_admin=False,
            auto_suffix=True,
        )
        if error:
            logger.warning(
                "Failed to convert apex instance %s during admin demotion: %s",
                inst["id"], error,
            )
            continue
        converted.append((inst["id"], inst["subdomain"], new_inst["subdomain"]))
    return converted


def terminate_instance(instance_id, *, reason="manual"):
    """Stop an instance and start its grace-period.

    The instance row is moved to ``terminated`` and its subdomain is
    archived.  The data directory is **not** wiped immediately when a
    grace period is configured, instead it is renamed to match the
    archived subdomain so the original name is free to reuse, and a
    ``data_retained_until`` timestamp is recorded so the periodic cleanup
    will hard-delete it once the grace window elapses.

    When the grace period is set to zero, the data directory is wiped
    immediately (legacy behavior).

    Returns ``(True, "")`` or ``(False, reason)``.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Instance is already terminated."

    data_dir = _instance_dir_for(inst)
    port = int(inst["port"]) if inst["port"] is not None else None
    _stop_process(data_dir, port=port)
    _invalidate_instance_caches(inst["subdomain"])

    grace_days = get_grace_period_days()
    ok, archived = terminate_and_archive_instance(
        instance_id,
        inst["subdomain"],
        grace_period_days=grace_days,
        reason=reason,
    )

    if grace_days > 0:
        # Retain data for the grace window: move it from the original
        # subdomain path to the archived subdomain path so the original
        # name is free to reuse while the data stays on disk.  Preserve
        # the row's domain_mode so apex archives stay namespaced under
        # ``__apex`` just like the live data dir was.
        if archived and os.path.isdir(data_dir):
            archived_dir = _instance_dir(archived, _domain_mode_of(inst))
            _move_dir_safely(data_dir, archived_dir)
    else:
        # Legacy behavior: wipe immediately.
        if os.path.isdir(data_dir):
            shutil.rmtree(data_dir, ignore_errors=True)

    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change(
            "instance_terminated",
            f"Instance '{inst['subdomain']}' terminated",
        )
    except Exception:
        pass

    return True, ""


def terminate_expired():
    """Terminate all instances that have exceeded their lifetime.

    Each terminated instance enters the configured grace period (see
    :func:`terminate_instance`) with ``terminated_reason='expired'`` so
    admins can distinguish auto-expired instances from manually
    terminated ones.

    Returns the number of instances terminated.
    """
    expired = get_expired_instances()
    count = 0
    for inst in expired:
        ok, _ = terminate_instance(inst["id"], reason="expired")
        if ok:
            count += 1
    return count


def purge_expired_grace_periods():
    """Hard-delete data dirs and DB rows for terminated instances past their grace window.

    Iterates through every instance whose ``data_retained_until`` is in the
    past, removes the on-disk data directory (best-effort), and deletes the
    instance row from the database (cascading to collaborators, suspension
    audit, and ownership-transfer records).

    Returns the number of instances fully deleted.  Designed to be called
    from the existing 5-minute periodic cleanup task.
    """
    rows = get_instances_with_expired_grace_period()
    purged = 0
    for inst in rows:
        try:
            data_dir = _instance_dir_for(inst)
        except ValueError:
            logger.warning(
                "Skipping grace-period purge for instance %s: invalid subdomain %r",
                inst["id"], inst["subdomain"],
            )
            continue
        if os.path.isdir(data_dir):
            shutil.rmtree(data_dir, ignore_errors=True)
        # Drop the host-owned recovery snapshots and quarantine marker too.
        shutil.rmtree(platform_state_dir(inst["id"]), ignore_errors=True)
        delete_instance(inst["id"])
        purged += 1
        logger.info(
            "Fully deleted terminated instance %s (subdomain=%s) after grace period expired",
            inst["id"], inst["subdomain"],
        )
    purge_deleted_account_tombstones()
    return purged


def enforce_storage_quotas(*, overrun_ratio=1.10):
    """Suspend active non-admin instances that materially exceed storage.

    The in-wiki request guard prevents ordinary writes at the exact limit.  A
    supervisor-level check is still required for background TTS output,
    interrupted imports, or older workers that have not yet reloaded their
    environment.  A small margin avoids suspending for SQLite WAL jitter.
    """
    from .db import get_all_active_instances

    suspended = 0
    for row in get_all_active_instances():
        inst = dict(row)
        if inst.get("status") not in ("running", "stopped"):
            continue
        owner = get_account_by_id(inst.get("account_id"))
        inst["account_is_admin"] = bool(owner and owner.get("is_admin"))
        limit_mb = _instance_storage_limit_mb(inst)
        if limit_mb is None:
            continue
        used = get_storage_bytes(_instance_dir_for(inst), force_refresh=True)
        limit = int(limit_mb) * 1024 * 1024
        if used <= int(limit * max(1.0, float(overrun_ratio))):
            continue
        ok, _reason = suspend_instance(
            inst["id"],
            reason=(
                f"Automatic storage protection: {used / (1024 * 1024):.1f} MB "
                f"used, {limit_mb} MB allowed. Contact the platform administrator."
            ),
            reason_visible=True,
            time_visible=False,
            performed_by="system:storage-quota",
        )
        if ok:
            suspended += 1
            logger.warning(
                "Suspended instance %s after storage overrun (%d > %d bytes)",
                inst.get("subdomain"), used, limit,
            )
    return suspended


def cleanup_expired_instance_suspensions():
    """Auto-unsuspend instances whose timed suspension has elapsed.

    Only affects instances with ``status = 'suspended'`` **and** a non-NULL
    ``suspended_until`` that is in the past.  Permanent suspensions
    (``suspended_until IS NULL``) are never touched.

    Returns the number of instances unsuspended.
    """
    from .db import get_expired_instance_suspensions
    ids = get_expired_instance_suspensions()
    count = 0
    for instance_id in ids:
        ok, _ = unsuspend_instance(instance_id)
        if ok:
            count += 1
    if count:
        logger.info("Auto-unsuspended %d instance(s) with expired timed suspensions", count)
    return count


def extend_instance(instance_id, extra_days):
    """Extend the expiry of an instance by *extra_days* days.

    Updates the hosting service expiry for an existing instance.

    Returns ``(True, "")`` or ``(False, reason)``.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot extend a terminated instance."

    # Apex instances are intentionally perpetual (``expires_at = NULL``)
    # and have no expiry to extend.  Treat the call as a no-op success
    # rather than spuriously rewriting the expiry to ``now + extra_days``.
    if _domain_mode_of(inst) == "apex":
        return True, ""

    now = datetime.now(timezone.utc)

    try:
        current_expiry = datetime.fromisoformat(inst["expires_at"]).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        current_expiry = now

    new_expiry = current_expiry + timedelta(days=extra_days)
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET expires_at=? WHERE id=?",
            (new_expiry.isoformat(), instance_id),
        )
        conn.commit()


    return True, ""


def set_instance_expiry(instance_id, expires_at):
    """Set an instance expiry and keep lifecycle state in sync.

    ``expires_at`` must be a timezone-aware UTC-ish ``datetime``.  When
    the new expiry is already due, the instance is immediately moved
    through the normal expired-instance termination flow so it enters
    the configured grace period instead of lingering as running/stopped.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify a terminated instance."
    if _domain_mode_of(inst) == "apex":
        return False, "Apex instances are perpetual and cannot be given an expiry."
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)

    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET expires_at=? WHERE id=?",
            (expires_at.isoformat(), instance_id),
        )
        conn.commit()

    refreshed = get_instance(instance_id)
    if refreshed is None:
        return False, "Instance not found."

    now = datetime.now(timezone.utc)
    if expires_at <= now:
        return terminate_instance(instance_id, reason="expired")

    return True, ""


def _tenant_table_exists(conn, name):
    """Return True when a tenant DB has a table called *name*."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _revoke_tenant_user_api_tokens(conn, user_ids):
    """Deactivate the wiki API tokens of *user_ids* in a tenant DB.

    A password reset from the portal is a recovery action.  The wiki checks a
    token only against its own state, so a token minted by whoever had the
    old password would otherwise keep working after the reset.  Does the
    same as the wiki's own ``revoke_user_api_service_tokens`` and nothing
    when the API service table does not exist.  Returns the number revoked.
    """
    if not user_ids or not _tenant_table_exists(conn, "api_service__tokens"):
        return 0
    revoked = 0
    for user_id in user_ids:
        revoked += conn.execute(
            "UPDATE api_service__tokens SET active=0 WHERE user_id=? AND active=1",
            (user_id,),
        ).rowcount
    return revoked


def reset_instance_password(instance_id):
    """Generate and install a new admin password for a BananaWiki instance.

    Updates the admin user's password directly in the instance's SQLite
    database, revokes that user's wiki API tokens, and stores the new
    plaintext password in ``admin_password_plain`` so the owner sees it once
    on their instance detail page (mirroring the initial-provisioning flow).

    Returns ``(True, new_password)`` or ``(False, reason)``.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot reset password on a terminated instance."

    admin_username = inst["admin_username"] or "admin"
    new_password = _generate_temp_password()
    hashed_pw = generate_password_hash(new_password)

    data_dir = _instance_dir_for(inst)
    db_path = os.path.join(data_dir, "bananawiki.db")

    if not os.path.lexists(db_path):
        return False, "Instance database not found. The instance may not have been fully provisioned yet."

    try:
        # The tenant may be running, so open its DB without following a link
        # it planted there.
        conn = connect_tenant_db(data_dir, db_path)
    except (OSError, ValueError):
        return False, t("hosting.instance_ops.db_path_unsafe", default="The instance database path is not safe to open.")
    except sqlite3.Error:
        logger.exception("Failed to open the DB of instance %s", instance_id)
        return False, "Failed to update the instance password."

    try:
        conn.execute("PRAGMA journal_mode=WAL")

        # Ensure the force_password_change column exists (safe to re-run)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN force_password_change INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Reset the provisioned admin account; if it is gone, every admin and
        # owner account, so the platform can always get someone back in.
        user_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM users WHERE username=? COLLATE NOCASE",
                (admin_username,),
            )
        ]
        if not user_ids:
            user_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM users WHERE role IN ('admin', 'owner')"
                )
            ]
        if not user_ids:
            return False, "Admin user not found in the instance database."
        conn.executemany(
            "UPDATE users SET password=?, force_password_change=1 WHERE id=?",
            [(hashed_pw, user_id) for user_id in user_ids],
        )
        revoked = _revoke_tenant_user_api_tokens(conn, user_ids)
        conn.commit()
        if revoked:
            logger.info(
                "Password reset for instance %s revoked %d wiki API token(s)",
                instance_id, revoked,
            )
    except Exception:
        logger.exception("Failed to reset password for instance %s", instance_id)
        return False, "Failed to update the instance password."
    finally:
        conn.close()

    # Store the new plaintext password so it is displayed once on the
    # instance detail page, matching the initial-provisioning UX.
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET admin_username=?, admin_password_plain=? WHERE id=?",
            (admin_username, new_password, instance_id),
        )
        conn.commit()

    return True, new_password


def reset_wiki(instance_id):
    """Wipe an instance's content and re-seed it to factory defaults.

    Stops the running process, deletes the instance database and uploads,
    recreates the data directory structure, re-seeds the database with a
    fresh admin user, and restarts the process.  The instance keeps its
    subdomain, port, and hosting record.

    Returns ``(True, new_password)`` or ``(False, reason)``.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot reset a terminated instance."
    if inst["status"] == "suspended":
        return False, "Cannot reset a suspended instance."

    # Stop the process first
    stop_ok, stop_reason = stop_instance(instance_id)
    if not stop_ok and stop_reason != "already stopped":
        return False, f"Failed to stop instance: {stop_reason}"

    data_dir = _instance_dir_for(inst)
    db_path = os.path.join(data_dir, "bananawiki.db")

    # Remove the database file (a link planted there is removed as a link)
    if os.path.lexists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            logger.exception("Failed to remove DB for reset: %s", db_path)
            return False, "Failed to remove instance database."

    # Remove the SQLite sidecars.  os.remove() deletes a link itself and
    # never what it points at.
    for suffix in ("-wal", "-shm", "-journal"):
        wal_path = db_path + suffix
        if os.path.lexists(wal_path):
            try:
                os.remove(wal_path)
            except OSError:
                pass

    # Remove uploads and attachments (but keep the directory structure).
    # The tenant can replace "storage" or any folder in it with a link, and
    # shutil.rmtree() on data_dir/storage/<name> would then empty whatever
    # that link points at, such as another tenant's uploads.  So only real
    # folders are deleted; a link in their place is removed as a link.
    storage_dir = os.path.join(data_dir, "storage")
    has_storage = os.path.lexists(storage_dir)
    if os.path.islink(storage_dir) or (has_storage and not os.path.isdir(storage_dir)):
        os.remove(storage_dir)
    if has_storage:
        os.makedirs(storage_dir, exist_ok=True)
        for name in _TENANT_ASSET_DIRS:
            target = os.path.join(storage_dir, name)
            if os.path.islink(target) or (
                os.path.lexists(target) and not os.path.isdir(target)
            ):
                os.remove(target)
            elif os.path.isdir(target):
                try:
                    shutil.rmtree(target)
                except OSError:
                    pass
            # Re-create the empty directory
            os.makedirs(target, exist_ok=True)

    # Also remove legacy asset folders (real folders only; the portal's own
    # <name> -> storage/<name> links stay in place).
    for name in _TENANT_ASSET_DIRS:
        legacy = os.path.join(data_dir, name)
        if os.path.isdir(legacy) and not os.path.islink(legacy):
            try:
                shutil.rmtree(legacy)
            except OSError:
                pass

    # Re-seed the database
    admin_username = inst["admin_username"] or "admin"
    new_password = _generate_temp_password()
    from .db import get_hosting_settings
    _hs = get_hosting_settings()
    _global_tour_enabled = bool(_hs and _hs.get("global_tour_enabled"))
    seeded = _seed_instance_db(db_path, admin_username, new_password, global_tour_enabled=_global_tour_enabled)
    if not seeded:
        return False, "Failed to re-seed instance database."

    # A plugin quarantine deliberately survives a factory reset: the tenant's
    # owner can start a reset, and it leaves external_plugins on disk, so
    # clearing the quarantine here would let the owner bring back a plugin
    # the operator stopped.  Only an operator lifts it.

    # Store the new plaintext password for the owner to see once
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET admin_password_plain=? WHERE id=?",
            (new_password, instance_id),
        )
        conn.commit()

    # Restart the process
    restart_ok, restart_reason = restart_instance(instance_id)
    if not restart_ok:
        return False, f"Database reset but failed to restart: {restart_reason}"

    return True, new_password


def toggle_easy_wiki(instance_id, enable):
    """Toggle EasyWiki mode on a hosted instance.

    Stops the instance, updates the ``easy_wiki`` flag in the hosting
    database, then restarts the process so the new environment variable
    ``BW_EASY_WIKI`` takes effect.

    Parameters
    ----------
    instance_id : str
        ID of the instance to toggle.
    enable : bool
        ``True`` to switch to EasyWiki mode, ``False`` for full
        BananaWiki.

    Returns ``(True, "")`` on success or ``(False, reason)`` on failure.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify a terminated instance."
    if inst["status"] == "suspended":
        return False, "Cannot modify a suspended instance."

    new_value = 1 if enable else 0
    if bool(inst.get("easy_wiki")) == enable:
        return False, "Wiki is already in EasyWiki mode." if enable else "Wiki is already in full mode."

    # Stop the instance so the environment variable takes effect on restart.
    if inst["status"] == "running":
        stop_ok, stop_reason = stop_instance(instance_id)
        if not stop_ok and stop_reason != "already stopped":
            return False, f"Failed to stop instance: {stop_reason}"

    # Update the flag.
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET easy_wiki=? WHERE id=?",
            (new_value, instance_id),
        )
        conn.commit()

    # Restart the instance with the updated environment.
    restart_ok, restart_reason = restart_instance(instance_id)
    if not restart_ok:
        return False, f"Flag updated but failed to restart: {restart_reason}"

    return True, ""


def make_instance_indefinite(instance_id):
    """Remove the expiry limit from a BananaWiki instance.

    Sets the hosting service expiry 100 years into the future.

    Returns ``(True, "")`` or ``(False, reason)``.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify a terminated instance."

    now = datetime.now(timezone.utc)
    far_future = (now + timedelta(days=365 * 100)).isoformat()

    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET expires_at=? WHERE id=?",
            (far_future, instance_id),
        )
        conn.commit()


    return True, ""


def set_instance_storage_limit(instance_id, storage_limit_mb=None):
    """Set a per-instance storage cap in MB; ``None`` means platform default.

    Pass ``0`` to mark the instance as unlimited.
    """
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify a terminated instance."
    inst_dict = dict(inst)
    if str(inst_dict.get("domain_mode") or "hosting") == "apex":
        storage_limit_mb = 0

    value = None if storage_limit_mb is None else int(storage_limit_mb)
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET storage_limit_mb=? WHERE id=?",
            (value, instance_id),
        )
        conn.commit()
    # The hard request guard receives its ceiling through the process
    # environment, so a running instance must reload after a quota change.
    if inst["status"] == "running":
        ok, reason = restart_instance(instance_id)
        if not ok:
            return False, f"Quota saved, but the instance could not restart: {reason}"
    return True, ""


def apply_upload_policy_to_instance(instance_id, *, restart_running=False):
    """Sync the effective hosting upload policy into one wiki database."""
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify a terminated instance."
    data_dir = _instance_dir_for(inst)
    policy = get_effective_instance_upload_policy(inst)
    db_path = _instance_db_path(data_dir)
    if os.path.isfile(db_path):
        _apply_upload_policy_to_instance_db(db_path, policy)
    if restart_running and inst["status"] == "running":
        ok, reason = force_restart_instance(instance_id)
        if not ok:
            return False, reason
    return True, ""


def apply_upload_policy_to_all_instances(*, restart_running=False):
    """Sync the effective hosting upload policy into every active wiki."""
    synced = 0
    restarted = 0
    failed = 0
    for inst in get_all_active_instances():
        ok, _reason = apply_upload_policy_to_instance(
            inst["id"], restart_running=restart_running,
        )
        if ok:
            synced += 1
            if restart_running and inst["status"] == "running":
                restarted += 1
        else:
            failed += 1
    return {"synced": synced, "restarted": restarted, "failed": failed}


def set_instance_upload_policy(instance_id, upload_max_size_mb=None, upload_blocked_extensions=""):
    """Set per-instance wiki upload controls from the hosting admin UI."""
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify a terminated instance."
    size_value = None
    if upload_max_size_mb not in (None, ""):
        size_value = _bounded_upload_size_mb(upload_max_size_mb)
    blocked = _normalize_upload_extensions(upload_blocked_extensions)
    if not db_set_instance_upload_policy(instance_id, size_value, blocked):
        return False, "Failed to update upload policy."
    return apply_upload_policy_to_instance(instance_id, restart_running=True)


def force_restart_instance(instance_id):
    """Hard stop and re-start an instance, clearing all stale state.

    The regular :func:`restart_instance` path refuses to act when an
    instance is marked ``running`` and the cached health probe returns
    True.  After a VPS reboot we routinely see the opposite failure
    mode: the row says ``running``, the recorded PID happens to be
    alive (frequently because PID values were reused, e.g. systemd at
    PID 1, dbus, sshd, ...), but no Gunicorn we own is bound to the
    instance's port.  Visitors then see the proxy's "Starting up…"
    splash forever and the only manual fix is "pause then resume".

    This helper short-circuits that loop by:

    1. Force-stopping any process the PID file points at (best-effort).
    2. Deleting the PID + boot-id sidecar so callers cannot mistake an
       unrelated process for a healthy instance.
    3. Wiping cached health/storage results.
    4. Marking the instance ``stopped`` and then re-running
       :func:`restart_instance`, which performs the full start sequence
       with retries.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot restart a terminated instance."
    if inst["status"] == "suspended":
        return False, "Cannot restart a suspended instance: unsuspend it first."

    data_dir = _instance_dir_for(inst)
    port = int(inst["port"]) if inst["port"] is not None else None

    # Best-effort stop.  We swallow errors here because the PID may
    # already be dead: what we really care about is that the PID file
    # is gone so the next ``_start_process`` call doesn't think we are
    # already running.
    try:
        _stop_process(data_dir, port=port)
    except Exception:
        logger.exception("force_restart_instance: stop step failed for %s", instance_id)

    # Strip any PID + boot-id sidecar that survived the stop (typically
    # because the recorded PID belonged to an unrelated process after a
    # reboot and ``_stop_process`` refused to signal it).
    pid_path = _pid_file(data_dir)
    try:
        os.unlink(pid_path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("force_restart_instance: could not remove PID file %s", pid_path)
    boot_id_path = _boot_id_path(data_dir)
    try:
        os.unlink(boot_id_path)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    # Also clear the ``.starting`` lock so lazy recovery doesn't think
    # another worker is already trying to bring this instance back.
    starting_lock = os.path.join(data_dir, ".starting")
    try:
        os.unlink(starting_lock)
    except FileNotFoundError:
        pass
    except OSError:
        pass

    _invalidate_instance_caches(inst["subdomain"])
    if not update_instance_status(instance_id, "stopped", expected_status=inst["status"]):
        return False, "The instance changed state while it was being restarted."
    return restart_instance(instance_id)


def hard_delete_terminated_instance(instance_id):
    """Delete a terminated instance row and any retained on-disk data."""
    from .db import get_hosting_db_context

    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] != "terminated":
        return False, "Only terminated instances can be deleted."
    try:
        data_dir = _instance_dir_for(inst)
    except ValueError:
        data_dir = None
    if data_dir and os.path.isdir(data_dir):
        shutil.rmtree(data_dir, ignore_errors=True)
    # Drop the host-owned recovery snapshots and quarantine marker too.
    shutil.rmtree(platform_state_dir(instance_id), ignore_errors=True)
    with get_hosting_db_context() as conn:
        conn.execute("DELETE FROM instances WHERE id=?", (instance_id,))
        conn.commit()
    return True, ""


# The most tenant users the portal reads and renders at once.  A tenant admin
# can bulk-create users, so the portal pages never load the whole table.
_INSTANCE_USER_LIST_LIMIT = 200


def list_instance_users_page(instance_id, *, limit=_INSTANCE_USER_LIST_LIMIT, offset=0):
    """Return ``(users, total)`` for one page of a hosted wiki's users.

    *users* holds at most *limit* dicts with ``username``, ``role`` and
    ``created_at``, oldest first, starting at *offset*; *total* is the number
    of users in the wiki.  The tenant may be running, so the DB is opened
    read-only through :func:`connect_tenant_db`.  Returns ``([], 0)`` when
    the instance or its database cannot be read.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return [], 0
    data_dir = _instance_dir_for(inst)
    db_path = os.path.join(data_dir, "bananawiki.db")
    if not os.path.lexists(db_path):
        return [], 0
    try:
        limit = max(1, min(int(limit), _INSTANCE_USER_LIST_LIMIT))
    except (TypeError, ValueError):
        limit = _INSTANCE_USER_LIST_LIMIT
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    try:
        conn = connect_tenant_db(
            data_dir, db_path, read_only=True, timeout=10, max_seconds=10
        )
    except (OSError, ValueError, sqlite3.Error):
        logger.warning(
            "Not listing users of %s: its database is not a regular file "
            "inside the instance directory or cannot be opened", instance_id,
        )
        return [], 0
    try:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        # Column values are tenant data too; cap what one row can pull into
        # the portal's memory.
        rows = conn.execute(
            "SELECT substr(username, 1, 200), substr(role, 1, 40), "
            "substr(created_at, 1, 40) FROM users "
            "ORDER BY created_at, username LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    except Exception:
        logger.exception("Failed to list users for instance %s", instance_id)
        return [], 0
    finally:
        conn.close()
    users = [{"username": r[0], "role": r[1], "created_at": r[2]} for r in rows]
    return users, int(total)


def list_instance_users(instance_id, *, limit=_INSTANCE_USER_LIST_LIMIT):
    """Return up to *limit* users of a hosted wiki, oldest first.

    Each item is a dict with ``username``, ``role``, and ``created_at`` keys.
    Returns an empty list if the instance or its database cannot be read.
    Use :func:`list_instance_users_page` to also get the total.
    """
    users, _total = list_instance_users_page(instance_id, limit=limit)
    return users


def set_instance_user_password(instance_id, username, password, *, role="user"):
    """Create or update a user directly in a hosted wiki DB.

    Setting the password of an existing user is a recovery action, so it
    also revokes that user's wiki API tokens.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify users on a terminated instance."
    uname = (username or "").strip()
    if not uname:
        return False, "Username is required."
    if not password:
        return False, "Password is required."
    role = (role or "user").strip().lower()
    if role not in ("user", "editor", "admin", "owner"):
        return False, "Invalid role."

    data_dir = _instance_dir_for(inst)
    db_path = os.path.join(data_dir, "bananawiki.db")
    if not os.path.lexists(db_path):
        return False, "Instance database not found."
    hashed = generate_password_hash(password)
    try:
        conn = connect_tenant_db(data_dir, db_path)
    except (OSError, ValueError):
        return False, t("hosting.instance_ops.db_path_unsafe", default="The instance database path is not safe to open.")
    except sqlite3.Error:
        logger.exception("Failed to open the DB of instance %s", instance_id)
        return False, "Failed to update the instance user."
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE username=? COLLATE NOCASE",
            (uname,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET password=?, role=? WHERE id=?",
                (hashed, role, row[0]),
            )
            _revoke_tenant_user_api_tokens(conn, [row[0]])
        else:
            user_id = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            conn.execute(
                "INSERT INTO users (id, username, password, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, uname, hashed, role, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    except Exception:
        logger.exception("Failed to set instance user password for %s", instance_id)
        return False, "Failed to update the instance user."
    finally:
        conn.close()
    return True, ""


def remove_instance_user(instance_id, username):
    """Delete a user from a hosted wiki DB by username."""
    inst = get_instance(instance_id)
    if inst is None:
        return False, "Instance not found."
    if inst["status"] == "terminated":
        return False, "Cannot modify users on a terminated instance."
    uname = (username or "").strip()
    if not uname:
        return False, "Username is required."

    data_dir = _instance_dir_for(inst)
    db_path = os.path.join(data_dir, "bananawiki.db")
    if not os.path.lexists(db_path):
        return False, "Instance database not found."
    try:
        conn = connect_tenant_db(data_dir, db_path)
    except (OSError, ValueError):
        return False, t("hosting.instance_ops.db_path_unsafe", default="The instance database path is not safe to open.")
    except sqlite3.Error:
        logger.exception("Failed to open the DB of instance %s", instance_id)
        return False, "Failed to remove the instance user."
    try:
        row = conn.execute(
            "SELECT role FROM users WHERE username=? COLLATE NOCASE",
            (uname,),
        ).fetchone()
        if row is None:
            return False, "User not found."
        if row[0] == "owner":
            return False, "Protected admin users cannot be removed."
        conn.execute("DELETE FROM users WHERE username=? COLLATE NOCASE", (uname,))
        conn.commit()
    except Exception:
        logger.exception("Failed to remove user for instance %s", instance_id)
        return False, "Failed to remove the instance user."
    finally:
        conn.close()
    return True, ""


def _pick_restore_subdomain(instance_id, original, *, domain_mode="hosting"):
    """Choose a free subdomain for a restored instance.

    Prefers the original name when it's available, otherwise appends a
    short random suffix to deconflict.  Returns ``None`` when no usable
    subdomain can be derived.

    ``domain_mode`` scopes the uniqueness check so that a restored
    apex-mode instance can land on a slug that happens to be claimed by
    an unrelated hosting-mode instance (and vice versa).  The composite
    UNIQUE on ``(subdomain, domain_mode)`` enforces the same scoping at
    the SQL level when ``restore_instance_in_place`` runs.
    """
    if not original:
        return None
    existing = get_instance_by_subdomain(original, domain_mode=domain_mode)
    if existing is None or existing["id"] == instance_id:
        return original
    for _ in range(8):
        suffix = secrets.token_hex(2)
        candidate = f"{original}-r{suffix}"
        if len(candidate) > config.SUBDOMAIN_MAX_LENGTH:
            candidate = candidate[:config.SUBDOMAIN_MAX_LENGTH].rstrip("-") or candidate[:config.SUBDOMAIN_MAX_LENGTH]
        if get_instance_by_subdomain(candidate, domain_mode=domain_mode) is None:
            return candidate
    return None


def restore_instance(instance_id, *, extend_days=None):
    """Restore a terminated instance.

    When the instance is still inside its grace window and its data
    directory is on disk, this performs a **true restore**: a new port is
    allocated, the data dir is moved back to the original (or a
    deconflicted) subdomain path, the row is flipped to ``running`` with
    a fresh ``expires_at``, and the runtime is restarted.

    When no retained data is available (legacy terminated instances or
    instances whose grace window has already been purged), it falls back
    to provisioning a brand-new instance under the original subdomain so
    existing admin flows keep working.

    ``extend_days`` controls how far in the future the new ``expires_at``
    is set; defaults to :data:`config.INSTANCE_DURATION_DAYS`.  Pass a
    larger value to grant the owner additional time when restoring.

    Returns ``(instance_dict, None)`` on success or ``(None, reason)`` on
    failure, same shape as :func:`provision_instance`.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return None, "Instance not found."
    if inst["status"] != "terminated":
        return None, "Only terminated instances can be restored."

    archived_subdomain = inst["subdomain"]
    original_subdomain = _original_subdomain_from_archived(archived_subdomain)
    if not original_subdomain:
        return None, "Could not determine the original subdomain from the terminated instance."

    # Preserve the original ``domain_mode`` (apex vs hosting) so the
    # restored row stays in the same namespace and the deconflicting
    # uniqueness check below only looks at peer rows in that namespace.
    own_domain_mode = (
        inst["domain_mode"] if "domain_mode" in inst.keys() else "hosting"
    ) or "hosting"

    archived_data_dir = _instance_dir(archived_subdomain, own_domain_mode)
    has_retained_data = (
        inst["data_retained_until"] is not None
        and os.path.isdir(archived_data_dir)
    )

    duration_days = (
        int(extend_days)
        if extend_days is not None and int(extend_days) > 0
        else config.INSTANCE_DURATION_DAYS
    )

    if not has_retained_data:
        # Legacy / purged instance: fall back to provisioning a fresh one.
        new_inst, error = provision_instance(
            inst["account_id"],
            original_subdomain,
            domain_mode=own_domain_mode,
            account_is_admin=(own_domain_mode == "apex"),
        )
        if new_inst and extend_days is not None and int(extend_days) > config.INSTANCE_DURATION_DAYS:
            extra = int(extend_days) - config.INSTANCE_DURATION_DAYS
            extend_instance(new_inst["id"], extra)
            new_inst = dict(get_instance(new_inst["id"]) or new_inst)
            new_inst["url"] = _instance_url(new_inst)
        return new_inst, error

    # True in-place restore: keep the existing instance row and data dir.
    target_subdomain = _pick_restore_subdomain(
        instance_id, original_subdomain, domain_mode=own_domain_mode,
    )
    if not target_subdomain:
        return None, "Could not pick a free subdomain for restore."

    # Apex instances are intentionally perpetual; preserve the NULL
    # expires_at when restoring instead of stamping a 14-day trial.
    if own_domain_mode == "apex":
        expires_at = None
    else:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=duration_days)).isoformat()
    ok, port, reason_code = restore_instance_in_place(
        instance_id, target_subdomain, expires_at,
    )
    if not ok:
        if reason_code == "subdomain_in_use":
            return None, "The original name is in use; could not restore."
        if reason_code == "no_free_port":
            return None, "No available ports.  Please try again later."
        return None, "The instance could not be restored."

    # Move the data dir from the archived subdomain back to the target
    # subdomain so the running process can find it.
    target_data_dir = _instance_dir(target_subdomain, own_domain_mode)
    if archived_data_dir != target_data_dir:
        _move_dir_safely(archived_data_dir, target_data_dir)

    inst = dict(get_instance(instance_id))
    inst["url"] = _instance_url(inst)

    # Boot the process again from the retained data.
    try:
        started = _start_process(inst, target_data_dir)
    except Exception:
        logger.exception("Unhandled error starting restored instance %s", instance_id)
        update_instance_status(instance_id, "stopped")
        return None, "An unexpected error occurred while starting the restored instance."
    if not started:
        # Roll the row back to terminated so the admin can retry: keep
        # the data so the next attempt still has it.
        update_instance_status(instance_id, "stopped")
        logger.error(
            "Restored instance %s failed to start; left in stopped state with data retained.",
            instance_id,
        )
        return None, (
            "The instance was restored but the process did not become reachable. "
            "It is now in the 'stopped' state: try restarting it."
        )

    _invalidate_instance_caches(target_subdomain)
    # Pre-warm the health cache so the first proxy request after restore
    # hits cache instead of doing a cold health check.
    _set_cached_health(target_subdomain, True)


    try:
        from .backups import notify_change as hosting_notify_change
        hosting_notify_change(
            "instance_backup_restored",
            f"Instance '{target_subdomain}' restored from grace-period",
        )
    except Exception:
        pass

    return inst, None


def recover_running_instances():
    """Start processes for all instances marked as running in the database.

    Called once on hosting portal startup to recover from restarts.
    Returns the number of instances recovered.

    Recovery happens in parallel because each ``_start_process`` call
    blocks for up to ``_STARTUP_TIMEOUT + _HTTP_READY_TIMEOUT`` seconds
    waiting for Gunicorn to become reachable.  Doing this serially after
    a VPS restart with N instances means the Nth instance only starts
    booting after roughly ``N * 60`` seconds: long enough for the
    proxy to return 404s for instances that haven't been visited yet.

    Run this from the dedicated maintenance service or the portal's
    background recovery thread. Each start has bounded attempts/timeouts;
    collect every result, including later waves, before reporting completion.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Portal startup is where an operator reads the log, so say it here when
    # tenant containers can reach the network.
    container_runtime.warn_if_tenant_network_outbound()

    active = get_all_active_instances()
    pending = []
    for inst in active:
        if inst["status"] != "running":
            continue
        inst_dict = dict(inst)
        data_dir = _instance_dir_for(inst_dict)
        if not os.path.isdir(data_dir):
            logger.warning(
                "Instance %s data directory missing at %s: skipping recovery",
                inst_dict["id"], data_dir,
            )
            continue

        port = int(inst_dict["port"]) if inst_dict.get("port") is not None else None

        # Clean up stale PID files before checking liveness.  Passing the
        # port ensures PIDs left over from a previous boot are not treated
        # as live (their numeric value often matches an unrelated process
        # after a reboot).
        _clean_stale_pid(data_dir, port=port)

        pid = _read_pid(data_dir)
        if _is_our_instance_process(pid, data_dir, port=port):
            if _instance_http_ready(inst_dict):
                continue  # Already running and serving HTTP.
            logger.warning(
                "Instance %s has a live process but is not HTTP-ready; "
                "queuing it for recovery",
                inst_dict["id"],
            )

        # Detect port conflicts: if the port is already accepting connections
        # and it is NOT our process, log a warning so the admin can investigate.
        if port is not None:
            bind_host = _instance_bind_host()
            connect_host = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
            try:
                if _is_port_accepting(connect_host, port, timeout=0.3):
                    # The port is occupied but _is_our_instance_process returned
                    # False above: some other process has taken this port.
                    logger.warning(
                        "Instance %s port %d is occupied by another process: "
                        "recovery will preserve that listener",
                        inst_dict["id"], port,
                    )
            except Exception:
                pass

        pending.append((inst_dict, data_dir))

    if not pending:
        logger.info("Recovery: all %d running instance(s) already healthy", len(active))
        return 0

    # Cap concurrency to avoid spawning hundreds of Gunicorn masters at
    # once on hosts with lots of instances.  Recovery still runs cleanly
    # in waves of `max_workers` instances.
    max_workers = min(_RECOVERY_MAX_WORKERS, len(pending))
    count = 0

    def _recover_one(inst_dict, data_dir):
        try:
            if _start_process(inst_dict, data_dir):
                logger.info("Instance %s recovered successfully", inst_dict["id"])
                return True
            logger.error(
                "Instance %s failed to recover after %d attempt(s)",
                inst_dict["id"], _START_ATTEMPTS,
            )
        except Exception:
            logger.exception("Unexpected error recovering instance %s", inst_dict["id"])
        return False

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bw-recover") as pool:
        futures = {
            pool.submit(_recover_one, inst_dict, data_dir): inst_dict["id"]
            for inst_dict, data_dir in pending
        }
        for fut in as_completed(futures):
            try:
                if fut.result():
                    count += 1
            except Exception:
                logger.exception("Recovery worker for instance %s crashed", futures[fut])

    logger.info(
        "Recovery complete: %d succeeded, %d failed out of %d pending",
        count, len(pending) - count, len(pending),
    )
    return count


def _seed_demo_content(db_path, admin_user_id, preset):
    """Populate an instance database with demo articles, kanban boards, and canvases.

    Uses raw SQL against the instance's SQLite database to insert content
    defined by *preset* (a dict from ``demo_presets``).  The BananaWiki process
    will handle full schema migrations on startup, so the core tables
    (pages, kanban_boards, kanban_columns, kanban_tickets, canvas__layouts)
    are expected to already exist after :func:`_seed_instance_db`.

    Returns ``True`` on success.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=20)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")

        for title, slug, content in preset.get("articles", []):
            conn.execute(
                "INSERT OR IGNORE INTO pages (title, slug, content, last_edited_by) "
                "VALUES (?, ?, ?, ?)",
                (title, slug, content, admin_user_id),
            )

        # Set the first article as the home page
        articles = preset.get("articles", [])
        if articles:
            conn.execute(
                "UPDATE pages SET is_home=1 WHERE slug=?",
                (articles[0][1],),
            )

        for board in preset.get("kanban", []):
            cursor = conn.execute(
                "INSERT INTO kanban_boards (title, description, created_by) VALUES (?, ?, ?)",
                (board["title"], board.get("description", ""), admin_user_id),
            )
            board_id = cursor.lastrowid
            for col_idx, col in enumerate(board.get("columns", [])):
                cur_col = conn.execute(
                    "INSERT INTO kanban_columns (board_id, title, sort_order) VALUES (?, ?, ?)",
                    (board_id, col["title"], col_idx),
                )
                col_id = cur_col.lastrowid
                for ticket_idx, ticket in enumerate(col.get("tickets", [])):
                    conn.execute(
                        "INSERT INTO kanban_tickets "
                        "(column_id, title, description, priority, created_by, sort_order) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            col_id,
                            ticket["title"],
                            ticket.get("description", ""),
                            ticket.get("priority", "medium"),
                            admin_user_id,
                            ticket_idx,
                        ),
                    )

        for canvas in preset.get("canvases", []):
            conn.execute(
                "INSERT OR IGNORE INTO canvas__layouts "
                "(slug, title, description, creator_id, visibility, data) "
                "VALUES (?, ?, ?, ?, 'public', ?)",
                (
                    canvas["slug"],
                    canvas["title"],
                    canvas.get("description", ""),
                    admin_user_id,
                    canvas["data"],
                ),
            )

        conn.commit()
        conn.close()
        return True
    except Exception:
        logger.exception("Failed to seed demo content into %s", db_path)
        return False


def provision_demo_instances(account_id, preset_ids, subdomain_prefix="demo"):
    """Provision one demo instance per requested preset.

    Each instance is created with a subdomain like
    ``{subdomain_prefix}-{preset_id}`` and seeded with the preset's
    articles, kanban boards, and canvases.

    Returns a list of ``(preset_id, instance_dict_or_None, error_or_None)``
    tuples: one per requested preset.
    """
    from .demo_presets import DEMO_PRESETS_BY_ID

    results = []
    for pid in preset_ids:
        preset = DEMO_PRESETS_BY_ID.get(pid)
        if preset is None:
            results.append((pid, None, f"Preset sconosciuto: {pid}"))
            continue

        subdomain = f"{subdomain_prefix}-{pid}"
        inst, error = provision_instance(account_id, subdomain)
        if error:
            results.append((pid, None, error))
            continue

        # Seed demo content into the freshly provisioned instance DB.
        # Demo instances are always provisioned in hosting mode, so use
        # the default domain_mode for the data dir lookup.
        data_dir = _instance_dir(subdomain, "hosting")
        db_path = os.path.join(data_dir, "bananawiki.db")

        # Look up the admin user ID that was seeded during provisioning.
        admin_uid = None
        try:
            _conn = sqlite3.connect(db_path, timeout=20)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=20000")
            row = _conn.execute(
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()
            if row:
                admin_uid = row[0]
            _conn.close()
        except Exception:
            pass

        if admin_uid is None:
            admin_uid = "demo0001"

        seeded = _seed_demo_content(db_path, admin_uid, preset)
        if not seeded:
            logger.warning(
                "Demo content seeding failed for instance %s (preset %s): "
                "instance is running but empty.",
                inst["id"], pid,
            )

        results.append((pid, inst, None))

    return results
