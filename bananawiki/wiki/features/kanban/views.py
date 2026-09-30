"""HTML pages: board list, my tickets, board, history, sharing forms, export and import."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from flask import Response, abort, redirect, render_template, request, send_file, url_for

from ... import auth, settings
from ...db import db
from ...i18n import t
from ...markdown import excerpt
from ...registry import feature_blueprint
from ..pages.service import slugify
from . import access, events, filters, guard, history, service, store, transfer
from .fields import KanbanError
from .filters import site_today

bp = feature_blueprint("kanban", "kanban", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/kanban")

GLOBAL_SETTINGS = ("kanban_access", "kanban_write_access")
GLOBAL_SWITCHES = ("kanban_public_access_enabled", "kanban_open_access")


def _flash_error(error: KanbanError) -> None:
    auth.flash_t(error.key, "error", **error.values)


def _to_board(board_id: int):
    return redirect(url_for("kanban.board", board_id=board_id))


# ── Board list ────────────────────────────────────────────────────────────────


@bp.get("/kanban")
@auth.public_read
def index():
    user = auth.current_user()
    if not access.can_open_kanban(user):
        if user is None:
            return auth.redirect_to_login()
        abort(403)
    kanban_settings = {name: settings.get(name) for name in (*GLOBAL_SETTINGS, *GLOBAL_SWITCHES)}
    archived = service.ordered_boards(user, archived=True)
    show_archived = request.args.get("archived") == "1"
    return render_template(
        "kanban/list.html",
        boards=[{**row, "excerpt": excerpt(row["description"], 160)} for row in service.ordered_boards(user)],
        archived_boards=[{**row, "excerpt": excerpt(row["description"], 160), "can_restore": access.is_owner(user, row)}
                         for row in archived] if show_archived else [],
        archived_count=len(archived),
        show_archived=show_archived,
        can_create=access.can_create(user),
        can_reorder=user is not None,
        open_access=access.open_access(),
        order_version=service.list_order_version(),
        kanban_settings=kanban_settings,
        show_settings=access.is_admin(user) and access.can_use(user),
        show_mine=access.can_use(user),
    )


DUE_GROUPS = ("overdue", "today", "week", "later", "none")


def _due_group(due: str | None, today: date) -> str:
    if not due:
        return "none"
    day = date.fromisoformat(due)
    if day < today:
        return "overdue"
    if day == today:
        return "today"
    return "week" if day <= today + timedelta(days=7) else "later"


@bp.get("/kanban/mine")
def mine():
    """Tickets assigned to the current user on every board they can open, grouped by due date."""
    user = auth.current_user()
    if user is None:
        return auth.redirect_to_login()
    if not access.can_use(user):
        abort(403)
    today = site_today()
    try:
        flt = filters.parse(request.args)
    except KanbanError as error:
        _flash_error(error)
        flt = filters.TicketFilter()
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name in DUE_GROUPS}
    tickets = service.assigned_tickets(user, flt=flt)
    for ticket in tickets:
        groups[_due_group(ticket["due_date"], today)].append(ticket)
    return render_template("kanban/mine.html", groups=[(name, groups[name]) for name in DUE_GROUPS if groups[name]],
                           total=len(tickets), limited=len(tickets) >= service.MY_TICKETS_LIMIT, filter=flt)


@bp.post("/kanban/settings")
@auth.admin_required
def save_settings():
    """Global kanban access (also editable from the administration pages)."""
    values: dict[str, Any] = {}
    for name in GLOBAL_SETTINGS:
        choice = request.form.get(name, "admin")
        values[name] = choice if choice in access.ACCESS_SETTING_VALUES else "admin"
    for name in GLOBAL_SWITCHES:
        values[name] = 1 if request.form.get(name) else 0
    settings.update(values)
    service.apply_global_settings()
    auth.flash_t("kanban.flash.settings_saved", "success")
    return redirect(url_for("kanban.index"))


@bp.post("/kanban/create")
def create():
    user = auth.current_user()
    if not access.can_create(user):
        abort(403)
    try:
        board = service.create_board(user, request.form.get("title"), request.form.get("description"))
    except KanbanError as error:
        _flash_error(error)
        return redirect(url_for("kanban.index"))
    auth.flash_t("kanban.flash.board_created", "success")
    return _to_board(board["id"])


@bp.post("/kanban/import")
def import_board():
    user = auth.current_user()
    if not access.can_create(user):
        abort(403)
    guard.rate_limited("import", 10)
    try:
        board = transfer.import_board(user, request.files.get("import_file"))
    except KanbanError as error:
        _flash_error(error)
        return redirect(url_for("kanban.index"))
    auth.flash_t("kanban.flash.board_imported", "success", title=board["title"])
    return _to_board(board["id"])


# ── Board ─────────────────────────────────────────────────────────────────────


def board_page_data(board: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    can_write = access.can_write(user, board)
    return {
        "state": store.board_state(board),
        "seq": events.head(board["id"]),
        "filter": {"soon_days": filters.SOON_DAYS, "week_days": filters.WEEK_DAYS},
        "user_id": user["id"] if user else None,
        "can_write": can_write,
        "can_comment": access.can_comment(user, board),
        "is_admin": access.is_admin(user),
        "assignable": access.assignable_users(board) if can_write else [],
    }


@bp.get("/kanban/<int:board_id>")
@auth.public_read
def board(board_id: int):
    row = guard.board(board_id, "view")
    user = auth.current_user()
    is_owner = access.is_owner(user, row)
    return render_template(
        "kanban/board.html",
        board=row,
        data=board_page_data(row, user),
        can_write=access.can_write(user, row),
        is_owner=is_owner,
        can_export=access.can_export(user, row),
        shares=service.shares(row) if is_owner else [],
        share_users=db.all("SELECT id, username, role FROM users ORDER BY username COLLATE NOCASE")
        if is_owner else [],
        shareable_roles=access.shareable_roles() if is_owner else [],
    )


@bp.post("/kanban/<int:board_id>/edit")
def edit(board_id: int):
    row = guard.board(board_id, "write")
    try:
        service.update_board(row, auth.current_user(), request.form.get("title"), request.form.get("description"))
    except KanbanError as error:
        _flash_error(error)
    else:
        auth.flash_t("kanban.flash.board_updated", "success")
    return _to_board(board_id)


@bp.post("/kanban/<int:board_id>/delete")
def delete(board_id: int):
    row = guard.board(board_id, "owner")
    service.delete_board(row, auth.current_user())
    auth.flash_t("kanban.flash.board_deleted", "success")
    return redirect(url_for("kanban.index"))


@bp.post("/kanban/<int:board_id>/archive")
def archive(board_id: int):
    row = guard.board(board_id, "owner")
    service.archive_board(row, auth.current_user())
    auth.flash_t("kanban.flash.board_archived", "success", title=row["title"])
    return redirect(url_for("kanban.index"))


@bp.post("/kanban/<int:board_id>/restore")
def restore(board_id: int):
    row = guard.board(board_id, "owner")
    service.restore_board(row, auth.current_user())
    auth.flash_t("kanban.flash.board_restored", "success", title=row["title"])
    return _to_board(board_id)


@bp.post("/kanban/<int:board_id>/share")
def share(board_id: int):
    """Sharing form actions: add_user, add_role, remove_user, remove_role, set_visibility, transfer_ownership."""
    row = guard.board(board_id, "owner")
    user = auth.current_user()
    action = request.form.get("action", "")
    level = request.form.get("access_level", "view")
    try:
        if action == "add_user":
            service.add_share(row, "user", request.form.get("user_id", ""), level, user)
        elif action == "add_role":
            service.add_share(row, "role", request.form.get("role", ""), level, user)
        elif action == "remove_user":
            service.remove_share(row, "user", request.form.get("user_id", ""), user)
        elif action == "remove_role":
            service.remove_share(row, "role", request.form.get("role", ""), user)
        elif action == "set_visibility":
            service.set_visibility(row, request.form.get("visibility", ""), user)
        elif action == "transfer_ownership":
            owner = service.transfer(row, request.form.get("user_id", ""), user)
            auth.flash_t("kanban.flash.transferred", "success", username=owner["username"])
            if not access.can_view(auth.current_user(), store.get_board(board_id)):
                return redirect(url_for("kanban.index"))
            return _to_board(board_id)
        else:
            raise KanbanError("kanban.error.unknown_action")
    except KanbanError as error:
        _flash_error(error)
    else:
        auth.flash_t("kanban.flash.sharing_saved", "success")
    return _to_board(board_id)


@bp.get("/kanban/<int:board_id>/export")
def export(board_id: int):
    row = guard.board(board_id, "export")
    guard.rate_limited("export", 10)
    document, bundle = transfer.export(row)
    name = slugify(row["title"])[:80] or "board"
    if request.args.get("format") == "json" or not bundle:
        body = json.dumps(document, indent=2, ensure_ascii=False)
        return Response(body, mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.kanban.json"'})
    return send_file(transfer.export_zip(document, bundle), mimetype="application/zip", as_attachment=True,
                     download_name=f"{name}.kanban.zip")


# ── History ───────────────────────────────────────────────────────────────────


@bp.get("/kanban/<int:board_id>/history")
@auth.public_read
def board_history(board_id: int):
    row = guard.board(board_id, "view")
    user = auth.current_user()
    return render_template("kanban/history.html", board=row, entries=history.entries(board_id),
                           can_revert=access.can_write(user, row), is_admin=access.is_admin(user))


@bp.get("/kanban/<int:board_id>/history/<int:entry_id>")
@auth.public_read
def history_entry(board_id: int, entry_id: int):
    row = guard.board(board_id, "view")
    entry = history.entry(board_id, entry_id)
    if entry is None:
        abort(404)
    user = auth.current_user()
    previous = history.previous_state(board_id, entry_id)
    changes = None if previous is None else [_describe(key, values) for key, values in
                                             history.changes(previous, entry["state"])]
    return render_template("kanban/history_entry.html", board=row, entry=entry, state=entry["state"],
                           changes=changes, can_revert=access.can_write(user, row), is_admin=access.is_admin(user))


_FIELD_LABELS = {"title": "kanban.field.title", "description": "kanban.field.description",
                 "priority": "kanban.field.priority", "due_date": "kanban.field.due_date",
                 "color": "kanban.field.color", "labels": "kanban.field.labels",
                 "assignee_usernames": "kanban.field.assignees"}


def _describe(key: str, values: dict[str, Any]) -> str:
    """One line of a history entry's list of changes."""
    if "fields" in values:
        values = {**values, "fields": ", ".join(t(_FIELD_LABELS[name]).lower() for name in values["fields"])}
    return t(key, **values)


