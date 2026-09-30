"""Kanban boards, columns, tickets, comments and ticket attachments (``kanban`` scope).

Every call applies the board rules of ``features/kanban/access.py`` to the
token's owner: a board they cannot see answers 404 (so is everything on
it), a board they can see but not change answers 403. Writing needs write
access to the board; board settings (visibility) and deletion belong to its
creator and administrators; creating boards needs ``kanban.create`` and
global write access. Changes go through the kanban services, so they show up
live on open boards and in the board's activity and history like changes
made in the browser. Checklist items follow their ticket (reading needs the
board, changing needs write access); ``/kanban/my-tickets`` lists the tickets
assigned to the caller on every board they can open.

Archiving: an archived ticket keeps its column and fields, is left out of
the board and of "my tickets", and can still be read, changed, commented
on and deleted, but not moved (409 ``ticket_archived``); restoring puts it
at the end of its column. An archived board (archived and restored by its
owner) is read-only for everyone: writes answer 403, while its owner keeps
visibility changes and deletion; the board list leaves archived boards out
unless ``?archived=1``. The board and "my tickets" accept the board filter
parameters ``q``, ``who``, ``label``, ``priority`` and ``due`` (400
``invalid_filter`` otherwise). All of it answers 404 while the Kanban
feature is off.
"""

from __future__ import annotations

from typing import Any

from flask import request

from .... import storage
from ...kanban import access, extras, filters, service, store
from ...kanban.fields import (
    MAX_BOARD_TITLE,
    MAX_CHECKLIST_TEXT,
    MAX_COLUMN_TITLE,
    MAX_COMMENT,
    MAX_TICKET_TITLE,
    KanbanError,
)
from .. import serialize
from ..errors import ApiError, flag, from_service, invalid, json_body, page_window, row_id, window_fields
from . import bp, caller, ok, requires
from .attachments_api import send_stored, upload_error

FEATURE = "kanban"
_NEEDS = {"view": access.can_view, "comment": access.can_comment, "write": access.can_write,
          "owner": access.is_owner}
_TICKET_FIELDS = {"title": str, "description": str, "priority": str, "color": str, "due_date": (str, type(None)),
                  "labels": list, "assignees": list}


# ── Loading with the board check ──────────────────────────────────────────────


def _board(board_id: Any, need: str = "view") -> dict[str, Any]:
    board = store.get_board(board_id)
    user = caller()
    if board is None or not access.can_view(user, board):
        raise ApiError(404, "board_not_found")
    if not _NEEDS[need](user, board):
        raise ApiError(403, "board_forbidden")
    return board


def _column(column_id: int, need: str) -> tuple[dict[str, Any], dict[str, Any]]:
    column = store.get_column(column_id)
    if column is None:
        raise ApiError(404, "column_not_found")
    return _board(column["board_id"], need), column


