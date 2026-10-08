"""Board, column and ticket operations.

Routes check permissions; these functions validate input and apply a change
in one transaction that also writes the activity log, the sync event and the
board history snapshot. Attachment files are removed from disk after the
transaction commits (together with any 1.4 ``file_blobs`` copy, which is
deleted inside it). After the commit the registry events of
:mod:`.signals` are emitted (``kanban.board.*``, ``kanban.ticket.*``).

Archiving a ticket takes it off the board without deleting anything: it keeps
its column, fields, comments and files, leaves the column order and comes
back at the end of its column when restored. Archiving a board hides it from
the board list and makes it read-only until it is restored.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import g, has_app_context

from ....core.i18n import Catalog
from ....core.sqlite import Session
from ....core.timeutil import now_sql
from ...db import db
from ...i18n import t
from ...markdown import render
from . import access, events, fields, filters, history, signals, store
from .fields import KanbanError

User = dict[str, Any]
BULK_LIMIT = 500
ARCHIVE_LIST_LIMIT = 500


def _log(board_id: int, user: User | None, *, op: str, payload: dict[str, Any], action: str, message: str,
         coalesce: bool = False, entry: str | None = None) -> None:
    """Activity entry, sync event and history snapshot for a change (inside its transaction).

    *entry* names the history entry when it differs from the activity *message*.
    """
    user_id = user["id"] if user else None
    events.append(board_id, op, payload, user_id)
    events.log_activity(board_id, user_id, action, message)
    history.record(board_id, user_id, entry or message, coalesce=coalesce)


def _column_payload(column: dict[str, Any]) -> dict[str, Any]:
    return {"id": column["id"], "title": column["title"], "wip_limit": column.get("wip_limit"),
            "tickets": store.ticket_ids(column["id"])}


# ── Boards ────────────────────────────────────────────────────────────────────


def create_board(user: User, title: Any, description: Any = "", *, default_columns: bool = True) -> dict[str, Any]:
    clean_title = fields.title(title, fields.MAX_BOARD_TITLE)
    clean_description = fields.description(description)
    with db.transaction():
        board_id = db.insert("kanban_boards", {
            "title": clean_title, "description": clean_description, "created_by": user["id"],
            "created_at": now_sql(), "visibility": "public",
        })
        if default_columns:
            for index, key in enumerate(("kanban.column.todo", "kanban.column.doing", "kanban.column.done")):
                db.insert("kanban_columns", {"board_id": board_id, "title": t(key), "sort_order": index,
                                             "created_at": now_sql()})
        _log(board_id, user, op="board_reset", payload={}, action="board_created",
             message=t("kanban.log.board_created", title=clean_title))
    board = store.get_board(board_id)
    assert board is not None
    signals.board("created", board, user)
    return board


def update_board(board: dict[str, Any], user: User, title: Any, description: Any) -> None:
    values = {"title": fields.title(title, fields.MAX_BOARD_TITLE), "description": fields.description(description)}
    with db.transaction():
        db.update("kanban_boards", values, "id = ?", (board["id"],))
        _log(board["id"], user, op="board_updated", payload=values, action="board_updated",
             message=t("kanban.log.board_updated"))
    signals.board_changed(board["id"], user)


def delete_board(board: dict[str, Any], user: User | None = None) -> None:
    """Delete the board with everything on it (emits only ``kanban.board.deleted``)."""
    refs = store.attachment_refs("c.board_id = ?", (board["id"],))
    row = store.get_board(board["id"]) or board
    with db.transaction():
        store.delete_blobs(refs)
        db.execute("DELETE FROM kanban_boards WHERE id = ?", (board["id"],))
    store.delete_files(refs)
    signals.board("deleted", row, user)


def archive_board(board: dict[str, Any], user: User | None) -> dict[str, Any]:
    """Hide the board from the board list and make it read-only; archiving twice changes nothing."""
    if not board.get("archived_at"):
        user_id = signals.actor_id(user)
        with db.transaction():
            db.update("kanban_boards", {"archived_at": now_sql(), "archived_by": user_id}, "id = ?", (board["id"],))
            events.append(board["id"], "board_reset", {}, user_id)
            events.log_activity(board["id"], user_id, "board_archived", t("kanban.log.board_archived"))
        signals.board_changed(board["id"], user)
    fresh = store.get_board(board["id"])
    assert fresh is not None
    return fresh


def restore_board(board: dict[str, Any], user: User | None) -> dict[str, Any]:
    """Bring an archived board back to the list and make it writable again."""
    if board.get("archived_at"):
        user_id = signals.actor_id(user)
        with db.transaction():
            db.update("kanban_boards", {"archived_at": None, "archived_by": None}, "id = ?", (board["id"],))
            events.append(board["id"], "board_reset", {}, user_id)
            events.log_activity(board["id"], user_id, "board_restored", t("kanban.log.board_restored"))
        signals.board_changed(board["id"], user)
    fresh = store.get_board(board["id"])
    assert fresh is not None
    return fresh


# ── Sharing ───────────────────────────────────────────────────────────────────


def _access_changed(board_id: int, unassigned: list[int], user: User | None) -> None:
    """Events after a sharing change: the board, and every ticket that lost an assignee."""
    signals.tickets_changed("updated", unassigned, board_id, user)
    signals.board_changed(board_id, user)


def set_visibility(board: dict[str, Any], visibility: str, user: User | None = None) -> None:
    if visibility not in access.VISIBILITIES:
        raise KanbanError("kanban.error.invalid_visibility")
    with db.transaction():
        db.update("kanban_boards", {"visibility": visibility}, "id = ?", (board["id"],))
        unassigned = remove_invalid_assignees(store.get_board(board["id"]) or board)
    _access_changed(board["id"], unassigned, user)


def _valid_share(share_type: str, target: str, level: str) -> tuple[str, str, str]:
    if share_type not in access.SHARE_TYPES:
        raise KanbanError("kanban.error.invalid_share")
    if level not in access.ACCESS_LEVELS:
        level = "view"
    target = str(target or "").strip()
    if share_type == "role":
        if target not in access.shareable_roles():
            raise KanbanError("kanban.error.role_not_allowed", role=target)
    elif not db.scalar("SELECT 1 FROM users WHERE id = ?", (target,)):
        raise KanbanError("kanban.error.user_not_found")
    return share_type, target, level


def add_share(board: dict[str, Any], share_type: str, target: str, level: str, user: User | None = None) -> None:
    share_type, target, level = _valid_share(share_type, target, level)
    with db.transaction():
        db.execute(
            "INSERT INTO kanban_board_shares (board_id, share_type, target, access_level, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(board_id, share_type, target) DO UPDATE SET "
            "access_level = excluded.access_level",
            (board["id"], share_type, target, level, now_sql()),
        )
        unassigned = remove_invalid_assignees(board)
    _access_changed(board["id"], unassigned, user)


def remove_share(board: dict[str, Any], share_type: str, target: str, user: User | None = None) -> None:
    with db.transaction():
        db.execute("DELETE FROM kanban_board_shares WHERE board_id = ? AND share_type = ? AND target = ?",
                   (board["id"], share_type, str(target or "")))
        unassigned = remove_invalid_assignees(board)
    _access_changed(board["id"], unassigned, user)


def replace_shares(board: dict[str, Any], visibility: str | None, shares: list[Any] | None,
                   user: User | None = None) -> None:
    """The JSON settings API: set visibility and replace every share (invalid entries are skipped)."""
    with db.transaction():
        if visibility is not None:
            if visibility not in access.VISIBILITIES:
                raise KanbanError("kanban.error.invalid_visibility")
            db.update("kanban_boards", {"visibility": visibility}, "id = ?", (board["id"],))
        if shares is not None:
            db.execute("DELETE FROM kanban_board_shares WHERE board_id = ?", (board["id"],))
            for share in shares:
                if not isinstance(share, dict):
                    continue
                try:
                    share_type, target, level = _valid_share(str(share.get("share_type") or ""),
                                                             str(share.get("target") or ""),
                                                             str(share.get("access_level") or "view"))
                except KanbanError:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO kanban_board_shares (board_id, share_type, target, access_level, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (board["id"], share_type, target, level, now_sql()),
                )
        unassigned = remove_invalid_assignees(store.get_board(board["id"]) or board)
    _access_changed(board["id"], unassigned, user)


def shares(board: dict[str, Any]) -> list[dict[str, Any]]:
    return db.all(
        "SELECT s.share_type, s.target, s.access_level, u.username FROM kanban_board_shares s "
        "LEFT JOIN users u ON s.share_type = 'user' AND u.id = s.target WHERE s.board_id = ? "
        "ORDER BY s.share_type, s.target",
        (board["id"],),
    )


def transfer(board: dict[str, Any], new_owner_id: str, user: User | None = None) -> dict[str, Any]:
    owner = db.one("SELECT id, username FROM users WHERE id = ?", (str(new_owner_id or ""),))
    if owner is None:
        raise KanbanError("kanban.error.user_not_found")
    db.update("kanban_boards", {"created_by": owner["id"]}, "id = ?", (board["id"],))
    signals.board_changed(board["id"], user)
    return owner


def remove_invalid_assignees(board: dict[str, Any]) -> list[int]:
    """Unassign people who can no longer reach *board* (after sharing or access changes).

    Returns the ids of the tickets that changed.
    """
    allowed = access.assignable_ids(board)
    rows = db.all(
        "SELECT t.id, t.assigned_to, a.user_id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
        "LEFT JOIN kanban_ticket_assignees a ON a.ticket_id = t.id WHERE c.board_id = ? "
        "AND (t.assigned_to IS NOT NULL OR a.user_id IS NOT NULL) ORDER BY a.assigned_at",
        (board["id"],),
    )
    current: dict[int, list[str]] = {}
    legacy: dict[int, str | None] = {}
    for row in rows:
        current.setdefault(row["id"], [])
        legacy[row["id"]] = row["assigned_to"]
        if row["user_id"]:
            current[row["id"]].append(row["user_id"])
    changed: list[int] = []
    for ticket_id, people in current.items():
        if not people and legacy[ticket_id]:
            people = [legacy[ticket_id]]  # type: ignore[list-item]
        kept = [person for person in people if person in allowed]
        if kept != people or (legacy[ticket_id] or None) != (kept[0] if kept else None):
            store.set_assignees(ticket_id, kept)
            if kept != people:
                changed.append(ticket_id)
    return changed


def apply_global_settings() -> None:
    """After ``kanban_access`` changes: drop role shares and assignees that lost access."""
    allowed = access.shareable_roles()
    marks = ",".join("?" * len(allowed)) or "''"
    unassigned: dict[int, list[int]] = {}
    with db.transaction():
        db.execute(f"DELETE FROM kanban_board_shares WHERE share_type = 'role' AND target NOT IN ({marks})",
                   allowed)
        for board in db.all("SELECT * FROM kanban_boards"):
            changed = remove_invalid_assignees(board)
            if changed:
                unassigned[board["id"]] = changed
    for board_id, ticket_ids in unassigned.items():
        signals.tickets_changed("updated", ticket_ids, board_id, None)


# ── Board order ───────────────────────────────────────────────────────────────


def _order_key(user: User | None) -> str | None:
    return None if user is None or access.open_access() else user["id"]


def _saved_order(key: str | None) -> list[int]:
    return db.column("SELECT board_id FROM kanban_user_board_order WHERE user_id IS ? ORDER BY sort_order", (key,))


def ordered_boards(user: User | None, *, archived: bool = False) -> list[dict[str, Any]]:
    """The boards *user* can open, in their saved order (or the shared one with open access).

    Archived boards are left out; with ``archived=True`` only they are listed.
    """
    boards = [board for board in access.visible_boards(user) if bool(board.get("archived_at")) == archived]
    by_id = {board["id"]: board for board in boards}
    ordered = store.reorder(list(by_id), _saved_order(_order_key(user)), bounded=False)
    return [by_id[board_id] for board_id in ordered]


def save_board_order(user: User, board_ids: list[Any]) -> None:
    """Save the order of the active boards *user* can see (archived ones keep their places after them);
    boards others see keep their places in a shared order."""
    active = [board["id"] for board in ordered_boards(user)]
    archived = [board["id"] for board in ordered_boards(user, archived=True)]
    visible = active + archived
    wanted = store.reorder(active, board_ids) + archived
    key = _order_key(user)
    if key is None:
        every = store.reorder(db.column("SELECT id FROM kanban_boards ORDER BY created_at DESC, id DESC"),
                              _saved_order(None), bounded=False)
        slots = iter(wanted)
        visible_set = set(visible)
        wanted = [next(slots) if board_id in visible_set else board_id for board_id in every]
    with db.transaction():
        db.execute("DELETE FROM kanban_user_board_order WHERE user_id IS ?", (key,))
        db.executemany(
            "INSERT INTO kanban_user_board_order (user_id, board_id, sort_order) VALUES (?, ?, ?)",
            [(key, board_id, index) for index, board_id in enumerate(wanted)],
        )


def list_order_version() -> str:
    """Changes whenever the shared board order changes (clients reload the list)."""
    return format(zlib.crc32(",".join(map(str, _saved_order(None))).encode()), "08x")


# ── Columns ───────────────────────────────────────────────────────────────────


def create_column(board: dict[str, Any], user: User, title: Any) -> dict[str, Any]:
    clean = fields.title(title, fields.MAX_COLUMN_TITLE)
    with db.transaction():
        position = int(db.scalar("SELECT COUNT(*) FROM kanban_columns WHERE board_id = ?", (board["id"],),
                                 default=0))
        column_id = db.insert("kanban_columns", {"board_id": board["id"], "title": clean, "sort_order": position,
                                                 "created_at": now_sql()})
        column = {"id": column_id, "title": clean, "wip_limit": None, "tickets": []}
        _log(board["id"], user, op="column_upsert", payload={"column": column, "order": store.column_ids(board["id"])},
             action="column_created", message=t("kanban.log.column_created", title=clean))
    signals.board_changed(board["id"], user)
    return column


def rename_column(board: dict[str, Any], column: dict[str, Any], user: User, title: Any) -> dict[str, Any]:
    clean = fields.title(title, fields.MAX_COLUMN_TITLE)
    with db.transaction():
        db.update("kanban_columns", {"title": clean}, "id = ?", (column["id"],))
        payload = _column_payload({**column, "title": clean})
        _log(board["id"], user, op="column_upsert", payload={"column": payload, "order": store.column_ids(board["id"])},
             action="column_renamed", message=t("kanban.log.column_renamed", title=clean))
    signals.board_changed(board["id"], user)
    return payload


def update_column(board: dict[str, Any], column: dict[str, Any], user: User, data: dict[str, Any]) -> dict[str, Any]:
    """Change a column's ``title`` and/or ``wip_limit`` (whichever *data* holds) in one step."""
    values: dict[str, Any] = {}
    if "title" in data:
        values["title"] = fields.title(data["title"], fields.MAX_COLUMN_TITLE)
    if "wip_limit" in data:
        values["wip_limit"] = fields.wip_limit(data["wip_limit"])
    if not values:
        raise KanbanError("kanban.error.nothing_to_change")
    updated = {**column, **values}
    if "title" in values and values["title"] != column["title"]:
        action, message = "column_renamed", t("kanban.log.column_renamed", title=updated["title"])
    else:
        action, message = "column_limit", t("kanban.log.column_limit", title=updated["title"])
    with db.transaction():
        db.update("kanban_columns", values, "id = ?", (column["id"],))
        payload = _column_payload(updated)
        _log(board["id"], user, op="column_upsert", payload={"column": payload, "order": store.column_ids(board["id"])},
             action=action, message=message)
    signals.board_changed(board["id"], user)
    return payload


