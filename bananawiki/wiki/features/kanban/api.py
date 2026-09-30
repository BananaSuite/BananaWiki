"""JSON endpoints used by the board script and page embeds (1.4 URLs)."""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify, request, url_for

from ... import auth, settings, storage
from ...i18n import t
from ...markdown import render
from . import access, events, extras, filters, guard, service, store
from .fields import KanbanError
from .views import bp


@bp.errorhandler(KanbanError)
def _kanban_error(error: KanbanError):
    return jsonify({"error": t(error.key, **error.values)}), error.status


@bp.errorhandler(storage.UploadError)
def _upload_error(error: storage.UploadError):
    return jsonify({"error": t(error.key, **error.values)}), 400


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _user() -> dict[str, Any]:
    user = auth.current_user()
    if user is None:
        abort(401)
    return user


# ── Board list ────────────────────────────────────────────────────────────────


@bp.post("/api/kanban/board-order")
def board_order():
    user = _user()
    if not access.can_open_kanban(user):
        abort(403)
    board_ids = _body().get("board_ids")
    if not isinstance(board_ids, list):
        raise KanbanError("kanban.error.invalid_order")
    service.save_board_order(user, board_ids)
    return jsonify({"ok": True, "list_order_version": service.list_order_version()})


@bp.get("/api/kanban/list-order-version")
@auth.public_read
def list_order_version():
    if not access.can_open_kanban(auth.current_user()):
        abort(403)
    return jsonify({"list_order_version": service.list_order_version()})


# ── Board state and sync ──────────────────────────────────────────────────────


def _state(board: dict[str, Any]) -> dict[str, Any]:
    return {**store.board_state(board), "seq": events.head(board["id"])}


def _sync(board: dict[str, Any]):
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    return jsonify(events.since(board["id"], since, exclude_session=events.client_session()))


@bp.get("/api/kanban/<int:board_id>/state")
@auth.public_read
def board_state(board_id: int):
    """The board; with filter parameters (``q``, ``who``, ``label``, ``priority``, ``due``) only the
    matching tickets, plus ``filter``, ``shown`` and ``total``."""
    board = guard.board(board_id, "view")
    flt = filters.parse(request.args)
    state = _state(board)
    if not flt.active:
        return jsonify(state)
    user = auth.current_user()
    return jsonify(filters.apply_to_state(state, flt, user_id=user["id"] if user else None))


@bp.get("/api/kanban/<int:board_id>/sync")
@auth.public_read
def sync(board_id: int):
    return _sync(guard.board(board_id, "view"))


@bp.get("/api/embed/kanban/<board_ref>")
@auth.public_read
def embed(board_ref: str):
    """Data for ``[[kanban board="<id>"]]`` embeds (the same read check as the board page)."""
    board = guard.board(board_ref, "view")
    return jsonify({**_state(board), "url": url_for("kanban.board", board_id=board["id"])})


@bp.get("/api/embed/kanban/<board_ref>/sync")
@auth.public_read
def embed_sync(board_ref: str):
    return _sync(guard.board(board_ref, "view"))


@bp.get("/api/kanban/<int:board_id>/activity")
@auth.public_read
def activity(board_id: int):
    guard.board(board_id, "view")
    return jsonify({"entries": events.recent_activity(board_id)})


# ── Board settings ────────────────────────────────────────────────────────────


@bp.get("/api/kanban/<int:board_id>/settings")
def get_settings(board_id: int):
    board = guard.board(board_id, "owner")
    return jsonify({
        "visibility": board["visibility"],
        "created_by": board["created_by"],
        "created_by_username": board["creator_username"],
        "shares": [{"share_type": s["share_type"], "target": s["target"], "access_level": s["access_level"],
                    "target_username": s["username"]} for s in service.shares(board)],
        "kanban_access": settings.get("kanban_access") or "admin",
        "kanban_write_access": settings.get("kanban_write_access") or "admin",
        "shareable_roles": access.shareable_roles(),
    })


