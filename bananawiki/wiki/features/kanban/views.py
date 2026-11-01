"""HTML pages: board list, my tickets, board, history, sharing forms, export and import."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from flask import Response, abort, redirect, render_template, request, send_file, url_for

from ....core import downloads
from ....core.numbers import ascii_int
from ... import accounts, auth, settings, storage
from ...db import db
from ...i18n import t
from ...markdown import excerpt
from ...registry import feature_blueprint
from ..audit import record_settings
from ..audit.service import record
from ..pages.service import slugify
from . import access, events, filters, guard, history, logtext, service, store, transfer
from .fields import KanbanError
from .filters import site_today

bp = feature_blueprint("kanban", "kanban", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/kanban")

GLOBAL_SETTINGS = ("kanban_access", "kanban_write_access")
GLOBAL_SWITCHES = ("kanban_public_access_enabled", "kanban_open_access")
#: Boards one page of the board list shows (and archived boards listed at most).
LIST_PAGE_SIZE = 100


def _flash_error(error: KanbanError) -> None:
    auth.flash_t(error.key, "error", **error.values)


def _to_board(board_id: int):
    return redirect(url_for("kanban.board", board_id=board_id))


# Board list


@bp.get("/kanban")
@auth.public_read
def index():
    user = auth.current_user()
    if not access.can_open_kanban(user):
        if user is None:
            return auth.redirect_to_login()
        abort(403)
    kanban_settings = {name: settings.get(name) for name in (*GLOBAL_SETTINGS, *GLOBAL_SWITCHES)}
    show_archived = request.args.get("archived") == "1"
    # One page of boards per request, so a wiki with many boards costs the same to list.
    pages = max(1, -(-service.count_boards(user) // LIST_PAGE_SIZE))
    page = min(max(ascii_int(request.args.get("page", "1"), 6) or 1, 1), pages)
    boards = service.ordered_boards(user, limit=LIST_PAGE_SIZE, offset=(page - 1) * LIST_PAGE_SIZE)
    archived = service.ordered_boards(user, archived=True, limit=LIST_PAGE_SIZE) if show_archived else []
    return render_template(
        "kanban/list.html",
        boards=[{**row, "excerpt": excerpt(row["description"], 160, keep=True)} for row in boards],
        archived_boards=[{**row, "excerpt": excerpt(row["description"], 160, keep=True),
                          "can_restore": access.is_owner(user, row)} for row in archived],
        archived_count=service.count_boards(user, archived=True),
        show_archived=show_archived,
        page=page,
        pages=pages,
        can_create=access.can_create(user),
        # Moving boards keeps the boards of later pages after the first page, so only the first page can.
        can_reorder=user is not None and page == 1,
        open_access=access.open_access(),
        order_version=service.list_order_version(),
        kanban_settings=kanban_settings,
        show_settings=access.is_admin(user) and access.can_use(user),
        show_mine=access.can_use(user),
    )


DUE_GROUPS = ("overdue", "today", "week", "later", "none", "done")


def _due_group(due: str | None, today: date, done: bool = False) -> str:
    if done:  # finished work in a done column: its date no longer makes it urgent
        return "done"
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
        groups[_due_group(ticket["due_date"], today, ticket.get("column_done", False))].append(ticket)
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
    previous = settings.load()
    settings.update(values)
    record_settings(previous, values, via="kanban")
    service.apply_global_settings()
    auth.flash_t("kanban.flash.settings_saved", "success")
    return redirect(url_for("kanban.index"))


@bp.post("/kanban/create")
def create():
    user = auth.current_user()
    if not access.can_create(user):
        abort(403)
    guard.rate_limited("create", guard.CREATES_PER_MINUTE)
    try:
        board = service.create_board(user, request.form.get("title"), request.form.get("description"))
    except KanbanError as error:
        _flash_error(error)
        return redirect(url_for("kanban.index"))
    auth.flash_t("kanban.flash.board_created", "success")
    return _to_board(board["id"])


@bp.post("/kanban/import")
@auth.large_body
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


# Board


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


def _share_directory(user: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Accounts the sharing dialog offers, for owners who may search the member list (``search.users``).

    Only active accounts and no roles: the dialog is not a way to learn who
    the administrators are. Owners without the permission type a user name.
    """
    if not user or not auth.has_permission("search.users", user):
        return []
    return db.all("SELECT id, username FROM users WHERE suspended = 0 "
                  "AND COALESCE(approval_status, 'approved') = 'approved' ORDER BY username COLLATE NOCASE")


def _share_target() -> str:
    """The account a sharing form names: ``user_id`` from the list, or a typed ``username``."""
    user_id = request.form.get("user_id", "")
    if user_id or "username" not in request.form:
        return user_id
    account = accounts.by_username(request.form.get("username", ""))
    return account["id"] if account else ""


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
        share_users=_share_directory(user) if is_owner else [],
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
            service.add_share(row, "user", _share_target(), level, user)
        elif action == "add_role":
            service.add_share(row, "role", request.form.get("role", ""), level, user)
        elif action == "remove_user":
            service.remove_share(row, "user", request.form.get("user_id", ""), user)
        elif action == "remove_role":
            service.remove_share(row, "role", request.form.get("role", ""), user)
        elif action == "set_visibility":
            service.set_visibility(row, request.form.get("visibility", ""), user)
        elif action == "transfer_ownership":
            owner = service.transfer(row, _share_target(), user)
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
@downloads.place_first
def export(board_id: int):
    row = guard.board(board_id, "export")
    guard.rate_limited("export", 10)
    document, bundle = transfer.export(row)
    name = slugify(row["title"])[:80] or "board"
    if request.args.get("format") == "json" or not bundle:
        body = json.dumps(document, indent=2, ensure_ascii=False)
        return Response(body, mimetype="application/json",
                        headers={"Content-Disposition": storage.content_disposition(f"{name}.kanban.json")})
    try:
        archive = transfer.export_zip(document, bundle)
    except KanbanError as error:
        _flash_error(error)
        return _to_board(board_id)
    return send_file(archive, mimetype="application/zip", as_attachment=True, download_name=f"{name}.kanban.zip")


