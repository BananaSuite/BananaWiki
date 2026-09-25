"""Login and general rate limiting helpers."""

from collections import deque
import functools
import ipaddress
import threading
import time

from flask import request, flash, jsonify, abort

import db
from wiki_logger import log_action


def _client_key(remote_addr):
    """Return a stable rate-limit bucket key for *remote_addr*.

    For IPv4 we use the address verbatim.  For IPv6 we collapse to the /64
    prefix because a single residential or hosting customer typically has the
    entire /64 to themselves; without this an attacker with one IPv6 /64 has
    2**64 distinct addresses and can trivially evade per-IP rate limits by
    rotating through them.

    Falls back to ``"unknown"`` for missing or unparseable values rather than
    raising.
    """
    if not remote_addr:
        return "unknown"
    try:
        addr = ipaddress.ip_address(remote_addr)
    except ValueError:
        # Could be a header value with a port, brackets, or junk.  Strip
        # surrounding brackets / port and try once more.
        cleaned = remote_addr.strip().lstrip("[").split("]", 1)[0]
        cleaned = cleaned.rsplit(":", 1)[0] if cleaned.count(":") == 1 else cleaned
        try:
            addr = ipaddress.ip_address(cleaned)
        except ValueError:
            return remote_addr  # opaque; use as-is so tests stay stable
    if isinstance(addr, ipaddress.IPv6Address):
        # IPv4-mapped IPv6 (e.g. ``::ffff:1.2.3.4``) collapses to its embedded
        # IPv4 address so that an attacker cannot bucket separately from the
        # same physical client by switching representation.
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        # /64 network as canonical text, e.g. ``2001:db8:1:2::/64``.
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _current_client_key():
    """Return the rate-limit bucket key for the current Flask request."""
    return _client_key(request.remote_addr)


# Login attempts are counted in the database, not in process memory, so a
# brute-force attempt cannot simply be spread across Gunicorn workers.
class _RateLimitStore(dict):
    """Compatibility shim for tests; backing data lives in DB."""

    def clear(self):
        """Clear both the in-memory dict and the DB login_attempts table."""
        super().clear()
        db.clear_all_login_attempts()


_LOGIN_ATTEMPTS = _RateLimitStore()
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 60  # seconds


def _check_login_rate_limit():
    """Return True if the request client is allowed to attempt login."""
    key = _current_client_key()
    recent = db.count_recent_login_attempts(key, _LOGIN_WINDOW)
    return recent < _LOGIN_MAX_ATTEMPTS


def _record_login_attempt():
    """Record a failed login attempt for the current client."""
    db.record_login_attempt(_current_client_key())


def _clear_login_attempts():
    """Clear failed login attempts for the current client (on successful login)."""
    db.clear_login_attempts(_current_client_key())


# Ordinary endpoint limits stay in process memory instead, per IP and per
# bucket.  The effective allowance is therefore multiplied by the worker
# count, which is acceptable for throttling but not for anything
# security-sensitive.
_RL_LOCK = threading.Lock()


class _RLStore(dict):
    """Compatibility shim for tests; backing data lives in the DB."""

    def clear(self):
        """Clear both the in-memory dict and the DB rate_limit_hits table."""
        super().clear()
        db.clear_all_rate_limit_hits()


_RL_STORE: dict = _RLStore()
_RL_GLOBAL_MAX = 1200           # max requests per window for all endpoints
_RL_GLOBAL_WINDOW = 60          # window size in seconds
_SAFE_NAV_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_html_navigation_request():
    """Return True for ordinary browser page navigation/reload requests.

    The answer comes from the request method, path and Accept header, all
    of which the client chooses, so it is a convenience for page renders and
    never a security boundary.  See ``rate_limit(exempt_html_nav=...)``.
    """
    if request.method not in _SAFE_NAV_METHODS:
        return False
    if request.path.startswith("/api/"):
        return False
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "text/html"


def _rl_check_local(ip, bucket, max_requests, window):
    """Fast in-memory sliding-window rate-limit check.

    All rate-limit buckets use per-worker in-memory accounting.  The
    previous DB-backed approach (``BEGIN IMMEDIATE`` per check) serialised
    every request on the SQLite write lock, causing random latency spikes
    and perceived page hangs under even moderate concurrency.

    Per-worker accounting means a client can technically reach
    ``max_requests × num_workers`` before being throttled, but this is an
    acceptable trade-off: rate limiting is a soft abuse-prevention
    mechanism and the per-worker limits still cap individual workers.
    """
    now = time.time()
    cutoff = now - window
    key = (ip, bucket)

    with _RL_LOCK:
        hits = _RL_STORE.get(key)
        if hits is None or not isinstance(hits, deque):
            hits = deque()
            _RL_STORE[key] = hits

        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= max_requests:
            return False

        hits.append(now)
        return True


def _rl_check(ip, bucket, max_requests, window):
    """Return True if the request is within the rate limit, and record it.

    All buckets (global and per-route) use fast in-memory sliding-window
    accounting.  This avoids the SQLite ``BEGIN IMMEDIATE`` serialisation
    that previously caused random page hangs under concurrent load.

    When ``max_requests`` is exactly **0** the bucket is fully blocked
    (every request is denied without recording a DB hit).  This is useful
    for admin-driven lockouts or test scenarios.

    A *negative* ``max_requests`` is treated as a misconfiguration and
    logged once per process; we fail *open* (allow the request) rather than
    fail closed because a typo here would otherwise lock every user out of
    every page.
    """
    if max_requests == 0:
        return False
    if max_requests < 0:
        global _RL_MISCONFIG_LOGGED
        if not _RL_MISCONFIG_LOGGED:
            import logging
            logging.getLogger("bananawiki").error(
                "Rate-limit misconfigured (bucket=%r, max=%d): failing open.",
                bucket, max_requests,
            )
            _RL_MISCONFIG_LOGGED = True
        return True
    return _rl_check_local(ip, bucket, max_requests, window)


# Module-level flag so we only log the misconfig warning once per worker.
_RL_MISCONFIG_LOGGED = False


def rate_limit(max_requests=60, window=60, exempt_html_nav=True):
    """Route decorator that enforces a per-client rate limit.

    By default, GET requests for HTML pages are not counted, so that
    reloading a form page does not use up the allowance meant for its POSTs.
    Any client can ask for HTML, though, so a GET that does real work on
    every call (building an export, reading a large file, re-encoding audio)
    must pass ``exempt_html_nav=False`` or its limit does not apply to
    anyone who sends ``Accept: text/html``.
    """
    def decorator(f):
        """Bind the wrapped view to its own counter bucket."""
        bucket = f.__name__
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            """Count this call and refuse it once the bucket is spent."""
            if exempt_html_nav and _is_html_navigation_request():
                return f(*args, **kwargs)
            ip = _current_client_key()
            if not _rl_check(ip, bucket, max_requests, window):
                log_action("rate_limited", request, endpoint=bucket)
                if request.path.startswith("/api/"):
                    from helpers._translations import t
                    return jsonify({"error": t("error.rate_limit_exceeded")}), 429
                from helpers._translations import t
                flash(t("error.rate_limit_exceeded"), "error")
                abort(429)
            return f(*args, **kwargs)
        return wrapper
    return decorator
