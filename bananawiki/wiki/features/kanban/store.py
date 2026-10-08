"""Row access for boards, columns and tickets (no permission checks here).

Tickets keep several assignees in ``kanban_ticket_assignees``. The legacy
``kanban_tickets.assigned_to`` column still holds the first of them so older
data stays readable: a ticket with no assignee rows but an ``assigned_to``
shows that user as its assignee.

Archived tickets (``archived_at`` set) keep their column but are not part of
the board: they are left out of the column order, the board state and every
list except :func:`archived_tickets`.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
from typing import Any

from ....core.timeutil import now_sql
from ... import storage
from ...db import db
from . import fields

FOLDER = "kanban_attachments"
# Ids an ordering may name beyond the current ones (left over from rows deleted meanwhile).
REORDER_SLACK = 100

TICKET_SELECT = (
    "SELECT t.*, c.board_id, lu.username AS legacy_assignee, cu.username AS created_by_username, "
    "au.username AS archived_by_username, "
    "(SELECT COUNT(*) FROM kanban_ticket_attachments a WHERE a.ticket_id = t.id) AS attachment_count, "
    "(SELECT COUNT(*) FROM kanban_ticket_comments m WHERE m.ticket_id = t.id) AS comment_count, "
    "(SELECT COUNT(*) FROM kanban_ticket_checklist k WHERE k.ticket_id = t.id) AS checklist_total, "
    "(SELECT COUNT(*) FROM kanban_ticket_checklist k WHERE k.ticket_id = t.id AND k.done = 1) AS checklist_done "
    "FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
    "LEFT JOIN users lu ON lu.id = t.assigned_to LEFT JOIN users cu ON cu.id = t.created_by "
    "LEFT JOIN users au ON au.id = t.archived_by"
)
ACTIVE = "t.archived_at IS NULL"


def get_board(board_id: Any) -> dict[str, Any] | None:
    try:
        board_id = int(board_id)
    except (TypeError, ValueError):
        return None
    return db.one(
        "SELECT b.*, COALESCE(u.username, '') AS creator_username FROM kanban_boards b "
        "LEFT JOIN users u ON u.id = b.created_by WHERE b.id = ?",
        (board_id,),
    )


def get_column(column_id: Any) -> dict[str, Any] | None:
    try:
        column_id = int(column_id)
    except (TypeError, ValueError):
        return None
    return db.one("SELECT * FROM kanban_columns WHERE id = ?", (column_id,))


def get_ticket(ticket_id: Any) -> dict[str, Any] | None:
    """The ticket with ``board_id`` (through its column) and display fields."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return None
    return db.one(f"{TICKET_SELECT} WHERE t.id = ?", (ticket_id,))


def columns_of(board_id: int) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM kanban_columns WHERE board_id = ? ORDER BY sort_order, id", (board_id,))


def column_ids(board_id: int) -> list[int]:
    return db.column("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY sort_order, id", (board_id,))


def ticket_ids(column_id: int) -> list[int]:
    """The column's active tickets in order (archived ones are not part of it)."""
    return db.column("SELECT id FROM kanban_tickets WHERE column_id = ? AND archived_at IS NULL "
                     "ORDER BY sort_order, id", (column_id,))


def reorder(current: list[int], wanted: Iterable[Any], *, bounded: bool = True) -> list[int]:
    """*current* rearranged to follow *wanted*; unknown ids are ignored, missing ones keep their order.

    A *bounded* (sent by a client) *wanted* is read up to ``len(current) +
    REORDER_SLACK`` entries: a longer list is no ordering of *current*.
    """
    known = set(current)
    ordered: dict[int, None] = {}
    for raw in islice(wanted, len(current) + REORDER_SLACK if bounded else None):
        try:
            item = int(raw)
        except (TypeError, ValueError):
            continue
        if item in known:
            ordered.setdefault(item)
    return [*ordered, *(item for item in current if item not in ordered)]


def write_column_order(board_id: int, ordered: list[int]) -> None:
    db.executemany(
        "UPDATE kanban_columns SET sort_order = ? WHERE id = ? AND board_id = ?",
        [(index, column_id, board_id) for index, column_id in enumerate(ordered)],
    )


