"""Contributor leaderboard.

Revision sizes are computed once per history entry (:func:`refresh`, run by a
job and, for small backlogs, before a view) into ``leaderboard_revisions``.
Views aggregate that table in SQL, restricted to the pages the viewer may
see, so no revision content is loaded into Python. Characters added and
removed are measured as the change in size against the page's previous
revision (1.4 ran a full text diff of every revision on every view).
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any

from ....core.timeutil import sql_in
from ...db import db
from ..pages import service as pages

SORTS = ("score", "edits", "chars", "pages", "active", "recent")
RANGES = {"all": None, "30d": 30, "90d": 90, "year": 365}
BATCH = 500
INLINE_BACKLOG = 2000
LIMIT = 250

_SORT_KEYS = {
    "score": lambda e: e["score"], "edits": lambda e: e["edit_count"], "chars": lambda e: e["total"],
    "pages": lambda e: e["pages_touched"], "active": lambda e: e["active_days"],
    "recent": lambda e: e["last_contribution"] or "",
}


# ── Cache maintenance ─────────────────────────────────────────────────────────


def backlog() -> int:
    last = db.scalar("SELECT COALESCE(MAX(history_id), 0) FROM leaderboard_revisions", default=0)
    return int(db.scalar("SELECT COUNT(*) FROM page_history WHERE id > ?", (last,), default=0))


def refresh(max_rows: int | None = None) -> int:
    """Record sizes for history entries not seen yet; return how many were added."""
    done = 0
    while max_rows is None or done < max_rows:
        with db.transaction():
            last = db.scalar("SELECT COALESCE(MAX(history_id), 0) FROM leaderboard_revisions", default=0)
            rows = db.all(
                "SELECT id, page_id, length(content) AS size, is_revert, created_at FROM page_history "
                "WHERE id > ? ORDER BY id LIMIT ?",
                (last, BATCH),
            )
            for row in rows:
                previous = db.one(
                    "SELECT size FROM leaderboard_revisions WHERE page_id = ? AND history_id < ? "
                    "ORDER BY history_id DESC LIMIT 1",
                    (row["page_id"], row["id"]),
                )
                first = not db.scalar("SELECT 1 FROM page_history WHERE page_id = ? AND id < ? LIMIT 1",
                                      (row["page_id"], row["id"]))
                before = previous["size"] if previous else 0
                size = int(row["size"] or 0)
                db.insert("leaderboard_revisions", {
                    "history_id": row["id"], "page_id": row["page_id"], "size": size,
                    "added": max(0, size - before), "removed": max(0, before - size), "is_first": 1 if first else 0,
                    "is_revert": 1 if row["is_revert"] else 0, "created_at": row["created_at"],
                    "day": (row["created_at"] or "")[:10],
                })
        done += len(rows)
        if len(rows) < BATCH:
            break
    return done


def refresh_all() -> int:
    return refresh()


# ── Ranking ───────────────────────────────────────────────────────────────────


def _since(range_key: str) -> str | None:
    days = RANGES.get(range_key)
    return sql_in(days=-days) if days else None


def _base(viewer: dict[str, Any] | None, since: str | None) -> tuple[str, list[Any]]:
    """FROM/WHERE shared by the queries: revisions of visible pages by existing accounts.

    ``page_history`` is read only through its (edited_by, id) index, so no
    revision content is touched.
    """
    where, params = pages.visible_filter(viewer)
    sql = (
        "FROM page_history ph INDEXED BY idx_page_history_editor JOIN leaderboard_revisions lr ON lr.history_id = ph.id "
        "JOIN users u ON u.id = ph.edited_by "
        f"WHERE lr.page_id IN (SELECT p.id FROM pages p WHERE {where})"
    )
    if since:
        sql += " AND lr.created_at >= ?"
        params = [*params, since]
    return sql, params


def _streaks(days: list[str]) -> tuple[int, int]:
    """(longest, current) runs of consecutive days; *days* sorted ascending."""
    longest = current = 0
    previous: date | None = None
    for text in days:
        try:
            day = date.fromisoformat(text)
        except ValueError:
            continue
        current = current + 1 if previous and day - previous == timedelta(days=1) else 1
        longest = max(longest, current)
        previous = day
    return longest, current


def _score(entry: dict[str, Any]) -> int:
    """The 1.4 impact score: breadth, creation, consistency and volume, minus reverts."""
    net_positive = max(entry["added"] - entry["removed"], 0)
    score = (entry["edit_count"] * 10 + entry["pages_touched"] * 20 + entry["pages_created"] * 30
             + entry["active_days"] * 8 + entry["longest_streak"] * 4 + entry["total"] / 120
             + net_positive / 250 - entry["revert_count"] * 3)
    return max(0, round(score))


def ranking(viewer: dict[str, Any] | None, *, sort: str = "score", range_key: str = "all") -> dict[str, Any]:
    """Every contributor's statistics, ranked, plus site-wide totals."""
    if backlog() <= INLINE_BACKLOG:
        refresh()
    sort = sort if sort in SORTS else "score"
    base, params = _base(viewer, _since(range_key))
    rows = db.all(
        "SELECT ph.edited_by AS user_id, u.username, COUNT(*) AS edit_count, SUM(lr.added) AS added, "
        "SUM(lr.removed) AS removed, SUM(lr.is_first) AS pages_created, COUNT(DISTINCT lr.page_id) AS pages_touched, "
        "COUNT(DISTINCT lr.day) AS active_days, SUM(lr.is_revert) AS revert_count, "
        "SUM(CASE WHEN lr.added + lr.removed = 0 THEN 1 ELSE 0 END) AS metadata_edits, "
        "MIN(lr.created_at) AS first_contribution, MAX(lr.created_at) AS last_contribution, "
        "MAX(lr.added + lr.removed) AS largest_edit, MAX(lr.history_id) AS last_history_id "
        f"{base} GROUP BY ph.edited_by",
        params,
    )
    day_rows = db.all(f"SELECT DISTINCT ph.edited_by AS user_id, lr.day {base} ORDER BY 1, 2", params)
    days_by_user: dict[str, list[str]] = {}
    for row in day_rows:
        days_by_user.setdefault(row["user_id"], []).append(row["day"])
    entries = []
    for row in rows:
        entry = dict(row)
        entry["total"] = entry["added"] + entry["removed"]
        entry["net"] = entry["added"] - entry["removed"]
        entry["avg_change"] = round(entry["total"] / entry["edit_count"]) if entry["edit_count"] else 0
        entry["longest_streak"], entry["current_streak"] = _streaks(days_by_user.get(entry["user_id"], []))
        entry["score"] = _score(entry)
        entries.append(entry)
    entries.sort(key=lambda e: e["username"].casefold())
    entries.sort(key=lambda e: (_SORT_KEYS[sort](e), e["score"], e["edit_count"], e["total"]), reverse=True)
    for rank, entry in enumerate(entries, start=1):
        entry["rank"] = rank
    all_days = {day for days in days_by_user.values() for day in days}
    summary = {
        "contributors": len(entries),
        "edits": sum(e["edit_count"] for e in entries),
        "pages_created": sum(e["pages_created"] for e in entries),
        "pages_touched": int(db.scalar(f"SELECT COUNT(DISTINCT lr.page_id) {base}", params, default=0)),
        "active_days": len(all_days),
        "added": sum(e["added"] for e in entries),
        "removed": sum(e["removed"] for e in entries),
    }
    return {"entries": entries, "summary": summary, "sort": sort, "range": range_key}


