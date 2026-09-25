"""WSGI middleware that reverse-proxies subdomain requests to instances.

In subdomain mode the nginx wildcard routes all ``*.hosting.DOMAIN``
traffic to the hosting portal.  This middleware intercepts those
requests, looks up the target instance port in the hosting database,
and proxies the request to ``127.0.0.1:<port>``, so individual
instance ports never need to be exposed to the internet.

Requests whose Host header matches the portal's own ``BASE_DOMAIN``
(without a subdomain prefix) pass through to the Flask app unmodified.
"""

import http.client
import logging
import os
import socket
import stat
import threading
import time
from html import escape as html_escape

from . import config
from ops_observability import record_http_error

logger = logging.getLogger("hosting.proxy")

# Upstream connection timeout (seconds).  Covers both connect() and any
# individual socket read/write: long enough to allow PDF export of large
# pages on the hosted instance, but short enough that a hung instance
# does not pin a portal worker indefinitely.
#
# Sized to match the portal Gunicorn worker timeout (120 s in
# hosting/gunicorn.conf.py) so a slow upstream gets cut off by *this*
# timeout (clean 504 response) instead of the worker timeout (worker
# SIGKILL: surfaces as a generic 502 with no Retry-After).  Must stay
# at-or-below the worker timeout for that ordering to hold.
_UPSTREAM_TIMEOUT = 120

# Block size used when streaming the response body back to the client.
# 64 KiB is large enough to amortise per-iteration overhead but small
# enough to keep memory bounded for huge downloads.
_STREAM_CHUNK = 65536

#  Crash isolation between instances
#
# Without these safeguards a single wedged or crashing instance could
# starve the entire hosting portal and every *other* instance:
#
# * Every request to the broken instance would block a portal Gunicorn
#   worker for the full ``_UPSTREAM_TIMEOUT`` (120 s).  N concurrent
#   browser refreshes drain the entire worker pool, and visitors of
#   completely unrelated instances see a hard 504 from the portal
#   itself.
# * Even after the instance came back, queued requests against it kept
#   slamming the upstream as fast as the broken master could refuse
#   them, multiplying its instability.
#
# Two cheap mechanisms fix both:
#
# * A **per-instance circuit breaker**.  Repeated upstream failures open
#   the breaker; while it is open the proxy short-circuits to the
#   branded "recovering" splash in <1 ms instead of attempting another
#   connection.  The breaker auto-half-opens after a cooldown so a
#   single probe request can close it once the instance is healthy.
# * A **per-instance concurrency cap**.  At most
#   ``_INSTANCE_CONCURRENCY_CAP`` requests may be in-flight to one
#   instance at a time.  Excess requests get the splash page rather than
#   stacking on top of an already-overwhelmed instance.  This is a
#   pressure-release valve, not a hard rate limit. A healthy instance
#   serving many users continues to scale up across the portal worker
#   pool naturally.
_CIRCUIT_BREAKER_FAILURE_THRESHOLD = max(
    2,
    int(os.environ.get("BW_HOSTING_CIRCUIT_FAILURE_THRESHOLD", "5")),
)
_CIRCUIT_BREAKER_COOLDOWN_SECONDS = max(
    5.0,
    float(os.environ.get("BW_HOSTING_CIRCUIT_COOLDOWN_SECONDS", "15")),
)
_INSTANCE_CONCURRENCY_CAP = max(
    1,
    int(os.environ.get("BW_HOSTING_INSTANCE_CONCURRENCY_CAP", "32")),
)

# Cache for container bridge IPs in Docker mode.
# (subdomain, domain_mode) -> (ip: str, fetched_at: float)
# TTL is intentionally short (10 s) so a stop+restart picks up the new IP
# quickly, but long enough to avoid running ``docker inspect`` on every
# single proxied request (which would add ~20-50 ms per request).
_container_ip_cache: dict = {}
_container_ip_cache_lock = threading.Lock()
_CONTAINER_IP_CACHE_TTL = 10.0


def _invalidate_container_ip_cache(subdomain, domain_mode):
    """Remove the cached container IP for an instance (call on stop/restart)."""
    key = (subdomain, domain_mode)
    with _container_ip_cache_lock:
        _container_ip_cache.pop(key, None)


_circuit_breaker_lock = threading.Lock()
# (subdomain, domain_mode) -> {"failures": int, "opened_at": float|None}
_circuit_breaker_state = {}

# (subdomain, domain_mode) -> threading.BoundedSemaphore
_instance_semaphores = {}
_instance_semaphores_lock = threading.Lock()


def _get_instance_semaphore(subdomain, domain_mode):
    """Return the per-instance semaphore, creating it on first use."""
    key = (subdomain, domain_mode)
    with _instance_semaphores_lock:
        sema = _instance_semaphores.get(key)
        if sema is None:
            sema = threading.BoundedSemaphore(_INSTANCE_CONCURRENCY_CAP)
            _instance_semaphores[key] = sema
        return sema


def _circuit_is_open(subdomain, domain_mode):
    """Return True if the breaker is open *and* still within cooldown."""
    key = (subdomain, domain_mode)
    now = time.monotonic()
    with _circuit_breaker_lock:
        state = _circuit_breaker_state.get(key)
        if state is None:
            return False
        opened_at = state.get("opened_at")
        if opened_at is None:
            return False
        if now - opened_at >= _CIRCUIT_BREAKER_COOLDOWN_SECONDS:
            # Half-open: allow one probe request through by clearing
            # ``opened_at``.  If that probe also fails we'll re-open via
            # ``_record_upstream_failure``.
            state["opened_at"] = None
            return False
        return True


