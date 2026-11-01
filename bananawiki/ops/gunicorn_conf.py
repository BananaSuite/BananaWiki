"""Gunicorn settings for the wiki (``gunicorn -c python:bananawiki.ops.gunicorn_conf``).

The root ``gunicorn.conf.py`` re-exports this module, so the service command
the 1.4 updater writes (``gunicorn -c gunicorn.conf.py wsgi:app``) keeps working.

Environment: ``BW_HOST`` (default 127.0.0.1), ``BW_PORT`` (5001),
``BW_WORKERS`` (2), ``BW_THREADS`` (4), ``BW_DOWNLOAD_THREADS`` (8),
``BW_WORKER_TIMEOUT`` (120),
``BW_WRITE_TIMEOUT`` (300 seconds a response write may stall, 0 for no limit),
``BW_ACCESS_LOG`` ("-" for stdout, "off" to disable).
"""

from __future__ import annotations

import os
from typing import Any

from .access_log import AccessLogger as _AccessLogger
from .write_timeout import guard as _guard
from .write_timeout import patch_graceful_close as _patch_graceful_close


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
    for candidate in ("/dev/shm", "/tmp"):  # noqa: S108  # nosec B108 - tmpfs heartbeat files
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return None


bind = f"{_bind_host(os.environ.get('BW_HOST', '127.0.0.1') or '127.0.0.1')}:{_int('BW_PORT', 5001, 1, 65535)}"
# SQLite serialises writes: a few workers with threads beat many processes.
workers = _int("BW_WORKERS", 2, 1, 16)
worker_class = "gthread"
# Large responses hold a thread for as long as their client takes to read them: they get BW_DOWNLOAD_THREADS more,
# and never more than that at once (bananawiki.core.downloads), so the BW_THREADS stay for everything else.
threads = _int("BW_THREADS", 4, 1, 32) + _int("BW_DOWNLOAD_THREADS", 8, 1, 32)
# Every worker builds its own application (create_app serialises schema
# migrations and secret-key creation with file locks; the schema is already
# current, see on_starting). Not preloading keeps two things working: the
# background-job scheduler is started per worker, and the plugin manager's
# restart button sends SIGHUP to the master, whose new workers load the
# plugins again. BananaWiki's own modules, which on_starting imported in the
# master, are not reloaded that way: restart the service after an update.
preload_app = False
timeout = _int("BW_WORKER_TIMEOUT", 120, 10, 600)
graceful_timeout = timeout
# Not a Gunicorn setting (it has none): see post_worker_init.
_write_timeout = _int("BW_WRITE_TIMEOUT", 300, 0, 3600)
# One request per connection. A client that sends requests back to back without reading the answers (pipelining)
# would otherwise fill the connection's buffers with small responses until the next write holds its thread: small
# responses take no download place (bananawiki.core.downloads). Closing costs nothing (no wait, see below); browsers
# and proxies open the next connection as quickly as they reuse one on a local network.
keepalive = 0
max_requests = 5000
max_requests_jitter = 500
# The application applies ProxyFix itself (BW_PROXY_MODE); Gunicorn only trusts loopback.
forwarded_allow_ips = "127.0.0.1,::1"
# No control socket: the release tree is read-only and nothing uses it.
control_socket_disable = True
# Closing a connection never waits for the client (see bananawiki.ops.write_timeout).
_patch_graceful_close()
# Control characters a client sends are escaped (see bananawiki.ops.access_log).
logger_class = _AccessLogger
accesslog = None if os.environ.get("BW_ACCESS_LOG", "-") == "off" else os.environ.get("BW_ACCESS_LOG", "-")
errorlog = "-"
loglevel = "info"
_tmp = _worker_tmp_dir()
if _tmp:
    worker_tmp_dir = _tmp


def on_starting(server: Any) -> None:
    """Create or upgrade the database in the master, before any worker starts.

    A worker upgrading a large database would outlive ``timeout`` and be
    killed, rolling the upgrade back, and the workers waiting for it would
    fail to boot and stop Gunicorn. The master has no timeout; the workers
    then find the schema current. The master imports the application's
    modules but builds no application and keeps no database connection for
    the workers to inherit.
    """
    from bananawiki.wiki.config import load_config
    from bananawiki.wiki.takeover import prepare_storage

    server.log.info("Preparing the database before starting the workers.")
    prepare_storage(load_config())


def post_worker_init(worker: Any) -> None:
    """Free the thread of a client that stops reading a download (see :mod:`bananawiki.ops.write_timeout`)."""
    worker.wsgi = _guard(worker.wsgi, _write_timeout)