def _ticket(ticket_id: int, need: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise ApiError(404, "ticket_not_found")
    return _board(ticket["board_id"], need), ticket


def _run(action, *args: Any, **kwargs: Any) -> Any:
    """Call a kanban service, turning its refusals into API errors."""
    try:
        return action(*args, **kwargs)
    except KanbanError as error:
        raise from_service(error, error.status) from None
    except storage.UploadError as error:
        raise upload_error(error) from None


def _title(data: dict[str, Any], maximum: int) -> str:
    value = data.get("title")
    if not isinstance(value, str) or not value.strip():
        raise invalid("title", "required")
    if len(value) > maximum:
        raise invalid("title", "too_long", maximum=maximum)
    return value


def _ticket_data(data: dict[str, Any]) -> dict[str, Any]:
    """The known ticket fields, type-checked; the kanban service validates their values."""
    clean: dict[str, Any] = {}
    for name, kind in _TICKET_FIELDS.items():
        if name not in data:
            continue
        if not isinstance(data[name], kind):
            raise invalid(name, "array" if kind is list else "string")
        if kind is list and not all(isinstance(item, str) for item in data[name]):
            raise invalid(name, "string")
        clean[name] = data[name]
    return clean


def _board_payload(board: dict[str, Any]) -> dict[str, Any]:
    user = caller()
    return serialize.board(board, can_write=access.can_write(user, board), is_owner=access.is_owner(user, board))


def _ticket_payload(ticket_id: int) -> dict[str, Any]:
    row = store.get_ticket(ticket_id)
    card = store.ticket_card(ticket_id)
    if row is None or card is None:
        raise ApiError(404, "ticket_not_found")
    return serialize.ticket(card, row)


# ── Boards ────────────────────────────────────────────────────────────────────


def _filter() -> filters.TicketFilter:
    return _run(filters.parse, request.args)


def _ids(data: dict[str, Any]) -> list[int]:
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        raise invalid("ids", "array")
    return [row_id(item, "ids") for item in ids]


@bp.get("/kanban/boards")
@requires("kanban", feature=FEATURE)
def list_boards():
    """Active boards in the caller's order; ``?archived=1`` lists the archived ones instead."""
    limit, offset = page_window()
    archived = request.args.get("archived", "0")
    if archived not in ("0", "1", "true", "false"):
        raise invalid("archived", "boolean")
    boards = service.ordered_boards(caller(), archived=archived in ("1", "true"))
    window = boards[offset:offset + limit + 1]
    return ok(boards=[_board_payload(board) for board in window[:limit]], **window_fields(limit, offset, len(window)))


@bp.post("/kanban/boards")
@requires("kanban", write=True, feature=FEATURE)
def create_board():
    user = caller()
    if not access.can_create(user):
        raise ApiError(403, "cannot_create_board")
    data = json_body()
    description = data.get("description", "")
    if not isinstance(description, str):
        raise invalid("description", "string")
    default_columns = flag(data.get("default_columns", True), "default_columns")
    board = _run(service.create_board, user, _title(data, MAX_BOARD_TITLE), description,
                 default_columns=default_columns)
    return ok(201, board=_board_payload(board))


@bp.get("/kanban/boards/<int:board_id>")
@requires("kanban", feature=FEATURE)
def get_board(board_id: int):
    """The board with its columns in order and every active ticket's card, optionally filtered."""
    board = _board(board_id)
    flt = _filter()
    state = store.board_state(board)
    extra: dict[str, Any] = {}
    if flt.active:
        state = filters.apply_to_state(state, flt, user_id=caller()["id"])
        extra = {"filter": state["filter"], "shown": state["shown"], "total": state["total"]}
    tickets = [serialize.ticket(card) for card in state["tickets"].values()]
    return ok(board={**_board_payload(board), "columns": state["columns"]}, tickets=tickets, **extra)


@bp.put("/kanban/boards/<int:board_id>")
@requires("kanban", write=True, feature=FEATURE)
def update_board(board_id: int):
    """Title and description need write access; visibility belongs to the owner (also while archived)."""
    board = _board(board_id)
    data = json_body()
    for name in ("title", "description", "visibility"):
        if name in data and not isinstance(data[name], str):
            raise invalid(name, "string")
    if ("title" in data or "description" in data) and not access.can_write(caller(), board):
        raise ApiError(403, "board_forbidden")
    if "visibility" in data:
        if not access.is_owner(caller(), board):
            raise ApiError(403, "board_forbidden")
        if data["visibility"] not in access.VISIBILITIES:
            raise invalid("visibility", "choice", options=", ".join(access.VISIBILITIES))
    if "title" in data or "description" in data:
        title = _title(data, MAX_BOARD_TITLE) if "title" in data else board["title"]
        _run(service.update_board, board, caller(), title, data.get("description", board.get("description") or ""))
    if "visibility" in data:
        _run(service.set_visibility, board, data["visibility"])
    return ok(board=_board_payload(store.get_board(board_id)))


@bp.delete("/kanban/boards/<int:board_id>")
@requires("kanban", write=True, feature=FEATURE)
def delete_board(board_id: int):
    board = _board(board_id, "owner")
    _run(service.delete_board, board)
    return ok(deleted=True, id=board_id)


@bp.post("/kanban/boards/<int:board_id>/archive")
@requires("kanban", write=True, feature=FEATURE)
def archive_board(board_id: int):
    """Owner only: hide the board from the list and make it read-only (archiving twice changes nothing)."""
    board = _board(board_id, "owner")
    return ok(board=_board_payload(_run(service.archive_board, board, caller())))


@bp.post("/kanban/boards/<int:board_id>/restore")
@requires("kanban", write=True, feature=FEATURE)
def restore_board(board_id: int):
    board = _board(board_id, "owner")
    return ok(board=_board_payload(_run(service.restore_board, board, caller())))


# ── Archived tickets ──────────────────────────────────────────────────────────


@bp.get("/kanban/boards/<int:board_id>/archived-tickets")
@requires("kanban", feature=FEATURE)
def list_archived_tickets(board_id: int):
    """Archived tickets, most recently archived first (the newest 500 at most)."""
    board = _board(board_id)
    limit, offset = page_window()
    rows = service.archived_tickets(board, limit=service.ARCHIVE_LIST_LIMIT)[offset:offset + limit + 1]
    tickets = [{**serialize.ticket(row), "column_title": row["column_title"],
                "archived_by_username": row["archived_by_username"]} for row in rows[:limit]]
    return ok(tickets=tickets, total=store.archived_count(board["id"]), **window_fields(limit, offset, len(rows)))


@bp.post("/kanban/boards/<int:board_id>/tickets/archive")
@requires("kanban", write=True, feature=FEATURE)
def archive_board_tickets(board_id: int):
    """``{ids: [ticket ids]}`` (all on this board, at most 500); answers how many were archived."""
    board = _board(board_id, "write")
    tickets = _run(service.tickets_of_board, board, _ids(json_body()))
    return ok(archived=_run(service.archive_tickets, board, tickets, caller()))


@bp.post("/kanban/boards/<int:board_id>/tickets/restore")
@requires("kanban", write=True, feature=FEATURE)
def restore_board_tickets(board_id: int):
    board = _board(board_id, "write")
    tickets = _run(service.tickets_of_board, board, _ids(json_body()))
    return ok(restored=_run(service.restore_tickets, board, tickets, caller()))


# ── Columns ───────────────────────────────────────────────────────────────────


@bp.post("/kanban/boards/<int:board_id>/columns")
@requires("kanban", write=True, feature=FEATURE)
def create_column(board_id: int):
    board = _board(board_id, "write")
    column = _run(service.create_column, board, caller(), _title(json_body(), MAX_COLUMN_TITLE))
    return ok(201, column=column)


@bp.post("/kanban/boards/<int:board_id>/columns/reorder")
@requires("kanban", write=True, feature=FEATURE)
def reorder_columns(board_id: int):
    board = _board(board_id, "write")
    order = json_body().get("order")
    if not isinstance(order, list):
        raise invalid("order", "array")
    return ok(order=_run(service.reorder_columns, board, [row_id(item, "order") for item in order], caller()))


@bp.put("/kanban/columns/<int:column_id>")
@requires("kanban", write=True, feature=FEATURE)
def update_column(column_id: int):
    """``{title?, wip_limit?}``: rename the column and/or set its work-in-progress limit (null or 0 removes it)."""
    board, column = _column(column_id, "write")
    data = json_body()
    changes: dict[str, Any] = {}
    if "title" in data:
        changes["title"] = _title(data, MAX_COLUMN_TITLE)
    if "wip_limit" in data:
        limit = data["wip_limit"]
        if limit is not None and type(limit) is not int:
            raise invalid("wip_limit", "integer")
        changes["wip_limit"] = limit
    if not changes:
        raise invalid("title", "required")
    return ok(column=_run(service.update_column, board, column, caller(), changes))


@bp.delete("/kanban/columns/<int:column_id>")
@requires("kanban", write=True, feature=FEATURE)
def delete_column(column_id: int):
    board, column = _column(column_id, "write")
    _run(service.delete_columns, board, [column], caller())
    return ok(deleted=True, id=column_id)


@bp.post("/kanban/columns/<int:column_id>/archive")
@requires("kanban", write=True, feature=FEATURE)
def archive_column(column_id: int):
    """Archive every active ticket of the column ("clear the Done column")."""
    board, column = _column(column_id, "write")
    return ok(archived=_run(service.archive_column, board, column, caller()))


# ── Tickets ───────────────────────────────────────────────────────────────────


@bp.post("/kanban/columns/<int:column_id>/tickets")
@requires("kanban", write=True, feature=FEATURE)
def create_ticket(column_id: int):
    """The title accepts the board's shorthand (``@user``, ``+label``, ``!high``, ``due:``, ``color:``)."""
    board, column = _column(column_id, "write")
    data = _ticket_data(json_body())
    data["title"] = _title(data, MAX_TICKET_TITLE)
    card = _run(service.create_ticket, board, column, caller(), data)
    return ok(201, ticket=_ticket_payload(card["id"]))


@bp.get("/kanban/tickets/<int:ticket_id>")
@requires("kanban", feature=FEATURE)
def get_ticket(ticket_id: int):
    _ticket(ticket_id, "view")
    return ok(ticket=_ticket_payload(ticket_id))


@bp.put("/kanban/tickets/<int:ticket_id>")
@requires("kanban", write=True, feature=FEATURE)
def update_ticket(ticket_id: int):
    board, ticket = _ticket(ticket_id, "write")
    data = _ticket_data(json_body())
    if "title" in data:
        data["title"] = _title(data, MAX_TICKET_TITLE)
    _run(service.update_ticket, board, ticket, caller(), data)
    return ok(ticket=_ticket_payload(ticket_id))


@bp.post("/kanban/tickets/<int:ticket_id>/move")
@requires("kanban", write=True, feature=FEATURE)
def move_ticket(ticket_id: int):
    """``{column_id, position?}``: position is 0-based; omitted, the ticket goes to the end."""
    board, ticket = _ticket(ticket_id, "write")
    data = json_body()
    target = store.get_column(row_id(data.get("column_id"), "column_id"))
    if target is None or target["board_id"] != board["id"]:
        raise ApiError(404, "column_not_found")
    position = data.get("position")
    if position is not None and (type(position) is not int or position < 0):
        raise invalid("position", "integer")
    result = _run(service.move_ticket, board, ticket, target, position, caller())
    return ok(ticket=_ticket_payload(ticket_id), columns=result["columns"])


@bp.delete("/kanban/tickets/<int:ticket_id>")
@requires("kanban", write=True, feature=FEATURE)
def delete_ticket(ticket_id: int):
    board, ticket = _ticket(ticket_id, "write")
    _run(service.delete_tickets, board, [ticket], caller())
    return ok(deleted=True, id=ticket_id)


@bp.post("/kanban/tickets/<int:ticket_id>/archive")
@requires("kanban", write=True, feature=FEATURE)
def archive_ticket(ticket_id: int):
    board, ticket = _ticket(ticket_id, "write")
    changed = _run(service.archive_tickets, board, [ticket], caller())
    return ok(ticket=_ticket_payload(ticket_id), changed=bool(changed))


@bp.post("/kanban/tickets/<int:ticket_id>/restore")
@requires("kanban", write=True, feature=FEATURE)
def restore_ticket(ticket_id: int):
    board, ticket = _ticket(ticket_id, "write")
    changed = _run(service.restore_tickets, board, [ticket], caller())
    return ok(ticket=_ticket_payload(ticket_id), changed=bool(changed))


# ── Checklists ────────────────────────────────────────────────────────────────


def _checklist_answer(result: dict[str, Any], ticket_id: int, status: int = 200):
    return ok(status, checklist=result["checklist"], ticket=_ticket_payload(ticket_id))


def _checklist_item(item_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    item = store.get_checklist_item(item_id)
    if item is None:
        raise ApiError(404, "checklist_item_not_found")
    return _board(item["board_id"], "write"), item


def _checklist_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid("text", "required")
    if len(value) > MAX_CHECKLIST_TEXT:
        raise invalid("text", "too_long", maximum=MAX_CHECKLIST_TEXT)
    return value


@bp.get("/kanban/tickets/<int:ticket_id>/checklist")
@requires("kanban", feature=FEATURE)
def list_checklist(ticket_id: int):
    _ticket(ticket_id, "view")
    return ok(checklist=store.checklist(ticket_id))


@bp.post("/kanban/tickets/<int:ticket_id>/checklist")
@requires("kanban", write=True, feature=FEATURE)
def add_checklist_item(ticket_id: int):
    """``{text}``: append an item; answers the whole checklist and the ticket (with its progress)."""
    board, ticket = _ticket(ticket_id, "write")
    result = _run(service.add_checklist_item, board, ticket, caller(), _checklist_text(json_body().get("text")))
    return _checklist_answer(result, ticket_id, 201)


@bp.post("/kanban/tickets/<int:ticket_id>/checklist/reorder")
@requires("kanban", write=True, feature=FEATURE)
def reorder_checklist(ticket_id: int):
    """``{order: [item ids]}``; ids of other tickets' items are ignored."""
    board, ticket = _ticket(ticket_id, "write")
    order = json_body().get("order")
    if not isinstance(order, list):
        raise invalid("order", "array")
    result = _run(service.reorder_checklist, board, ticket, [row_id(item, "order") for item in order], caller())
    return _checklist_answer(result, ticket_id)


@bp.put("/kanban/checklist/<int:item_id>")
@requires("kanban", write=True, feature=FEATURE)
def update_checklist_item(item_id: int):
    """``{text?, done?}``: change an item's text and/or tick it."""
    board, item = _checklist_item(item_id)
    data = json_body()
    changes: dict[str, Any] = {}
    if "text" in data:
        changes["text"] = _checklist_text(data["text"])
    if "done" in data:
        changes["done"] = flag(data["done"], "done")
    if not changes:
        raise invalid("text", "required")
    return _checklist_answer(_run(service.update_checklist_item, board, item, caller(), changes), item["ticket_id"])


@bp.delete("/kanban/checklist/<int:item_id>")
@requires("kanban", write=True, feature=FEATURE)
def delete_checklist_item(item_id: int):
    board, item = _checklist_item(item_id)
    result = _run(service.delete_checklist_item, board, item, caller())
    return ok(deleted=True, id=item_id, checklist=result["checklist"], ticket=_ticket_payload(item["ticket_id"]))


# ── My tickets ────────────────────────────────────────────────────────────────


@bp.get("/kanban/my-tickets")
@requires("kanban", feature=FEATURE)
def my_tickets():
    """Active tickets assigned to the caller on active boards they can open, earliest due date first."""
    user = caller()
    if not access.can_use(user):
        raise ApiError(403, "kanban_forbidden")
    limit, offset = page_window()
    flt = _filter()
    rows = service.assigned_tickets(user, limit=offset + limit + 1, flt=flt if flt.active else None)[offset:]
    tickets = [{**serialize.ticket(row), "board_id": row["board_id"], "board_title": row["board_title"],
                "column_title": row["column_title"]} for row in rows[:limit]]
    return ok(tickets=tickets, **window_fields(limit, offset, len(rows)))


# ── Comments ──────────────────────────────────────────────────────────────────


def _content(data: dict[str, Any]) -> str:
    value = data.get("content")
    if not isinstance(value, str):
        raise invalid("content", "string")
    if len(value) > MAX_COMMENT:
        raise invalid("content", "too_long", maximum=MAX_COMMENT)
    return value


def _own_comment(comment_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    comment = extras.get_comment(comment_id)
    if comment is None:
        raise ApiError(404, "comment_not_found")
    _board_row, ticket = _ticket(comment["ticket_id"], "comment")
    if not access.can_moderate_comment(caller(), comment):
        raise ApiError(403, "comment_forbidden")
    return ticket, comment


@bp.get("/kanban/tickets/<int:ticket_id>/comments")
@requires("kanban", feature=FEATURE)
def list_comments(ticket_id: int):
    _ticket(ticket_id, "view")
    return ok(comments=[serialize.comment(row) for row in extras.comments(ticket_id)])


@bp.post("/kanban/tickets/<int:ticket_id>/comments")
@requires("kanban", write=True, feature=FEATURE)
def add_comment(ticket_id: int):
    _board_row, ticket = _ticket(ticket_id, "comment")
    row = _run(extras.add_comment, ticket, caller(), _content(json_body()))
    return ok(201, comment=serialize.comment(row))


@bp.put("/kanban/comments/<int:comment_id>")
@requires("kanban", write=True, feature=FEATURE)
def edit_comment(comment_id: int):
    _ticket_row, comment = _own_comment(comment_id)
    _run(extras.edit_comment, comment, _content(json_body()))
    return ok(comment=serialize.comment(extras.get_comment(comment_id)))


@bp.delete("/kanban/comments/<int:comment_id>")
@requires("kanban", write=True, feature=FEATURE)
def delete_comment(comment_id: int):
    ticket, comment = _own_comment(comment_id)
    _run(extras.delete_comment, ticket, comment, caller())
    return ok(deleted=True, id=comment_id)


# ── Attachments ───────────────────────────────────────────────────────────────


def _ticket_attachment(attachment_id: int, need: str) -> dict[str, Any]:
    row = extras.get_attachment(attachment_id)
    if row is None:
        raise ApiError(404, "attachment_not_found")
    _board(row["board_id"], need)
    return row


@bp.get("/kanban/tickets/<int:ticket_id>/attachments")
@requires("kanban", feature=FEATURE)
def list_ticket_attachments(ticket_id: int):
    _ticket(ticket_id, "view")
    return ok(attachments=[serialize.attachment(row) for row in extras.attachments(ticket_id)])


@bp.post("/kanban/tickets/<int:ticket_id>/attachments")
@requires("kanban", write=True, feature=FEATURE)
def upload_ticket_attachment(ticket_id: int):
    """``multipart/form-data`` with the file in the ``file`` field."""
    _board_row, ticket = _ticket(ticket_id, "write")
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiError(400, "file_required")
    stored = _run(extras.add_attachment, ticket, caller(), upload)
    row = extras.get_attachment(stored["id"]) or stored
    return ok(201, attachment=serialize.attachment({**row, "uploader": caller()["username"]}))


@bp.get("/kanban/attachments/<int:attachment_id>")
@requires("kanban", feature=FEATURE)
def download_ticket_attachment(attachment_id: int):
    return send_stored(store.FOLDER, _ticket_attachment(attachment_id, "view"))


@bp.delete("/kanban/attachments/<int:attachment_id>")
@requires("kanban", write=True, feature=FEATURE)
def delete_ticket_attachment(attachment_id: int):
    row = _ticket_attachment(attachment_id, "write")
    _run(extras.delete_attachment, row, caller())
    return ok(deleted=True, id=attachment_id)