def set_wip_limit(board: dict[str, Any], column: dict[str, Any], user: User, limit: Any) -> dict[str, Any]:
    """Set (or with ``None``/0/empty, remove) a column's work-in-progress limit."""
    return update_column(board, column, user, {"wip_limit": limit})


def delete_columns(board: dict[str, Any], columns: list[dict[str, Any]], user: User) -> None:
    """Delete columns with their tickets, archived ones included (``kanban.ticket.deleted`` for each)."""
    ids = [column["id"] for column in columns]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    refs = store.attachment_refs(f"c.id IN ({marks})", tuple(ids))
    doomed = db.all(f"{store.TICKET_SELECT} WHERE t.column_id IN ({marks}) ORDER BY t.id", ids)
    with db.transaction():
        store.delete_blobs(refs)
        db.execute(f"DELETE FROM kanban_columns WHERE id IN ({marks}) AND board_id = ?", (*ids, board["id"]))
        remaining = store.column_ids(board["id"])
        store.write_column_order(board["id"], remaining)
        for column in columns:
            events.append(board["id"], "column_deleted", {"column_id": column["id"], "order": remaining},
                          user["id"])
        names = ", ".join(column["title"] for column in columns)
        events.log_activity(board["id"], user["id"], "column_deleted", t("kanban.log.column_deleted", title=names))
        history.record(board["id"], user["id"], t("kanban.log.column_deleted", title=names))
    store.delete_files(refs)
    board_row = store.get_board(board["id"]) or board
    signals.tickets("deleted", doomed, board_row, user)
    signals.board("updated", board_row, user)