# History


@bp.app_template_global("kanban_log_message")
def _log_message_global(stored: str | None) -> str:
    """A history or activity message in the reader's language (see :mod:`.logtext`)."""
    return logtext.render(stored)


@bp.get("/kanban/<int:board_id>/history")
@auth.public_read
def board_history(board_id: int):
    row = guard.board(board_id, "view")
    guard.rate_limited("history-view", 30)
    user = auth.current_user()
    return render_template("kanban/history.html", board=row, entries=history.entries(board_id),
                           can_revert=access.can_write(user, row), is_admin=access.is_admin(user))


@bp.get("/kanban/<int:board_id>/history/<int:entry_id>")
@auth.public_read
def history_entry(board_id: int, entry_id: int):
    row = guard.board(board_id, "view")
    # A snapshot may hold thousands of tickets: their descriptions are rendered within a budget.
    guard.rate_limited("history-view", 30)
    entry = history.entry(board_id, entry_id)
    if entry is None:
        abort(404)
    user = auth.current_user()
    previous = history.previous_state(board_id, entry_id)
    changes = None if previous is None else [_describe(key, values) for key, values in
                                             history.changes(previous, entry["state"])]
    history.describe_tickets(entry["state"])
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
    # Each restore of a large board rewrites it and stores two snapshots, like an import.
    guard.rate_limited("revert", 10)
    entry = history.entry(board_id, entry_id)
    if entry is None:
        abort(404)
    try:
        result = service.revert(row, entry, auth.current_user())
    except KanbanError as error:
        _flash_error(error)
        return _to_board(board_id)
    if result["kept_tickets"]:
        auth.flash_t("kanban.flash.reverted_kept", "success", count=result["kept_tickets"])
    else:
        auth.flash_t("kanban.flash.reverted", "success")
    return _to_board(board_id)


def _audit_history(action: str, board: dict[str, Any], **details: Any) -> None:
    """Record a change to *board*'s history (as ``page.history_*``); the account acted as is named too."""
    if auth.is_impersonating():
        details["as_user"] = auth.current_user()["username"]
    record(action, target_type="board", target_id=board["id"],
           details={"title": board["title"], "creator": board.get("creator_username"), **details})


@bp.post("/kanban/<int:board_id>/history/<int:entry_id>/delete")
def delete_history_entry(board_id: int, entry_id: int):
    board = guard.board(board_id, "view")
    if not access.is_admin(auth.current_user()):
        abort(403)
    entry = history.entry(board_id, entry_id)
    if entry is None or not history.delete_entry(board_id, entry_id):
        abort(404)
    _audit_history("kanban.history_entry_deleted", board, entry=entry_id, editor=entry["username"],
                   created_at=entry["created_at"])
    auth.flash_t("kanban.flash.history_entry_deleted", "success")
    return redirect(url_for("kanban.board_history", board_id=board_id))


@bp.post("/kanban/<int:board_id>/history/clear")
def clear_history(board_id: int):
    board = guard.board(board_id, "view")
    if not access.is_admin(auth.current_user()):
        abort(403)
    count = history.clear(board_id)
    if count:
        _audit_history("kanban.history_cleared", board, count=count)
    auth.flash_t("kanban.flash.history_cleared", "success")
    return redirect(url_for("kanban.board_history", board_id=board_id))


# Only the member sees their own order of the board list.
_IMPERSONATION_UNAUDITED = frozenset({"kanban.board_order"})
_IMPERSONATION_TARGETS = (("board", "board_id"), ("column", "column_id"), ("ticket", "ticket_id"),
                          ("comment", "comment_id"), ("attachment", "attachment_id"), ("checklist_item", "item_id"))


@bp.after_request
def _audit_impersonated_action(response):
    """Record each Kanban change an administrator makes in a member's name while impersonating them.

    Comments, tickets, columns, boards, sharing, imports and reverts show only
    the member's name to the other members of the board and in its history, so
    each one gets an audit entry with the administrator as actor (refused
    attempts too, with their status), as chat actions and page changes do.
    """
    if request.method in ("GET", "HEAD", "OPTIONS") or request.endpoint in _IMPERSONATION_UNAUDITED \
            or not auth.is_impersonating():
        return response
    values = request.view_args or {}
    target = next(((kind, values[key]) for kind, key in _IMPERSONATION_TARGETS if key in values), (None, None))
    record("kanban.impersonated_action", target_type=target[0], target_id=target[1],
           details={"as_user": auth.current_user()["username"], "endpoint": request.endpoint,
                    "status": response.status_code})
    return response


def nav_visible(user: dict[str, Any] | None) -> bool:
    return access.can_open_kanban(user)

