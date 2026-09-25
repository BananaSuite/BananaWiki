"""Hosted instance storage paths, process files, and public URLs."""

import logging
import os
from . import config

logger = logging.getLogger(__name__)


def _safe_instance_dir(subdomain):
    """Return the filesystem path for an instance's data directory.

    Validates that the resolved path stays within ``INSTANCES_DIR`` to
    prevent path-traversal attacks.
    """
    base = os.path.realpath(config.INSTANCES_DIR)
    joined = os.path.realpath(os.path.join(base, subdomain))
    if not joined.startswith(base + os.sep) and joined != base:
        raise ValueError(f"Invalid subdomain path: {subdomain}")
    return joined


def _instance_dir(subdomain, domain_mode="hosting"):
    """Return the filesystem path for an instance's data directory.

    Apex-mode and hosting-mode instances are allowed to share a slug,
    e.g. ``wiki`` may exist as both ``wiki.example.com`` (apex) and
    ``wiki-hosting.example.com`` (hosting).  Their public URLs are
    distinct, so they must also have distinct on-disk data dirs.
    Apex instances therefore get an ``__apex`` suffix; hosting instances
    keep the bare-subdomain path so existing deployments don't move.
    """
    if domain_mode == "apex":
        return _safe_instance_dir(f"{subdomain}__apex")
    return _safe_instance_dir(subdomain)


def _domain_mode_of(inst):
    """Return ``inst["domain_mode"]`` with a fallback to ``"hosting"``.

    Accepts dicts, ``sqlite3.Row`` objects, and anything else that
    supports ``__getitem__`` keyed by column name.  Used by the
    ``_instance_dir_for`` helper so call sites can thread the row
    through without thinking about apex/hosting namespacing.
    """
    try:
        mode = inst["domain_mode"]
    except (KeyError, IndexError, TypeError):
        return "hosting"
    if not mode:
        return "hosting"
    return str(mode)


def _instance_dir_for(inst):
    """Return the data dir for instance row *inst*.

    Convenience wrapper around :func:`_instance_dir` that pulls the
    subdomain and domain_mode off the row in one call.
    """
    return _instance_dir(inst["subdomain"], _domain_mode_of(inst))


def _pid_file(data_dir):
    """Return the path to the PID file inside an instance's data directory."""
    return os.path.join(data_dir, "bananawiki.pid")


def _tts_worker_pid_file(data_dir):
    """Return the path to the per-instance TTS worker PID file."""
    return os.path.join(data_dir, "tts_worker.pid")


def _format_host_for_url(host: str) -> str:
    """Format *host* for use in a URL authority component.

    IPv6 addresses must be enclosed in square brackets per RFC 3986
    (e.g. ``2001:db8::1`` → ``[2001:db8::1]``).
    Already-bracketed addresses and IPv4/hostname values pass through unchanged.
    """
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _instance_url(inst):
    """Build the public URL for *inst* based on the hosting mode.

    Apex-mode instances (``domain_mode == "apex"``) are always served at
    the bare ``{slug}.{BASE_DOMAIN}``, regardless of
    ``INSTANCE_URL_SUFFIX``.  Hosting-mode instances use the configured
    flat suffix format.
    """
    if config.HOSTING_MODE == "port":
        port = inst["port"]
        if port is None:
            # Instance has been terminated and its port freed. No URL.
            return ""
        return f"{config.HOSTING_PUBLIC_SCHEME}://{_format_host_for_url(config.HOSTING_PUBLIC_HOST)}:{port}"
    slug = inst["subdomain"]
    domain_mode = "hosting"
    try:
        # Row objects expose columns by name; dicts likewise.
        mode = inst["domain_mode"]
        if mode:
            domain_mode = str(mode)
    except (KeyError, IndexError, TypeError):
        pass
    if domain_mode == "apex":
        return f"https://{slug}.{config.BASE_DOMAIN}"
    suffix = config.INSTANCE_URL_SUFFIX
    if suffix:
        return f"https://{slug}-{suffix}.{config.BASE_DOMAIN}"
    return f"https://{slug}.{config.BASE_DOMAIN}"


def _instance_bind_host():
    """Return the bind host for spawned Gunicorn instances.

    In port mode instances bind to ``0.0.0.0`` so they are directly
    reachable from outside the machine.  In subdomain mode they bind to
    ``127.0.0.1`` and rely on nginx to proxy traffic.
    """
    if config.HOSTING_MODE == "port":
        return "0.0.0.0"
    return "127.0.0.1"


def _instance_db_path(data_dir):
    """Return the BananaWiki SQLite DB path inside an instance data dir."""
    return os.path.join(data_dir, "bananawiki.db")


def _original_subdomain_from_archived(archived_subdomain):
    """Extract the original subdomain from an archived terminated subdomain."""
    marker = "--terminated-"
    if not archived_subdomain or marker not in archived_subdomain:
        return ""
    return archived_subdomain.split(marker)[0]