def reorder_columns(board: dict[str, Any], order: list[Any], user: User) -> list[int]:
    with db.transaction():
        ordered = store.reorder(store.column_ids(board["id"]), order)
        store.write_column_order(board["id"], ordered)
        _log(board["id"], user, op="columns_reordered", payload={"order": ordered}, action="columns_reordered",
             message=t("kanban.log.columns_reordered"), coalesce=True)
    signals.board_changed(board["id"], user)
    return ordered


# ── Tickets ───────────────────────────────────────────────────────────────────


def _assignees(board: dict[str, Any], raw: Any, *, strict: bool) -> list[str]:
    """Validate assignee ids: each must be able to reach the board."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise KanbanError("kanban.error.invalid_assignees")
    allowed = access.assignable_ids(board)
    result: dict[str, None] = {}
    for item in raw:
        user_id = str(item or "").strip()
        if not user_id or user_id in result:
            continue
        if user_id not in allowed:
            if strict:
                raise KanbanError("kanban.error.assignee_no_access")
            continue
        result[user_id] = None
    return list(result)


def _ids_for_usernames(usernames: list[str]) -> list[str]:
    if not usernames:
        return []
    marks = ",".join("?" * len(usernames))
    rows = db.all(f"SELECT id, username FROM users WHERE lower(username) IN ({marks})",
                  [name.lower() for name in usernames])
    by_name = {row["username"].lower(): row["id"] for row in rows}
    return [by_name[name.lower()] for name in usernames if name.lower() in by_name]


def _ticket_values(data: dict[str, Any], shorthand: fields.Shorthand | None) -> dict[str, Any]:
    """Validated column values from *data*; shorthand fills fields the request did not set."""
    values: dict[str, Any] = {}
    if shorthand is not None:
        values["title"] = fields.title(shorthand.title, fields.MAX_TICKET_TITLE)
        for name in ("priority", "color", "due_date"):
            if name not in data and getattr(shorthand, name):
                values[name] = getattr(shorthand, name)
        if "labels" not in data and shorthand.labels:
            values["labels"] = fields.encode_labels(shorthand.labels)
    if "description" in data:
        values["description"] = fields.description(data["description"])
    if "priority" in data:
        values["priority"] = fields.priority(data["priority"])
    if "color" in data:
        values["color"] = fields.color(data["color"])
    if "due_date" in data:
        values["due_date"] = fields.due_date(data["due_date"])
    if "labels" in data:
        values["labels"] = fields.encode_labels(fields.labels(data["labels"]))
    return values


def _requested_assignees(board: dict[str, Any], data: dict[str, Any], shorthand: fields.Shorthand | None,
                         *, strict: bool) -> list[str] | None:
    if "assignees" in data:
        return _assignees(board, data["assignees"], strict=strict)
    if "assigned_to" in data:
        return _assignees(board, [data["assigned_to"]] if data["assigned_to"] else [], strict=strict)
    if shorthand is not None and shorthand.usernames:
        return _assignees(board, _ids_for_usernames(shorthand.usernames), strict=False)
    return None


def create_ticket(board: dict[str, Any], column: dict[str, Any], user: User, data: dict[str, Any]) -> dict[str, Any]:
    shorthand = fields.parse_shorthand(data.get("title"))
    values = _ticket_values(data, shorthand)
    assignees = _requested_assignees(board, data, shorthand, strict=False) or []
    with db.transaction():
        position = len(store.ticket_ids(column["id"]))
        ticket_id = db.insert("kanban_tickets", {
            "column_id": column["id"], "title": values.pop("title"), "description": values.pop("description", ""),
            "priority": values.pop("priority", "medium"), "created_by": user["id"], "sort_order": position,
            "created_at": now_sql(), **values,
        })
        store.set_assignees(ticket_id, assignees)
        card = store.ticket_card(ticket_id)
        assert card is not None
        _log(board["id"], user, op="ticket_upsert",
             payload={"ticket": card, "columns": {str(column["id"]): store.ticket_ids(column["id"])}},
             action="ticket_created", message=t("kanban.log.ticket_created", title=card["title"]))
    signals.tickets_changed("created", [ticket_id], board["id"], user)
    return card


def update_ticket(board: dict[str, Any], ticket: dict[str, Any], user: User, data: dict[str, Any]) -> dict[str, Any]:
    shorthand = fields.parse_shorthand(data["title"]) if "title" in data else None
    values = _ticket_values(data, shorthand)
    assignees = _requested_assignees(board, data, shorthand, strict=True)
    with db.transaction():
        if "description" in values and values["description"] != (ticket["description"] or ""):
            history.record_description(ticket["id"], ticket["description"] or "", values["description"], user["id"])
        db.update("kanban_tickets", values, "id = ?", (ticket["id"],))
        if assignees is not None:
            store.set_assignees(ticket["id"], assignees)
        card = store.ticket_card(ticket["id"])
        assert card is not None
        _log(board["id"], user, op="ticket_upsert", payload={"ticket": card, "columns": {}},
             action="ticket_updated", message=t("kanban.log.ticket_updated", title=card["title"]))
    signals.tickets_changed("updated", [ticket["id"]], board["id"], user)
    return card


def move_ticket(board: dict[str, Any], ticket: dict[str, Any], target: dict[str, Any], position: Any,
                user: User) -> dict[str, Any]:
    """Put *ticket* at *position* (0-based) in *target*; both columns are renumbered.

    Archived tickets cannot be moved (restore them first).
    """
    if ticket.get("archived_at"):
        raise KanbanError("kanban.error.ticket_archived", status=409)
    try:
        index = max(0, int(position))
    except (TypeError, ValueError):
        index = 10**9
    with db.transaction():
        source_id = ticket["column_id"]
        source = [tid for tid in store.ticket_ids(source_id) if tid != ticket["id"]]
        destination = source if target["id"] == source_id else store.ticket_ids(target["id"])
        destination.insert(min(index, len(destination)), ticket["id"])
        store.write_ticket_order(target["id"], destination)
        columns = {str(target["id"]): destination}
        if target["id"] != source_id:
            store.write_ticket_order(source_id, source)
            columns[str(source_id)] = source
        card = store.ticket_card(ticket["id"])
        assert card is not None
        # The history entry leaves the column out, so moving a ticket back and forth shares one entry.
        _log(board["id"], user, op="ticket_upsert", payload={"ticket": card, "columns": columns},
             action="ticket_moved", coalesce=True,
             message=t("kanban.log.ticket_moved", title=card["title"], column=target["title"]),
             entry=t("kanban.log.ticket_moved_entry", title=card["title"]))
    signals.moved([(ticket["id"], source_id)], board["id"], user)
    return {"ticket": card, "columns": columns}


def reorder_tickets(board: dict[str, Any], column: dict[str, Any], order: list[Any], user: User) -> list[int]:
    """Reorder within one column; ids of other columns are ignored."""
    with db.transaction():
        before = store.ticket_ids(column["id"])
        ordered = store.reorder(before, order)
        store.write_ticket_order(column["id"], ordered)
        _log(board["id"], user, op="tickets_reordered", payload={"columns": {str(column["id"]): ordered}},
             action="tickets_reordered", message=t("kanban.log.tickets_reordered"), coalesce=True)
    shifted = [ticket_id for index, ticket_id in enumerate(ordered) if before[index] != ticket_id]
    signals.moved([(ticket_id, column["id"]) for ticket_id in shifted], board["id"], user)
    return ordered


def delete_tickets(board: dict[str, Any], tickets: list[dict[str, Any]], user: User) -> None:
    """Delete tickets for good (active or archived)."""
    ids = [ticket["id"] for ticket in tickets]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    refs = store.attachment_refs(f"t.id IN ({marks})", tuple(ids))
    doomed = db.all(f"{store.TICKET_SELECT} WHERE t.id IN ({marks}) ORDER BY t.id", ids)
    touched = sorted({ticket["column_id"] for ticket in tickets})
    with db.transaction():
        store.delete_blobs(refs)
        db.execute(f"DELETE FROM kanban_tickets WHERE id IN ({marks})", ids)
        columns = {}
        for column_id in touched:
            remaining = store.ticket_ids(column_id)
            store.write_ticket_order(column_id, remaining)
            columns[str(column_id)] = remaining
        names = ", ".join(ticket["title"] for ticket in tickets)
        archived = [ticket["id"] for ticket in tickets if ticket.get("archived_at")]
        _log(board["id"], user, op="ticket_deleted", payload={"ticket_ids": ids, "columns": columns, "archived": archived},
             action="ticket_deleted", message=t("kanban.log.ticket_deleted", title=names[:300]))
    store.delete_files(refs)
    signals.tickets("deleted", doomed, store.get_board(board["id"]) or board, user)


def _selection(raw_ids: Any) -> list[Any]:
    """A bulk request's list of ids, refused before any work when it holds more than ``BULK_LIMIT``."""
    if not isinstance(raw_ids, list) or not raw_ids:
        raise KanbanError("kanban.error.nothing_selected")
    if len(raw_ids) > BULK_LIMIT:
        raise KanbanError("kanban.error.too_many")
    return raw_ids


