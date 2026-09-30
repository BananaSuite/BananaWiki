"""Who is editing a page right now (``editing_sessions``).

The editor sends a heartbeat every ``HEARTBEAT_SECONDS``; a session without a
heartbeat for ``STALE_SECONDS`` no longer counts. Stale rows are pruned by a
background job.
"""

from __future__ import annotations

from typing import Any

from ....core.timeutil import now_sql, sql_in
from ...db import db

HEARTBEAT_SECONDS = 20
STALE_SECONDS = 50
PRUNE_AFTER_SECONDS = 3600


def heartbeat(page_id: int, user: dict[str, Any]) -> None:
    now = now_sql()
    db.execute(
        "INSERT INTO editing_sessions (page_id, user_id, username, last_heartbeat, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(page_id, user_id) DO UPDATE SET last_heartbeat = excluded.last_heartbeat, "
        "username = excluded.username",
        (page_id, user["id"], user["username"], now, now),
    )


def active_editors(page_id: int, *, exclude_user_id: str | None = None) -> list[str]:
    """Usernames with a fresh heartbeat on *page_id* (current names, not the stored ones)."""
    return db.column(
        "SELECT u.username FROM editing_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.page_id = ? AND s.last_heartbeat > ? AND s.user_id IS NOT ? ORDER BY s.last_heartbeat DESC",
        (page_id, sql_in(seconds=-STALE_SECONDS), exclude_user_id),
    )


def stop(page_id: int, user_id: str) -> None:
    db.execute("DELETE FROM editing_sessions WHERE page_id = ? AND user_id = ?", (page_id, user_id))


def prune() -> int:
    """Background job: forget sessions that stopped sending heartbeats."""
    return db.execute(
        "DELETE FROM editing_sessions WHERE last_heartbeat < ?", (sql_in(seconds=-PRUNE_AFTER_SECONDS),)
    ).rowcount