def _record_upstream_failure(subdomain, domain_mode, reason):
    """Increment failure count for an instance; open the breaker if threshold hit."""
    key = (subdomain, domain_mode)
    with _circuit_breaker_lock:
        state = _circuit_breaker_state.setdefault(
            key, {"failures": 0, "opened_at": None},
        )
        state["failures"] = int(state.get("failures", 0)) + 1
        if state["failures"] >= _CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            state["opened_at"] = time.monotonic()
            logger.warning(
                "Circuit breaker OPEN for instance %s (%s) after %d "
                "consecutive failures (last: %s); fast-failing for %.0fs",
                subdomain,
                domain_mode,
                state["failures"],
                reason,
                _CIRCUIT_BREAKER_COOLDOWN_SECONDS,
            )


def _record_upstream_success(subdomain, domain_mode):
    """Reset failure count for an instance after a successful upstream response."""
    key = (subdomain, domain_mode)
    with _circuit_breaker_lock:
        state = _circuit_breaker_state.get(key)
        if state is None:
            return
        if state.get("failures") or state.get("opened_at"):
            logger.info(
                "Circuit breaker CLOSED for instance %s (%s) after successful response",
                subdomain,
                domain_mode,
            )
        state["failures"] = 0
        state["opened_at"] = None


def _reset_circuit_breaker_state_for_tests():
    """Test helper: clear breaker + semaphore state."""
    with _circuit_breaker_lock:
        _circuit_breaker_state.clear()
    with _instance_semaphores_lock:
        _instance_semaphores.clear()


# Track in-flight lazy recoveries so we don't kick off the same instance
# multiple times when many requests arrive in parallel after a VPS restart.
_lazy_recovery_lock = threading.Lock()
_lazy_recovery_in_flight = set()
# Cooldown between recovery attempts for the same subdomain so a permanently
# broken instance doesn't get hammered with restart attempts.  30 s gives
# the previous soft-recovery attempt time to finish and the instance time
# to stabilise before we try again.
_LAZY_RECOVERY_COOLDOWN = 30.0
_lazy_recovery_last_attempt = {}
# Consecutive recovery attempts that found a PID-alive Gunicorn but no
# HTTP readiness.  Used to escalate to a full force-restart cycle once
# the soft path (just call ``_start_process`` again) has demonstrably
# failed to bring the instance back. The symptom users reported as
# "stuck on the Starting up… page until I pause and resume manually".
#
# Set to 1 so the *first* failed soft attempt is enough to trigger a
# force-restart on the next refresh.  Anything higher means users sit
# on the splash for ``N * _LAZY_RECOVERY_COOLDOWN`` seconds before the
# auto-recovery actually does what pause-and-resume would have done.
#
# This is safe to keep aggressive because the proxy hot-path now uses
# ``check_instance_process_alive`` (PID + listening-socket probe, no
# HTTP).  That check only returns False for genuinely broken instances:
# transient HTTP slowness in a healthy Gunicorn is absorbed upstream
# by gthread and never reaches the escalation counter.  In other words:
# when we get here, the master *is* wedged, and ``_start_process``
# (which no-ops on a live PID) can never recover it; only a full
# force-restart will.
_LAZY_RECOVERY_ESCALATE_AFTER = 1
_lazy_recovery_failure_count = {}

# Headers that must NOT be forwarded between hops (RFC 2616 §13.5.1).
_HOP_BY_HOP = frozenset(
    h.lower()
    for h in (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    )
)


def _extract_subdomain(host):
    """Return ``(slug, domain_mode)`` if *host* resolves to a hosted instance.

    Three host shapes are recognised:

    * ``{slug}-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``: hosting mode.
      ``INSTANCE_URL_SUFFIX`` is stripped off and the bare slug is
      returned alongside ``"hosting"``.
    * ``{slug}.{BASE_DOMAIN}``: apex mode (admin-claimed).  Returns
      ``(slug, "apex")`` when a live apex-mode instance row exists.
    * Anything else: returns ``(None, None)`` so the host falls through
      to the portal Flask app.

    The mode is propagated to the database lookup so that the same slug
    can exist in both apex and hosting mode (different public URLs)
    without the proxy picking the wrong row.
    """
    base = config.BASE_DOMAIN.lower()
    if not base:
        return None, None

    # Strip optional port from host header (e.g. "luca.wiki.example.com:80")
    host_lower = host.lower().split(":")[0]

    if host_lower == base:
        # Exact match: this is the base domain itself.
        return None, None

    # When PORTAL_DOMAIN differs from BASE_DOMAIN (e.g. portal at
    # "hosting.example.com" while BASE_DOMAIN is "example.com"), the
    # portal's host must pass through to the Flask app, not be treated
    # as an instance subdomain.
    portal = config.EFFECTIVE_PORTAL_DOMAIN
    if portal and host_lower == portal:
        return None, None

    suffix = "." + base
    if host_lower.endswith(suffix):
        sub = host_lower[: -len(suffix)]
        # Must be a single label (no extra dots) and non-empty.
        if sub and "." not in sub:
            url_suffix = config.INSTANCE_URL_SUFFIX
            if url_suffix:
                sep = "-" + url_suffix
                if sub.endswith(sep):
                    inner = sub[: -len(sep)]
                    if not inner:
                        # The subdomain was *only* the suffix (e.g. "-hosting").
                        # Not a valid instance name.
                        return None, None
                    return inner, "hosting"
                # Host doesn't end with the hosting suffix: try apex
                # lookup (admins can claim {slug}.{BASE_DOMAIN}).
                apex_slug = _resolve_apex_slug(sub)
                if apex_slug is None:
                    return None, None
                return apex_slug, "apex"
            # No suffix configured: bare slug is the instance name.
            # Treat as hosting by default; the explicit row's mode
            # controls downstream lookups when there is no separate
            # apex URL shape to disambiguate.
            return sub, "hosting"

    return None, None


