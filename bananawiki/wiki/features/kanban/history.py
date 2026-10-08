"""Board history: structural snapshots and a revert that never loses ticket data.

Every change stores a snapshot of the board's structure (title, description,
columns with their WIP limit, tickets with their fields, assignees,
checklist and ``"archived": true`` for archived tickets, which follow the
active ones of their column) in
``kanban_board_history``. 1.6 snapshots carry column and ticket ids; 1.4
snapshots only titles.

Reverting (unlike 1.4, which deleted every column and rebuilt the board, so
comments, attachments and description history were lost):

* columns and tickets are matched to the snapshot by id, or by title for 1.4
  snapshots, and get the snapshot's titles, fields, assignees and positions
  (and, for snapshots that hold them, column WIP limits and checklists); a
  ticket is archived when the snapshot marks it so and active otherwise;
* columns and tickets deleted since the snapshot are recreated;
* nothing is deleted except columns created after the snapshot that end up
  empty (archived tickets count). Tickets created after the snapshot stay on
  the board, after the restored ones, together with their comments and
  attachments; archived ones stay archived.

Consecutive moves of a ticket (whichever column they lead to), reorderings
and checklist changes of a ticket by the same person within a short window
share one entry, and identical snapshots are skipped. A board keeps its newest
``HISTORY_KEPT`` entries, and of those only as many as fit in
``HISTORY_MAX_BYTES``: a large board keeps fewer versions, never fewer than
the newest. A ticket's description history (``kanban_ticket_history``) is
bounded the same way by ``TICKET_HISTORY_KEPT`` and ``TICKET_HISTORY_MAX_BYTES``.
"""

from __future__ import annotations

import json
from typing import Any

from ....core.timeutil import now_sql, parse, utcnow
from ...db import db
from . import fields, store

HISTORY_KEPT = 200
HISTORY_MAX_BYTES = 64 * 1024 * 1024
TICKET_HISTORY_KEPT = 100
TICKET_HISTORY_MAX_BYTES = 1024 * 1024
COALESCE_SECONDS = 120
SNAPSHOT_VERSION = 2


def snapshot(board_id: int) -> dict[str, Any] | None:
    board = db.one("SELECT id, title, description FROM kanban_boards WHERE id = ?", (board_id,))
    if board is None:
        return None
    columns = store.columns_of(board_id)
    tickets = db.all(
        "SELECT t.id, t.column_id, t.title, t.description, t.priority, t.due_date, t.color, t.labels, t.archived_at "
        "FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id WHERE c.board_id = ? "
        "ORDER BY t.archived_at IS NOT NULL, t.sort_order, t.id",
        (board_id,),
    )
    assignees: dict[int, list[tuple[str, str]]] = {}
    for row in db.all(
        "SELECT a.ticket_id, u.id, u.username FROM kanban_ticket_assignees a JOIN users u ON u.id = a.user_id "
        "JOIN kanban_tickets t ON t.id = a.ticket_id JOIN kanban_columns c ON c.id = t.column_id "
        "WHERE c.board_id = ? ORDER BY a.assigned_at, u.username",
        (board_id,),
    ):
        assignees.setdefault(row["ticket_id"], []).append((row["id"], row["username"]))
    checklists: dict[int, list[dict[str, Any]]] = {}
    for row in db.all(
        "SELECT k.ticket_id, k.text, k.done FROM kanban_ticket_checklist k JOIN kanban_tickets t ON t.id = k.ticket_id "
        "JOIN kanban_columns c ON c.id = t.column_id WHERE c.board_id = ? ORDER BY k.sort_order, k.id",
        (board_id,),
    ):
        checklists.setdefault(row["ticket_id"], []).append({"text": row["text"], "done": bool(row["done"])})
    by_column: dict[int, list[dict[str, Any]]] = {column["id"]: [] for column in columns}
    for ticket in tickets:
        people = assignees.get(ticket["id"], [])
        item: dict[str, Any] = {
            "id": ticket["id"],
            "title": ticket["title"],
            "description": ticket["description"] or "",
            "priority": ticket["priority"],
            "due_date": ticket["due_date"] or None,
            "color": ticket["color"] or "",
            "labels": fields.stored_labels(ticket["labels"]),
            "assignee_ids": [user_id for user_id, _ in people],
            "assignee_usernames": [name for _, name in people],
            "checklist": checklists.get(ticket["id"], []),
        }
        if ticket["archived_at"]:
            item["archived"] = True
        by_column[ticket["column_id"]].append(item)
    return {
        "version": SNAPSHOT_VERSION,
        "title": board["title"],
        "description": board["description"] or "",
        "columns": [
            {"id": column["id"], "title": column["title"], "wip_limit": column.get("wip_limit"),
             "tickets": by_column[column["id"]]}
            for column in columns
        ],
    }