def write_ticket_order(column_id: int, ordered: list[int]) -> None:
    db.executemany(
        "UPDATE kanban_tickets SET column_id = ?, sort_order = ? WHERE id = ?",
        [(column_id, index, ticket_id) for index, ticket_id in enumerate(ordered)],
    )


# ── Assignees ─────────────────────────────────────────────────────────────────


def set_assignees(ticket_id: int, user_ids: Iterable[str]) -> list[str]:
    """Replace the assignee set; ``assigned_to`` follows the first assignee."""
    cleaned: list[str] = []
    for user_id in user_ids:
        if user_id and user_id not in cleaned:
            cleaned.append(user_id)
    now = now_sql()
    db.execute("DELETE FROM kanban_ticket_assignees WHERE ticket_id = ?", (ticket_id,))
    db.executemany(
        "INSERT OR IGNORE INTO kanban_ticket_assignees (ticket_id, user_id, assigned_at) VALUES (?, ?, ?)",
        [(ticket_id, user_id, now) for user_id in cleaned],
    )
    db.execute("UPDATE kanban_tickets SET assigned_to = ? WHERE id = ?", (cleaned[0] if cleaned else None, ticket_id))
    return cleaned


def assignees_by_ticket(where: str, params: tuple[Any, ...]) -> dict[int, list[dict[str, str]]]:
    rows = db.all(
        "SELECT a.ticket_id, u.id, u.username FROM kanban_ticket_assignees a JOIN users u ON u.id = a.user_id "
        f"JOIN kanban_tickets t ON t.id = a.ticket_id JOIN kanban_columns c ON c.id = t.column_id WHERE {where} "
        "ORDER BY a.assigned_at, u.username COLLATE NOCASE",
        params,
    )
    result: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        result.setdefault(row["ticket_id"], []).append({"id": row["id"], "username": row["username"]})
    return result


def card(row: dict[str, Any], assignees: list[dict[str, str]] | None) -> dict[str, Any]:
    """What a board shows for a ticket (no description)."""
    if not assignees and row.get("assigned_to") and row.get("legacy_assignee"):
        assignees = [{"id": row["assigned_to"], "username": row["legacy_assignee"]}]
    return {
        "id": row["id"],
        "column_id": row["column_id"],
        "title": row["title"],
        "priority": row["priority"] if row["priority"] in fields.PRIORITIES else "medium",
        "due_date": (row.get("due_date") or "")[:10] or None,
        "color": row.get("color") or "",
        "labels": fields.stored_labels(row.get("labels")),
        "assignees": assignees or [],
        "attachment_count": row.get("attachment_count") or 0,
        "comment_count": row.get("comment_count") or 0,
        "checklist_total": row.get("checklist_total") or 0,
        "checklist_done": row.get("checklist_done") or 0,
        "has_description": bool((row.get("description") or "").strip()),
    }


def ticket_card(ticket_id: int) -> dict[str, Any] | None:
    row = get_ticket(ticket_id)
    if row is None:
        return None
    return card(row, assignees_by_ticket("t.id = ?", (ticket_id,)).get(ticket_id))


def board_state(board: dict[str, Any]) -> dict[str, Any]:
    """The whole board for the client: columns in order and every active ticket's card."""
    columns = columns_of(board["id"])
    rows = db.all(f"{TICKET_SELECT} WHERE c.board_id = ? AND {ACTIVE} "
                  "ORDER BY c.sort_order, c.id, t.sort_order, t.id", (board["id"],))
    assignees = assignees_by_ticket("c.board_id = ?", (board["id"],))
    tickets: dict[int, list[int]] = {column["id"]: [] for column in columns}
    cards = {}
    for row in rows:
        tickets[row["column_id"]].append(row["id"])
        cards[str(row["id"])] = card(row, assignees.get(row["id"]))
    return {
        "board": {"id": board["id"], "title": board["title"], "description": board["description"] or "",
                  "visibility": board.get("visibility") or "public", "archived_at": board.get("archived_at")},
        "columns": [{"id": c["id"], "title": c["title"], "wip_limit": c.get("wip_limit"), "tickets": tickets[c["id"]]}
                    for c in columns],
        "tickets": cards,
        "archived_count": archived_count(board["id"]),
    }