def _resolve_apex_slug(slug):
    """Return *slug* if it points to a live apex-mode instance, else ``None``."""
    if not slug:
        return None
    from .db import get_apex_instance_by_subdomain

    inst = get_apex_instance_by_subdomain(slug)
    if inst is None:
        return None
    return slug


def _is_potential_apex_host(host):
    """Return ``True`` when *host* is a single label under ``BASE_DOMAIN``."""
    base = config.BASE_DOMAIN.lower()
    if not base:
        return False

    host_lower = host.lower().split(":")[0]
    portal = config.EFFECTIVE_PORTAL_DOMAIN
    if host_lower == base or (portal and host_lower == portal):
        return False

    suffix = "." + base
    if not host_lower.endswith(suffix):
        return False

    sub = host_lower[: -len(suffix)]
    return bool(sub and "." not in sub)


def _get_instance_port(subdomain, domain_mode="hosting"):
    """Look up the running instance port for *subdomain* in *domain_mode*.

    Returns the port number (int) or ``None`` if no running instance is
    found.  Uses a direct database query to avoid importing the full
    instance_manager module (which has heavy startup side-effects).
    """
    # Import lazily to avoid circular imports during app creation.
    from .db import get_instance_by_subdomain
    from .instance_manager import check_instance_process_alive

    inst = get_instance_by_subdomain(subdomain, domain_mode=domain_mode)
    if inst is None:
        return None
    if inst["status"] != "running":
        return None
    if not check_instance_process_alive(dict(inst)):
        return None
    return inst["port"]


def _classify_subdomain(subdomain, domain_mode="hosting"):
    """Classify *subdomain* in *domain_mode* into ``(port, status)``.

    Returns one of:

    * ``(port, "ready")``: instance is running and process is alive.
    * ``(None, "recovering")``: DB says running but process is dead.
      Caller should respond with the "Starting up" splash and trigger
      lazy recovery.
    * ``(None, "stopped")``: instance exists but is paused.
    * ``(None, "suspended")``: instance exists but has been suspended by an admin.
    * ``(None, "missing")``: no instance row for this subdomain in *domain_mode*.
    """
    from .db import get_instance_by_subdomain
    from .instance_manager import check_instance_process_alive

    inst = get_instance_by_subdomain(subdomain, domain_mode=domain_mode)
    if inst is None:
        return None, "missing"
    if inst["status"] == "running":
        if check_instance_process_alive(dict(inst)):
            return inst["port"], "ready"
        return None, "recovering"
    if inst["status"] == "stopped":
        return None, "stopped"
    if inst["status"] == "suspended":
        return None, "suspended"
    return None, "missing"


def _starting_lock_is_fresh(starting_lock):
    """Return ``True`` when *starting_lock* is a regular file touched in the last minute.

    The lock sits in the tenant's data directory, so it is checked with
    ``lstat``: a link planted under that name is not followed and does not
    count as a lock.
    """
    try:
        st = os.lstat(starting_lock)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and time.time() - st.st_mtime < 60


def _is_instance_starting(subdomain, domain_mode="hosting"):
    """Return ``True`` when a foreground provision/restart is in progress."""
    try:
        from .instance_manager import _instance_dir

        starting_lock = os.path.join(
            _instance_dir(subdomain, domain_mode), ".starting",
        )
    except (OSError, ValueError):
        return False
    return _starting_lock_is_fresh(starting_lock)


