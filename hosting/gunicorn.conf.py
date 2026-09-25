"""
Gunicorn configuration file for the BananaWiki Hosting Platform.

Usage:
    gunicorn hosting.wsgi:app -c hosting/gunicorn.conf.py

All settings here can be overridden via command-line flags or environment
variables.  See https://docs.gunicorn.org/en/stable/settings.html
"""

import os


def _resolve_worker_tmp_dir():
    """Return ``/dev/shm`` when usable so Gunicorn heartbeats stay in RAM.

    Gunicorn writes per-worker heartbeat files to ``worker_tmp_dir``
    (default ``/tmp``).  When the underlying filesystem is slow or
    contended those writes can stall long enough for the arbiter to
    think a worker is dead and ``SIGKILL`` it.  Routing the heartbeats
    through tmpfs eliminates that I/O failure mode and is the standard
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


def _format_host_for_bind(host: str) -> str:
    """Format *host* for use in a Gunicorn bind address.

    Gunicorn requires IPv6 addresses to be enclosed in square brackets
    (e.g. ``::`` → ``[::]``, ``2001:db8::1`` → ``[2001:db8::1]``).
    Already-bracketed addresses and IPv4/hostname values pass through unchanged.
    """
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


_host = os.environ.get("HOSTING_HOST", "127.0.0.1")
_port = os.environ.get("HOSTING_PORT", "5099")
bind = f"{_format_host_for_bind(_host)}:{_port}"

# The hosting portal doubles as a reverse proxy (SubdomainProxyMiddleware)
# and accesses its own SQLite database.  2 workers × 4 threads = 8 slots
# is enough proxy capacity without hammering the SQLite write lock.  The
# circuit breaker + concurrency cap in the proxy layer handle overload.
workers = 2

# Worker class: ``gthread`` provides better I/O concurrency than ``sync``
# workers.  In subdomain hosting mode the portal acts as a reverse proxy to
# every running instance via ``SubdomainProxyMiddleware``; with ``sync``
# workers a single slow upstream instance (e.g. one that's still booting
# after a VPS restart, or busy compiling a PDF export) blocks the entire
# worker process and starves all other requests, including dashboard
# requests for unrelated subdomains.  ``gthread`` lets unrelated requests
# proceed while one thread is parked on a slow upstream.
worker_class = "gthread"
threads = 4

# Preload the app in the master process so all workers share the same secret
# key.  Background recovery/cleanup threads are intentionally skipped during
# preload and started from post_worker_init after fork.
preload_app = True
# Production deploys run lifecycle cleanup, recovery, and scheduled backups in
# the generated <service>-maintenance.service.  Keeping those jobs out of workers
# prevents duplicate timers after reloads and guarantees they survive traffic
# worker restarts.  Developers may still call start_background_services()
# explicitly when running without the supplied units.
raw_env = ["BANANAWIKI_HOSTING_SKIP_BACKGROUND_SERVICES=1"]

# When behind nginx, Gunicorn trusts the X-Forwarded-* headers.
# This is handled by ProxyFix in hosting/app.py when HOSTING_PROXY_MODE is on.
forwarded_allow_ips = "127.0.0.1,::1"

accesslog = "-"        # stdout
errorlog = "-"         # stderr
loglevel = "info"

# Gunicorn 25 enables a local control socket by default. The hosting portal
# does not use gunicornc, and hardened systemd units may block writes to the
# default socket directory, so disable the control socket explicitly.
control_socket_disable = True

# 120 s gives admin lifecycle actions enough headroom to finish synchronously
# without the arbiter killing the worker.  ``_start_process`` in the instance
# manager waits up to ``_STARTUP_TIMEOUT (30 s) + _HTTP_READY_TIMEOUT (30 s) =
# 60 s`` for a freshly-spawned managed-wiki Gunicorn to accept connections.
# Routes that call this synchronously: admin "restart instance", admin
# "unsuspend instance", user "restart instance": were exceeding the previous
# 30 s ceiling and surfacing as random ``500 Internal Server Error`` responses
# whenever the spawned wiki was even slightly slow to come up (cold caches,
# bytecode pre-compilation, schema migration on first boot).  Bumping the
# portal-side timeout removes that failure mode without changing the spawn
# semantics, and 120 s is still tight enough that a genuinely wedged spawn
# does not hold the worker forever.
timeout = 120          # worker timeout (seconds)
graceful_timeout = timeout  # let accepted requests finish during worker recycling
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
