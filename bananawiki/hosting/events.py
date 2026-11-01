"""Append-only moderation log (``hosting_events``).

Entries are never changed; the maintenance service deletes those older than
``HOSTING_EVENT_RETENTION_DAYS`` (:func:`prune`).
"""

from __future__ import annotations

from typing import Any

from ..core.timeutil import now_sql, sql_in
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


def subjects_since(action: str, actor_id: str, cutoff: str) -> set[str]:
    """The ids of the subjects *actor_id* recorded *action* for after *cutoff* (only the log since then is read)."""
    rows = db.all("SELECT subject_id FROM hosting_events WHERE id > ? AND action = ? AND actor_id = ? "
                  "AND created_at > ?", (_last_id_before(cutoff), action, actor_id, cutoff))
    return {row["subject_id"] for row in rows}


def recorded_since(subject_type: str, subject_id: str, action: str, reason: str, cutoff: str) -> bool:
    """Whether *action* with *reason* was recorded for the subject after *cutoff* (only the log since then is read)."""
    return db.scalar("SELECT 1 FROM hosting_events WHERE subject_type = ? AND subject_id = ? AND id > ? "
                     "AND action = ? AND reason = ? AND created_at > ? LIMIT 1",
                     (subject_type, subject_id, _last_id_before(cutoff), action, str(reason or "")[:2000],
                      cutoff)) is not None


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


def _last_id_before(cutoff: str) -> int:
    """The id up to which entries are older than *cutoff*, or 0 when the first one is not.

    Entries are appended in time order, so a binary search over the ids finds
    it in a few indexed reads: pruning never scans the whole log (it has no
    index on ``created_at``).
    """
    first = db.one("SELECT id, created_at FROM hosting_events ORDER BY id LIMIT 1")
    if first is None or first["created_at"] >= cutoff:
        return 0
    low, high = int(first["id"]), int(db.scalar("SELECT MAX(id) FROM hosting_events"))
    while low < high:
        middle = (low + high + 1) // 2
        row = db.one("SELECT id, created_at FROM hosting_events WHERE id >= ? ORDER BY id LIMIT 1", (middle,))
        if row["created_at"] < cutoff:
            low = int(row["id"])
        else:
            high = middle - 1
    return low


def prune(retention_days: int) -> int:
    """Delete entries older than *retention_days* (0 keeps them all); returns how many went.

    The records of a wiki creation that never finished are kept: they decide
    whether the data under its name may be deleted (see
    :func:`.instances._runtime_may_own_data`). So is the fact that a wiki
    still in use, or still restorable, changed owner, without who did it or for whom: it decides
    whether the admin-password reset covers every administrator account
    (see :func:`.instances._owner_changed`).
    """
    if retention_days <= 0:
        return 0
    cutoff = sql_in(days=-retention_days)
    last = _last_id_before(cutoff)
    if not last:
        return 0
    from .instances import PROVISIONING_REFUSED, PROVISIONING_STARTED

    # A terminated wiki whose data is kept can still be restored under the same id, so it keeps the record too.
    owner_changed = ("subject_type = 'instance' AND action = 'instance.owner_changed' "
                     "AND subject_id IN (SELECT id FROM instances WHERE status != 'terminated' "
                     "OR data_retained_until IS NOT NULL)")
    with db.transaction():
        db.execute("UPDATE hosting_events SET actor_id = NULL, reason = '' WHERE id <= ? AND created_at < ? "  # noqa: S608
                   "AND " + owner_changed, (last, cutoff))
        return db.execute(
            "DELETE FROM hosting_events WHERE id <= ? AND created_at < ? AND NOT (subject_type = 'instance' "  # noqa: S608
            "AND action IN (?, ?) AND subject_id IN (SELECT id FROM instances "
            "WHERE provisioning_state != 'ready' AND status != 'terminated')) AND NOT (" + owner_changed + ")",
            (last, cutoff, PROVISIONING_STARTED, PROVISIONING_REFUSED),
        ).rowcount
