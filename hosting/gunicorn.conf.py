"""Gunicorn settings for the hosting portal (``gunicorn -c hosting/gunicorn.conf.py hosting.wsgi:app``).

Environment: ``HOSTING_HOST`` (default 127.0.0.1), ``HOSTING_PORT`` (5099),
``HOSTING_WORKERS`` (2), ``HOSTING_THREADS`` (4), ``HOSTING_WORKER_TIMEOUT``
(120) and ``HOSTING_ACCESS_LOG`` ("-" for stdout, "off" to disable).

The portal only serves web requests: lifecycle jobs run in the separate
``hosting.maintenance`` service and privileged container operations go
through the root runtime agent, so workers need no Docker access.
"""

from __future__ import annotations

import os


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return max(low, min(high, value))


def _bind_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _worker_tmp_dir() -> str | None:
    """Heartbeats on tmpfs, so slow disk I/O never looks like a dead worker."""
    for candidate in ("/dev/shm", "/tmp"):  # noqa: S108 - tmpfs heartbeat files
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return None


bind = (f"{_bind_host(os.environ.get('HOSTING_HOST', '127.0.0.1') or '127.0.0.1')}:"
        f"{_int('HOSTING_PORT', 5099, 1, 65535)}")
# SQLite serialises writes: a few workers with threads beat many processes.
workers = _int("HOSTING_WORKERS", 2, 1, 16)
worker_class = "gthread"
threads = _int("HOSTING_THREADS", 4, 1, 32)
# Each worker opens its own database connections and runtime-agent client;
# create_app serialises migrations and secret-key creation with file locks.
preload_app = False
timeout = _int("HOSTING_WORKER_TIMEOUT", 120, 10, 600)
graceful_timeout = timeout
keepalive = 2
max_requests = 5000
max_requests_jitter = 500
# The application applies ProxyFix itself (HOSTING_PROXY_MODE); Gunicorn only trusts loopback.
forwarded_allow_ips = "127.0.0.1,::1"
control_socket_disable = True
accesslog = None if os.environ.get("HOSTING_ACCESS_LOG", "-") == "off" else os.environ.get("HOSTING_ACCESS_LOG", "-")
errorlog = "-"
loglevel = "info"
_tmp = _worker_tmp_dir()
if _tmp:
    worker_tmp_dir = _tmp