def record(board_id: int, user_id: str | None, message: str, *, is_revert: bool = False,
           coalesce: bool = False) -> int | None:
    """Store the board's current state; call inside the transaction that changed it."""
    state = snapshot(board_id)
    if state is None:
        return None
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
    latest = db.one(
        "SELECT id, snapshot, edited_by, edit_message, is_revert, created_at FROM kanban_board_history "
        "WHERE board_id = ? ORDER BY id DESC LIMIT 1",
        (board_id,),
    )
    if latest and latest["snapshot"] == encoded and not is_revert:
        return None
    values = {"title": state["title"], "description": state["description"], "snapshot": encoded,
              "edited_by": user_id, "edit_message": message[:500], "is_revert": 1 if is_revert else 0,
              "created_at": now_sql()}
    if coalesce and latest and _can_merge(latest, user_id, message):
        db.update("kanban_board_history", values, "id = ?", (latest["id"],))
        entry_id = int(latest["id"])
    else:
        entry_id = db.insert("kanban_board_history", {"board_id": board_id, **values})
    _prune("kanban_board_history", "board_id", board_id, "length(CAST(snapshot AS BLOB))",
           HISTORY_KEPT, HISTORY_MAX_BYTES)
    return entry_id


def _prune(table: str, owner: str, owner_id: int, size: str, kept: int, max_bytes: int) -> None:
    """Keep the newest *kept* rows of *owner_id*, and of those the newest that fit in *max_bytes*
    (the newest row always stays, whatever its size)."""
    db.execute(
        f"DELETE FROM {table} WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER newest AS position, "
        f"SUM({size}) OVER newest AS total FROM {table} WHERE {owner} = ? WINDOW newest AS (ORDER BY id DESC)) "
        "WHERE position > 1 AND (position > ? OR total > ?))",
        (owner_id, kept, max_bytes),
    )


def record_description(ticket_id: int, old: str, new: str, user_id: str | None) -> None:
    """Add a ticket's description change to its history; call inside the transaction that made it."""
    db.insert("kanban_ticket_history", {"ticket_id": ticket_id, "old_description": old, "new_description": new,
                                        "changed_by": user_id, "created_at": now_sql()})
    _prune("kanban_ticket_history", "ticket_id", ticket_id,
           "length(CAST(old_description AS BLOB)) + length(CAST(new_description AS BLOB))",
           TICKET_HISTORY_KEPT, TICKET_HISTORY_MAX_BYTES)


def _can_merge(latest: dict[str, Any], user_id: str | None, message: str) -> bool:
    moment = parse(latest["created_at"])
    return (
        not latest["is_revert"]
        and latest["edited_by"] == user_id
        and latest["edit_message"] == message
        and moment is not None
        and (utcnow() - moment).total_seconds() <= COALESCE_SECONDS
    )


def entries(board_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT h.id, h.edit_message, h.is_revert, h.created_at, u.username FROM kanban_board_history h "
        "LEFT JOIN users u ON u.id = h.edited_by WHERE h.board_id = ? ORDER BY h.id DESC",
        (board_id,),
    )


def entry(board_id: int, entry_id: int) -> dict[str, Any] | None:
    row = db.one(
        "SELECT h.*, u.username FROM kanban_board_history h LEFT JOIN users u ON u.id = h.edited_by "
        "WHERE h.id = ? AND h.board_id = ?",
        (entry_id, board_id),
    )
    if row is not None:
        row["state"] = decode(row["snapshot"])
    return row