@bp.put("/api/kanban/<int:board_id>/settings")
def put_settings(board_id: int):
    board = guard.board(board_id, "owner")
    data = _body()
    shares = data.get("shares")
    if shares is not None and not isinstance(shares, list):
        raise KanbanError("kanban.error.invalid_share")
    service.replace_shares(board, data.get("visibility"), shares, auth.current_user())
    return jsonify({"ok": True})


# ── Columns ───────────────────────────────────────────────────────────────────


@bp.post("/api/kanban/<int:board_id>/columns")
def create_column(board_id: int):
    board = guard.board(board_id, "write")
    return jsonify(service.create_column(board, _user(), _body().get("title"))), 201


@bp.put("/api/kanban/columns/<int:column_id>")
def rename_column(column_id: int):
    """Rename a column and/or set its WIP limit (``{"title": ..., "wip_limit": ...}``, either or both)."""
    board, column = guard.column(column_id, "write")
    return jsonify(service.update_column(board, column, _user(), _body()))


@bp.delete("/api/kanban/columns/<int:column_id>")
def delete_column(column_id: int):
    board, column = guard.column(column_id, "write")
    service.delete_columns(board, [column], _user())
    return jsonify({"ok": True})


@bp.post("/api/kanban/<int:board_id>/columns/reorder")
def reorder_columns(board_id: int):
    board = guard.board(board_id, "write")
    order = _body().get("order")
    if not isinstance(order, list):
        raise KanbanError("kanban.error.invalid_order")
    return jsonify({"ok": True, "order": service.reorder_columns(board, order, _user())})


@bp.post("/api/kanban/<int:board_id>/columns/bulk")
def bulk_columns(board_id: int):
    board = guard.board(board_id, "write")
    data = _body()
    if data.get("action") != "delete":
        raise KanbanError("kanban.error.unknown_action")
    columns = service.columns_of_board(board, data.get("column_ids"))
    service.delete_columns(board, columns, _user())
    return jsonify({"updated": len(columns)})


# ── Tickets ───────────────────────────────────────────────────────────────────


@bp.post("/api/kanban/columns/<int:column_id>/tickets")
def create_ticket(column_id: int):
    board, column = guard.column(column_id, "write")
    return jsonify(service.create_ticket(board, column, _user(), _body())), 201


@bp.post("/api/kanban/columns/<int:column_id>/tickets/reorder")
def reorder_tickets(column_id: int):
    board, column = guard.column(column_id, "write")
    order = _body().get("order")
    if not isinstance(order, list):
        raise KanbanError("kanban.error.invalid_order")
    return jsonify({"ok": True, "order": service.reorder_tickets(board, column, order, _user())})


@bp.get("/api/kanban/tickets/<int:ticket_id>")
@auth.public_read
def get_ticket(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "view")
    user = auth.current_user()
    return jsonify({**service.ticket_detail(ticket), "can_write": access.can_write(user, board),
                    "can_comment": access.can_comment(user, board)})


@bp.put("/api/kanban/tickets/<int:ticket_id>")
def update_ticket(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "write")
    service.update_ticket(board, ticket, _user(), _body())
    fresh = store.get_ticket(ticket_id)
    return jsonify({"ok": True, **service.ticket_detail(fresh)})  # type: ignore[arg-type]


@bp.delete("/api/kanban/tickets/<int:ticket_id>")
def delete_ticket(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "write")
    service.delete_tickets(board, [ticket], _user())
    return jsonify({"ok": True})


@bp.post("/api/kanban/tickets/<int:ticket_id>/move")
def move_ticket(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "write")
    data = _body()
    target = store.get_column(data.get("column_id"))
    if target is None or target["board_id"] != board["id"]:
        raise KanbanError("kanban.error.not_on_board", status=404)
    position = data.get("position", data.get("sort_order", 0))
    return jsonify({"ok": True, **service.move_ticket(board, ticket, target, position, _user())})


