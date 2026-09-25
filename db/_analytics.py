"""Per-wiki request analytics.

A very small daily roll-up table that records, per UTC day:

* ``request``: every non-static HTTP request handled by the wiki.
* ``page_view``: successful renders of ``/wiki/<slug>`` pages.
* ``error``: any response with status code ``>= 500`` (or unhandled
  exception).

The table is written from ``app.before_request_hook`` (for ``request``
and ``page_view``) and from the global 500/exception handler.  Each write
is a single ``INSERT OR REPLACE`` keyed on ``(day, kind)`` so the hot
path remains cheap.

Reads happen from the hosting portal, which opens the wiki's
``bananawiki.db`` read-only to render the analytics page.  This module
deliberately exposes ``get_analytics_summary`` so the portal can call it
against an arbitrary connection (see ``hosting/instance_manager.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import sqlite3

from ._connection import get_db_context, retry_on_busy


# Kinds we recognise.  Anything else is silently ignored so a typo in a
# call site can never crash the hot path.
ALLOWED_KINDS = frozenset({"request", "page_view", "error"})


def _utc_today() -> str:
    """Return today's date as a ``YYYY-MM-DD`` string in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_request(kind: str = "request") -> None:
    """Increment the daily counter for *kind* in the current UTC day.

    Safe to call from any request hook: failures are swallowed so a
    transient lock/IO error never disrupts the user-facing response.
    Unknown ``kind`` values are ignored to keep the call site
    forgiving.
    """
    if kind not in ALLOWED_KINDS:
        return
    day = _utc_today()
    try:
        with get_db_context() as conn:
            conn.execute(
                "INSERT INTO analytics_daily (day, kind, count) VALUES (?, ?, 1) "
                "ON CONFLICT(day, kind) DO UPDATE SET count = count + 1",
                (day, kind),
            )
            conn.commit()
    except sqlite3.Error:
        # Analytics is best-effort; do not block the user-facing
        # request just because the counter could not be persisted.
        return


@retry_on_busy
def get_analytics_summary(
    days: int = 30,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, object]:
    """Return a daily analytics roll-up for the last *days* UTC days.

    The result is a dict with two keys:

    * ``totals``: dict mapping each kind to its total over the window.
    * ``daily``, list of ``{"day": "YYYY-MM-DD", "request": N, ...}``
      ordered oldest first, with one entry per day in the window
      (missing days are filled with zeros so callers can render a chart
      without gap-handling).

    If *conn* is provided it is used as the connection (useful for
    reading another instance's database from the hosting portal); the
    caller owns its lifetime.  Otherwise the wiki's own connection is
    used.
    """
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 30
    if d <= 0:
        d = 1
    days = max(1, min(d, 365))
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)

    day_index: Dict[str, Dict[str, int]] = {}
    cursor = start
    while cursor <= end:
        day_index[cursor.strftime("%Y-%m-%d")] = {k: 0 for k in ALLOWED_KINDS}
        cursor += timedelta(days=1)

    sql = (
        "SELECT day, kind, count FROM analytics_daily "
        "WHERE day BETWEEN ? AND ? ORDER BY day ASC"
    )
    params = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    def _fetch(c: sqlite3.Connection) -> List[sqlite3.Row]:
        """Return all analytics rows in the requested day range."""
        # The hosting portal may pass a connection that hasn't set
        # row_factory.  Force per-row dict-style access so we don't
        # care.
        c.row_factory = sqlite3.Row
        try:
            return c.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Older instance DBs may not have the table yet; treat as
            # "no data" rather than crashing the analytics page.
            return []

    if conn is not None:
        rows = _fetch(conn)
    else:
        with get_db_context() as own:
            rows = _fetch(own)

    for row in rows:
        day = row["day"]
        kind = row["kind"]
        if day in day_index and kind in ALLOWED_KINDS:
            day_index[day][kind] = int(row["count"] or 0)

    daily: List[Dict[str, object]] = []
    totals = {k: 0 for k in ALLOWED_KINDS}
    for day, counts in sorted(day_index.items()):
        entry: Dict[str, object] = {"day": day}
        for kind, value in counts.items():
            entry[kind] = value
            totals[kind] += value
        daily.append(entry)

    return {"totals": totals, "daily": daily, "window_days": days}


def reset_analytics() -> None:
    """Delete every analytics row.  Intended for tests / admin tooling."""
    try:
        with get_db_context() as conn:
            conn.execute("DELETE FROM analytics_daily")
            conn.commit()
    except sqlite3.Error:
        return
