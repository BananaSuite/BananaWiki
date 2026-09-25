"""Append-only records of hosting moderation and domain changes."""

from datetime import datetime, timezone

from ._connection import get_hosting_db_context


def record_event(conn, subject_type, subject_id, action, actor_id=None, reason=""):
    if subject_type not in {"account", "instance"}:
        raise ValueError("Invalid event subject")
    conn.execute(
        "INSERT INTO hosting_events (subject_type, subject_id, action, actor_id, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subject_type, subject_id, action, actor_id, str(reason)[:2000], datetime.now(timezone.utc).isoformat()),
    )


def list_events(subject_type=None, subject_id=None, limit=100):
    clauses, values = [], []
    if subject_type:
        clauses.append("e.subject_type=?")
        values.append(subject_type)
    if subject_id:
        clauses.append("e.subject_id=?")
        values.append(subject_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    values.append(max(1, min(int(limit), 500)))
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT e.*, a.username AS actor_name FROM hosting_events e "
            "LEFT JOIN accounts a ON a.id=e.actor_id" + where + " ORDER BY e.id DESC LIMIT ?",
            values,
        ).fetchall()
