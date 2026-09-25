"""Low-overhead managed-instance storage quota enforcement."""

from __future__ import annotations

import os
import threading
import time

import config

_lock = threading.Lock()
_cached_at = 0.0
_cached_bytes = 0
_CACHE_SECONDS = 3.0
_HEADROOM_BYTES = 2 * 1024 * 1024


def storage_usage_bytes(*, force=False):
    """Return the total bytes used by all files under the instance directory."""
    global _cached_at, _cached_bytes
    now = time.monotonic()
    with _lock:
        if not force and now - _cached_at < _CACHE_SECONDS:
            return _cached_bytes
        total = 0
        root = os.path.realpath(config.INSTANCE_DIR)
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, filename))
                    except OSError:
                        pass
        except OSError:
            pass
        _cached_bytes = total
        _cached_at = now
        return total


def body_size_unknown(content_length, environ):
    """Return whether a request sends a body without declaring its size.

    The quota check sizes an incoming body from ``Content-Length``.  A body
    sent with ``Transfer-Encoding: chunked`` has none (Werkzeug reports
    ``None`` even when both headers are present), yet Werkzeug still reads
    it, up to the app-wide ``MAX_CONTENT_LENGTH`` of several hundred MB.
    Such a request would pass the check as if it were empty.

    A request with neither header has no body at all, so only the
    transfer-encoding case counts as unknown.
    """
    if content_length is not None:
        return False
    return bool((environ.get("HTTP_TRANSFER_ENCODING") or "").strip())


def mutation_would_exceed_quota(content_length=0):
    """Return (would_exceed, used_bytes, limit_bytes) for the incoming request."""
    limit = int(getattr(config, "STORAGE_LIMIT_BYTES", 0) or 0)
    if limit <= 0:
        return False, 0, 0
    try:
        incoming = max(0, int(content_length or 0))
    except (TypeError, ValueError):
        incoming = 0
    used = storage_usage_bytes()
    return used + incoming + _HEADROOM_BYTES > limit, used, limit


def invalidate_storage_usage_cache():
    """Force the next storage_usage_bytes() call to recompute from disk."""
    global _cached_at
    with _lock:
        _cached_at = 0.0
