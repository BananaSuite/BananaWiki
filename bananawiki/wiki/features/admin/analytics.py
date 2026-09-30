"""Daily request counters in ``analytics_daily`` (dashboard and hosting portal).

Kinds, per UTC day: ``request`` (every non-static request), ``page_view``
(successful wiki page views) and ``error`` (responses with status >= 500).
Counts are buffered in memory and written at most every few seconds, so a
busy wiki does not pay one database write per request.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import timedelta
from typing import Any

from flask import Flask, Response, current_app, request

from ....core.timeutil import utcnow
from ... import auth
from ...db import db

log = logging.getLogger("bananawiki.analytics")

KINDS = ("request", "page_view", "error")
FLUSH_SECONDS = 10.0
_EXTENSION = "bananawiki.analytics"


class Counter:
    """Per-process buffer of ``(day, kind) -> count``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counts: dict[tuple[str, str], int] = {}
        self.flushed_at = time.monotonic()

    def add(self, kind: str) -> None:
        key = (utcnow().strftime("%Y-%m-%d"), kind)
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1

    def due(self) -> bool:
        return time.monotonic() - self.flushed_at >= FLUSH_SECONDS

    def take(self) -> dict[tuple[str, str], int]:
        with self.lock:
            counts, self.counts = self.counts, {}
            self.flushed_at = time.monotonic()
        return counts


def _counter() -> Counter:
    return current_app.extensions[_EXTENSION]


def flush() -> None:
    """Write buffered counts (best effort: analytics never break a request)."""
    counts = _counter().take()
    if not counts:
        return
    try:
        db.executemany(
            "INSERT INTO analytics_daily (day, kind, count) VALUES (?, ?, ?) "
            "ON CONFLICT(day, kind) DO UPDATE SET count = count + excluded.count",
            [(day, kind, count) for (day, kind), count in counts.items()],
        )
    except sqlite3.Error as error:
        log.warning("Could not store analytics counters: %s", error)


def _count(response: Response) -> Response:
    endpoint = request.endpoint or ""
    if not endpoint or endpoint == "static" or endpoint.endswith(".static"):
        return response
    if auth.view_stateless(current_app.view_functions.get(endpoint)):
        return response
    counter = _counter()
    counter.add("request")
    if endpoint == "pages.view" and response.status_code == 200:
        counter.add("page_view")
    if response.status_code >= 500:
        counter.add("error")
    if current_app.config.get("TESTING") or counter.due():
        flush()
    return response


def install(app: Flask) -> None:
    app.extensions[_EXTENSION] = Counter()
    app.after_request(_count)


def summary(days: int = 30) -> dict[str, Any]:
    """Totals and one row per day (oldest first, gaps filled with zeros)."""
    days = max(1, min(int(days), 365))
    end = utcnow().date()
    start = end - timedelta(days=days - 1)
    index = {(start + timedelta(days=i)).isoformat(): dict.fromkeys(KINDS, 0) for i in range(days)}
    rows = db.all("SELECT day, kind, count FROM analytics_daily WHERE day BETWEEN ? AND ?",
                  (start.isoformat(), end.isoformat()))
    for row in rows:
        if row["day"] in index and row["kind"] in KINDS:
            index[row["day"]][row["kind"]] = int(row["count"] or 0)
    daily = [{"day": day, **counts} for day, counts in sorted(index.items())]
    totals = {kind: sum(entry[kind] for entry in daily) for kind in KINDS}
    peak = max((entry["request"] for entry in daily), default=0)
    return {"daily": daily, "totals": totals, "peak": peak, "days": days}