# ── Archive ───────────────────────────────────────────────────────────────────


def archived_count(board_id: int) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
        "WHERE c.board_id = ? AND t.archived_at IS NOT NULL", (board_id,), default=0,
    ))


def archived_tickets(board_id: int, limit: int) -> list[dict[str, Any]]:
    """Archived tickets of a board, most recently archived first: card, column and who archived it when."""
    rows = db.all(
        f"{TICKET_SELECT} WHERE c.board_id = ? AND t.archived_at IS NOT NULL "
        "ORDER BY t.archived_at DESC, t.id DESC LIMIT ?",
        (board_id, max(1, int(limit))),
    )
    if not rows:
        return []
    ids = tuple(row["id"] for row in rows)
    people = assignees_by_ticket(f"t.id IN ({','.join('?' * len(ids))})", ids)
    titles = {column["id"]: column["title"] for column in columns_of(board_id)}
    return [
        {**card(row, people.get(row["id"])), "column_title": titles.get(row["column_id"], ""),
         "archived_at": row["archived_at"], "archived_by_username": row.get("archived_by_username") or ""}
        for row in rows
    ]


# ── Checklists ────────────────────────────────────────────────────────────────


def checklist(ticket_id: int) -> list[dict[str, Any]]:
    """A ticket's checklist items in order."""
    return [
        {"id": row["id"], "text": row["text"], "done": bool(row["done"])}
        for row in db.all("SELECT id, text, done FROM kanban_ticket_checklist WHERE ticket_id = ? "
                          "ORDER BY sort_order, id", (ticket_id,))
    ]


def get_checklist_item(item_id: Any) -> dict[str, Any] | None:
    """The item with its ``board_id`` (through its ticket and column)."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    return db.one(
        "SELECT k.*, c.board_id FROM kanban_ticket_checklist k JOIN kanban_tickets t ON t.id = k.ticket_id "
        "JOIN kanban_columns c ON c.id = t.column_id WHERE k.id = ?",
        (item_id,),
    )


def checklist_ids(ticket_id: int) -> list[int]:
    return db.column("SELECT id FROM kanban_ticket_checklist WHERE ticket_id = ? ORDER BY sort_order, id",
                     (ticket_id,))


def write_checklist_order(ticket_id: int, ordered: list[int]) -> None:
    db.executemany(
        "UPDATE kanban_ticket_checklist SET sort_order = ? WHERE id = ? AND ticket_id = ?",
        [(index, item_id, ticket_id) for index, item_id in enumerate(ordered)],
    )


def replace_checklist(ticket_id: int, items: Iterable[dict[str, Any]]) -> None:
    """Replace a ticket's checklist (revert and import); *items* are already validated."""
    now = now_sql()
    db.execute("DELETE FROM kanban_ticket_checklist WHERE ticket_id = ?", (ticket_id,))
    db.executemany(
        "INSERT INTO kanban_ticket_checklist (ticket_id, text, done, sort_order, created_at) VALUES (?, ?, ?, ?, ?)",
        [(ticket_id, item["text"], 1 if item["done"] else 0, index, now) for index, item in enumerate(items)],
    )


# ── Attachment files ──────────────────────────────────────────────────────────


def attachment_refs(where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Stored files and legacy blob ids of the attachments matching *where*."""
    return db.all(
        "SELECT a.id, a.filename, a.blob_id FROM kanban_ticket_attachments a "
        "JOIN kanban_tickets t ON t.id = a.ticket_id JOIN kanban_columns c ON c.id = t.column_id "
        f"WHERE {where}",
        params,
    )


def delete_blobs(refs: list[dict[str, Any]]) -> None:
    """Remove the 1.4 ``file_blobs`` copies; call inside the deleting transaction."""
    blob_ids = [ref["blob_id"] for ref in refs if ref.get("blob_id")]
    db.executemany("DELETE FROM file_blobs WHERE id = ?", [(blob_id,) for blob_id in blob_ids])


def delete_files(refs: list[dict[str, Any]]) -> None:
    """Remove files from disk; call after the transaction committed."""
    for ref in refs:
        storage.delete(FOLDER, ref["filename"])
