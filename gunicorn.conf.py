"""
Gunicorn configuration file for BananaWiki.

Usage:
    gunicorn wsgi:app -c gunicorn.conf.py

All settings here can be overridden via command-line flags or environment
variables. See https://docs.gunicorn.org/en/stable/settings.html
"""

import os

import config as _bw_config  # underscore prefix avoids Gunicorn setting clash


def _resolve_worker_tmp_dir():
    """Return ``/dev/shm`` when usable so Gunicorn heartbeats stay in RAM.

    Gunicorn writes per-worker heartbeat files to ``worker_tmp_dir``
    (default ``/tmp``).  When the underlying filesystem is slow, full,
    or behaves oddly under heavy load, those writes can stall long
    enough for the arbiter to think a worker is dead and ``SIGKILL``
    it, which the user observes as random 500s, long timeouts, and
    pages that "recover on their own" once a fresh worker boots.
    Mounting heartbeats in tmpfs eliminates the I/O failure mode.
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

def _format_host_for_bind(host: str) -> str:
    """Format *host* for use in a Gunicorn bind address.

    Gunicorn requires IPv6 addresses to be enclosed in square brackets
    (e.g. ``::`` → ``[::]``, ``2001:db8::1`` → ``[2001:db8::1]``).
    Already-bracketed addresses and IPv4/hostname values pass through unchanged.
    """
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


# Uses HOST and PORT from config.py by default.
# Override: gunicorn wsgi:app --bind 0.0.0.0:5001
bind = f"{_format_host_for_bind(_bw_config.HOST)}:{_bw_config.PORT}"

# SQLite serialises all writes, so more workers just increases lock
# contention without improving throughput.  2 workers × 4 threads = 8
# concurrent request slots: plenty for a wiki that serialises on DB.
workers = 2

# Worker class: gthread provides better I/O concurrency than sync workers
# (database queries, file I/O, and external API calls won't block the entire
# worker).  Each worker spawns *threads* threads for concurrent request handling.
worker_class = "gthread"
threads = 4

# Preload the app in the master process so all workers share the same secret
# key and database connections are not inherited across the fork boundary.
# Without this, a race condition can occur on first startup where each worker
# independently generates a different key, causing session and CSRF validation
# failures ("Your session has expired").
preload_app = True
raw_env = ["BANANAWIKI_SKIP_BACKGROUND_SERVICES=1"]


def post_worker_init(worker):
    from app import start_runtime_services

    if start_runtime_services(force=True):
        worker.log.info("Started BananaWiki runtime background services")

# When behind nginx, Gunicorn trusts the X-Forwarded-* headers.
# This is handled by ProxyFix in app.py when PROXY_MODE = True.
forwarded_allow_ips = "127.0.0.1,::1"

accesslog = "-"        # stdout
errorlog = "-"         # stderr
loglevel = "info"

# Gunicorn 25 enables a local control socket by default. In hardened systemd
# units with ProtectSystem=strict this can fail if Gunicorn chooses a path
# under the app directory (for example $HOME/.gunicorn). BananaWiki does not
# use gunicornc, so disable the socket to avoid unexpected filesystem writes.
control_socket_disable = True

timeout = 120          # worker timeout (seconds): allows for PDF export of large pages
graceful_timeout = timeout  # let active exports finish during worker recycling
keepalive = 2          # keep-alive connections (seconds)

# Restart workers after handling this many requests to limit memory growth.
max_requests = 5000
max_requests_jitter = 500

# Place worker heartbeat files on tmpfs (RAM) when available so the arbiter
# never confuses slow disk I/O for a dead worker.  See _resolve_worker_tmp_dir
# above for the rationale.
_worker_tmp = _resolve_worker_tmp_dir()
if _worker_tmp:
    worker_tmp_dir = _worker_tmp


def pre_request(worker, request):
    """Close stalled socket reads/writes while allowing long active transfers."""
    request.unreader.sock.settimeout(60)