def tickets_of_board(board: dict[str, Any], raw_ids: Any) -> list[dict[str, Any]]:
    """Resolve a list of ticket ids that must all belong to *board*."""
    selection = _selection(raw_ids)
    try:
        ids = list(dict.fromkeys(int(raw) for raw in selection))
    except (TypeError, ValueError) as error:
        raise KanbanError("kanban.error.nothing_selected") from error
    marks = ",".join("?" * len(ids))
    rows = db.all(
        f"SELECT t.*, c.board_id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
        f"WHERE t.id IN ({marks})",
        ids,
    )
    if len(rows) != len(ids) or any(row["board_id"] != board["id"] for row in rows):
        raise KanbanError("kanban.error.not_on_board", status=404)
    return rows


def columns_of_board(board: dict[str, Any], raw_ids: Any) -> list[dict[str, Any]]:
    columns: dict[int, dict[str, Any]] = {}
    for raw in _selection(raw_ids):
        column = store.get_column(raw)
        if column is None or column["board_id"] != board["id"]:
            raise KanbanError("kanban.error.not_on_board", status=404)
        columns.setdefault(column["id"], column)
    return list(columns.values())


def bulk_update(board: dict[str, Any], tickets: list[dict[str, Any]], user: User, action: str,
                data: dict[str, Any]) -> int:
    """``delete``, ``archive``, ``restore``, ``assign``, ``priority``, ``move``, ``color`` or ``due_date``
    for many tickets at once. ``archive`` and ``restore`` return how many tickets changed state; the other
    changes refuse archived tickets."""
    if action == "delete":
        delete_tickets(board, tickets, user)
        return len(tickets)
    if action == "archive":
        return archive_tickets(board, tickets, user)
    if action == "restore":
        return restore_tickets(board, tickets, user)
    if any(ticket.get("archived_at") for ticket in tickets):
        raise KanbanError("kanban.error.ticket_archived", status=409)
    target: dict[str, Any] | None = None
    people: list[str] = []
    values: dict[str, Any] = {}
    if action == "move":
        target = store.get_column(data.get("column_id"))
        if target is None or target["board_id"] != board["id"]:
            raise KanbanError("kanban.error.not_on_board", status=404)
    elif action == "assign":
        people = _assignees(board, data.get("assignees") or [], strict=True)
    elif action in ("priority", "color", "due_date"):
        values = _ticket_values({action: data.get(action)}, None)
    else:
        raise KanbanError("kanban.error.unknown_action")
    with db.transaction():
        if target is not None:
            _bulk_move(tickets, target)
        for ticket in tickets:
            if action == "assign":
                store.set_assignees(ticket["id"], people)
            elif values:
                db.update("kanban_tickets", values, "id = ?", (ticket["id"],))
        _log(board["id"], user, op="board_reset", payload={}, action=f"tickets_bulk_{action}",
             message=t(f"kanban.log.bulk_{action}", count=len(tickets)))
    if target is not None:
        signals.moved([(ticket["id"], ticket["column_id"]) for ticket in tickets], board["id"], user)
    else:
        signals.tickets_changed("updated", [ticket["id"] for ticket in tickets], board["id"], user)
    return len(tickets)