@bp.post("/api/kanban/<int:board_id>/tickets/bulk")
def bulk_tickets(board_id: int):
    board = guard.board(board_id, "write")
    data = _body()
    tickets = service.tickets_of_board(board, data.get("ticket_ids"))
    count = service.bulk_update(board, tickets, _user(), str(data.get("action") or ""), data)
    return jsonify({"updated": count})


# ── Archive ───────────────────────────────────────────────────────────────────


@bp.get("/api/kanban/<int:board_id>/archived")
@auth.public_read
def archived(board_id: int):
    board = guard.board(board_id, "view")
    return jsonify({"tickets": service.archived_tickets(board), "total": store.archived_count(board["id"]),
                    "limit": service.ARCHIVE_LIST_LIMIT})


@bp.post("/api/kanban/tickets/<int:ticket_id>/archive")
def archive_ticket(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "write")
    return jsonify({"updated": service.archive_tickets(board, [ticket], _user())})


@bp.post("/api/kanban/tickets/<int:ticket_id>/restore")
def restore_ticket(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "write")
    count = service.restore_tickets(board, [ticket], _user())
    return jsonify({"updated": count, "ticket": store.ticket_card(ticket_id)})


@bp.post("/api/kanban/columns/<int:column_id>/archive")
def archive_column(column_id: int):
    board, column = guard.column(column_id, "write")
    return jsonify({"updated": service.archive_column(board, column, _user())})


# ── Checklists ────────────────────────────────────────────────────────────────


@bp.get("/api/kanban/tickets/<int:ticket_id>/checklist")
@auth.public_read
def list_checklist(ticket_id: int):
    guard.ticket(ticket_id, "view")
    return jsonify({"checklist": store.checklist(ticket_id)})


@bp.post("/api/kanban/tickets/<int:ticket_id>/checklist")
def add_checklist_item(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "write")
    return jsonify(service.add_checklist_item(board, ticket, _user(), _body().get("text"))), 201


@bp.post("/api/kanban/tickets/<int:ticket_id>/checklist/reorder")
def reorder_checklist(ticket_id: int):
    board, ticket = guard.ticket(ticket_id, "write")
    order = _body().get("order")
    if not isinstance(order, list):
        raise KanbanError("kanban.error.invalid_order")
    return jsonify(service.reorder_checklist(board, ticket, order, _user()))


