"""Board history: structural snapshots and a revert that never loses ticket data.

Every change stores a snapshot of the board's structure (title, description,
columns with their WIP limit, tickets with their fields, assignees,
checklist and ``"archived": true`` for archived tickets, which follow the
active ones of their column) in ``kanban_board_history``. Snapshots carry
column and ticket ids; those written by 1.4 carry only titles.

Reverting keeps comments, attachments and description history:

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
share one entry, and identical snapshots are skipped. A board whose content
is over ``LARGE_BOARD_BYTES`` (see :func:`board_bytes`) gets a new entry at
most every ``HISTORY_MIN_SECONDS``, and a smaller one at most every
``HISTORY_MIN_SECONDS`` scaled by its share of ``LARGE_BOARD_BYTES`` (none for
a board small enough that this is under ``MIN_THROTTLE_SECONDS``), so one
board's history grows by about ``LARGE_BOARD_BYTES`` a minute at most,
whatever its size: changes in between are part of the next entry (and a
revert first records the board as it is). A board keeps its
newest ``HISTORY_KEPT`` entries, and of those only as many as fit in
``HISTORY_MAX_BYTES``: a large board keeps fewer versions, never fewer than
the newest. A ticket's description history (``kanban_ticket_history``) is
bounded the same way by ``TICKET_HISTORY_KEPT`` and ``TICKET_HISTORY_MAX_BYTES``.

Boards are cheap to multiply, so each account's history writes are bounded
too: what an entry adds to its board's history (after the pruning above, so
a board whose history is full costs about nothing) is counted, in bytes,
against its author's ``STORED_MIB_PER_DAY`` in 24 hours, kept in
``rate_limit_hits`` (``STORED_BUCKET``, shared with imports, see
:mod:`.transfer`) so that every worker shares it and deleting the boards
gives nothing back. Past the budget the change is still made and recorded,
but the board's history may no longer grow: its oldest entries make room for
the new one. Entries the server writes for an account deletion or merge are
not counted against anyone.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ....core.ratelimit import SqlLimiter
from ....core.timeutil import now_sql, parse, sql_in, utcnow
from ...db import db
from ...markdown import plain_html, render, shared_highlighting
from . import fields, store

HISTORY_KEPT = 200
HISTORY_MAX_BYTES = 64 * 1024 * 1024
TICKET_HISTORY_KEPT = 100
TICKET_HISTORY_MAX_BYTES = 1024 * 1024
COALESCE_SECONDS = 120
SNAPSHOT_VERSION = 2
LARGE_BOARD_BYTES = 1024 * 1024
HISTORY_MIN_SECONDS = 60
# History and import bytes one account may add in 24 hours: the throttles bound one board, and an account can
# fill many.
STORED_MIB_PER_DAY = 128
STORED_BUCKET = "kanban:stored"
# Smaller boards wait less, in proportion to their size; under this many seconds, not at all.
MIN_THROTTLE_SECONDS = 2
# octet_length (SQLite 3.43) measures a value without reading it.
_BYTES = "octet_length({})" if sqlite3.sqlite_version_info >= (3, 43) else "length(CAST({} AS BLOB))"


def board_bytes(board_id: int) -> int:
    """About the bytes the board's tickets take in a snapshot (text, labels, checklists and assignees),
    without reading them."""
    tickets = ("FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id WHERE c.board_id = ?")
    text = " + ".join(f"COALESCE({_BYTES.format(column)}, 0)" for column in ("t.title", "t.description", "t.labels"))
    # An assignee is in a snapshot twice, by id and by username (each quoted, with a comma).
    person = f"{_BYTES.format('u.id')} + {_BYTES.format('u.username')} + 6"
    return int(db.scalar(
        f"SELECT (SELECT COALESCE(SUM({text} + 100), 0) {tickets}) + (SELECT COALESCE(SUM("
        f"COALESCE({_BYTES.format('k.text')}, 0) + 30), 0) FROM kanban_ticket_checklist k "
        f"JOIN kanban_tickets t ON t.id = k.ticket_id JOIN kanban_columns c ON c.id = t.column_id "
        f"WHERE c.board_id = ?) + (SELECT COALESCE(SUM({person}), 0) FROM kanban_ticket_assignees a "
        f"JOIN users u ON u.id = a.user_id JOIN kanban_tickets t ON t.id = a.ticket_id "
        "JOIN kanban_columns c ON c.id = t.column_id WHERE c.board_id = ?)",
        (board_id, board_id, board_id), default=0) or 0)


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
        # "done" only when set, so snapshots of boards without done columns stay as they were.
        "columns": [
            {"id": column["id"], "title": column["title"], "wip_limit": column.get("wip_limit"),
             **({"done": True} if column.get("is_done") else {}), "tickets": by_column[column["id"]]}
            for column in columns
        ],
    }


def record(board_id: int, user_id: str | None, message: str, *, is_revert: bool = False,
           coalesce: bool = False, force: bool = False, charged: bool = True) -> int | None:
    """Store the board's current state; call inside the transaction that changed it.

    A board is not snapshotted again within :func:`snapshot_interval` seconds
    of its latest entry, unless *force* (or *is_revert*). What the entry adds
    to the board's history counts against *user_id*'s daily budget unless
    *charged* is false (entries the server writes, such as an account's
    hand-over); past the budget the oldest entries make room for it instead.
    """
    latest = db.one(
        "SELECT id, edited_by, edit_message, is_revert, created_at FROM kanban_board_history "
        "WHERE board_id = ? ORDER BY id DESC LIMIT 1",
        (board_id,),
    )
    if (latest and not (force or is_revert) and latest["created_at"] >= sql_in(seconds=-HISTORY_MIN_SECONDS)):
        wait = snapshot_interval(board_bytes(board_id))
        if wait and latest["created_at"] >= sql_in(seconds=-wait):
            return None
    state = snapshot(board_id)
    if state is None:
        return None
    # As UTF-8, so the stored snapshot is about the size board_bytes measures (escaped as ASCII, a letter
    # outside it took up to six bytes and an emoji twelve).
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if latest and not is_revert and db.scalar("SELECT snapshot FROM kanban_board_history WHERE id = ?",
                                              (latest["id"],)) == encoded:
        return None
    values = {"title": state["title"], "description": state["description"], "snapshot": encoded,
              "edited_by": user_id, "edit_message": message[:500], "is_revert": 1 if is_revert else 0,
              "created_at": now_sql()}
    before = _history_bytes(board_id)
    if coalesce and latest and _can_merge(latest, user_id, message):
        db.update("kanban_board_history", values, "id = ?", (latest["id"],))
        entry_id = int(latest["id"])
    else:
        entry_id = db.insert("kanban_board_history", {"board_id": board_id, **values})
    _prune("kanban_board_history", "board_id", board_id, _BYTES.format("snapshot"), HISTORY_KEPT, HISTORY_MAX_BYTES)
    growth = _history_bytes(board_id) - before
    if charged and growth > 0 and not _reserve(user_id, growth):
        # Out of budget: the change stays recorded, but the board's history does not grow (beyond the
        # newest entry itself, which is about the board's own size).
        _prune("kanban_board_history", "board_id", board_id, _BYTES.format("snapshot"), HISTORY_KEPT, before)
    return entry_id


def _history_bytes(board_id: int) -> int:
    return int(db.scalar(f"SELECT COALESCE(SUM({_BYTES.format('snapshot')}), 0) FROM kanban_board_history "
                         "WHERE board_id = ?", (board_id,), default=0) or 0)


def _reserve(user_id: str | None, size: int) -> bool:
    """Count *size* bytes stored by *user_id* against ``STORED_MIB_PER_DAY``; False past it."""
    return not user_id or SqlLimiter(db.session).reserve_amount(
        f"user:{user_id}", STORED_BUCKET, size, STORED_MIB_PER_DAY * 1024 * 1024, 86400)


def charge(user_id: str | None, size: int) -> None:
    """Count *size* bytes imported by *user_id* against ``STORED_MIB_PER_DAY``; raise a 429
    :class:`KanbanError` past it (call inside the import's transaction)."""
    if size > 0 and not _reserve(user_id, size):
        raise fields.KanbanError("kanban.error.daily_storage_limit", 429, maximum=STORED_MIB_PER_DAY)


def snapshot_interval(size: int) -> float:
    """Seconds a board of *size* bytes (see :func:`board_bytes`) waits between two history entries.

    Proportional to the size, up to ``HISTORY_MIN_SECONDS`` from ``LARGE_BOARD_BYTES`` on, so the
    snapshots of one board add up to about ``LARGE_BOARD_BYTES`` a minute whatever its size; 0 when
    that is under ``MIN_THROTTLE_SECONDS`` (small boards keep an entry per change).
    """
    wait = HISTORY_MIN_SECONDS * min(1.0, size / LARGE_BOARD_BYTES)
    return wait if wait >= MIN_THROTTLE_SECONDS else 0.0


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


# Source characters of ticket descriptions one view of a snapshot renders as Markdown.
MAX_SNAPSHOT_RENDER_CHARS = 200_000
# Rendered HTML of those descriptions one view carries.
MAX_SNAPSHOT_HTML_CHARS = 2 * 1024 * 1024


def describe_tickets(state: dict[str, Any]) -> None:
    """Give each ticket of *state* a ``description_html`` at a bounded cost.

    A snapshot may hold thousands of tickets, and their short descriptions
    are not kept in the render cache, so one view renders at most
    :data:`MAX_SNAPSHOT_RENDER_CHARS` of their text, in board order and
    sharing one highlighting allowance, and only until their HTML reaches
    :data:`MAX_SNAPSHOT_HTML_CHARS`. The descriptions after that are shown as
    typed.
    """
    source_left, html_left = MAX_SNAPSHOT_RENDER_CHARS, MAX_SNAPSHOT_HTML_CHARS
    with shared_highlighting():
        for column in state["columns"]:
            for ticket in column["tickets"]:
                text = ticket.get("description")
                text = text if isinstance(text, str) else ""
                if text and len(text) <= source_left and html_left > 0:
                    html = render(text)
                    source_left -= len(text)
                    html_left -= len(html)
                else:
                    html = plain_html(text) if text else ""
                ticket["description_html"] = html


def previous_state(board_id: int, entry_id: int) -> dict[str, Any] | None:
    """The snapshot recorded just before *entry_id* (None for the oldest entry)."""
    raw = db.scalar("SELECT snapshot FROM kanban_board_history WHERE board_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                    (board_id, entry_id))
    return None if raw is None else decode(raw)


# Differences between two snapshots

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
    limit, done), tickets (added, removed, moved, archived, restored, changed
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
        if bool(before.get("done")) != bool(column.get("done")):
            result.append(("kanban.diff.column_done" if column.get("done") else "kanban.diff.column_not_done",
                           {"title": title}))
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


# Revert


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
    text = " ".join(fields.plain_text(value).split())[:maximum]
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
    description = fields.plain_text(item.get("description"))[:fields.MAX_DESCRIPTION]
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
        # A deleted ticket comes back under its own id when that is free (ids are never reused),
        # so reverting to the same version again finds it instead of adding another copy.
        wanted = item.get("id")
        reuse = (isinstance(wanted, int) and not isinstance(wanted, bool) and wanted > 0
                 and db.scalar("SELECT 1 FROM kanban_tickets WHERE id = ?", (wanted,)) is None)
        ticket_id = db.insert("kanban_tickets", {**values, "column_id": column_id, "created_by": user_id,
                                                 "sort_order": 0, "created_at": now_sql(),
                                                 **({"id": wanted} if reuse else {})})
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
                                "description": fields.plain_text(state.get("description"))[:fields.MAX_DESCRIPTION]},
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
        column_values["is_done"] = fields.stored_done_flag(item.get("done"))  # left out: not done
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