def _bulk_move(tickets: list[dict[str, Any]], target: dict[str, Any]) -> None:
    """Append *tickets* to *target* (in board order) and close the gaps they leave."""
    moving = [ticket["id"] for ticket in sorted(tickets, key=lambda row: (row["sort_order"], row["id"]))]
    leaving = set(moving)
    for column_id in {ticket["column_id"] for ticket in tickets} - {target["id"]}:
        store.write_ticket_order(column_id, [tid for tid in store.ticket_ids(column_id) if tid not in leaving])
    staying = [tid for tid in store.ticket_ids(target["id"]) if tid not in leaving]
    store.write_ticket_order(target["id"], staying + moving)


# ── Archive ───────────────────────────────────────────────────────────────────


def archive_tickets(board: dict[str, Any], tickets: list[dict[str, Any]], user: User | None) -> int:
    """Take tickets off the board (already archived ones are skipped); returns how many were archived."""
    active = [ticket for ticket in tickets if not ticket.get("archived_at")]
    if not active:
        return 0
    ids = [ticket["id"] for ticket in active]
    marks = ",".join("?" * len(ids))
    user_id = signals.actor_id(user)
    with db.transaction():
        db.execute(f"UPDATE kanban_tickets SET archived_at = ?, archived_by = ? WHERE id IN ({marks})",
                   (now_sql(), user_id, *ids))
        columns = {}
        for column_id in sorted({ticket["column_id"] for ticket in active}):
            remaining = store.ticket_ids(column_id)
            store.write_ticket_order(column_id, remaining)
            columns[str(column_id)] = remaining
        names = ", ".join(ticket["title"] for ticket in active)
        events.append(board["id"], "tickets_archived", {"ticket_ids": ids, "columns": columns}, user_id)
        events.log_activity(board["id"], user_id, "ticket_archived", t("kanban.log.ticket_archived", title=names[:300]))
        history.record(board["id"], user_id, t("kanban.log.ticket_archived", title=names[:300]))
    signals.tickets_changed("updated", ids, board["id"], user)
    return len(ids)