def _checklist_item(item_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    item = store.get_checklist_item(item_id)
    if item is None:
        abort(404)
    return guard.board(item["board_id"], "write"), item


@bp.put("/api/kanban/checklist/<int:item_id>")
def update_checklist_item(item_id: int):
    board, item = _checklist_item(item_id)
    return jsonify(service.update_checklist_item(board, item, _user(), _body()))


@bp.delete("/api/kanban/checklist/<int:item_id>")
def delete_checklist_item(item_id: int):
    board, item = _checklist_item(item_id)
    return jsonify(service.delete_checklist_item(board, item, _user()))


# ── My tickets ────────────────────────────────────────────────────────────────


@bp.get("/api/kanban/my-tickets")
def my_tickets():
    user = _user()
    if not access.can_use(user):
        abort(403)
    tickets = service.assigned_tickets(user, flt=filters.parse(request.args))
    for ticket in tickets:
        ticket["url"] = url_for("kanban.board", board_id=ticket["board_id"], _anchor=f"ticket-{ticket['id']}")
    return jsonify({"tickets": tickets})


# ── Ticket description history ────────────────────────────────────────────────


@bp.get("/api/kanban/tickets/<int:ticket_id>/history")
@auth.public_read
def ticket_history(ticket_id: int):
    guard.ticket(ticket_id, "view")
    return jsonify(extras.description_history(ticket_id))


@bp.get("/api/kanban/history/<int:entry_id>")
@auth.public_read
def ticket_history_entry(entry_id: int):
    entry = extras.get_history_entry(entry_id)
    if entry is None:
        abort(404)
    guard.ticket(entry["ticket_id"], "view")
    return jsonify({
        "id": entry["id"], "ticket_id": entry["ticket_id"], "editor_name": entry["editor_name"],
        "created_at": entry["created_at"],
        "old_description": entry["old_description"], "new_description": entry["new_description"],
        "new_description_html": render(entry["new_description"] or ""),
        "diff_html": str(extras.diff_html(entry["old_description"], entry["new_description"])),
    })


# ── Comments ──────────────────────────────────────────────────────────────────


@bp.get("/api/kanban/tickets/<int:ticket_id>/comments")
@auth.public_read
def list_comments(ticket_id: int):
    guard.ticket(ticket_id, "view")
    user = auth.current_user()
    return jsonify([extras.comment_payload(row, user) for row in extras.comments(ticket_id)])


@bp.post("/api/kanban/tickets/<int:ticket_id>/comments")
def add_comment(ticket_id: int):
    _board, ticket = guard.ticket(ticket_id, "comment")
    guard.rate_limited("comment", 30)
    user = _user()
    return jsonify(extras.comment_payload(extras.add_comment(ticket, user, _body().get("content")), user)), 201


def _own_comment(comment_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    comment = extras.get_comment(comment_id)
    if comment is None:
        abort(404)
    _board, ticket = guard.ticket(comment["ticket_id"], "comment")
    if not access.can_moderate_comment(auth.current_user(), comment):
        abort(403)
    return ticket, comment


@bp.put("/api/kanban/comments/<int:comment_id>")
def edit_comment(comment_id: int):
    _ticket, comment = _own_comment(comment_id)
    guard.rate_limited("comment", 30)
    extras.edit_comment(comment, _body().get("content"))
    updated = extras.get_comment(comment_id)
    return jsonify(extras.comment_payload(updated, auth.current_user()))  # type: ignore[arg-type]


@bp.delete("/api/kanban/comments/<int:comment_id>")
def delete_comment(comment_id: int):
    ticket, comment = _own_comment(comment_id)
    extras.delete_comment(ticket, comment, _user())
    return jsonify({"ok": True})


# ── Attachments ───────────────────────────────────────────────────────────────


def _attachment_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "original_name": row["original_name"], "file_size": row["file_size"],
            "uploader_name": row.get("uploader_name") or "", "uploaded_at": row.get("uploaded_at")}


@bp.get("/api/kanban/tickets/<int:ticket_id>/attachments")
@auth.public_read
def list_attachments(ticket_id: int):
    guard.ticket(ticket_id, "view")
    return jsonify([_attachment_payload(row) for row in extras.attachments(ticket_id)])


@bp.post("/api/kanban/tickets/<int:ticket_id>/attachments")
def upload_attachment(ticket_id: int):
    _board, ticket = guard.ticket(ticket_id, "write")
    guard.rate_limited("upload", 20)
    stored = extras.add_attachment(ticket, _user(), request.files.get("file"))
    return jsonify({**stored, "name": stored["original_name"], "size": stored["file_size"]}), 201


def _attachment(attachment_id: int, need: str) -> dict[str, Any]:
    row = extras.get_attachment(attachment_id)
    if row is None:
        abort(404)
    guard.board(row["board_id"], need)
    return row


@bp.delete("/api/kanban/attachments/<int:attachment_id>")
def delete_attachment(attachment_id: int):
    row = _attachment(attachment_id, "write")
    guard.rate_limited("upload", 20)
    extras.delete_attachment(row, _user())
    return jsonify({"ok": True})


@bp.get("/api/kanban/attachments/<int:attachment_id>/download")
@auth.public_read
def download_attachment(attachment_id: int):
    row = _attachment(attachment_id, "view")
    return storage.send(store.FOLDER, row["filename"], download_name=row["original_name"],
                        inline=request.args.get("inline") == "1", blob_id=row.get("blob_id"))