def _render_proxy_error_page(title, message_html, *, refresh=False):
    """Return HTML for a branded proxy error page."""
    refresh_tag = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    {refresh_tag}
    <title>{title} | BananaWiki</title>
    <style>
        body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #16161f; color: #f2f2f5; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 1rem; box-sizing: border-box; }}
        .card {{ background: #222230; padding: 2.5rem; border-radius: 12px; max-width: 480px; width: 100%; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.3); border: 1px solid #333345; }}
        .icon {{ margin-bottom: 0.5rem; }}
        h1 {{ margin-top: 0; font-size: 1.35rem; color: #fff; font-weight: 650; }}
        p {{ line-height: 1.6; color: #b3b3bf; margin-bottom: 1.25rem; font-size: 0.92rem; }}
        .contact {{ font-size: 0.85rem; color: #888; }}
        .contact a {{ display: inline; background: none; border: none; padding: 0; color: #7e9ada; text-decoration: underline; }}
        .contact a:hover {{ color: #a0b8e8; }}
        .portal-link {{ display: inline-block; background: rgba(255,255,255,0.05); color: #fff; text-decoration: none; padding: 0.6rem 1.2rem; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); transition: background 0.2s; font-size: 0.9rem; }}
        .portal-link:hover {{ background: rgba(255,255,255,0.1); }}
        a {{ color: #7e9ada; text-decoration: none; }}
        a:hover {{ color: #a0b8e8; }}
        .details {{ background: #1a1a28; border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0 1.5rem; text-align: left; }}
        .details dt {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: #888; margin-bottom: 0.2rem; }}
        .details dd {{ margin: 0 0 0.85rem; color: #d0d0dc; font-size: 0.92rem; word-break: break-word; }}
        .details dd:last-child {{ margin-bottom: 0; }}
        .loader {{ display: inline-block; width: 40px; height: 40px; border: 3px solid rgba(255,255,255,0.1); border-radius: 50%; border-top-color: #fca021; animation: spin 1s ease-in-out infinite; margin-bottom: 1rem; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="card">
        {message_html}
    </div>
</body>
</html>"""


def _proxy_error_response(classification, subdomain, domain_mode="hosting"):
    """Build a WSGI response tuple for a non-ready instance."""
    safe_sub = html_escape(subdomain)
    safe_portal = html_escape(config.EFFECTIVE_PORTAL_DOMAIN or config.BASE_DOMAIN)
    safe_contact = html_escape(config.HOSTING_CONTACT_EMAIL)
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Cache-Control", "no-store, must-revalidate"),
    ]

    if classification == "recovering":
        if not _is_instance_starting(subdomain, domain_mode):
            _trigger_lazy_recovery(subdomain, domain_mode)
        # Return 200 (not 502) so upstream CDNs / browsers render *our*
        # branded "starting up" splash with the auto-refresh meta tag.
        # When this returned 502, Cloudflare and some browser/extension
        # combinations would mask the body with their own generic
        # "Bad Gateway" page and the user would never see the refresh
        # instruction: surfacing as "I deployed an instance and got
        # bad gateway forever" even though lazy-recovery was actively
        # bringing the upstream back up in the background.
        status = "200 OK"
        headers.append(("Retry-After", "5"))
        body = _render_proxy_error_page(
            "Starting up",
            f"<div class=\"loader\"></div>"
            f"<h1>Starting up&hellip;</h1>"
            f"<p>The wiki instance <strong>{safe_sub}</strong> is "
            "starting up. This page will refresh automatically.</p>",
            refresh=True,
        )
    elif classification == "stopped":
        status = "503 Service Unavailable"
        body = _render_proxy_error_page(
            "Service Unavailable",
            f"<h1>Service Unavailable</h1>"
            f"<p>The wiki instance <strong>{safe_sub}</strong> is "
            "currently paused. Restart it from the hosting portal.</p>"
            f"<a href=\"https://{safe_portal}\">Return to hosting portal</a>",
        )
    elif classification == "suspended":
        details_html = ""
        try:
            from .db import get_instance_by_subdomain
            suspended_inst = get_instance_by_subdomain(subdomain, domain_mode=domain_mode)
            if suspended_inst:
                detail_rows = []
                reason = suspended_inst.get("suspend_reason")
                reason_visible = suspended_inst.get("suspend_reason_visible")
                suspended_until = suspended_inst.get("suspended_until")
                time_visible = suspended_inst.get("suspend_time_visible")
                if reason and reason_visible:
                    detail_rows.append(
                        f"<dt>Reason</dt>"
                        f"<dd>{html_escape(str(reason))}</dd>"
                    )
                if suspended_until and time_visible:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        exp = _dt.fromisoformat(suspended_until)
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=_tz.utc)
                        expiry_str = exp.strftime("%Y-%m-%d %H:%M UTC")
                        detail_rows.append(
                            f"<dt>Access will be restored</dt>"
                            f"<dd>{html_escape(expiry_str)}</dd>"
                        )
                    except (ValueError, TypeError):
                        pass
                elif not suspended_until and not (reason and reason_visible):
                    # Permanent with no visible reason: show duration note
                    detail_rows.append(
                        "<dt>Duration</dt>"
                        "<dd>This suspension is indefinite.</dd>"
                    )
                if detail_rows:
                    details_html = '<dl class="details">' + "".join(detail_rows) + "</dl>"
        except Exception:
            pass
        status = "403 Forbidden"
        body = _render_proxy_error_page(
            "Wiki Unavailable",
            f'<div class="icon">'
            '<svg viewBox="0 0 24 24" width="48" height="48" fill="none" '
            'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
            'stroke-linejoin="round" style="color:#7e9ada">'
            '<circle cx="12" cy="12" r="10"/>'
            '<line x1="12" y1="8" x2="12" y2="12"/>'
            '<line x1="12" y1="16" x2="12.01" y2="16"/>'
            "</svg></div>"
            f"<h1>This wiki is currently unavailable</h1>"
            f"<p>The wiki instance <strong>{safe_sub}</strong> has been "
            "temporarily taken offline.</p>"
            f"{details_html}"
            '<p class="contact">If you believe this is a mistake, '
            "please email "
            f'<a href="mailto:{safe_contact}">{safe_contact}</a>.'
            "</p>"
            f'<a href="https://{safe_portal}" class="portal-link">'
            "Go to hosting portal</a>",
        )
    else:
        status = "404 Not Found"
        body = _render_proxy_error_page(
            "Not Found",
            f"<h1>Not Found</h1>"
            f"<p>No wiki instance found for <strong>{safe_sub}</strong>.</p>"
            f"<a href=\"https://{safe_portal}\">Return to hosting portal</a>",
        )

    return status, headers, [body.encode("utf-8")]


def _trigger_lazy_recovery(subdomain, domain_mode="hosting"):
    """Spawn a background thread to restart an instance that should be running.

    No-ops if a recovery attempt is already in flight or if the cooldown
    window since the last attempt has not yet elapsed.  This keeps the
    request path responsive (502 returns immediately) while making the
    portal self-healing after a VPS restart.

    Recovery state is keyed by ``(subdomain, domain_mode)`` so apex and
    hosting instances sharing the same slug recover independently.
    """
    key = (subdomain, domain_mode)
    now = time.monotonic()
    with _lazy_recovery_lock:
        if key in _lazy_recovery_in_flight:
            return
        last = _lazy_recovery_last_attempt.get(key, 0.0)
        if now - last < _LAZY_RECOVERY_COOLDOWN:
            return
        _lazy_recovery_in_flight.add(key)
        _lazy_recovery_last_attempt[key] = now

    def _worker():
        try:
            from .db import get_instance_by_subdomain
            from .instance_manager import (
                _clean_stale_pid,
                _instance_http_ready,
                _instance_dir_for,
                _invalidate_instance_caches,
                _is_our_instance_process,
                _read_pid,
                _start_process,
                force_restart_instance,
            )

            inst = get_instance_by_subdomain(subdomain, domain_mode=domain_mode)
            if inst is None or inst["status"] != "running":
                return
            data_dir = _instance_dir_for(inst)
            inst_dict = dict(inst)
            port = int(inst_dict["port"]) if inst_dict.get("port") is not None else None

            # Abort if the instance is currently being provisioned or restarted
            # by a foreground request (denoted by a recent .starting file).
            if _starting_lock_is_fresh(os.path.join(data_dir, ".starting")):
                logger.info("Skipping lazy recovery for '%s': already starting in foreground", subdomain)
                return

            # Strip any PID file that does not point at our Gunicorn (most
            # commonly a leftover from before a VPS reboot whose numeric PID
            # now matches an unrelated process such as systemd).  Without
            # this step the next check would think the instance was already
            # running and we'd never restart it. The exact failure mode
            # users were reporting as "instance returns 404 forever after a
            # VPS restart".
            _clean_stale_pid(data_dir, port=port)
            pid = _read_pid(data_dir)
            our_process_alive = _is_our_instance_process(pid, data_dir, port=port)
            if our_process_alive and _instance_http_ready(inst_dict):
                # Process is alive AND serving traffic: health probably
                # bounced back; just invalidate the cache so the next
                # request re-checks.
                _invalidate_instance_caches(inst["subdomain"])
                with _lazy_recovery_lock:
                    _lazy_recovery_failure_count.pop(key, None)
                return

            # Escalation path: when our Gunicorn master is *alive* but
            # the HTTP socket isn't answering, ``_start_process`` will
            # short-circuit (it no-ops on a live PID), so the soft
            # path can never recover this state.  The master is
            # almost certainly wedged: stuck in a long migration,
            # deadlocked on a worker initialisation, or holding the
            # bind socket without ever accepting connections.  Cycle
            # it aggressively (force-stop, wipe PID + boot id,
            # restart) on the FIRST attempt so the user never has to
            # manually pause-and-resume just to wake an instance up.
            #
            # We still re-check the ``.starting`` lock above and
            # ``_is_instance_starting`` upstream so a legitimate
            # in-progress startup is never killed.
            with _lazy_recovery_lock:
                fail_count = _lazy_recovery_failure_count.get(key, 0)
            should_force_restart = our_process_alive and (
                fail_count >= _LAZY_RECOVERY_ESCALATE_AFTER
            )
            if should_force_restart:
                logger.warning(
                    "Force-restarting instance '%s' after %d failed soft "
                    "recovery attempts",
                    subdomain, fail_count,
                )
                ok, reason = force_restart_instance(inst["id"])
                if ok:
                    logger.info("Force-restart of '%s' succeeded", subdomain)
                    with _lazy_recovery_lock:
                        _lazy_recovery_failure_count.pop(key, None)
                else:
                    logger.warning(
                        "Force-restart of '%s' failed: %s", subdomain, reason
                    )
                _invalidate_instance_caches(inst["subdomain"])
                return

            logger.info("Lazy-recovering instance '%s'", subdomain)
            _invalidate_instance_caches(inst["subdomain"])
            started = _start_process(inst_dict, data_dir)
            if started and _instance_http_ready(inst_dict):
                logger.info("Lazy-recovered instance '%s'", subdomain)
                with _lazy_recovery_lock:
                    _lazy_recovery_failure_count.pop(key, None)
            else:
                logger.warning(
                    "Lazy recovery for '%s' did not produce a healthy instance",
                    subdomain,
                )
                with _lazy_recovery_lock:
                    _lazy_recovery_failure_count[key] = fail_count + 1
        except Exception:
            logger.exception("Unexpected error during lazy recovery of '%s'", subdomain)
        finally:
            with _lazy_recovery_lock:
                _lazy_recovery_in_flight.discard(key)

    threading.Thread(
        target=_worker,
        name=f"lazy-recover-{subdomain}-{domain_mode}",
        daemon=True,
    ).start()


class _LimitedReader:
    """File-like wrapper that exposes at most ``length`` bytes of *stream*.

    Used to feed ``http.client.HTTPConnection.request`` a streaming body
    without first buffering the whole upload in RAM.  ``http.client``
    treats objects with a ``read()`` method specially and pulls bytes in
    8 KiB blocks until ``read()`` returns ``b""``, so capping the total
    bytes returned matches the upstream server's expected
    ``Content-Length`` exactly even when ``wsgi.input`` happens to be a
    raw socket that would otherwise block waiting for more data.
    """

    __slots__ = ("_stream", "_remaining")

    def __init__(self, stream, length):
        self._stream = stream
        self._remaining = max(0, int(length))

    def read(self, size=-1):
        if self._remaining <= 0:
            return b""
        if size is None or size < 0:
            to_read = self._remaining
        else:
            to_read = min(size, self._remaining)
        chunk = self._stream.read(to_read)
        if not chunk:
            # Upstream / client disconnected before delivering the
            # promised bytes.  Returning b"" lets http.client finish the
            # send loop; the upstream Gunicorn will then 400 the request,
            # which is the correct behaviour for a truncated body.
            self._remaining = 0
            return b""
        self._remaining -= len(chunk)
        return chunk


def _bad_gateway_response(message=None):
    try:
        record_http_error("hosting_proxy", 502, route="<proxy>", upstream="instance")
    except Exception:
        pass
    status = "502 Bad Gateway"
    resp_headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Cache-Control", "no-store, must-revalidate"),
        ("Retry-After", "5"),
    ]
    detail = html_escape(
        message
        or (
            "The wiki instance is not responding. It may be starting up, "
            "so please try again in a few seconds."
        )
    )
    body_text = _render_proxy_error_page(
        "Bad Gateway",
        f"<h1>502 Bad Gateway</h1>"
        f"<p>{detail}</p>",
        refresh=True,
    )
    return status, resp_headers, [body_text.encode("utf-8")]


def _gateway_timeout_response():
    try:
        record_http_error("hosting_proxy", 504, route="<proxy>", upstream="instance")
    except Exception:
        pass
    status = "504 Gateway Timeout"
    resp_headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Cache-Control", "no-store, must-revalidate"),
        ("Retry-After", "5"),
    ]
    body_text = _render_proxy_error_page(
        "Gateway Timeout",
        "<h1>504 Gateway Timeout</h1>"
        "<p>The wiki instance took too long to respond. "
        "Please try again in a few seconds.</p>",
        refresh=True,
    )
    return status, resp_headers, [body_text.encode("utf-8")]


def _resolve_upstream(port, subdomain=None, domain_mode=None):
    """Return ``(host, port)`` for the upstream connection.

    In Docker mode the published host port is unreachable from the host
    when the container network was created with ``--internal`` (Docker
    does not install the iptables DNAT/ACCEPT rules in that case).  The
    host can always reach the container directly on its bridge IP, so we
    look that up and use the container-internal port instead.

    The bridge IP is cached for ``_CONTAINER_IP_CACHE_TTL`` seconds so
    that ``docker inspect`` is not invoked on every proxied request.

    In process mode (or when the container IP cannot be determined) we
    fall back to ``127.0.0.1:{port}``.
    """
    if config.HOSTING_INSTANCE_RUNTIME == "docker" and subdomain and domain_mode:
        key = (subdomain, domain_mode)
        now = time.monotonic()
        with _container_ip_cache_lock:
            cached = _container_ip_cache.get(key)
            if cached is not None and (now - cached[1]) < _CONTAINER_IP_CACHE_TTL:
                return cached[0], config.HOSTING_CONTAINER_INTERNAL_PORT

        try:
            from .db import get_instance_by_subdomain
            from . import container_runtime as _cr
            from .instance_manager import _instance_dir_for

            inst = get_instance_by_subdomain(subdomain, domain_mode=domain_mode)
            if inst is not None:
                data_dir = _instance_dir_for(inst)
                ip = _cr.container_ip(data_dir)
                if ip:
                    with _container_ip_cache_lock:
                        _container_ip_cache[key] = (ip, now)
                    return ip, config.HOSTING_CONTAINER_INTERNAL_PORT
        except Exception:
            pass
    return "127.0.0.1", port


def _proxy_request(environ, port, subdomain=None, domain_mode=None):
    """Proxy the WSGI request in *environ* to the instance upstream.

    In process mode this is ``127.0.0.1:<port>``.  In Docker mode the
    upstream is resolved to the container's bridge IP and internal port
    via :func:`_resolve_upstream`: Docker's ``--internal`` network flag
    prevents published host ports from being reachable, so we bypass
    port publishing entirely and talk to the container directly on the
    bridge network.

    Returns an ``(status, response_headers, body_iterator)`` tuple
    suitable for the WSGI ``start_response`` protocol.

    The request body is *streamed* to the upstream connection rather
    than buffered fully in memory.  Without streaming, a 100 MiB image
    or video upload (custom-pages and canvas attachments allow this)
    would hold an entire copy of the body in the portal worker before
    a single byte was forwarded: doubling latency, pinning the
    worker, and on tight VPS plans triggering OOM-induced 500s.

    ``subdomain`` / ``domain_mode`` are forwarded to the circuit
    breaker so repeated upstream failures fast-fail subsequent
    requests instead of pinning portal workers waiting for a wedged
    instance.  Both default to ``None`` for legacy call-sites and
    unit tests that bypass the breaker entirely.
    """
    upstream_host, upstream_port = _resolve_upstream(port, subdomain, domain_mode)
    method = environ["REQUEST_METHOD"]
    # Reconstruct the full path + query string.
    # PATH_INFO is decoded by the WSGI server, but http.client sends
    # the path as-is (calls .encode('ascii')), so we must re-encode
    # non-ASCII characters (e.g. à → %C3%A0) before proxying.
    import urllib.parse
    raw_path = environ.get("PATH_INFO", "/")
    path = urllib.parse.quote(raw_path, safe="/:@!$&'()*+,;=-._~")
    qs = environ.get("QUERY_STRING", "")
    if qs:
        path = f"{path}?{qs}"

    # Determine whether a request body is expected.  We only forward a
    # ``Content-Length`` (and a body) when CONTENT_LENGTH parses cleanly
    # to a non-negative integer; an unparseable value would otherwise
    # cause http.client to emit a header that the upstream Gunicorn
    # would block on indefinitely waiting for bytes that never arrive.
    raw_content_length = environ.get("CONTENT_LENGTH")
    body_length = None
    if raw_content_length:
        try:
            parsed = int(raw_content_length)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed >= 0:
            body_length = parsed

    body = None
    if body_length is not None and body_length > 0:
        wsgi_input = environ.get("wsgi.input")
        if wsgi_input is not None:
            body = _LimitedReader(wsgi_input, body_length)

    # Build upstream headers.  Forward all headers except hop-by-hop.
    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            header_name = key[5:].replace("_", "-").lower()
            if header_name not in _HOP_BY_HOP:
                headers[header_name] = value
    # CONTENT_TYPE and CONTENT_LENGTH are stored outside HTTP_* in WSGI.
    ct = environ.get("CONTENT_TYPE")
    if ct:
        headers["content-type"] = ct
    if body_length is not None:
        # Always emit a numeric Content-Length so the upstream knows
        # exactly how much body to expect (matches what _LimitedReader
        # will deliver).
        headers["content-length"] = str(body_length)

    # Add / overwrite forwarding headers so the instance sees the real
    # client information.
    remote_addr = environ.get("REMOTE_ADDR", "127.0.0.1")
    existing_xff = headers.get("x-forwarded-for", "")
    if existing_xff:
        headers["x-forwarded-for"] = f"{existing_xff}, {remote_addr}"
    else:
        headers["x-forwarded-for"] = remote_addr
    headers["x-forwarded-host"] = environ.get("HTTP_HOST", "")
    # ProxyFix wraps outside this middleware, but hosting/app.py only installs
    # it when HOSTING_PROXY_MODE is on, so ``wsgi.url_scheme`` may still be the
    # raw Gunicorn scheme even when nginx already supplied a trusted
    # X-Forwarded-Proto header.  Prefer that incoming proxy header so
    # hosted instances see the same public scheme as the browser; otherwise
    # CSRF/session cookies can be serialized for the wrong deployment shape.
    forwarded_proto = (environ.get("HTTP_X_FORWARDED_PROTO") or "").split(",", 1)[0].strip()
    headers["x-forwarded-proto"] = forwarded_proto or environ.get("wsgi.url_scheme", "http")

    # Retry budget for transient connection resets.  During Gunicorn worker
    # recycling (max_requests) the dying worker may reset in-flight TCP
    # connections.  A single immediate retry on ConnectionResetError lands
    # on the replacement worker and avoids surfacing a 502 to the user.
    # Only safe/idempotent methods are retried to prevent duplicate
    # side-effects.
    _RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
    max_attempts = 2 if method in _RETRYABLE_METHODS else 1

    last_exc = None
    for attempt in range(max_attempts):
        conn = http.client.HTTPConnection(
            upstream_host, upstream_port, timeout=_UPSTREAM_TIMEOUT
        )
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            break  # success
        except socket.timeout as exc:
            # Upstream accepted the connection but stopped responding.  Use
            # 504 so CDNs and clients know to retry rather than treating it
            # as a permanent error.
            logger.warning(
                "Proxy to %s:%d timed out after %ds: %s",
                upstream_host,
                upstream_port,
                _UPSTREAM_TIMEOUT,
                exc,
            )
            if subdomain is not None and domain_mode is not None:
                _record_upstream_failure(subdomain, domain_mode, f"socket.timeout: {exc}")
            try:
                conn.close()
            except Exception:
                pass
            return _gateway_timeout_response()
        except ConnectionResetError as exc:
            # Worker recycling (max_requests) can reset the connection.
            # Retry once for idempotent requests.
            last_exc = exc
            try:
                conn.close()
            except Exception:
                pass
            if attempt < max_attempts - 1:
                logger.debug(
                    "Proxy to %s:%d got connection reset, retrying (%d/%d)",
                    upstream_host, upstream_port, attempt + 1, max_attempts,
                )
                time.sleep(0.1)
                continue
            logger.warning("Proxy to %s:%d failed after retry: %s", upstream_host, upstream_port, exc)
            if subdomain is not None and domain_mode is not None:
                _record_upstream_failure(subdomain, domain_mode, repr(exc))
            return _bad_gateway_response()
        except (ConnectionRefusedError, OSError) as exc:
            logger.warning("Proxy to %s:%d failed: %s", upstream_host, upstream_port, exc)
            if subdomain is not None and domain_mode is not None:
                _record_upstream_failure(subdomain, domain_mode, repr(exc))
            try:
                conn.close()
            except Exception:
                pass
            return _bad_gateway_response()
    else:
        # All retry attempts exhausted (should not reach here, but safety net)
        logger.warning("Proxy to %s:%d exhausted retries: %s", upstream_host, upstream_port, last_exc)
        if subdomain is not None and domain_mode is not None:
            _record_upstream_failure(subdomain, domain_mode, repr(last_exc))
        return _bad_gateway_response()

    status = f"{resp.status} {resp.reason}"
    if resp.status >= 500:
        try:
            record_http_error("hosting_proxy_upstream", resp.status, route=path, upstream=f"{upstream_host}:{upstream_port}")
        except Exception:
            pass
        if subdomain is not None and domain_mode is not None:
            _record_upstream_failure(subdomain, domain_mode, f"http_{resp.status}")
    elif subdomain is not None and domain_mode is not None:
        # Any 1xx/2xx/3xx/4xx response means the upstream is alive enough
        # to answer; close the breaker.
        _record_upstream_success(subdomain, domain_mode)
    resp_headers = [
        (k, v)
        for k, v in resp.getheaders()
        if k.lower() not in _HOP_BY_HOP
    ]

    # Read the response body in chunks to support large responses
    # (file downloads, migration exports, etc.).
    def body_iter():
        try:
            while True:
                try:
                    chunk = resp.read(_STREAM_CHUNK)
                except (socket.timeout, OSError) as exc:
                    # Upstream went away mid-stream: log once and stop
                    # rather than propagating into the WSGI server.  The
                    # client sees a truncated response, which is the best
                    # we can do without buffering the whole body first.
                    logger.warning(
                        "Proxy stream from %s:%d aborted: %s",
                        upstream_host,
                        upstream_port,
                        exc,
                    )
                    break
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                resp.close()
            finally:
                conn.close()

    return status, resp_headers, body_iter()


class SubdomainProxyMiddleware:
    """WSGI middleware that proxies subdomain requests to wiki instances.

    In subdomain hosting mode, nginx routes ``*.BASE_DOMAIN`` requests
    to the hosting portal.  This middleware intercepts those requests
    and proxies them to the correct instance's ``127.0.0.1:<port>``.

    Requests whose Host header matches the portal's ``BASE_DOMAIN``
    (the portal itself) pass through to the inner WSGI app unchanged.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        # Only active in subdomain mode.
        if config.HOSTING_MODE != "subdomain":
            return self.app(environ, start_response)

        host = environ.get("HTTP_HOST", "")
        subdomain, domain_mode = _extract_subdomain(host)

        # Only a verified, permitted domain may route to an instance. An
        # unknown Host must never reach the portal's authentication pages.
        host_name = host.lower().split(":", 1)[0].rstrip(".")
        if subdomain is None:
            from .domains import resolve_domain
            custom_instance = resolve_domain(host_name)
            if custom_instance:
                subdomain = custom_instance["subdomain"]
                domain_mode = custom_instance["domain_mode"]
            elif host_name not in {
                config.BASE_DOMAIN.lower(), config.EFFECTIVE_PORTAL_DOMAIN.lower(),
                "localhost", "127.0.0.1",
            } and not _is_potential_apex_host(host):
                start_response("404 Not Found", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
                return [b"No wiki is configured for this hostname."]

        if subdomain is None:
            if _is_potential_apex_host(host):
                status = "404 Not Found"
                headers = [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store, must-revalidate"),
                ]
                host_label = host.lower().split(":")[0]
                safe_host = html_escape(host_label)
                portal = config.EFFECTIVE_PORTAL_DOMAIN or config.BASE_DOMAIN
                safe_portal = html_escape(portal)
                body = _render_proxy_error_page(
                    "Not Found",
                    f"<h1>Not Found</h1>"
                    f"<p>No wiki instance found for <strong>{safe_host}</strong>.</p>"
                    f"<a href=\"https://{safe_portal}\">Return to hosting portal</a>",
                )
                start_response(status, headers)
                return [body.encode("utf-8")]
            # Not a subdomain request: pass through to the Flask portal.
            return self.app(environ, start_response)

        from .db import get_instance_by_subdomain
        from .domains import instance_can_serve
        instance = get_instance_by_subdomain(subdomain, domain_mode=domain_mode)
        if instance and not instance_can_serve(instance["id"]):
            port, classification = None, "suspended"
        else:
            port, classification = _classify_subdomain(subdomain, domain_mode)
        if port is None:
            status, headers, body = _proxy_error_response(
                classification, subdomain, domain_mode,
            )
            start_response(status, headers)
            return body

        # Crash isolation: if the breaker is open for this instance,
        # short-circuit straight to the recovering splash.  This keeps
        # a wedged instance from monopolising portal Gunicorn workers
        # for full 120s timeouts.
        if _circuit_is_open(subdomain, domain_mode):
            from .instance_manager import _invalidate_instance_caches

            _invalidate_instance_caches(subdomain)
            status, resp_headers, body = _proxy_error_response(
                "recovering", subdomain, domain_mode,
            )
            start_response(status, resp_headers)
            return body

        # Per-instance concurrency cap: if too many requests are already
        # in flight to this instance, return the recovering splash now
        # instead of stacking another request on an overwhelmed master.
        sema = _get_instance_semaphore(subdomain, domain_mode)
        if not sema.acquire(blocking=False):
            logger.warning(
                "Instance %s (%s) at concurrency cap (%d); returning recovering splash",
                subdomain,
                domain_mode,
                _INSTANCE_CONCURRENCY_CAP,
            )
            status, resp_headers, body = _proxy_error_response(
                "recovering", subdomain, domain_mode,
            )
            start_response(status, resp_headers)
            return body

        try:
            # Proxy the request to the instance.
            status, resp_headers, body_iter = _proxy_request(
                environ, port,
                subdomain=subdomain,
                domain_mode=domain_mode,
            )
        except Exception:
            # Defensive: any unhandled exception in the upstream proxy
            # must release the semaphore so a single bug doesn't lock
            # the instance out forever.
            try:
                sema.release()
            except ValueError:
                pass
            raise

        if status.startswith("502 "):
            try:
                sema.release()
            except ValueError:
                pass
            from .instance_manager import _invalidate_instance_caches

            _invalidate_instance_caches(subdomain)
            status, resp_headers, body = _proxy_error_response(
                "recovering", subdomain, domain_mode,
            )
            start_response(status, resp_headers)
            return body

        # Wrap the body iterator so the semaphore is released exactly
        # once when the upstream response is fully consumed (or the
        # client disconnects mid-stream).
        def _release_on_close(iter_):
            released = {"done": False}

            def _release_once():
                if released["done"]:
                    return
                released["done"] = True
                try:
                    sema.release()
                except ValueError:
                    pass

            try:
                for chunk in iter_:
                    yield chunk
            finally:
                _release_once()

        body_iter = _release_on_close(body_iter)
        start_response(status, resp_headers)
        return body_iter