def restore_tickets(board: dict[str, Any], tickets: list[dict[str, Any]], user: User | None) -> int:
    """Put archived tickets back at the end of their columns; returns how many were restored."""
    archived = sorted((ticket for ticket in tickets if ticket.get("archived_at")),
                      key=lambda row: (row["archived_at"], row["id"]))
    if not archived:
        return 0
    ids = [ticket["id"] for ticket in archived]
    user_id = signals.actor_id(user)
    with db.transaction():
        columns: dict[str, list[int]] = {}
        for ticket in archived:
            order = store.ticket_ids(ticket["column_id"]) + [ticket["id"]]
            db.update("kanban_tickets", {"archived_at": None, "archived_by": None}, "id = ?", (ticket["id"],))
            store.write_ticket_order(ticket["column_id"], order)
            columns[str(ticket["column_id"])] = order
        if len(ids) == 1:
            events.append(board["id"], "ticket_upsert",
                          {"ticket": store.ticket_card(ids[0]), "columns": columns, "restored": True}, user_id)
        else:
            events.append(board["id"], "board_reset", {}, user_id)
        names = ", ".join(ticket["title"] for ticket in archived)
        events.log_activity(board["id"], user_id, "ticket_restored", t("kanban.log.ticket_restored", title=names[:300]))
        history.record(board["id"], user_id, t("kanban.log.ticket_restored", title=names[:300]))
    signals.tickets_changed("updated", ids, board["id"], user)
    return len(ids)


def archive_column(board: dict[str, Any], column: dict[str, Any], user: User | None) -> int:
    """Archive every active ticket of a column ("clear the Done column"); returns how many."""
    rows = db.all("SELECT t.*, c.board_id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
                  "WHERE t.column_id = ? AND t.archived_at IS NULL ORDER BY t.sort_order, t.id", (column["id"],))
    return archive_tickets(board, rows, user)


def archived_tickets(board: dict[str, Any], limit: int = ARCHIVE_LIST_LIMIT) -> list[dict[str, Any]]:
    """The board's archived tickets, most recently archived first (card, ``column_title``,
    ``archived_at``, ``archived_by_username``)."""
    return store.archived_tickets(board["id"], min(max(1, int(limit)), ARCHIVE_LIST_LIMIT))


def ticket_detail(ticket: dict[str, Any]) -> dict[str, Any]:
    card = store.ticket_card(ticket["id"]) or {}
    return {
        **card,
        "archived_at": ticket.get("archived_at"),
        "archived_by_username": ticket.get("archived_by_username") or "",
        "checklist": store.checklist(ticket["id"]),
        "description": ticket["description"] or "",
        "description_html": render(ticket["description"] or ""),
        "created_by_username": ticket.get("created_by_username") or "",
        "created_at": ticket["created_at"],
    }


# ── Checklists ────────────────────────────────────────────────────────────────


def _checklist_changed(board: dict[str, Any], ticket: dict[str, Any], user: User) -> dict[str, Any]:
    """Log a checklist change and send the ticket's new card (with its progress) to other viewers."""
    card = store.ticket_card(ticket["id"])
    assert card is not None
    _log(board["id"], user, op="ticket_upsert", payload={"ticket": card, "columns": {}}, action="checklist_changed",
         message=t("kanban.log.checklist_changed", title=ticket["title"]), coalesce=True)
    return {"checklist": store.checklist(ticket["id"]), "ticket": card}


def add_checklist_item(board: dict[str, Any], ticket: dict[str, Any], user: User, text: Any) -> dict[str, Any]:
    """Append an item; returns the whole checklist and the updated card."""
    clean = fields.checklist_text(text)
    with db.transaction():
        count = int(db.scalar("SELECT COUNT(*) FROM kanban_ticket_checklist WHERE ticket_id = ?", (ticket["id"],),
                              default=0))
        if count >= fields.MAX_CHECKLIST_ITEMS:
            raise KanbanError("kanban.error.checklist_full", maximum=fields.MAX_CHECKLIST_ITEMS)
        db.insert("kanban_ticket_checklist", {"ticket_id": ticket["id"], "text": clean, "done": 0,
                                              "sort_order": count, "created_at": now_sql()})
        result = _checklist_changed(board, ticket, user)
    signals.tickets_changed("updated", [ticket["id"]], board["id"], user)
    return result