def attach_details(entries: list[dict[str, Any]]) -> None:
    """Latest page and profile visibility for the displayed entries (two queries)."""
    if not entries:
        return
    ids = [e["last_history_id"] for e in entries]
    marks = ",".join("?" for _ in ids)
    latest = {row["history_id"]: row for row in db.all(
        f"SELECT lr.history_id, p.title, p.slug FROM leaderboard_revisions lr JOIN pages p ON p.id = lr.page_id "
        f"WHERE lr.history_id IN ({marks})", ids)}
    users = [e["user_id"] for e in entries]
    marks = ",".join("?" for _ in users)
    published = set(db.column(
        f"SELECT user_id FROM user_profiles WHERE user_id IN ({marks}) AND page_published = 1 "
        "AND page_disabled_by_admin = 0", users))
    for entry in entries:
        page = latest.get(entry["last_history_id"])
        entry["last_page_title"] = page["title"] if page else ""
        entry["last_page_slug"] = page["slug"] if page else ""
        entry["profile_published"] = entry["user_id"] in published


def to_csv(entries: list[dict[str, Any]]) -> str:
    columns = ("rank", "username", "score", "edit_count", "pages_touched", "pages_created", "active_days",
               "longest_streak", "current_streak", "added", "removed", "net", "avg_change", "largest_edit",
               "revert_count", "metadata_edits", "first_contribution", "last_contribution")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(entries)
    return output.getvalue()
