"""Login and general rate limiting helpers for the hosting platform."""

import math
from datetime import datetime, timezone, timedelta

from ._connection import get_hosting_db_context


def record_hosting_login_attempt(ip):
    """Insert a failed login attempt record for the given *ip* address."""
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO hosting_login_attempts (ip, attempted_at) VALUES (?, ?)",
            (ip, now),
        )
        conn.commit()


def count_recent_hosting_login_attempts(ip, window_seconds):
    """Return count of attempts from *ip* within the last *window_seconds*."""
    with get_hosting_db_context() as conn:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()
        # Prune old entries to keep table small
        conn.execute(
            "DELETE FROM hosting_login_attempts WHERE attempted_at < ?",
            (cutoff,),
        )
        cnt = conn.execute(
            "SELECT COUNT(*) FROM hosting_login_attempts "
            "WHERE ip=? AND attempted_at >= ?",
            (ip, cutoff),
        ).fetchone()[0]
        conn.commit()
    return cnt


def clear_hosting_login_attempts(ip):
    """Remove all failed login attempt records for the given *ip* address."""
    with get_hosting_db_context() as conn:
        conn.execute("DELETE FROM hosting_login_attempts WHERE ip=?", (ip,))
        conn.commit()


def check_and_record_hosting_rate_limit(ip, bucket, max_requests, window_seconds):
    """Check rate limit and record a hit atomically.

    Returns ``True`` if the request is within the limit, ``False`` otherwise.
    Uses ``BEGIN IMMEDIATE`` so the check and record are atomic across
    all Gunicorn worker processes.
    """
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        # Prune old entries of this bucket only. Buckets have different
        # windows, and pruning all of them with the caller's window cut the
        # five-minute limits (second-factor codes among them) down to one
        # minute whenever a one-minute bucket was used.
        conn.execute(
            "DELETE FROM hosting_rate_limit_hits WHERE bucket=? AND hit_at < ?",
            (bucket, cutoff),
        )
        cnt = conn.execute(
            "SELECT COUNT(*) FROM hosting_rate_limit_hits "
            "WHERE ip=? AND bucket=? AND hit_at >= ?",
            (ip, bucket, cutoff),
        ).fetchone()[0]
        if cnt >= max_requests:
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO hosting_rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
            (ip, bucket, now),
        )
        conn.commit()
    return True


def get_hosting_rate_limit_retry_after(ip, bucket, window_seconds):
    """Return whole seconds until the oldest hit in *bucket* leaves the window.

    Meant for a request that :func:`check_and_record_hosting_rate_limit` has
    just refused, so the response can carry an accurate ``Retry-After``.
    *ip* is whatever key the hits were recorded under.  Never returns less
    than 1, because a client told to retry after 0 seconds would retry at once.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
    with get_hosting_db_context() as conn:
        oldest = conn.execute(
            "SELECT MIN(hit_at) FROM hosting_rate_limit_hits "
            "WHERE ip=? AND bucket=? AND hit_at >= ?",
            (ip, bucket, cutoff),
        ).fetchone()[0]
    if not oldest:
        return 1
    try:
        oldest_at = datetime.fromisoformat(oldest)
    except ValueError:
        return window_seconds
    if oldest_at.tzinfo is None:
        oldest_at = oldest_at.replace(tzinfo=timezone.utc)
    remaining = (oldest_at + timedelta(seconds=window_seconds) - now).total_seconds()
    return max(1, math.ceil(remaining))