def update_checklist_item(board: dict[str, Any], item: dict[str, Any], user: User,
                          data: dict[str, Any]) -> dict[str, Any]:
    """Change an item's ``text`` and/or tick it (``done``)."""
    values: dict[str, Any] = {}
    if "text" in data:
        values["text"] = fields.checklist_text(data["text"])
    if "done" in data:
        if not isinstance(data["done"], bool):
            raise KanbanError("kanban.error.invalid_checklist")
        values["done"] = 1 if data["done"] else 0
    if not values:
        raise KanbanError("kanban.error.nothing_to_change")
    ticket = store.get_ticket(item["ticket_id"])
    assert ticket is not None
    with db.transaction():
        db.update("kanban_ticket_checklist", values, "id = ?", (item["id"],))
        result = _checklist_changed(board, ticket, user)
    signals.tickets_changed("updated", [ticket["id"]], board["id"], user)
    return result


def delete_checklist_item(board: dict[str, Any], item: dict[str, Any], user: User) -> dict[str, Any]:
    ticket = store.get_ticket(item["ticket_id"])
    assert ticket is not None
    with db.transaction():
        db.execute("DELETE FROM kanban_ticket_checklist WHERE id = ?", (item["id"],))
        store.write_checklist_order(ticket["id"], store.checklist_ids(ticket["id"]))
        result = _checklist_changed(board, ticket, user)
    signals.tickets_changed("updated", [ticket["id"]], board["id"], user)
    return result


def reorder_checklist(board: dict[str, Any], ticket: dict[str, Any], order: list[Any],
                      user: User) -> dict[str, Any]:
    """Reorder a ticket's items; ids of other tickets' items are ignored."""
    with db.transaction():
        store.write_checklist_order(ticket["id"], store.reorder(store.checklist_ids(ticket["id"]), order))
        result = _checklist_changed(board, ticket, user)
    signals.tickets_changed("updated", [ticket["id"]], board["id"], user)
    return result


# ── My tickets ────────────────────────────────────────────────────────────────

MY_TICKETS_LIMIT = 500


MY_TICKETS_SCAN = 5000


def assigned_tickets(user: User, limit: int = MY_TICKETS_LIMIT,
                     flt: filters.TicketFilter | None = None) -> list[dict[str, Any]]:
    """Tickets assigned to *user* on every board they can open, earliest due date first.

    Each entry is the ticket's card plus ``board_id``, ``board_title`` and
    ``column_title``. Boards the user can no longer open, archived boards and
    archived tickets are left out. *flt* narrows the list with the board filter
    rules (its ``who`` part is ignored: these are the user's own tickets).
    """
    boards = {board["id"]: board for board in access.visible_boards(user) if not board.get("archived_at")}
    if not boards:
        return []
    limit = max(1, int(limit))
    today = filters.site_today()
    flt = filters.TicketFilter(q=flt.q, label=flt.label, priority=flt.priority, due=flt.due) if flt else None
    conditions, params = filters.sql_conditions(flt, today) if flt else ([], [])
    extra = "".join(f" AND ({condition})" for condition in conditions)
    in_python = bool(flt and (flt.q or flt.label))
    marks = ",".join("?" * len(boards))
    rows = db.all(
        f"{store.TICKET_SELECT} WHERE c.board_id IN ({marks}) AND {store.ACTIVE}{extra} AND (EXISTS (SELECT 1 FROM "
        "kanban_ticket_assignees a WHERE a.ticket_id = t.id AND a.user_id = ?) OR (t.assigned_to = ? AND NOT EXISTS "
        "(SELECT 1 FROM kanban_ticket_assignees a WHERE a.ticket_id = t.id))) "
        "ORDER BY t.due_date IS NULL, t.due_date, CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, t.id LIMIT ?",
        (*boards, *params, user["id"], user["id"], MY_TICKETS_SCAN if in_python else limit),
    )
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    people = store.assignees_by_ticket(f"t.id IN ({','.join('?' * len(ids))})", tuple(ids))
    column_ids = sorted({row["column_id"] for row in rows})
    columns = {row["id"]: row["title"] for row in db.all(
        f"SELECT id, title FROM kanban_columns WHERE id IN ({','.join('?' * len(column_ids))})", tuple(column_ids),
    )}
    result = []
    for row in rows:
        card = store.card(row, people.get(row["id"]))
        if in_python and not filters.matches(card, flt, user_id=user["id"], today=today):  # type: ignore[arg-type]
            continue
        result.append({**card, "board_id": row["board_id"], "board_title": boards[row["board_id"]]["title"],
                       "column_title": columns.get(row["column_id"], "")})
        if len(result) >= limit:
            break
    return result


# ── Revert ────────────────────────────────────────────────────────────────────


def revert(board: dict[str, Any], entry: dict[str, Any], user: User) -> dict[str, int]:
    with db.transaction():
        result = history.revert(board, entry["state"], user["id"])
        message = t("kanban.log.reverted", id=entry["id"])
        events.append(board["id"], "board_reset", {}, user["id"])
        events.log_activity(board["id"], user["id"], "board_reverted", message)
        history.record(board["id"], user["id"], message, is_revert=True)
    signals.board_changed(board["id"], user)
    return result


# ── Account clean-up ──────────────────────────────────────────────────────────


_HANDED_OVER = "_kanban_handed_over"


@dataclass(frozen=True)
class Released:
    """What :func:`release` changed: boards that passed to *heir*, boards where the
    account's tickets or comments changed owner, and boards it was only assigned on."""

    heir: dict[str, Any] | None
    owned: frozenset[int]
    added: frozenset[int]
    assigned: frozenset[int]

    @property
    def boards(self) -> list[int]:
        return sorted(self.owned | self.added | self.assigned)


def _credit(session: Session, user_id: str) -> str:
    """The note that heads a deleted account's comments once they show under a board owner's
    name, in the site's language (bundled translations only: no Flask application needed)."""
    username = str(session.scalar("SELECT username FROM users WHERE id = ?", (user_id,)) or "")
    language = str(session.scalar("SELECT interface_language FROM site_settings WHERE id = 1") or "")
    note = Catalog([Path(__file__).with_name("translations")]).translate(
        language, "kanban.comment.deleted_author", username=username.replace("_", r"\_"))
    return note + "\n\n"


