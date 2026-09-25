"""Per-user upload quota tracking.

Stores the rolling 24-hour upload count and total bytes on each ``users``
row (columns ``upload_window_start``, ``upload_window_count``,
``upload_window_bytes``).  Site-wide ceilings live in ``site_settings``
(``upload_quota_per_day_count`` and ``upload_quota_per_day_bytes``).  A
zero ceiling means *unlimited*: this preserves the previous behaviour
for sites that have not configured a limit.

The ``check_and_record_upload`` function is the only public entry point;
it atomically resets the rolling window when stale, validates the
incoming upload against the quota, and bumps the counter on success.

This module is intentionally small and dependency-free so it can be
imported from ``routes/uploads.py`` without pulling in the rest of the
``db`` package.
"""

from datetime import datetime, timedelta, timezone

from ._connection import get_db_context, retry_on_busy


_WINDOW = timedelta(hours=24)


def _parse_iso(value):
    """Parse an ISO-8601 timestamp.  Returns ``None`` on any failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


@retry_on_busy
def get_user_upload_usage(user_id):
    """Return ``(count, bytes_used, window_start)`` for *user_id*.

    Resets to ``(0, 0, now)`` if the existing window is older than 24
    hours or missing.  Does **not** persist the reset; callers that need
    persistence should use :func:`check_and_record_upload`.
    """
    now = datetime.now(timezone.utc)
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT upload_window_start, upload_window_count, "
            "upload_window_bytes FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if row is None:
        return 0, 0, now
    start = _parse_iso(row["upload_window_start"])
    if start is None or now - start >= _WINDOW:
        return 0, 0, now
    return (
        int(row["upload_window_count"] or 0),
        int(row["upload_window_bytes"] or 0),
        start,
    )


def check_and_record_upload(user_id, byte_count, settings=None):
    """Validate *byte_count* against the per-user daily quota and record it.

    Returns ``(True, None)`` when the upload fits inside the quota and
    the counters were bumped.  Returns ``(False, error_message)`` when
    the quota would be exceeded; in that case nothing is written.

    *settings* is the (optional) cached ``site_settings`` row: passing
    it avoids an extra DB round-trip when the caller already has it.
    When *settings* is omitted the function fetches the row itself.

    A quota of ``0`` (the default after migration) disables that
    particular ceiling, so existing deployments are not silently
    restricted.
    """
    byte_count = max(0, int(byte_count))
    now = datetime.now(timezone.utc)

    with get_db_context() as conn:
        if settings is None:
            settings = conn.execute(
                "SELECT upload_quota_per_day_count, upload_quota_per_day_bytes "
                "FROM site_settings WHERE id=1"
            ).fetchone()
        max_count = int((settings or {})["upload_quota_per_day_count"]) if settings else 0
        max_bytes = int((settings or {})["upload_quota_per_day_bytes"]) if settings else 0

        row = conn.execute(
            "SELECT upload_window_start, upload_window_count, "
            "upload_window_bytes FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return False, "User not found."

        start = _parse_iso(row["upload_window_start"])
        if start is None or now - start >= _WINDOW:
            cur_count = 0
            cur_bytes = 0
            window_start = now
        else:
            cur_count = int(row["upload_window_count"] or 0)
            cur_bytes = int(row["upload_window_bytes"] or 0)
            window_start = start

        new_count = cur_count + 1
        new_bytes = cur_bytes + byte_count

        if max_count > 0 and new_count > max_count:
            return False, (
                f"Daily upload limit reached ({max_count} files in 24 hours). "
                "Please try again later."
            )
        if max_bytes > 0 and new_bytes > max_bytes:
            mb = max_bytes // (1024 * 1024)
            return False, (
                f"Daily upload size limit reached ({mb} MB in 24 hours). "
                "Please try again later."
            )

        conn.execute(
            "UPDATE users SET upload_window_start=?, upload_window_count=?, "
            "upload_window_bytes=? WHERE id=?",
            (window_start.isoformat(), new_count, new_bytes, user_id),
        )
        conn.commit()
        return True, None