@bp.post("/kanban/<int:board_id>/revert/<int:entry_id>")
def revert(board_id: int, entry_id: int):
    row = guard.board(board_id, "write")
    entry = history.entry(board_id, entry_id)
    if entry is None:
        abort(404)
    result = service.revert(row, entry, auth.current_user())
    if result["kept_tickets"]:
        auth.flash_t("kanban.flash.reverted_kept", "success", count=result["kept_tickets"])
    else:
        auth.flash_t("kanban.flash.reverted", "success")
    return _to_board(board_id)


@bp.post("/kanban/<int:board_id>/history/<int:entry_id>/delete")
def delete_history_entry(board_id: int, entry_id: int):
    guard.board(board_id, "view")
    if not access.is_admin(auth.current_user()):
        abort(403)
    if not history.delete_entry(board_id, entry_id):
        abort(404)
    auth.flash_t("kanban.flash.history_entry_deleted", "success")
    return redirect(url_for("kanban.board_history", board_id=board_id))


@bp.post("/kanban/<int:board_id>/history/clear")
def clear_history(board_id: int):
    guard.board(board_id, "view")
    if not access.is_admin(auth.current_user()):
        abort(403)
    history.clear(board_id)
    auth.flash_t("kanban.flash.history_cleared", "success")
    return redirect(url_for("kanban.board_history", board_id=board_id))


def nav_visible(user: dict[str, Any] | None) -> bool:
    return access.can_open_kanban(user)