def _heir(session: Session, user_id: str, deleted_by: str | None) -> dict[str, Any] | None:
    """Who takes over a deleted account's boards: the administrator deleting it, else the
    longest-standing owner or administrator (active ones first). Administrators can open
    every board already, so a private board gains no reader."""
    if deleted_by and deleted_by != user_id:
        row = session.one("SELECT id, username FROM users WHERE id = ? AND role IN ('owner', 'admin')",
                          (deleted_by,))
        if row is not None:
            return row
    return session.one(
        "SELECT id, username FROM users WHERE id != ? AND role IN ('owner', 'admin') "
        "ORDER BY suspended, role != 'owner', created_at, rowid LIMIT 1",
        (user_id,),
    )


def release(session: Session, user_id: str, deleted_by: str | None = None) -> Released:
    """Hand over what an account about to be deleted added to kanban, in *session*'s transaction.

    The database deletes an account's boards, tickets and comments with it
    (``ON DELETE CASCADE``), other people's tickets and discussions included.
    Instead its boards go to an administrator (:func:`_heir`) and its tickets
    and comments to the owner of the board they are on, each comment headed
    by a note naming its author (:func:`_credit`); it leaves assignee lists,
    shares and saved orders. With no other administrator left, the
    account's own boards are deleted with it, as before; what it added to
    other people's boards is still kept. Needs no Flask application: the
    hosting operator's "remove wiki user" task (:mod:`bananawiki.ops.tenant_task`)
    calls it directly, :func:`hand_over` everywhere else.
    """
    session.execute("DELETE FROM kanban_user_board_order WHERE user_id = ?", (user_id,))
    session.execute("DELETE FROM kanban_board_shares WHERE share_type = 'user' AND target = ?", (user_id,))
    heir = _heir(session, user_id, deleted_by)
    owned = frozenset(session.column("SELECT id FROM kanban_boards WHERE created_by = ?", (user_id,)))
    added = frozenset(session.column(
        "SELECT c.board_id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
        "WHERE t.created_by = :uid "
        "UNION SELECT c.board_id FROM kanban_ticket_comments m JOIN kanban_tickets t ON t.id = m.ticket_id "
        "JOIN kanban_columns c ON c.id = t.column_id WHERE m.user_id = :uid",
        {"uid": user_id},
    ))
    assigned = frozenset(session.column(
        "SELECT c.board_id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
        "WHERE t.assigned_to = :uid "
        "UNION SELECT c.board_id FROM kanban_ticket_assignees a JOIN kanban_tickets t ON t.id = a.ticket_id "
        "JOIN kanban_columns c ON c.id = t.column_id WHERE a.user_id = :uid",
        {"uid": user_id},
    ))
    if heir is None:  # its own boards are deleted with the account
        added, assigned, owned = added - owned, assigned - owned, frozenset()
    released = Released(heir, owned, added - owned, assigned - owned - added)
    if not released.boards:
        return released
    if owned and heir is not None:
        session.execute("UPDATE kanban_boards SET created_by = ? WHERE created_by = ?", (heir["id"], user_id))
    session.execute(
        "UPDATE kanban_tickets SET created_by = (SELECT b.created_by FROM kanban_columns c "
        "JOIN kanban_boards b ON b.id = c.board_id WHERE c.id = kanban_tickets.column_id) WHERE created_by = ?",
        (user_id,),
    )
    session.execute(
        "UPDATE kanban_ticket_comments SET content = ? || content, user_id = (SELECT b.created_by "
        "FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id JOIN kanban_boards b ON b.id = c.board_id "
        "WHERE t.id = kanban_ticket_comments.ticket_id) WHERE user_id = ?",
        (_credit(session, user_id), user_id),
    )
    session.execute("DELETE FROM kanban_ticket_assignees WHERE user_id = ?", (user_id,))
    session.execute(
        "UPDATE kanban_tickets SET assigned_to = (SELECT a.user_id FROM kanban_ticket_assignees a "
        "WHERE a.ticket_id = kanban_tickets.id ORDER BY a.assigned_at LIMIT 1) WHERE assigned_to = ?",
        (user_id,),
    )
    return released


def hand_over(user: User, deleted_by: str | None = None, **_: Any) -> None:
    """``user.delete``, inside the deleting transaction: keep what the account added to kanban (:func:`release`).

    Every board that changed gets an activity entry and a reset for open
    pages (and a history entry when its state changed). This runs while the
    feature is switched off too.
    """
    released = release(db, user["id"], deleted_by)
    boards = released.boards
    if not boards:
        return
    heir = released.heir
    actor = heir["id"] if heir is not None and heir["id"] == deleted_by else None
    marks = ",".join("?" * len(boards))
    owners = {row["id"]: row["username"] for row in db.all(
        f"SELECT b.id, u.username FROM kanban_boards b JOIN users u ON u.id = b.created_by WHERE b.id IN ({marks})",
        boards,
    )}
    for board_id in boards:
        if board_id in released.owned:
            key = "kanban.log.account_deleted_board"
        elif board_id in released.added:
            key = "kanban.log.account_deleted"
        else:
            key = "kanban.log.account_deleted_assignee"
        message = t(key, username=user["username"], owner=owners.get(board_id, ""))
        events.append(board_id, "board_reset", {}, actor)
        events.log_activity(board_id, actor, "account_deleted", message)
        history.record(board_id, actor, message)
    if has_app_context():
        g.setdefault(_HANDED_OVER, {})[user["id"]] = (boards, actor)


def forget_user(user: User, **_: Any) -> None:
    """``user.deleted``: announce the boards :func:`hand_over` changed (``kanban.board.updated``)."""
    handed = g.get(_HANDED_OVER, {}).pop(user["id"], None) if has_app_context() else None
    if handed is None:
        return
    boards, actor = handed
    for board_id in boards:
        signals.board("updated", store.get_board(board_id), {"id": actor} if actor else None)
