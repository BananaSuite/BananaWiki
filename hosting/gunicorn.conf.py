"""Gunicorn settings for the hosting portal (``gunicorn -c hosting/gunicorn.conf.py hosting.wsgi:app``).

Environment: ``HOSTING_HOST`` (default 127.0.0.1), ``HOSTING_PORT`` (5099),
``HOSTING_WORKERS`` (2), ``HOSTING_THREADS`` (4), ``HOSTING_WORKER_TIMEOUT``
(120), ``HOSTING_WRITE_TIMEOUT`` (300 seconds a response write may stall, 0 for
no limit) and ``HOSTING_ACCESS_LOG`` ("-" for stdout, "off" to disable).

The portal only serves web requests: lifecycle jobs run in the separate
``hosting.maintenance`` service and privileged container operations go
through the root runtime agent, so workers need no Docker access.
"""

from __future__ import annotations

import os
from typing import Any

from bananawiki.core.downloads import PORTAL_PLACES
from bananawiki.hosting.wikihosts import WAITING_PLACES
from bananawiki.hosting.wikihosts import watch as _watch_proxy
from bananawiki.ops.access_log import AccessLogger as _AccessLogger
from bananawiki.ops.write_timeout import guard as _guard
from bananawiki.ops.write_timeout import patch_graceful_close as _patch_graceful_close


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
# Plus a thread for each place of a large response (bananawiki.core.downloads) and for each request waiting
# for a wiki the portal proxies (bananawiki.hosting.wikihosts). Proxied requests themselves take at most all
# but one of the HOSTING_THREADS, so that one always stays for the portal's own pages.
threads = _int("HOSTING_THREADS", 4, 1, 32) + PORTAL_PLACES + WAITING_PLACES
# Each worker opens its own database connections and runtime-agent client;
# create_app serialises migrations and secret-key creation with file locks.
preload_app = False
timeout = _int("HOSTING_WORKER_TIMEOUT", 120, 10, 600)
graceful_timeout = timeout
# Not a Gunicorn setting (it has none): see post_worker_init.
_write_timeout = _int("HOSTING_WRITE_TIMEOUT", 300, 0, 3600)
# One request per connection, closed without waiting for the client: see bananawiki/ops/gunicorn_conf.py.
keepalive = 0
_patch_graceful_close()
# A worker that reaches it stops accepting connections and finishes those in progress first: requests the portal
# proxies to a wiki are cut off then (post_worker_init), so a slow wiki never keeps both workers draining.
max_requests = 5000
max_requests_jitter = 500
# The application applies ProxyFix itself (HOSTING_PROXY_MODE); Gunicorn only trusts loopback.
forwarded_allow_ips = "127.0.0.1,::1"
control_socket_disable = True
# Control characters a client sends are escaped (see bananawiki.ops.access_log).
logger_class = _AccessLogger
accesslog = None if os.environ.get("HOSTING_ACCESS_LOG", "-") == "off" else os.environ.get("HOSTING_ACCESS_LOG", "-")
errorlog = "-"
loglevel = "info"
_tmp = _worker_tmp_dir()
if _tmp:
    worker_tmp_dir = _tmp


def post_worker_init(worker: Any) -> None:
    """Free the thread of a client that stops reading a download (:mod:`bananawiki.ops.write_timeout`).

    Also cut off proxied wiki requests once the worker is retired (:func:`bananawiki.hosting.wikihosts.watch`).
    """
    worker.wsgi = _guard(worker.wsgi, _write_timeout)
    _watch_proxy(lambda: worker.alive)