def decode(raw: str | None) -> dict[str, Any]:
    """A snapshot as a dict with a ``columns`` list, whatever was stored."""
    try:
        state = json.loads(raw or "{}")
    except (TypeError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    columns = state.get("columns") if isinstance(state.get("columns"), list) else []
    state["columns"] = [
        {**column, "tickets": [t for t in column.get("tickets") or [] if isinstance(t, dict)]}
        for column in columns if isinstance(column, dict)
    ]
    return state


def previous_state(board_id: int, entry_id: int) -> dict[str, Any] | None:
    """The snapshot recorded just before *entry_id* (None for the oldest entry)."""
    raw = db.scalar("SELECT snapshot FROM kanban_board_history WHERE board_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                    (board_id, entry_id))
    return None if raw is None else decode(raw)


# ── Differences between two snapshots ─────────────────────────────────────────

Change = tuple[str, dict[str, Any]]  # translation key and its values
_TICKET_FIELDS = ("title", "description", "priority", "due_date", "color", "labels", "assignee_usernames")


def _key(item: dict[str, Any]) -> Any:
    return ("id", item["id"]) if isinstance(item.get("id"), int) else ("title", str(item.get("title") or ""))


def _checklist_counts(item: dict[str, Any]) -> tuple[int, int]:
    items = [entry for entry in item.get("checklist") or [] if isinstance(entry, dict)]
    return sum(1 for entry in items if entry.get("done")), len(items)


def changes(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """What changed from snapshot *old* to snapshot *new*, as translation keys and values.

    Columns and tickets are matched by id (by title in 1.4 snapshots). Covers
    the board's title and description, columns (added, removed, renamed, WIP
    limit), tickets (added, removed, moved, archived, restored, changed
    fields) and checklist progress.
    """
    result: list[Change] = []
    if old.get("title") != new.get("title"):
        result.append(("kanban.diff.board_title", {"old": old.get("title") or "", "new": new.get("title") or ""}))
    if (old.get("description") or "") != (new.get("description") or ""):
        result.append(("kanban.diff.board_description", {}))
    old_columns = {_key(column): column for column in old["columns"]}
    new_columns = {_key(column): column for column in new["columns"]}
    for key, column in new_columns.items():
        before = old_columns.get(key)
        title = column.get("title") or ""
        if before is None:
            result.append(("kanban.diff.column_added", {"title": title}))
            continue
        if (before.get("title") or "") != title:
            result.append(("kanban.diff.column_renamed", {"old": before.get("title") or "", "new": title}))
        if before.get("wip_limit") != column.get("wip_limit"):
            limit = column.get("wip_limit")
            result.append(("kanban.diff.column_limit" if limit else "kanban.diff.column_limit_removed",
                           {"title": title, "limit": limit or ""}))
    for key, column in old_columns.items():
        if key not in new_columns:
            result.append(("kanban.diff.column_removed", {"title": column.get("title") or ""}))

    def tickets(state: dict[str, Any]) -> dict[Any, tuple[dict[str, Any], dict[str, Any]]]:
        return {_key(ticket): (ticket, column) for column in state["columns"] for ticket in column["tickets"]}

    old_tickets, new_tickets = tickets(old), tickets(new)
    for key, (ticket, column) in new_tickets.items():
        title = ticket.get("title") or ""
        found = old_tickets.get(key)
        if found is None:
            result.append(("kanban.diff.ticket_added", {"title": title, "column": column.get("title") or ""}))
            continue
        before, before_column = found
        if _key(before_column) != _key(column):
            result.append(("kanban.diff.ticket_moved", {"title": title, "column": column.get("title") or ""}))
        if bool(before.get("archived")) != bool(ticket.get("archived")):
            result.append(("kanban.diff.ticket_archived" if ticket.get("archived") else "kanban.diff.ticket_restored",
                           {"title": title}))
        changed = [name for name in _TICKET_FIELDS if name in before and before.get(name) != ticket.get(name)]
        if changed:
            result.append(("kanban.diff.ticket_changed", {"title": title, "fields": changed}))
        if "checklist" in ticket and before.get("checklist", []) != ticket["checklist"]:
            done, total = _checklist_counts(ticket)
            result.append(("kanban.diff.checklist", {"title": title, "done": done, "total": total}))
    for key, (ticket, _column) in old_tickets.items():
        if key not in new_tickets:
            result.append(("kanban.diff.ticket_removed", {"title": ticket.get("title") or ""}))
    return result


def delete_entry(board_id: int, entry_id: int) -> bool:
    return db.execute("DELETE FROM kanban_board_history WHERE id = ? AND board_id = ?",
                      (entry_id, board_id)).rowcount > 0


def clear(board_id: int) -> int:
    return db.execute("DELETE FROM kanban_board_history WHERE board_id = ?", (board_id,)).rowcount


# ── Revert ────────────────────────────────────────────────────────────────────


class _Matcher:
    """Pairs snapshot items with existing rows: by id first, then by title (1.4 snapshots)."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = {row["id"]: row for row in rows}
        self.used: set[int] = set()

    def take(self, item: dict[str, Any]) -> dict[str, Any] | None:
        wanted = item.get("id")
        if isinstance(wanted, int) and wanted in self.rows and wanted not in self.used:
            self.used.add(wanted)
            return self.rows[wanted]
        if "id" in item:
            return None
        title = str(item.get("title") or "")
        for row_id, row in self.rows.items():
            if row_id not in self.used and row["title"] == title:
                self.used.add(row_id)
                return row
        return None

    def leftovers(self) -> list[dict[str, Any]]:
        return [row for row_id, row in self.rows.items() if row_id not in self.used]


def _clean_title(value: Any, maximum: int, fallback: str) -> str:
    text = " ".join(str(value or "").split())[:maximum]
    return text or fallback


def _snapshot_assignees(item: dict[str, Any]) -> list[str]:
    ids = [str(x) for x in item.get("assignee_ids") or [] if x]
    names = [str(x) for x in item.get("assignee_usernames") or [] if x]
    found: list[str] = []
    if ids:
        marks = ",".join("?" * len(ids))
        existing = set(db.column(f"SELECT id FROM users WHERE id IN ({marks})", ids))
        found = [user_id for user_id in ids if user_id in existing]
    elif names:
        marks = ",".join("?" * len(names))
        by_name = {row["username"]: row["id"] for row in
                   db.all(f"SELECT id, username FROM users WHERE username IN ({marks})", names)}
        found = [by_name[name] for name in names if name in by_name]
    return found


def _restore_ticket(item: dict[str, Any], existing: dict[str, Any] | None, column_id: int,
                    user_id: str) -> tuple[int, bool]:
    """Write the snapshot's ticket; returns its id and whether it is archived."""
    title = _clean_title(item.get("title"), fields.MAX_TICKET_TITLE, "—")
    description = str(item.get("description") or "")[:fields.MAX_DESCRIPTION]
    priority = item.get("priority") if item.get("priority") in fields.PRIORITIES else "medium"
    try:
        due = fields.due_date(item.get("due_date"))
    except fields.KanbanError:
        due = None
    try:
        color = fields.color(item.get("color"))
    except fields.KanbanError:
        color = ""
    values: dict[str, Any] = {"title": title, "description": description, "priority": priority, "due_date": due,
                              "color": color, "labels": fields.encode_labels(fields.labels(item.get("labels") or []))}
    archived = item.get("archived") is True
    if not archived:
        values.update(archived_at=None, archived_by=None)
    elif existing is None or not existing.get("archived_at"):
        values.update(archived_at=now_sql(), archived_by=user_id)
    if existing is None:
        ticket_id = db.insert("kanban_tickets", {**values, "column_id": column_id, "created_by": user_id,
                                                 "sort_order": 0, "created_at": now_sql()})
    else:
        ticket_id = existing["id"]
        if (existing["description"] or "") != description:
            record_description(ticket_id, existing["description"] or "", description, user_id)
        db.update("kanban_tickets", {**values, "column_id": column_id}, "id = ?", (ticket_id,))
    store.set_assignees(ticket_id, _snapshot_assignees(item))
    if "checklist" in item:
        store.replace_checklist(ticket_id, fields.stored_checklist(item["checklist"]))
    return ticket_id, archived


def revert(board: dict[str, Any], state: dict[str, Any], user_id: str) -> dict[str, int]:
    """Bring *board* back to *state*; call inside a transaction. Returns what was kept or recreated."""
    board_id = board["id"]
    title = _clean_title(state.get("title"), fields.MAX_BOARD_TITLE, board["title"])
    db.update("kanban_boards", {"title": title,
                                "description": str(state.get("description") or "")[:fields.MAX_DESCRIPTION]},
              "id = ?", (board_id,))
    columns = _Matcher(store.columns_of(board_id))
    tickets = _Matcher(db.all(
        "SELECT t.* FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id WHERE c.board_id = ? "
        "ORDER BY t.sort_order, t.id",
        (board_id,),
    ))
    order: list[int] = []
    placed: dict[int, list[int]] = {}
    shelved: set[int] = set()  # columns holding archived tickets (never removed)
    recreated = 0
    for item in state.get("columns") or []:
        column = columns.take(item)
        column_values: dict[str, Any] = {"title": _clean_title(item.get("title"), fields.MAX_COLUMN_TITLE, "—")}
        if "wip_limit" in item:
            column_values["wip_limit"] = fields.stored_wip_limit(item["wip_limit"])
        if column is None:
            column_id = db.insert("kanban_columns", {**column_values, "board_id": board_id, "sort_order": 0,
                                                     "created_at": now_sql()})
            recreated += 1
        else:
            column_id = column["id"]
            db.update("kanban_columns", column_values, "id = ?", (column_id,))
        order.append(column_id)
        placed[column_id] = []
        for ticket_item in item.get("tickets") or []:
            existing = tickets.take(ticket_item)
            if existing is None:
                recreated += 1
            ticket_id, archived = _restore_ticket(ticket_item, existing, column_id, user_id)
            if archived:
                shelved.add(column_id)
            else:
                placed[column_id].append(ticket_id)
    kept = tickets.leftovers()
    for ticket in kept:
        if ticket.get("archived_at"):
            shelved.add(ticket["column_id"])
        else:
            placed.setdefault(ticket["column_id"], []).append(ticket["id"])
    removed_columns = 0
    for column in columns.leftovers():
        if placed.get(column["id"]) or column["id"] in shelved:
            order.append(column["id"])
        else:
            db.execute("DELETE FROM kanban_columns WHERE id = ?", (column["id"],))
            removed_columns += 1
    store.write_column_order(board_id, order)
    for column_id, ids in placed.items():
        store.write_ticket_order(column_id, ids)
    return {"kept_tickets": len(kept), "recreated": recreated, "removed_columns": removed_columns}
