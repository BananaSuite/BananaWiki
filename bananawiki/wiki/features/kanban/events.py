"""The per-board operation log (``kanban_events``) and the activity log.

Every change appends one event in the same transaction, so the sequence
number (``MAX(seq) + 1`` under ``BEGIN IMMEDIATE``) cannot collide. Clients
poll ``/sync?since=<seq>`` and apply the events to their copy of the board.
Operations and payloads:

* ``board_updated``   ``{title, description}``
* ``column_upsert``   ``{column, order}`` (``order``: column ids left to right)
* ``column_deleted``  ``{column_id, order}``
* ``columns_reordered`` ``{order}``
* ``ticket_upsert``   ``{ticket, columns}`` (``columns``: ``{column_id: [ticket ids]}``
  for every column whose order changed)
* ``ticket_upsert`` also carries ``restored: true`` when an archived ticket came back
* ``ticket_deleted``  ``{ticket_ids, columns, archived}`` (``archived``: the ids that were archived)
* ``tickets_archived`` ``{ticket_ids, columns}``: the tickets left the board for its archive
* ``tickets_reordered`` ``{columns}``
* ``board_reset``     ``{}``: reload the whole board (revert, bulk changes)

Clients treat any operation they do not know as ``board_reset``. The prune
job trims old events but always keeps each board's newest one, so sequence
numbers never restart.
"""

from __future__ import annotations

import json
from typing import Any

from flask import has_request_context, request

from ....core.timeutil import now_sql, sql_in
from ...db import db

SYNC_BATCH = 200
EVENT_RETENTION_DAYS = 2
EVENTS_KEPT_PER_BOARD = 1000
ACTIVITY_KEPT_PER_BOARD = 500
SESSION_HEADER = "X-Kanban-Session"


def client_session() -> str:
    if not has_request_context():
        return ""
    return (request.headers.get(SESSION_HEADER) or "").strip()[:64]


def append(board_id: int, op_type: str, payload: dict[str, Any], user_id: str | None) -> int:
    """Record an operation; call inside the transaction that made the change."""
    seq = int(db.scalar("SELECT COALESCE(MAX(seq), 0) FROM kanban_events WHERE board_id = ?", (board_id,),
                        default=0)) + 1
    db.insert("kanban_events", {
        "board_id": board_id, "seq": seq, "op_type": op_type,
        "payload": json.dumps(payload, separators=(",", ":")), "by_user_id": user_id,
        "by_session": client_session(), "created_at": now_sql(),
    })
    return seq


def head(board_id: int) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(seq), 0) FROM kanban_events WHERE board_id = ?", (board_id,),
                         default=0))


def since(board_id: int, seq: int, *, exclude_session: str = "") -> dict[str, Any]:
    """Events after *seq*, or ``reset: True`` when the client must reload the board.

    A reset is needed when the events the client missed were pruned, when its
    cursor is ahead of the log, or when too many changes piled up.
    """
    latest = head(board_id)
    if seq >= latest:
        return {"events": [], "seq": latest, "reset": seq > latest}
    oldest = int(db.scalar("SELECT COALESCE(MIN(seq), 0) FROM kanban_events WHERE board_id = ?", (board_id,),
                           default=0))
    if seq < oldest - 1 or latest - seq > SYNC_BATCH:
        return {"events": [], "seq": latest, "reset": True}
    rows = db.all(
        "SELECT seq, op_type, payload, by_user_id, by_session, created_at FROM kanban_events "
        "WHERE board_id = ? AND seq > ? ORDER BY seq",
        (board_id, seq),
    )
    events = []
    for row in rows:
        if exclude_session and row["by_session"] == exclude_session:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        events.append({"seq": row["seq"], "op": row["op_type"], "payload": payload,
                       "by_user_id": row["by_user_id"], "created_at": row["created_at"]})
    return {"events": events, "seq": latest, "reset": False}


def prune() -> int:
    """Background job: drop old events, keeping each board's newest one."""
    newest = "(SELECT MAX(e2.seq) FROM kanban_events e2 WHERE e2.board_id = kanban_events.board_id)"
    removed = db.execute(
        f"DELETE FROM kanban_events WHERE created_at < ? AND seq < {newest}",
        (sql_in(days=-EVENT_RETENTION_DAYS),),
    ).rowcount
    removed += db.execute(
        f"DELETE FROM kanban_events WHERE seq <= {newest} - ?", (EVENTS_KEPT_PER_BOARD,),
    ).rowcount
    db.execute(
        "DELETE FROM kanban_activity_log WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
        "(PARTITION BY board_id ORDER BY id DESC) AS n FROM kanban_activity_log) WHERE n > ?)",
        (ACTIVITY_KEPT_PER_BOARD,),
    )
    return removed


# ── Activity log ──────────────────────────────────────────────────────────────


def log_activity(board_id: int, user_id: str | None, action: str, details: str) -> None:
    db.insert("kanban_activity_log", {
        "board_id": board_id, "user_id": user_id, "action": action, "details": details[:1000],
        "created_at": now_sql(),
    })


def recent_activity(board_id: int, limit: int = 50) -> list[dict[str, Any]]:
    return db.all(
        "SELECT a.id, a.action, a.details, a.created_at, u.username FROM kanban_activity_log a "
        "LEFT JOIN users u ON u.id = a.user_id WHERE a.board_id = ? ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
        (board_id, limit),
    )
