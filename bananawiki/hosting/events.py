"""Append-only moderation log (``hosting_events``)."""

from __future__ import annotations

from typing import Any

from ..core.timeutil import now_sql
from .db import db

SUBJECTS = ("account", "instance")


def record(subject_type: str, subject_id: str, action: str, actor_id: str | None = None, reason: str = "") -> None:
    if subject_type not in SUBJECTS:
        raise ValueError("Invalid event subject")
    db.execute(
        "INSERT INTO hosting_events (subject_type, subject_id, action, actor_id, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subject_type, subject_id, action[:100], actor_id, str(reason or "")[:2000], now_sql()),
    )


def recent(subject_type: str | None = None, subject_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    clauses, values = [], []
    if subject_type in SUBJECTS:
        clauses.append("e.subject_type = ?")
        values.append(subject_type)
    if subject_id:
        clauses.append("e.subject_id = ?")
        values.append(subject_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    values.append(max(1, min(int(limit), 500)))
    return db.all(
        "SELECT e.*, a.username AS actor_name FROM hosting_events e LEFT JOIN accounts a ON a.id = e.actor_id"
        + where + " ORDER BY e.id DESC LIMIT ?",
        values,
    )
