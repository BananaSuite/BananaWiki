"""
BananaWiki: Kanban board routes.

Provides board listing, board view with drag-and-drop columns/tickets,
and JSON API endpoints for real-time board manipulation.
Per-board sharing allows fine-grained role-based and user-specific
view/write access control.
"""

import os
import io
import uuid
import json
import zipfile
import re
from datetime import datetime, timedelta, timezone

from flask import (
    render_template, request, redirect, url_for, flash, jsonify, abort,
    send_file, Response,
)
from werkzeug.utils import secure_filename

import config
import db
from helpers import (
    get_current_user, rate_limit,
    render_markdown,
    is_public_mode_active,
    is_joke_audio_extension, get_real_audio_ext,
    get_joke_success_message, get_joke_fail_message, convert_joke_audio,
    t,
)
from helpers._text import slugify
from helpers._diff import compute_diff_html
from helpers._validation import _is_valid_hex_color, allowed_attachment, get_effective_max_upload_size
from helpers._validation import _safe_ext as _validation_safe_ext
from wiki_logger import log_action
from sync import notify_file_upload, notify_file_deleted
from routes.chat import attachment_download_mimetype


def _has_global_kanban_access(user, settings):
    """Return True if the user has global kanban access based on site settings."""
    if not user:
        return False
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    if settings and settings.get("kanban_open_access"):
        return True
    access = "admin"
    if settings and "kanban_access" in settings.keys():
        access = settings["kanban_access"] or "admin"
    if access == "editor" and role == "editor":
        return True
    if access == "all":
        return True
    return False


def _kanban_access_required(f):
    """Decorator that checks the user has kanban access based on site settings.

    Access levels stored in ``site_settings.kanban_access``:
    - ``"admin"``, only admins (default)
    - ``"editor"``: admins + editors
    - ``"all"``: all logged-in users

    Users who do not meet the global access level but have been individually
    invited to at least one board (user-specific share) are also allowed
    through so they can access their assigned boards.
    """
    import functools

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        """Resolve the board and confirm the caller may reach it."""
        user = get_current_user()
        if not user:
            settings = db.get_site_settings()
            if (
                is_public_mode_active()
                and settings
                and settings.get("kanban_public_access_enabled")
                and request.method in ("GET", "HEAD", "OPTIONS")
            ):
                return f(*args, **kwargs)
            return redirect(url_for("login"))

        settings = db.get_site_settings()

        # Primary permission check (honors plugin state)
        if not db.has_permission(user, "kanban.view"):
            abort(404)

        if _has_global_kanban_access(user, settings):
            return f(*args, **kwargs)
        # Allow individually invited users through even if their role is
        # not globally permitted: they can only see boards they are
        # explicitly shared on (enforced in the individual route handlers).
        if db.kanban_user_has_any_share(user["id"]):
            return f(*args, **kwargs)
        flash(t("flash.you_do_not_have_the_required_permissions_to_23b4b4"), "error")
        abort(403)
    return wrapper


def _can_write_global(user, settings):
    """Return True if the user has global write access to kanban boards."""
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    if settings and settings.get("kanban_open_access"):
        return True
    write_access = "admin"
    if settings and "kanban_write_access" in settings.keys():
        write_access = settings["kanban_write_access"] or "admin"
    if write_access == "editor" and role == "editor":
        return True
    if write_access == "all":
        return True
    return False


def user_can_view_kanban_board(user, board, settings=None):
    """Return True if the logged-in *user* may read *board* and its contents.

    This is the single read check for a board: the board page, tickets,
    comments, attachments, history, activity, sync, the page embeds in
    routes/api.py and the personal data export all go through it, so none of
    them can show more than ``/kanban/<id>`` does.

    Users with global kanban access get the board's visibility and share
    rules, where ``public`` means "visible to everyone with global access".
    Everyone else only reaches boards they created or were individually
    invited to.  The db helper ``kanban_user_can_view_board`` alone is not
    enough, because it treats ``public`` as visible to any account.
    """
    if not user or not board:
        return False
    # Also covers the plugin being switched off, since has_permission
    # refuses permissions of disabled plugins.
    if not db.has_permission(user, "kanban.view"):
        return False
    if settings is None:
        settings = db.get_site_settings()
    if _has_global_kanban_access(user, settings):
        return db.kanban_user_can_view_board(user, board)
    return db.kanban_user_can_view_board_individual(user, board)


def _can_write(user, settings, board):
    """Return True if *user* may change *board*.

    The global ``kanban_write_access`` setting only applies to boards the
    user can view, as docs/kanban.md describes; a writer must not be able to
    edit a private board that is hidden from them.  Explicit write shares,
    the board creator and admins pass through ``kanban_user_can_write_board``.
    Board creation has no board yet and uses ``_can_write_global``.
    """
    if not board or not user_can_view_kanban_board(user, board, settings):
        return False
    if _can_write_global(user, settings):
        return True
    return db.kanban_user_can_write_board(user, board)


def _is_board_owner(user, board):
    """Return True if user is the board creator or an admin."""
    if not user or not board:
        return False
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    return board["created_by"] == user["id"]


def _resolve_ticket_board(ticket):
    """Return the board row that owns *ticket*, or ``None``.

    Follows the ticket → column → board chain.  Used by read-only API
    endpoints to enforce per-board access checks.
    """
    col = db.kanban_get_column(ticket["column_id"])
    if not col:
        return None
    return db.kanban_get_board(col["board_id"])


def _user_can_view_ticket(user, ticket):
    """Return True if *user* may view *ticket*, judged by its parent board."""
    board = _resolve_ticket_board(ticket)
    if not board:
        return False
    return user_can_view_kanban_board(user, board)


def _get_kanban_access(settings):
    """Return the current global ``kanban_access`` value from site settings."""
    if settings and "kanban_access" in settings.keys():
        return settings["kanban_access"] or "admin"
    return "admin"


def _get_kanban_write_access(settings):
    """Return the current global ``kanban_write_access`` value from site settings."""
    if settings and "kanban_write_access" in settings.keys():
        return settings["kanban_write_access"] or "admin"
    return "admin"


_KANBAN_PRIORITY_SHORTHAND = {
    "low": "low",
    "medium": "medium",
    "med": "medium",
    "high": "high",
    "critical": "critical",
    "crit": "critical",
}
_KANBAN_COLOR_SHORTHAND = {
    "red": "#e91e63",
    "orange": "#ff9800",
    "yellow": "#ffeb3b",
    "green": "#4caf50",
    "blue": "#2196f3",
    "purple": "#9c27b0",
}
_KANBAN_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$")


def _normalize_ticket_labels(raw_labels):
    """Return a deduplicated, validated list of kanban ticket labels."""
    if raw_labels is None:
        return []
    if isinstance(raw_labels, str):
        parts = re.split(r"[\s,]+", raw_labels.strip())
    elif isinstance(raw_labels, list):
        parts = raw_labels
    else:
        parts = []
    labels = []
    seen = set()
    for item in parts:
        label = str(item or "").strip()
        if label.startswith("+"):
            label = label[1:]
        if not label:
            continue
        label = label[:40]
        if not _KANBAN_LABEL_RE.match(label):
            continue
        lowered = label.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        labels.append(label)
    return labels


def _ticket_labels_from_row(ticket):
    """Return the normalised label list stored on *ticket*."""
    raw = ""
    try:
        raw = ticket["labels"]
    except (KeyError, TypeError):
        raw = ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = raw
    return _normalize_ticket_labels(data)


def _parse_kanban_due_shorthand(raw_value):
    """Resolve shorthand due-date tokens to ISO dates when possible."""
    value = (raw_value or "").strip().lower()
    if not value:
        return None
    today = datetime.now(timezone.utc).date()
    if value == "today":
        return today.isoformat()
    if value == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if value in {"nextweek", "next-week", "next_week"}:
        return (today + timedelta(days=7)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return None


def _parse_ticket_shorthand(raw_title):
    """Extract kanban shorthand metadata from a raw title string."""
    parts = str(raw_title or "").split()
    title_parts = []
    labels = []
    assignee_usernames = []
    priority = None
    color = None
    due_date = None
    for part in parts:
        lower = part.lower()
        if part.startswith("@") and len(part) > 1:
            assignee_usernames.append(part[1:])
            continue
        if part.startswith("+") and len(part) > 1:
            labels.append(part[1:])
            continue
        if part.startswith("!") and len(part) > 1:
            resolved = _KANBAN_PRIORITY_SHORTHAND.get(lower[1:])
            if resolved:
                priority = resolved
                continue
        if lower.startswith("color:"):
            value = lower.split(":", 1)[1]
            resolved = _KANBAN_COLOR_SHORTHAND.get(value, part.split(":", 1)[1])
            if _is_valid_hex_color(resolved):
                color = resolved
                continue
        if lower.startswith("due:"):
            resolved = _parse_kanban_due_shorthand(part.split(":", 1)[1])
            if resolved:
                due_date = resolved
                continue
        title_parts.append(part)
    return {
        "title": " ".join(title_parts).strip(),
        "labels": _normalize_ticket_labels(labels),
        "assignee_usernames": [u[:64] for u in assignee_usernames if re.fullmatch(r"[A-Za-z0-9_-]+", u or "")],
        "priority": priority,
        "color": color,
        "due_date": due_date,
    }


def _serialize_ticket_payload(ticket, include_description_html=False):
    """Return a JSON-serialisable kanban ticket payload."""
    if not ticket:
        return None
    creator = db.get_user_by_id(ticket["created_by"]) if ticket["created_by"] else None
    assignee = db.get_user_by_id(ticket["assigned_to"]) if ticket["assigned_to"] else None
    assignees_rows = db.kanban_list_ticket_assignees(ticket["id"])
    payload = {
        "id": ticket["id"],
        "column_id": ticket["column_id"],
        "title": ticket["title"],
        "description": ticket["description"],
        "priority": ticket["priority"],
        "assigned_to": ticket["assigned_to"],
        "assigned_to_username": assignee["username"] if assignee else None,
        "assignees": [{"id": r["id"], "username": r["username"]} for r in assignees_rows],
        "created_by": ticket["created_by"],
        "created_by_username": creator["username"] if creator else None,
        "sort_order": ticket["sort_order"],
        "created_at": ticket["created_at"],
        "due_date": ticket["due_date"] if "due_date" in ticket.keys() else None,
        "color": ticket["color"] if "color" in ticket.keys() else "",
        "labels": _ticket_labels_from_row(ticket),
        "attachment_count": ticket["attachment_count"] if "attachment_count" in ticket.keys() else 0,
    }
    if include_description_html:
        payload["description_html"] = render_markdown(ticket["description"] or "")
    return payload


def _remove_kanban_attachment_files(filenames):
    """Remove physical attachment files from the kanban attachment folder.

    Validates each path stays within the attachment root to prevent path
    traversal.  Silently ignores missing files or OS errors.
    """
    attach_root = os.path.abspath(config.KANBAN_ATTACHMENT_FOLDER)
    for fname in filenames:
        fpath = os.path.abspath(os.path.join(attach_root, fname))
        if os.path.commonpath([attach_root, fpath]) == attach_root and os.path.isfile(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass



def _kanban_emit(board_id, op_type, payload=None, user=None):
    """Append a realtime kanban event.  Failures must never break the route.

    ``X-Kanban-Session`` is read from the active request so collaborators see
    each other's writes but not their own echoes when polling ``/sync``.
    """
    if not board_id:
        return
    try:
        session_id = (request.headers.get("X-Kanban-Session") or "").strip()[:64]
        user_id = None
        if user is not None:
            try:
                user_id = user["id"]
            except (KeyError, TypeError):
                user_id = getattr(user, "id", None)
        db.kanban_append_event(
            board_id, op_type,
            payload=payload or {},
            by_user_id=user_id,
            by_session=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Realtime sync is best-effort; never propagate failures back to the
        # caller's mutation path.
        from wiki_logger import get_logger
        get_logger().warning("Kanban realtime emit failed: %s", exc, exc_info=True)


def _kanban_record_history(board_id, edit_message, user=None, is_revert=False):
    """Record a board-history snapshot for *board_id*.

    Failures are swallowed so a history-table issue never breaks the
    primary mutation.  Snapshots dedupe against the previous entry, so
    no-op writes do not pollute the history.
    """
    if not board_id:
        return
    try:
        user_id = None
        if user is not None:
            try:
                user_id = user["id"]
            except (KeyError, TypeError):
                user_id = getattr(user, "id", None)
        db.kanban_record_board_history(
            board_id,
            edited_by=user_id,
            edit_message=edit_message or "",
            is_revert=is_revert,
        )
    except Exception as exc:  # noqa: BLE001
        from wiki_logger import get_logger
        get_logger().warning("Kanban history record failed: %s", exc, exc_info=True)


def register_kanban_routes(app):
    """Register kanban board routes on *app*."""

    @app.route("/kanban")
    @_kanban_access_required
    def kanban_list():
        """List all kanban boards the user can view."""
        user = get_current_user()
        settings = db.get_site_settings()
        if not user:
            boards = db.kanban_list_public_boards()
            return render_template("kanban/list.html", boards=boards, can_write=False)
        has_global = _has_global_kanban_access(user, settings)
        open_access = bool(settings and settings.get("kanban_open_access"))
        if has_global:
            boards = db.kanban_list_boards_for_user(user)
            can_write = _can_write_global(user, settings)
        else:
            # Individually invited user: show only explicitly shared boards
            boards = db.kanban_list_boards_for_individual_user(user["id"])
            can_write = False  # Individually invited users cannot create boards
        # Apply per-user or global ordering
        order_key = None if open_access else user["id"]
        ordered_ids = db.kanban_get_user_board_order(order_key)
        if ordered_ids:
            board_map = {b["id"]: b for b in boards}
            ordered = [board_map[bid] for bid in ordered_ids if bid in board_map]
            # Append any boards not yet in the user's order (new boards)
            seen = set(ordered_ids)
            for b in boards:
                if b["id"] not in seen:
                    ordered.append(b)
            boards = ordered
        list_order_version = (settings or {}).get("list_order_version", 0)
        return render_template("kanban/list.html", boards=boards,
                               can_write=can_write, open_access=open_access,
                               list_order_version=list_order_version)

    @app.route("/api/kanban/board-order", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_board_order():
        """Save the new board ordering for the current user (or globally
        when kanban_open_access is enabled)."""
        user = get_current_user()
        if not user:
            return jsonify({"error": "Not logged in"}), 401
        settings = db.get_site_settings()
        data = request.get_json(silent=True) or {}
        board_ids = data.get("board_ids")
        if not isinstance(board_ids, list):
            return jsonify({"error": "board_ids must be a list"}), 400
        open_access = bool(settings and settings.get("kanban_open_access"))
        order_key = None if open_access else user["id"]
        db.kanban_save_user_board_order(order_key, board_ids)
        if open_access:
            # Bump the global version counter so other users pick up the change
            v = (settings.get("list_order_version") or 0) + 1
            db.update_site_settings(list_order_version=v)
        else:
            v = settings.get("list_order_version") or 0
        return jsonify({"ok": True, "list_order_version": v})

    @app.route("/api/kanban/list-order-version")
    @rate_limit(120, 60)
    def api_kanban_list_order_version():
        """Return the board list_order_version so clients can detect reorders."""
        settings = db.get_site_settings()
        v = (settings or {}).get("list_order_version", 0)
        return jsonify({"list_order_version": v})

    @app.route("/kanban/create", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def kanban_create_board():
        """Create a new kanban board with default columns."""
        user = get_current_user()
        settings = db.get_site_settings()
        if not user:
            return redirect(url_for("login"))
        if not _can_write_global(user, settings):
            flash(t("flash.you_do_not_have_the_required_permissions_to_11ac4e"), "error")
            return redirect(url_for("kanban_list"))
        title = request.form.get("title", "").strip()
        if not title:
            flash(t("flash.board_title_is_required_to_continue"), "error")
            return redirect(url_for("kanban_list"))
        if len(title) > 10000:
            flash(t("flash.board_title_cannot_exceed_10000_characters"), "error")
            return redirect(url_for("kanban_list"))
        description = request.form.get("description", "").strip()[:10000]
        board_id = db.kanban_create_board(title, description, user["id"])
        # Create default columns
        db.kanban_create_column(board_id, "To Do", 0)
        db.kanban_create_column(board_id, "In Progress", 1)
        db.kanban_create_column(board_id, "Done", 2)
        db.kanban_add_activity_log(board_id, user["id"], "board_created", f"Created board '{title}'")
        _kanban_record_history(board_id, "Created board", user=user)
        log_action("kanban_create_board", request, user=user, board_title=title)
        flash(t("flash.board_has_been_successfully_created"), "success")
        return redirect(url_for("kanban_board", board_id=board_id))

    @app.route("/kanban/<int:board_id>")
    @_kanban_access_required
    def kanban_board(board_id):
        """View a single kanban board with its columns and tickets."""
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        user = get_current_user()
        settings = db.get_site_settings()
        if not user:
            # Anonymous visitors only get this far in public mode with
            # kanban_public_access_enabled (see _kanban_access_required).
            can_view = board["visibility"] == "public"
        else:
            can_view = user_can_view_kanban_board(user, board, settings)
        if not can_view:
            flash(t("flash.you_do_not_have_the_required_permissions_to_cab781"), "error")
            return redirect(url_for("kanban_list"))
        columns = db.kanban_list_columns(board_id)
        # Build columns with their tickets, enriched with the multi-assignee
        # list so card badges can render every assignee (not just the primary).
        columns_data = []
        for col in columns:
            tickets = db.kanban_list_tickets(col["id"])
            enriched = []
            for tk in tickets:
                tk_dict = dict(tk)
                tk_dict["assignees"] = [
                    {"id": a["id"], "username": a["username"]}
                    for a in db.kanban_list_ticket_assignees(tk["id"])
                ]
                tk_dict["labels"] = _ticket_labels_from_row(tk)
                enriched.append(tk_dict)
            columns_data.append({
                "id": col["id"],
                "title": col["title"],
                "sort_order": col["sort_order"],
                "tickets": enriched,
            })
        can_write = _can_write(user, settings, board) if user else False
        is_owner = _is_board_owner(user, board)
        kanban_access = _get_kanban_access(settings)
        kanban_write_access = _get_kanban_write_access(settings)
        # Resolve board creator username
        creator = db.get_user_by_id(board["created_by"])
        creator_username = creator["username"] if creator else "Unknown"
        # Populate assignee list with only users who have access to this board
        if can_write or is_owner:
            users_list = db.kanban_get_board_accessible_users(board_id, kanban_access)
        else:
            users_list = []
        share_users_list = db.list_users() if is_owner else []
        shares = db.kanban_get_board_shares(board_id) if is_owner else []
        share_user_names = {u["id"]: u["username"] for u in share_users_list}
        return render_template("kanban/board.html", board=board,
                               columns=columns_data, can_write=can_write,
                               is_owner=is_owner, users_list=users_list,
                               share_users_list=share_users_list,
                               shares=shares,
                               share_user_names=share_user_names,
                               kanban_access=kanban_access,
                               kanban_write_access=kanban_write_access,
                               creator_username=creator_username,
                               render_markdown=render_markdown)

    @app.route("/kanban/<int:board_id>/edit", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def kanban_edit_board(board_id):
        """Update a board's title and description."""
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            flash(t("flash.you_do_not_have_the_required_permissions_to_c0ae90"), "error")
            return redirect(url_for("kanban_board", board_id=board_id))
        title = request.form.get("title", "").strip()
        if not title:
            flash(t("flash.board_title_is_required_to_continue"), "error")
            return redirect(url_for("kanban_board", board_id=board_id))
        if len(title) > 10000:
            flash(t("flash.board_title_cannot_exceed_10000_characters"), "error")
            return redirect(url_for("kanban_board", board_id=board_id))
        description = request.form.get("description", "").strip()[:10000]
        db.kanban_update_board(board_id, title=title, description=description)
        _kanban_record_history(board_id, "Edited board info", user=user)
        log_action("kanban_edit_board", request, user=user, board_id=board_id)
        flash(t("flash.board_has_been_successfully_updated"), "success")
        return redirect(url_for("kanban_board", board_id=board_id))

    @app.route("/kanban/<int:board_id>/delete", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def kanban_delete_board(board_id):
        """Delete a board and all its columns and tickets."""
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        if not _is_board_owner(user, board):
            flash(t("flash.you_do_not_have_the_required_permissions_to_e5a49a"), "error")
            return redirect(url_for("kanban_list"))
        orphaned_files = db.kanban_delete_board(board_id)
        _remove_kanban_attachment_files(orphaned_files)
        log_action("kanban_delete_board", request, user=user, board_id=board_id)
        flash(t("flash.board_has_been_successfully_deleted"), "success")
        return redirect(url_for("kanban_list"))

    @app.route("/kanban/<int:board_id>/share", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def kanban_board_share(board_id):
        """Manage board sharing permissions via simple form POST."""
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        if not _is_board_owner(user, board):
            flash(t("flash.you_do_not_have_the_required_permissions_to_697be6"), "error")
            abort(403)
        settings = db.get_site_settings()
        kanban_access = _get_kanban_access(settings)
        action = request.form.get("action", "")

        if action == "add_user":
            target_user_id = request.form.get("user_id", "").strip()
            access_level = request.form.get("access_level", "view")
            if access_level not in ("view", "write"):
                access_level = "view"
            if not target_user_id or not db.get_user_by_id(target_user_id):
                flash(t("flash.please_select_a_valid_user"), "error")
                return redirect(url_for("kanban_board", board_id=board_id))
            db.kanban_set_board_share(board_id, "user", target_user_id, access_level)
            flash(t("flash.permission_for_user_has_been_successfully_set"), "success")

        elif action == "add_role":
            role = request.form.get("role", "")
            access_level = request.form.get("access_level", "view")
            if access_level not in ("view", "write"):
                access_level = "view"
            if role not in ("user", "editor", "admin"):
                flash(t("flash.invalid_role"), "error")
                return redirect(url_for("kanban_board", board_id=board_id))
            # Only allow role shares for roles with global kanban access
            allowed_roles = {"admin"}
            if kanban_access == "editor":
                allowed_roles.add("editor")
            elif kanban_access == "all":
                allowed_roles.update({"editor", "user"})
            if role not in allowed_roles:
                flash(t("flash.cannot_share_with_role_role_it_does_not", role=role), "error")
                return redirect(url_for("kanban_board", board_id=board_id))
            db.kanban_set_board_share(board_id, "role", role, access_level)
            flash(t("flash.permission_for_role_role_has_been_successfully_set", role=role), "success")

        elif action == "remove_user":
            target_user_id = request.form.get("user_id", "").strip()
            if target_user_id:
                db.kanban_remove_board_share(board_id, "user", target_user_id)
                flash(t("flash.user_permission_has_been_successfully_removed"), "success")

        elif action == "remove_role":
            role = request.form.get("role", "")
            if role:
                db.kanban_remove_board_share(board_id, "role", role)
                flash(t("flash.role_permission_has_been_successfully_removed"), "success")

        elif action == "set_visibility":
            visibility = request.form.get("visibility", "private")
            if visibility in ("private", "shared", "public"):
                db.kanban_set_board_visibility(board_id, visibility)
                flash(t("flash.board_visibility_has_been_successfully_updated"), "success")

        elif action == "transfer_ownership":
            target_user_id = request.form.get("user_id", "").strip()
            target_user = db.get_user_by_id(target_user_id)
            if not target_user:
                flash(t("flash.please_select_a_valid_user"), "error")
                return redirect(url_for("kanban_board", board_id=board_id))
            db.kanban_update_board(board_id, created_by=target_user_id)
            log_action("kanban_transfer_ownership", request, user=user, board_id=board_id, new_owner=target_user_id)
            flash(t("flash.board_ownership_has_been_successfully_transferred_to_usernam", username=target_user['username']), "success")
            return redirect(url_for("kanban_list"))

        else:
            flash(t("flash.unknown_action"), "error")

        # Clean up assignees who lost access after sharing changes
        db.kanban_remove_assignees_without_board_access(board_id, kanban_access)
        log_action("kanban_update_sharing", request, user=user, board_id=board_id)
        return redirect(url_for("kanban_board", board_id=board_id))

    # From here down the handlers answer JSON for the board's front-end.  The
    # routes above render templates and redirect, so the two groups differ in
    # how they report failures: abort/flash above, error payloads below.
    @app.route("/api/kanban/<int:board_id>/columns", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_create_column(board_id):
        """API: create a new column in a board."""
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "Column title is required"}), 400
        if len(title) > 10000:
            return jsonify({"error": "Column title cannot exceed 10000 characters"}), 400
        # Auto sort_order: next available position
        existing = db.kanban_list_columns(board_id)
        sort_order = len(existing)
        col_id = db.kanban_create_column(board_id, title, sort_order)
        log_action("kanban_create_column", request, user=user, board_id=board_id, title=title)
        _kanban_emit(board_id, "column_created",
                     payload={"column_id": col_id, "title": title}, user=user)
        _kanban_record_history(board_id, f"Created column '{title}'", user=user)
        return jsonify({"id": col_id, "title": title, "sort_order": sort_order}), 201

    @app.route("/api/kanban/columns/<int:column_id>", methods=["PUT"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_update_column(column_id):
        """API: update a column's title."""
        user = get_current_user()
        col = db.kanban_get_column(column_id)
        if not col:
            return jsonify({"error": "Column not found"}), 404
        board = db.kanban_get_board(col["board_id"])
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "Column title is required"}), 400
        if len(title) > 10000:
            return jsonify({"error": "Column title cannot exceed 10000 characters"}), 400
        db.kanban_update_column(column_id, title=title)
        log_action("kanban_update_column", request, user=user, column_id=column_id, title=title)
        _kanban_emit(col["board_id"], "column_updated",
                     payload={"column_id": column_id, "title": title}, user=user)
        _kanban_record_history(col["board_id"], f"Renamed column to '{title}'", user=user)
        return jsonify({"ok": True})

    @app.route("/api/kanban/columns/<int:column_id>", methods=["DELETE"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_delete_column(column_id):
        """API: delete a column and all its tickets."""
        user = get_current_user()
        col = db.kanban_get_column(column_id)
        if not col:
            return jsonify({"error": "Column not found"}), 404
        board = db.kanban_get_board(col["board_id"])
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        orphaned_files = db.kanban_delete_column(column_id)
        _remove_kanban_attachment_files(orphaned_files)
        log_action("kanban_delete_column", request, user=user, column_id=column_id, title=col["title"])
        _kanban_emit(col["board_id"], "column_deleted",
                     payload={"column_id": column_id}, user=user)
        _kanban_record_history(col["board_id"], f"Deleted column '{col['title']}'", user=user)
        return jsonify({"ok": True})

    @app.route("/api/kanban/<int:board_id>/columns/reorder", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_reorder_columns(board_id):
        """API: reorder columns within a board."""
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        order = data.get("order", [])
        if not isinstance(order, list):
            return jsonify({"error": "Invalid order format"}), 400
        db.kanban_update_columns_sort_order(board_id, order)
        db.kanban_add_activity_log(board_id, user["id"], "columns_reordered", "Columns reordered")
        log_action("kanban_reorder_columns", request, user=user, board_id=board_id, order=order)
        _kanban_emit(board_id, "columns_reordered",
                     payload={"order": order}, user=user)
        _kanban_record_history(board_id, "Reordered columns", user=user)
        return jsonify({"ok": True})

    @app.route("/api/kanban/columns/<int:column_id>/tickets", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_create_ticket(column_id):
        """API: create a new ticket in a column."""
        user = get_current_user()
        col = db.kanban_get_column(column_id)
        if not col:
            return jsonify({"error": "Column not found"}), 404
        board = db.kanban_get_board(col["board_id"])
        if not board:
            return jsonify({"error": "Board not found"}), 404
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        shorthand = _parse_ticket_shorthand(data.get("title") or "")
        title = shorthand["title"]
        if not title:
            return jsonify({"error": "Ticket title is required"}), 400
        if len(title) > 10000:
            return jsonify({"error": "Ticket title cannot exceed 10000 characters"}), 400
        description = (data.get("description") or "").strip()[:10000]
        priority = data.get("priority") or shorthand["priority"] or "medium"
        if priority not in ("low", "medium", "high", "critical"):
            priority = "medium"
        # Multi-assignee support: ``assignees`` is the new canonical list of
        # user IDs.  ``assigned_to`` (single string) stays for back-compat
        # and is treated as the first/primary assignee.
        kanban_access = _get_kanban_access(settings)
        raw_assignees = []
        if isinstance(data.get("assignees"), list):
            raw_assignees = [str(x).strip() for x in data["assignees"] if x]
        elif shorthand["assignee_usernames"]:
            raw_assignees = [
                user_row["id"]
                for user_row in (db.get_user_by_username(username) for username in shorthand["assignee_usernames"])
                if user_row
            ]
        legacy_assigned = (data.get("assigned_to") or "").strip() or None
        if legacy_assigned and legacy_assigned not in raw_assignees:
            raw_assignees.insert(0, legacy_assigned)
        valid_assignees = []
        for uid in raw_assignees:
            if not db.get_user_by_id(uid):
                continue
            if not db.kanban_user_has_board_access(uid, board["id"], kanban_access):
                continue
            if uid not in valid_assignees:
                valid_assignees.append(uid)
        # Auto sort_order
        existing = db.kanban_list_tickets(column_id)
        sort_order = len(existing)
        ticket_id = db.kanban_create_ticket(column_id, title, description,
                                            user["id"], priority, sort_order)
        db.kanban_add_activity_log(board["id"], user["id"], "ticket_created", f"Created ticket '{title}'")
        if valid_assignees:
            db.kanban_set_ticket_assignees(ticket_id, valid_assignees)
        assigned_to = valid_assignees[0] if valid_assignees else None
        # Optional extra fields
        extra_updates = {}
        due_date = (data.get("due_date") or "").strip() or shorthand["due_date"] or None
        if due_date:
            extra_updates["due_date"] = due_date
        color = (data.get("color") or "").strip() or shorthand["color"] or ""
        if color and _is_valid_hex_color(color):
            extra_updates["color"] = color
        labels = _normalize_ticket_labels(data.get("labels") if "labels" in data else shorthand["labels"])
        if labels:
            extra_updates["labels"] = json.dumps(labels)
        if extra_updates:
            db.kanban_update_ticket(ticket_id, **extra_updates)
        ticket = db.kanban_get_ticket(ticket_id)
        log_action("kanban_create_ticket", request, user=user, ticket_id=ticket_id, title=title, column_id=column_id)
        _kanban_emit(board["id"], "ticket_created",
                     payload={"ticket_id": ticket_id, "column_id": column_id,
                              "title": title}, user=user)
        _kanban_record_history(board["id"], f"Created ticket '{title}'", user=user)
        return jsonify(_serialize_ticket_payload(ticket or {
            "id": ticket_id,
            "column_id": column_id,
            "title": title,
            "description": description,
            "priority": priority,
            "assigned_to": assigned_to,
            "created_by": user["id"],
            "sort_order": sort_order,
            "created_at": None,
            "due_date": due_date,
            "color": color,
            "labels": json.dumps(labels),
            "attachment_count": 0,
        })), 201

    @app.route("/api/kanban/tickets/<int:ticket_id>", methods=["GET"])
    @_kanban_access_required
    def api_kanban_get_ticket(ticket_id):
        """API: return ticket details including rendered description."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        if not _user_can_view_ticket(user, ticket):
            return jsonify({"error": "Permission denied"}), 403
        return jsonify(_serialize_ticket_payload(ticket, include_description_html=True))

    @app.route("/api/kanban/tickets/<int:ticket_id>", methods=["PUT"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_update_ticket(ticket_id):
        """API: update ticket fields (title, description, priority, assigned_to)."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        col = db.kanban_get_column(ticket["column_id"])
        board = db.kanban_get_board(col["board_id"]) if col else None
        if not board:
            return jsonify({"error": "Board not found"}), 404
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        updates = {}
        if "title" in data:
            shorthand = _parse_ticket_shorthand(data["title"] or "")
            title = shorthand["title"]
            if not title:
                return jsonify({"error": "Ticket title is required"}), 400
            if len(title) > 10000:
                return jsonify({"error": "Ticket title cannot exceed 10000 characters"}), 400
            updates["title"] = title
            if "priority" not in data and shorthand["priority"]:
                updates["priority"] = shorthand["priority"]
            if "due_date" not in data and shorthand["due_date"]:
                updates["due_date"] = shorthand["due_date"]
            if "color" not in data and shorthand["color"]:
                updates["color"] = shorthand["color"]
            if "labels" not in data and shorthand["labels"]:
                updates["labels"] = json.dumps(shorthand["labels"])
        if "description" in data:
            new_desc = (data["description"] or "").strip()[:10000]
            old_desc = ticket["description"] or ""
            if new_desc != old_desc:
                db.kanban_add_ticket_history_entry(ticket_id, old_desc, new_desc, user["id"])
            updates["description"] = new_desc
        if "priority" in data:
            priority = data["priority"]
            if priority not in ("low", "medium", "high", "critical"):
                return jsonify({"error": "Invalid priority"}), 400
            updates["priority"] = priority
        # Multi-assignee write: prefer ``assignees`` (list); fall back to
        # legacy single ``assigned_to`` for clients that haven't been
        # updated yet.
        if "assignees" in data:
            raw = data["assignees"] if isinstance(data["assignees"], list) else []
            kanban_access = _get_kanban_access(settings)
            valid_assignees = []
            for uid in raw:
                if not uid:
                    continue
                uid = str(uid).strip()
                if not uid or uid in valid_assignees:
                    continue
                if not db.get_user_by_id(uid):
                    return jsonify({"error": "Assigned user not found"}), 400
                if not db.kanban_user_has_board_access(uid, board["id"], kanban_access):
                    return jsonify({"error": "Assigned user does not have access to this board"}), 400
                valid_assignees.append(uid)
            db.kanban_set_ticket_assignees(ticket_id, valid_assignees)
            updates["_assignees_changed"] = True  # log marker only
        elif "assigned_to" in data:
            assigned = (data["assigned_to"] or "").strip() or None
            if assigned:
                if not db.get_user_by_id(assigned):
                    return jsonify({"error": "Assigned user not found"}), 400
                kanban_access = _get_kanban_access(settings)
                if not db.kanban_user_has_board_access(assigned, board["id"], kanban_access):
                    return jsonify({"error": "Assigned user does not have access to this board"}), 400
            db.kanban_set_ticket_assignees(ticket_id, [assigned] if assigned else [])
            updates["_assignees_changed"] = True  # log marker only
        elif "title" in data:
            shorthand = _parse_ticket_shorthand(data["title"] or "")
            if shorthand["assignee_usernames"]:
                resolved_ids = []
                kanban_access = _get_kanban_access(settings)
                for username in shorthand["assignee_usernames"]:
                    assignee_row = db.get_user_by_username(username)
                    if not assignee_row:
                        continue
                    if not db.kanban_user_has_board_access(assignee_row["id"], board["id"], kanban_access):
                        continue
                    if assignee_row["id"] not in resolved_ids:
                        resolved_ids.append(assignee_row["id"])
                db.kanban_set_ticket_assignees(ticket_id, resolved_ids)
                updates["_assignees_changed"] = True
        if "due_date" in data:
            updates["due_date"] = (data["due_date"] or "").strip() or None
        if "color" in data:
            color_val = (data["color"] or "").strip()
            if color_val and not _is_valid_hex_color(color_val):
                return jsonify({"error": "Invalid color format"}), 400
            updates["color"] = color_val
        if "labels" in data:
            updates["labels"] = json.dumps(_normalize_ticket_labels(data["labels"]))
        if updates:
            # ``_assignees_changed`` is a UI-only marker for activity logging.
            assignees_changed = updates.pop("_assignees_changed", False)
            if updates:
                db.kanban_update_ticket(ticket_id, **updates)
            changed_keys = list(updates.keys())
            if assignees_changed:
                changed_keys.append("assignees")
            if changed_keys:
                changed = ", ".join(changed_keys)
                db.kanban_add_activity_log(board["id"], user["id"], "ticket_updated", f"Updated '{ticket['title']}': {changed}")
                log_action("kanban_update_ticket", request, user=user, ticket_id=ticket_id, updates=changed_keys)
                _kanban_emit(board["id"], "ticket_updated",
                             payload={"ticket_id": ticket_id,
                                      "changed": changed_keys},
                             user=user)
                _kanban_record_history(
                    board["id"],
                    f"Updated ticket '{ticket['title']}' ({changed})",
                    user=user,
                )
        return jsonify({"ok": True})

    @app.route("/api/kanban/tickets/<int:ticket_id>", methods=["DELETE"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_delete_ticket(ticket_id):
        """API: delete a ticket."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        col = db.kanban_get_column(ticket["column_id"])
        board = db.kanban_get_board(col["board_id"]) if col else None
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        orphaned_files = db.kanban_delete_ticket(ticket_id)
        _remove_kanban_attachment_files(orphaned_files)
        db.kanban_add_activity_log(board["id"], user["id"], "ticket_deleted", f"Deleted ticket '{ticket['title']}'")
        log_action("kanban_delete_ticket", request, user=user, ticket_id=ticket_id, title=ticket["title"])
        _kanban_emit(board["id"], "ticket_deleted",
                     payload={"ticket_id": ticket_id}, user=user)
        _kanban_record_history(board["id"], f"Deleted ticket '{ticket['title']}'", user=user)
        return jsonify({"ok": True})

    @app.route("/api/kanban/tickets/<int:ticket_id>/move", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_move_ticket(ticket_id):
        """API: move a ticket to a different column and/or position."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        col = db.kanban_get_column(ticket["column_id"])
        board = db.kanban_get_board(col["board_id"]) if col else None
        if not board:
            return jsonify({"error": "Board not found"}), 404
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        target_column_id = data.get("column_id")
        if target_column_id is None:
            return jsonify({"error": "Target column is required"}), 400
        target_col = db.kanban_get_column(target_column_id)
        if not target_col:
            return jsonify({"error": "Target column not found"}), 404
        if target_col["board_id"] != board["id"]:
            return jsonify({"error": "Target column not found"}), 404
        sort_order = data.get("sort_order", 0)
        old_col_name = col["title"] if col else "?"
        new_col_name = target_col["title"] if target_col else "?"
        db.kanban_move_ticket(ticket_id, target_column_id, sort_order)
        if ticket["column_id"] != target_column_id:
            db.kanban_add_activity_log(
                board["id"], user["id"], "ticket_moved",
                f"Moved '{ticket['title']}' from '{old_col_name}' to '{new_col_name}'"
            )
        log_action("kanban_move_ticket", request, user=user, ticket_id=ticket_id,
                   from_column_id=ticket["column_id"], to_column_id=target_column_id, sort_order=sort_order)
        _kanban_emit(board["id"], "ticket_moved",
                     payload={"ticket_id": ticket_id,
                              "from_column_id": ticket["column_id"],
                              "to_column_id": target_column_id,
                              "sort_order": sort_order}, user=user)
        if ticket["column_id"] != target_column_id:
            _kanban_record_history(
                board["id"],
                f"Moved '{ticket['title']}' to '{new_col_name}'",
                user=user,
            )
        else:
            _kanban_record_history(
                board["id"],
                f"Reordered ticket '{ticket['title']}'",
                user=user,
            )
        return jsonify({"ok": True})

    @app.route("/api/kanban/columns/<int:column_id>/tickets/reorder", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_reorder_tickets(column_id):
        """API: reorder tickets within a column."""
        user = get_current_user()
        col = db.kanban_get_column(column_id)
        if not col:
            return jsonify({"error": "Column not found"}), 404
        board = db.kanban_get_board(col["board_id"])
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        order = data.get("order", [])
        if not isinstance(order, list):
            return jsonify({"error": "Invalid order format"}), 400
        db.kanban_update_tickets_sort_order(column_id, order)
        log_action("kanban_reorder_tickets", request, user=user, column_id=column_id, order=order)
        _kanban_emit(col["board_id"], "tickets_reordered",
                     payload={"column_id": column_id, "order": order},
                     user=user)
        _kanban_record_history(col["board_id"], "Reordered tickets", user=user)
        return jsonify({"ok": True})

    @app.route("/api/kanban/<int:board_id>/tickets/bulk", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_bulk_tickets(board_id):
        """API: perform a bulk action on a list of tickets in a board.

        Body: ``{"ticket_ids": [int, ...], "action": "assign" | "priority"
        | "move" | "delete" | "color" | "due_date", ...action-specific
        fields}``.

        Validates that every ticket belongs to *board_id* and the caller
        has write access before applying the change.  Returns
        ``{"updated": N}`` on success.
        """
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").strip()
        if not action:
            return jsonify({"error": "Action is required"}), 400
        raw_ids = data.get("ticket_ids", [])
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "ticket_ids must be a non-empty list"}), 400
        # Normalise + de-dupe ticket IDs.
        ticket_ids = []
        seen_ids = set()
        for raw in raw_ids:
            try:
                tid = int(raw)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid ticket id"}), 400
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            ticket_ids.append(tid)
        if len(ticket_ids) > 500:
            return jsonify({"error": "Too many tickets in a single request"}), 400
        # Validate every ticket belongs to this board.
        tickets = []
        for tid in ticket_ids:
            ticket = db.kanban_get_ticket(tid)
            if not ticket:
                return jsonify({"error": f"Ticket {tid} not found"}), 404
            col = db.kanban_get_column(ticket["column_id"])
            if not col or col["board_id"] != board["id"]:
                return jsonify({"error": "All tickets must belong to this board"}), 400
            tickets.append(ticket)

        if action == "assign":
            raw_assignees = data.get("assignees", [])
            if not isinstance(raw_assignees, list):
                return jsonify({"error": "assignees must be a list"}), 400
            kanban_access = _get_kanban_access(settings)
            valid_assignees = []
            for uid in raw_assignees:
                if not uid:
                    continue
                uid = str(uid).strip()
                if not uid or uid in valid_assignees:
                    continue
                if not db.get_user_by_id(uid):
                    return jsonify({"error": "Assigned user not found"}), 400
                if not db.kanban_user_has_board_access(uid, board["id"], kanban_access):
                    return jsonify({"error": "Assigned user does not have access to this board"}), 400
                valid_assignees.append(uid)
            for ticket in tickets:
                db.kanban_set_ticket_assignees(ticket["id"], valid_assignees)
            target = ", ".join(valid_assignees) if valid_assignees else "unassigned"
            db.kanban_add_activity_log(
                board["id"], user["id"], "tickets_bulk_assigned",
                f"Bulk assigned {len(tickets)} ticket(s): {target}",
            )
            log_action("kanban_bulk_assign", request, user=user, board_id=board_id,
                       ticket_ids=ticket_ids, assignees=valid_assignees)
            _kanban_record_history(
                board["id"],
                f"Bulk reassigned {len(tickets)} ticket(s)",
                user=user,
            )
            return jsonify({"updated": len(tickets)})

        if action == "priority":
            priority = data.get("priority")
            if priority not in ("low", "medium", "high", "critical"):
                return jsonify({"error": "Invalid priority"}), 400
            for ticket in tickets:
                db.kanban_update_ticket(ticket["id"], priority=priority)
            db.kanban_add_activity_log(
                board["id"], user["id"], "tickets_bulk_priority",
                f"Bulk set priority to '{priority}' on {len(tickets)} ticket(s)",
            )
            log_action("kanban_bulk_priority", request, user=user, board_id=board_id,
                       ticket_ids=ticket_ids, priority=priority)
            _kanban_record_history(
                board["id"],
                f"Bulk set priority to '{priority}' on {len(tickets)} ticket(s)",
                user=user,
            )
            return jsonify({"updated": len(tickets)})

        if action == "move":
            target_column_id = data.get("column_id")
            if target_column_id is None:
                return jsonify({"error": "Target column is required"}), 400
            try:
                target_column_id = int(target_column_id)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid target column"}), 400
            target_col = db.kanban_get_column(target_column_id)
            if not target_col or target_col["board_id"] != board["id"]:
                return jsonify({"error": "Target column not found"}), 404
            existing = db.kanban_list_tickets(target_column_id)
            next_sort = len(existing)
            for ticket in tickets:
                if ticket["column_id"] == target_column_id:
                    continue
                db.kanban_move_ticket(ticket["id"], target_column_id, next_sort)
                next_sort += 1
            db.kanban_add_activity_log(
                board["id"], user["id"], "tickets_bulk_moved",
                f"Bulk moved {len(tickets)} ticket(s) to '{target_col['title']}'",
            )
            log_action("kanban_bulk_move", request, user=user, board_id=board_id,
                       ticket_ids=ticket_ids, target_column_id=target_column_id)
            _kanban_record_history(
                board["id"],
                f"Bulk moved {len(tickets)} ticket(s) to '{target_col['title']}'",
                user=user,
            )
            return jsonify({"updated": len(tickets)})

        if action == "delete":
            orphaned = []
            for ticket in tickets:
                orphaned.extend(db.kanban_delete_ticket(ticket["id"]))
            _remove_kanban_attachment_files(orphaned)
            db.kanban_add_activity_log(
                board["id"], user["id"], "tickets_bulk_deleted",
                f"Bulk deleted {len(tickets)} ticket(s)",
            )
            log_action("kanban_bulk_delete_tickets", request, user=user,
                       board_id=board_id, ticket_ids=ticket_ids)
            _kanban_record_history(
                board["id"],
                f"Bulk deleted {len(tickets)} ticket(s)",
                user=user,
            )
            return jsonify({"updated": len(tickets)})

        if action == "color":
            color = (data.get("color") or "").strip()
            if color and not _is_valid_hex_color(color):
                return jsonify({"error": "Invalid color format"}), 400
            for ticket in tickets:
                db.kanban_update_ticket(ticket["id"], color=color)
            db.kanban_add_activity_log(
                board["id"], user["id"], "tickets_bulk_color",
                f"Bulk set color on {len(tickets)} ticket(s)",
            )
            log_action("kanban_bulk_color", request, user=user, board_id=board_id,
                       ticket_ids=ticket_ids, color=color)
            _kanban_record_history(
                board["id"],
                f"Bulk set color on {len(tickets)} ticket(s)",
                user=user,
            )
            return jsonify({"updated": len(tickets)})

        if action == "due_date":
            due_date = (data.get("due_date") or "").strip() or None
            for ticket in tickets:
                db.kanban_update_ticket(ticket["id"], due_date=due_date)
            db.kanban_add_activity_log(
                board["id"], user["id"], "tickets_bulk_due_date",
                f"Bulk set due date on {len(tickets)} ticket(s)",
            )
            log_action("kanban_bulk_due_date", request, user=user, board_id=board_id,
                       ticket_ids=ticket_ids, due_date=due_date)
            _kanban_record_history(
                board["id"],
                f"Bulk set due date on {len(tickets)} ticket(s)",
                user=user,
            )
            return jsonify({"updated": len(tickets)})

        return jsonify({"error": "Unknown action"}), 400

    @app.route("/api/kanban/<int:board_id>/columns/bulk", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_bulk_columns(board_id):
        """API: perform a bulk action on a list of columns in a board.

        Body: ``{"column_ids": [int, ...], "action": "delete"}``.

        Currently only ``delete`` is supported; deletion cascades to all
        tickets within the selected columns.
        """
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").strip()
        if not action:
            return jsonify({"error": "Action is required"}), 400
        raw_ids = data.get("column_ids", [])
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "column_ids must be a non-empty list"}), 400
        column_ids = []
        seen_col_ids = set()
        for raw in raw_ids:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid column id"}), 400
            if cid in seen_col_ids:
                continue
            seen_col_ids.add(cid)
            column_ids.append(cid)
        if len(column_ids) > 100:
            return jsonify({"error": "Too many columns in a single request"}), 400
        # Validate every column belongs to this board.
        cols = []
        for cid in column_ids:
            col = db.kanban_get_column(cid)
            if not col or col["board_id"] != board["id"]:
                return jsonify({"error": "All columns must belong to this board"}), 400
            cols.append(col)

        if action == "delete":
            orphaned = []
            titles = []
            for col in cols:
                orphaned.extend(db.kanban_delete_column(col["id"]))
                titles.append(col["title"])
            _remove_kanban_attachment_files(orphaned)
            db.kanban_add_activity_log(
                board["id"], user["id"], "columns_bulk_deleted",
                f"Bulk deleted {len(cols)} column(s): {', '.join(titles)}",
            )
            log_action("kanban_bulk_delete_columns", request, user=user,
                       board_id=board_id, column_ids=column_ids)
            _kanban_record_history(
                board["id"],
                f"Bulk deleted {len(cols)} column(s)",
                user=user,
            )
            return jsonify({"updated": len(cols)})

        return jsonify({"error": "Unknown action"}), 400

    @app.route("/api/kanban/<int:board_id>/settings", methods=["GET"])
    @_kanban_access_required
    def api_kanban_board_settings(board_id):
        """API: return the board's sharing settings.

        Includes global ``kanban_access`` and ``kanban_write_access`` so the
        frontend can disable role-share controls for roles that are not
        permitted by the site-wide settings.
        """
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        if not _is_board_owner(user, board):
            return jsonify({"error": "Permission denied"}), 403
        settings = db.get_site_settings()
        shares = db.kanban_get_board_shares(board_id)
        visibility = board["visibility"] if "visibility" in board.keys() else "private"
        creator = db.get_user_by_id(board["created_by"])
        shares_data = []
        for s in shares:
            entry = {
                "share_type": s["share_type"],
                "target": s["target"],
                "access_level": s["access_level"],
            }
            if s["share_type"] == "user":
                u = db.get_user_by_id(s["target"])
                entry["target_username"] = u["username"] if u else "Unknown"
            else:
                entry["target_username"] = None
            shares_data.append(entry)
        return jsonify({
            "visibility": visibility,
            "created_by": board["created_by"],
            "created_by_username": creator["username"] if creator else "Unknown",
            "shares": shares_data,
            "kanban_access": _get_kanban_access(settings),
            "kanban_write_access": _get_kanban_write_access(settings),
        })

    @app.route("/api/kanban/<int:board_id>/settings", methods=["PUT"])
    @_kanban_access_required
    @rate_limit()
    def api_kanban_update_board_settings(board_id):
        """API: update board visibility and sharing rules."""
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        if not _is_board_owner(user, board):
            return jsonify({"error": "Permission denied"}), 403
        settings = db.get_site_settings()
        kanban_access = _get_kanban_access(settings)
        # Determine which roles are allowed to have board-level shares based on
        # the global kanban_access setting.
        allowed_role_targets = {"admin"}
        if kanban_access == "editor":
            allowed_role_targets.add("editor")
        elif kanban_access == "all":
            allowed_role_targets.update({"editor", "user"})
        data = request.get_json(silent=True) or {}
        # Update visibility
        visibility = data.get("visibility")
        if visibility and visibility in ("private", "shared", "public"):
            db.kanban_set_board_visibility(board_id, visibility)
        # Update shares (replace all)
        shares = data.get("shares")
        if shares is not None:
            if not isinstance(shares, list):
                return jsonify({"error": "Invalid shares format"}), 400
            db.kanban_clear_board_shares(board_id)
            for share in shares:
                share_type = share.get("share_type", "")
                target = share.get("target", "")
                access_level = share.get("access_level", "view")
                if share_type not in ("role", "user"):
                    continue
                if access_level not in ("view", "write"):
                    access_level = "view"
                if share_type == "role":
                    # Only allow role shares for roles with global kanban access
                    if target not in allowed_role_targets:
                        continue
                if share_type == "user":
                    target = str(target).strip()
                    if not target or not db.get_user_by_id(target):
                        continue
                db.kanban_set_board_share(board_id, share_type, target, access_level)
            # After updating shares, remove any assignees who no longer have access
            db.kanban_remove_assignees_without_board_access(board_id, kanban_access)
        log_action("kanban_update_settings", request, user=user, board_id=board_id)
        return jsonify({"ok": True})

    def _safe_ext(filename):
        """Return the lowercase extension of filename, or '' if none."""
        if "." not in filename:
            return ""
        return filename.rsplit(".", 1)[1].lower()

    @app.route("/api/kanban/tickets/<int:ticket_id>/attachments", methods=["GET"])
    @_kanban_access_required
    def api_kanban_list_attachments(ticket_id):
        """API: list attachments for a ticket."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        if not _user_can_view_ticket(user, ticket):
            return jsonify({"error": "Permission denied"}), 403
        attachments = db.kanban_list_ticket_attachments(ticket_id)
        return jsonify([
            {
                "id": a["id"],
                "original_name": a["original_name"],
                "file_size": a["file_size"],
                "uploader_name": a["uploader_name"],
                "uploaded_at": a["uploaded_at"],
            }
            for a in attachments
        ])

    @app.route("/api/kanban/tickets/<int:ticket_id>/attachments", methods=["POST"])
    @_kanban_access_required
    @rate_limit(20, 60)
    def api_kanban_upload_attachment(ticket_id):
        """API: upload a file attachment to a ticket."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        col = db.kanban_get_column(ticket["column_id"])
        board = db.kanban_get_board(col["board_id"]) if col else None
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "No file selected"}), 400
        if not allowed_attachment(f.filename, settings=settings):
            return jsonify({"error": "File type not allowed"}), 400
        ext = _safe_ext(f.filename)
        if not ext:
            return jsonify({"error": "Invalid file extension"}), 400
        is_joke = is_joke_audio_extension(ext)
        stored_ext = get_real_audio_ext(ext) if is_joke else ext
        os.makedirs(config.KANBAN_ATTACHMENT_FOLDER, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}.{stored_ext}"
        attach_root = os.path.abspath(config.KANBAN_ATTACHMENT_FOLDER)
        filepath = os.path.abspath(os.path.join(attach_root, stored_name))
        if os.path.commonpath([attach_root, filepath]) != attach_root:
            return jsonify({"error": "Invalid upload path"}), 400
        max_size = get_effective_max_upload_size(settings)
        file_size = 0
        chunk_size = 64 * 1024
        _blob_buf = io.BytesIO()  # accumulate content for DB blob (dual-write)
        try:
            with open(filepath, "wb") as out:
                while True:
                    chunk = f.stream.read(chunk_size)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    if file_size > max_size:
                        out.close()
                        os.remove(filepath)
                        limit_mb = max_size // (1024 * 1024)
                        return jsonify({"error": f"File exceeds the {limit_mb} MB limit"}), 413
                    out.write(chunk)
                    _blob_buf.write(chunk)
        except OSError:
            if os.path.isfile(filepath):
                os.remove(filepath)
            return jsonify({"error": "Failed to save file"}), 500
        # Joke audio: convert mp5/mp7 → mp3 via ffmpeg
        if is_joke:
            ok, err = convert_joke_audio(filepath, filepath)
            if not ok:
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                return jsonify({
                    "error": get_joke_fail_message(ext),
                    "detail": err,
                    "joke": True,
                }), 400
        original_name = secure_filename(f.filename)
        # Dual-write: store buffered content in DB blob for DB-first reads.
        # For joke audio, re-read the converted file so the blob matches disk.
        blob_id = None
        try:
            blob_data = _blob_buf.getvalue()
            if is_joke:
                try:
                    with open(filepath, "rb") as cf:
                        blob_data = cf.read()
                except OSError:
                    pass
            blob_id = db.store_blob(stored_name, blob_data, "application/octet-stream")
        except Exception:
            pass  # blob storage is non-critical; disk remains source of truth
        attachment_id = db.kanban_add_ticket_attachment(ticket_id, stored_name, original_name, file_size, user["id"], blob_id=blob_id)
        log_action("kanban_upload_attachment", request, user=user, ticket_id=ticket_id, filename=original_name)
        notify_file_upload(stored_name, filepath, display_name=original_name)
        resp = {"id": attachment_id, "name": original_name, "size": file_size}
        if is_joke:
            resp["joke"] = True
            resp["joke_message"] = get_joke_success_message(ext)
        return jsonify(resp), 201

    @app.route("/api/kanban/attachments/<int:attachment_id>", methods=["DELETE"])
    @_kanban_access_required
    @rate_limit(20, 60)
    def api_kanban_delete_attachment(attachment_id):
        """API: delete a ticket attachment."""
        user = get_current_user()
        attachment = db.kanban_get_ticket_attachment(attachment_id)
        if not attachment:
            return jsonify({"error": "Not found"}), 404
        ticket = db.kanban_get_ticket(attachment["ticket_id"])
        col = db.kanban_get_column(ticket["column_id"]) if ticket else None
        board = db.kanban_get_board(col["board_id"]) if col else None
        settings = db.get_site_settings()
        # Admins can delete anything; writers can delete their own or any attachment
        if not _can_write(user, settings, board):
            return jsonify({"error": "Permission denied"}), 403
        attach_root = os.path.abspath(config.KANBAN_ATTACHMENT_FOLDER)
        filepath = os.path.abspath(os.path.join(attach_root, attachment["filename"]))
        if os.path.commonpath([attach_root, filepath]) == attach_root and os.path.isfile(filepath):
            os.remove(filepath)
        db.kanban_delete_ticket_attachment(attachment_id)
        log_action("kanban_delete_attachment", request, user=user, attachment_id=attachment_id)
        notify_file_deleted(attachment["filename"])
        return jsonify({"ok": True})

    @app.route("/api/kanban/attachments/<int:attachment_id>/download")
    @_kanban_access_required
    def api_kanban_download_attachment(attachment_id):
        """Download a kanban ticket attachment."""
        user = get_current_user()
        attachment = db.kanban_get_ticket_attachment(attachment_id)
        if not attachment:
            abort(404)
        ticket = db.kanban_get_ticket(attachment["ticket_id"])
        if not ticket or not _user_can_view_ticket(user, ticket):
            abort(403)
        attach_root = os.path.abspath(config.KANBAN_ATTACHMENT_FOLDER)
        # Try DB blob first, then fall back to disk
        blob_id = attachment["blob_id"] if "blob_id" in attachment.keys() else None
        # Only display-only types keep their real Content-Type; see
        # attachment_download_mimetype() in routes/chat.py.
        mimetype = attachment_download_mimetype(attachment["original_name"])
        if blob_id:
            content = db.get_blob_content(blob_id)
            if content:
                return send_file(io.BytesIO(content), as_attachment=True,
                                 download_name=attachment["original_name"], mimetype=mimetype)
        filepath = os.path.abspath(os.path.join(attach_root, attachment["filename"]))
        if os.path.commonpath([attach_root, filepath]) != attach_root:
            abort(404)
        if not os.path.isfile(filepath):
            abort(404)
        return send_file(filepath, as_attachment=True,
                         download_name=attachment["original_name"], mimetype=mimetype)

    @app.route("/api/kanban/tickets/<int:ticket_id>/history", methods=["GET"])
    @_kanban_access_required
    def api_kanban_ticket_history(ticket_id):
        """API: list description history for a ticket."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        if not _user_can_view_ticket(user, ticket):
            return jsonify({"error": "Permission denied"}), 403
        entries = db.kanban_list_ticket_history(ticket_id)
        return jsonify([
            {
                "id": e["id"],
                "editor_name": e["editor_name"],
                "created_at": e["created_at"],
            }
            for e in entries
        ])

    @app.route("/api/kanban/history/<int:entry_id>", methods=["GET"])
    @_kanban_access_required
    def api_kanban_history_entry(entry_id):
        """API: return a single history entry with diff."""
        user = get_current_user()
        entry = db.kanban_get_ticket_history_entry(entry_id)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        ticket = db.kanban_get_ticket(entry["ticket_id"])
        if not ticket or not _user_can_view_ticket(user, ticket):
            return jsonify({"error": "Permission denied"}), 403
        diff_html = compute_diff_html(entry["old_description"] or "", entry["new_description"] or "")
        return jsonify({
            "id": entry["id"],
            "ticket_id": entry["ticket_id"],
            "old_description": entry["old_description"],
            "new_description": entry["new_description"],
            "old_description_html": render_markdown(entry["old_description"] or ""),
            "new_description_html": render_markdown(entry["new_description"] or ""),
            "diff_html": diff_html,
            "editor_name": entry["editor_name"],
            "created_at": entry["created_at"],
        })

    @app.route("/api/kanban/tickets/<int:ticket_id>/comments", methods=["GET"])
    @_kanban_access_required
    def api_kanban_list_comments(ticket_id):
        """API: list comments for a ticket."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        if not _user_can_view_ticket(user, ticket):
            return jsonify({"error": "Permission denied"}), 403
        comments = db.kanban_list_ticket_comments(ticket_id)
        role = user["role"] if user else "user"
        is_admin = role in ("admin", "owner")
        return jsonify([
            {
                "id": c["id"],
                "author_name": c["author_name"],
                "user_id": c["user_id"],
                "content": c["content"],
                "content_html": render_markdown(c["content"]),
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
                "can_edit": is_admin or (user and c["user_id"] == user["id"]),
                "can_delete": is_admin or (user and c["user_id"] == user["id"]),
            }
            for c in comments
        ])

    @app.route("/api/kanban/tickets/<int:ticket_id>/comments", methods=["POST"])
    @_kanban_access_required
    @rate_limit(30, 60)
    def api_kanban_add_comment(ticket_id):
        """API: add a comment to a ticket."""
        user = get_current_user()
        ticket = db.kanban_get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        col = db.kanban_get_column(ticket["column_id"])
        board = db.kanban_get_board(col["board_id"]) if col else None
        # Any user who can view the board can comment on its tickets.
        if not user_can_view_kanban_board(user, board):
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "Comment content is required"}), 400
        if len(content) > 2000:
            return jsonify({"error": "Comment cannot exceed 2000 characters"}), 400
        comment_id = db.kanban_add_ticket_comment(ticket_id, user["id"], content)
        if board:
            db.kanban_add_activity_log(board["id"], user["id"], "comment_added", f"Commented on '{ticket['title']}'")
        comment = db.kanban_get_ticket_comment(comment_id)
        return jsonify({
            "id": comment["id"],
            "author_name": comment["author_name"],
            "user_id": comment["user_id"],
            "content": comment["content"],
            "content_html": render_markdown(comment["content"]),
            "created_at": comment["created_at"],
            "updated_at": comment["updated_at"],
            "can_edit": True,
            "can_delete": True,
        }), 201

    @app.route("/api/kanban/comments/<int:comment_id>", methods=["PUT"])
    @_kanban_access_required
    @rate_limit(30, 60)
    def api_kanban_update_comment(comment_id):
        """API: update a comment (author or admin only)."""
        user = get_current_user()
        comment = db.kanban_get_ticket_comment(comment_id)
        if not comment:
            return jsonify({"error": "Not found"}), 404
        ticket = db.kanban_get_ticket(comment["ticket_id"])
        if not ticket:
            return jsonify({"error": "Not found"}), 404
        board = _resolve_ticket_board(ticket)
        if not board:
            return jsonify({"error": "Not found"}), 404
        if not user_can_view_kanban_board(user, board):
            return jsonify({"error": "Permission denied"}), 403
        role = user["role"] if user else "user"
        is_admin = role in ("admin", "owner")
        if not is_admin and comment["user_id"] != user["id"]:
            return jsonify({"error": "Permission denied"}), 403
        data = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "Comment content is required"}), 400
        if len(content) > 2000:
            return jsonify({"error": "Comment cannot exceed 2000 characters"}), 400
        db.kanban_update_ticket_comment(comment_id, content)
        return jsonify({"ok": True})

    @app.route("/api/kanban/comments/<int:comment_id>", methods=["DELETE"])
    @_kanban_access_required
    @rate_limit(20, 60)
    def api_kanban_delete_comment(comment_id):
        """API: delete a comment (author or admin only)."""
        user = get_current_user()
        comment = db.kanban_get_ticket_comment(comment_id)
        if not comment:
            return jsonify({"error": "Not found"}), 404
        ticket = db.kanban_get_ticket(comment["ticket_id"])
        if not ticket:
            return jsonify({"error": "Not found"}), 404
        board = _resolve_ticket_board(ticket)
        if not board:
            return jsonify({"error": "Not found"}), 404
        if not user_can_view_kanban_board(user, board):
            return jsonify({"error": "Permission denied"}), 403
        role = user["role"] if user else "user"
        is_admin = role in ("admin", "owner")
        if not is_admin and comment["user_id"] != user["id"]:
            return jsonify({"error": "Permission denied"}), 403
        db.kanban_delete_ticket_comment(comment_id)
        return jsonify({"ok": True})

    @app.route("/kanban/<int:board_id>/export")
    @_kanban_access_required
    @rate_limit(10, 60, exempt_html_nav=False)
    def kanban_export_board(board_id):
        """Export a kanban board.  When tickets reference attachments, return
        a ``.kanban.zip`` bundle that contains ``board.json`` plus an
        ``attachments/`` folder.  Otherwise (or when ``?format=json`` is set)
        return the legacy ``.kanban.json``.

        Restricted to users with **write** access to the board: read-only
        viewers must not be able to bulk-download board contents and any
        attached files.
        """
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        if not user_can_view_kanban_board(user, board):
            flash(t("flash.you_do_not_have_the_required_permissions_to_033285"), "error")
            abort(403)
        if not db.kanban_user_can_write_board(user, board):
            flash(t("flash.you_do_not_have_the_required_permissions_to_033285"), "error")
            abort(403)

        columns = db.kanban_list_columns(board_id)
        columns_data = []
        # Gather (stored_filename, original_name, export_arcname) so the same
        # file can be reused if it ends up referenced in the bundle.
        bundle_files = []  # list of (disk_path, arcname)
        seen_assignment_keys = set()
        for col in columns:
            tickets = db.kanban_list_tickets(col["id"])
            tickets_data = []
            for tk in tickets:
                # Multi-assignee: export usernames so a re-import can resolve
                # them on the destination instance even if user IDs differ.
                assignees = db.kanban_list_ticket_assignees(tk["id"])
                attachments_meta = []
                for att in db.kanban_list_ticket_attachments(tk["id"]):
                    arcname = f"attachments/{tk['id']}/{att['filename']}"
                    attachments_meta.append({
                        "filename": att["filename"],
                        "original_name": att["original_name"],
                        "file_size": att["file_size"],
                        "bundle_path": arcname,
                    })
                    bundle_files.append((att["filename"], arcname))
                tickets_data.append({
                    "title": tk["title"],
                    "description": tk["description"] or "",
                    "priority": tk["priority"],
                    "due_date": tk["due_date"] if "due_date" in tk.keys() else None,
                    "color": tk["color"] if "color" in tk.keys() else "",
                    "labels": _ticket_labels_from_row(tk),
                    "assignee_usernames": [a["username"] for a in assignees],
                    "attachments": attachments_meta,
                })
            columns_data.append({
                "title": col["title"],
                "tickets": tickets_data,
            })

        export_obj = {
            "title": board["title"],
            "description": board["description"] or "",
            "columns": columns_data,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": "1.1",
        }

        slug = slugify(board["title"]) or "board"
        plain = request.args.get("format") == "json"
        if plain or not bundle_files:
            json_str = json.dumps(export_obj, indent=2)
            filename = f"{slug}.kanban.json"
            return Response(
                json_str,
                mimetype="application/json",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        attach_root = os.path.abspath(config.KANBAN_ATTACHMENT_FOLDER)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("board.json", json.dumps(export_obj, indent=2))
            for stored_name, arcname in bundle_files:
                fpath = os.path.abspath(os.path.normpath(os.path.join(attach_root, stored_name)))
                try:
                    if os.path.commonpath([attach_root, fpath]) != attach_root:
                        continue
                except ValueError:
                    continue
                if not os.path.isfile(fpath):
                    continue
                z.write(fpath, arcname=arcname)
        buf.seek(0)
        filename = f"{slug}.kanban.zip"
        # Avoid unused-import warning
        _ = seen_assignment_keys
        return Response(
            buf.read(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/kanban/import", methods=["POST"])
    @_kanban_access_required
    @rate_limit(10, 60)
    def kanban_import_board():
        """Import a kanban board from a JSON file."""
        user = get_current_user()
        settings = db.get_site_settings()
        if not _can_write_global(user, settings):
            flash(t("flash.you_do_not_have_the_required_permissions_to_60da71"), "error")
            return redirect(url_for("kanban_list"))

        if "import_file" not in request.files:
            flash(t("flash.no_file_part"), "error")
            return redirect(url_for("kanban_list"))
        file = request.files["import_file"]
        if file.filename == "":
            flash(t("flash.no_selected_file"), "error")
            return redirect(url_for("kanban_list"))

        # Accept either ``.kanban.json`` (legacy) or ``.kanban.zip`` bundle.
        raw = file.read()
        data = None
        attachment_blobs = {}  # bundle_arcname -> bytes
        is_zip = raw[:4] == b"PK\x03\x04"
        if is_zip:
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    total = 0
                    json_member = None
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        norm = info.filename.replace("\\", "/")
                        if norm.startswith("/") or ".." in norm.split("/"):
                            flash(t("flash.invalid_json_file"), "error")
                            return redirect(url_for("kanban_list"))
                        if info.file_size > config.MAX_IMPORT_MEMBER_SIZE:
                            flash(t("flash.invalid_json_file"), "error")
                            return redirect(url_for("kanban_list"))
                        total += info.file_size
                        if total > config.MAX_IMPORT_UNCOMPRESSED_SIZE:
                            flash(t("flash.invalid_json_file"), "error")
                            return redirect(url_for("kanban_list"))
                        if norm == "board.json":
                            json_member = info
                        elif norm.startswith("attachments/"):
                            attachment_blobs[norm] = z.read(info)
                    if json_member is None:
                        flash(t("flash.invalid_json_file"), "error")
                        return redirect(url_for("kanban_list"))
                    try:
                        data = json.loads(z.read(json_member))
                    except (ValueError, TypeError):
                        flash(t("flash.invalid_json_file"), "error")
                        return redirect(url_for("kanban_list"))
            except zipfile.BadZipFile:
                flash(t("flash.invalid_json_file"), "error")
                return redirect(url_for("kanban_list"))
        else:
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                flash(t("flash.invalid_json_file"), "error")
                return redirect(url_for("kanban_list"))

        if not isinstance(data, dict):
            flash(t("flash.invalid_json_file"), "error")
            return redirect(url_for("kanban_list"))

        title = data.get("title", "Imported Board").strip()[:10000]
        description = data.get("description", "").strip()[:10000]
        columns_data = data.get("columns", [])

        board_id = db.kanban_create_board(title, description, user["id"])
        kanban_access = _get_kanban_access(settings)
        os.makedirs(config.KANBAN_ATTACHMENT_FOLDER, exist_ok=True)
        attach_root = os.path.abspath(config.KANBAN_ATTACHMENT_FOLDER)

        for i, col_data in enumerate(columns_data):
            col_title = col_data.get("title", "Column").strip()[:10000]
            col_id = db.kanban_create_column(board_id, col_title, i)
            tickets_data = col_data.get("tickets", [])
            for j, t_data in enumerate(tickets_data):
                t_title = t_data.get("title", "Ticket").strip()[:10000]
                t_desc = t_data.get("description", "").strip()[:10000]
                t_priority = t_data.get("priority", "medium")
                if t_priority not in ("low", "medium", "high", "critical"):
                    t_priority = "medium"
                t_due = t_data.get("due_date")
                t_color = t_data.get("color", "")
                t_labels = _normalize_ticket_labels(t_data.get("labels"))

                ticket_id = db.kanban_create_ticket(col_id, t_title, t_desc, user["id"], t_priority, j)
                if t_due or t_color or t_labels:
                    updates = {}
                    if t_due:
                        updates["due_date"] = t_due
                    if t_color and _is_valid_hex_color(t_color):
                        updates["color"] = t_color
                    if t_labels:
                        updates["labels"] = json.dumps(t_labels)
                    if updates:
                        db.kanban_update_ticket(ticket_id, **updates)

                # Resolve and assign matching usernames on this instance.
                assignee_names = t_data.get("assignee_usernames") or []
                if isinstance(assignee_names, list) and assignee_names:
                    resolved_ids = []
                    for uname in assignee_names:
                        if not uname:
                            continue
                        u = db.get_user_by_username(str(uname))
                        if not u:
                            continue
                        if not db.kanban_user_has_board_access(u["id"], board_id, kanban_access):
                            continue
                        if u["id"] not in resolved_ids:
                            resolved_ids.append(u["id"])
                    if resolved_ids:
                        db.kanban_set_ticket_assignees(ticket_id, resolved_ids)

                # Restore bundled attachments for this ticket.
                bundled = t_data.get("attachments") or []
                if isinstance(bundled, list) and bundled and attachment_blobs:
                    for att in bundled:
                        if not isinstance(att, dict):
                            continue
                        bundle_path = att.get("bundle_path")
                        original_name = att.get("original_name") or "file"
                        if not bundle_path or bundle_path not in attachment_blobs:
                            continue
                        ext = _validation_safe_ext(att.get("filename") or original_name) or "bin"
                        stored_name = f"{uuid.uuid4().hex}.{ext}"
                        target_path = os.path.abspath(os.path.normpath(os.path.join(attach_root, stored_name)))
                        try:
                            if os.path.commonpath([attach_root, target_path]) != attach_root:
                                continue
                        except ValueError:
                            continue
                        try:
                            blob = attachment_blobs[bundle_path]
                            with open(target_path, "wb") as fh:
                                fh.write(blob)
                            blob_id = None
                            try:
                                blob_id = db.store_blob(stored_name, blob, "application/octet-stream")
                            except Exception:
                                pass
                            db.kanban_add_ticket_attachment(
                                ticket_id, stored_name,
                                str(original_name)[:500],
                                len(blob),
                                user["id"],
                                blob_id=blob_id,
                            )
                        except OSError:
                            continue

        log_action("kanban_import_board", request, user=user, board_id=board_id, title=title)
        _kanban_record_history(board_id, "Imported board", user=user)
        flash(t("flash.board_title_has_been_successfully_imported", title=title), "success")
        return redirect(url_for("kanban_board", board_id=board_id))

    @app.route("/api/kanban/<int:board_id>/activity")
    @_kanban_access_required
    @rate_limit()
    def api_kanban_activity_log(board_id):
        """API: return the recent activity log for a board."""
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        if not user_can_view_kanban_board(user, board):
            return jsonify({"error": "Permission denied"}), 403
        entries = db.kanban_get_activity_log(board_id, limit=50)
        return jsonify({"entries": entries})

    @app.route("/api/kanban/<int:board_id>/sync")
    @_kanban_access_required
    @rate_limit(240, 60)
    def api_kanban_sync(board_id):
        """Return realtime board events with ``seq > since``.

        Clients poll this every couple of seconds and refetch the board (or
        the affected ticket) when ``events`` is non-empty.  ``X-Kanban-
        Session`` lets the server omit the caller's own writes from the feed,
        and ``seq`` in the response is the current head so clients can
        advance their cursor on empty polls too.
        """
        user = get_current_user()
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        if not user_can_view_kanban_board(user, board):
            return jsonify({"error": "Permission denied"}), 403
        try:
            since = int(request.args.get("since", "0"))
        except (TypeError, ValueError):
            since = 0
        session_id = (request.headers.get("X-Kanban-Session") or "").strip()[:64]
        rows = db.kanban_events_since(
            board_id, since,
            exclude_session=session_id or None,
        )
        events = []
        for row in rows or []:
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
            events.append({
                "id": row["id"],
                "seq": row["seq"],
                "op_type": row["op_type"],
                "payload": payload,
                "by_user_id": row["by_user_id"],
                "by_session": row["by_session"],
                "created_at": row["created_at"],
            })
        head_seq = db.kanban_latest_event_seq(board_id)
        try:
            db.kanban_prune_events(board_id)
        except Exception:  # noqa: BLE001
            pass
        return jsonify({"events": events, "seq": head_seq})

    @app.route("/kanban/<int:board_id>/history")
    @_kanban_access_required
    def kanban_board_history(board_id):
        """Display the revision history for a kanban board."""
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        user = get_current_user()
        settings = db.get_site_settings()
        if not user_can_view_kanban_board(user, board, settings):
            flash(t("flash.you_do_not_have_the_required_permissions_to_cab781"), "error")
            return redirect(url_for("kanban_list"))
        history = db.kanban_list_board_history(board_id)
        can_write = _can_write(user, settings, board)
        is_admin = bool(user and user["role"] in ("admin", "owner"))
        return render_template(
            "kanban/history.html",
            board=board,
            history=history,
            can_revert=can_write,
            is_admin=is_admin,
        )

    @app.route("/kanban/<int:board_id>/history/<int:entry_id>")
    @_kanban_access_required
    def kanban_board_history_entry(board_id, entry_id):
        """Display a specific historical revision of a kanban board."""
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        user = get_current_user()
        settings = db.get_site_settings()
        if not user_can_view_kanban_board(user, board, settings):
            flash(t("flash.you_do_not_have_the_required_permissions_to_cab781"), "error")
            return redirect(url_for("kanban_list"))
        entry = db.kanban_get_board_history_entry(entry_id)
        if not entry or entry["board_id"] != board_id:
            abort(404)
        try:
            snapshot = json.loads(entry["snapshot"] or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        can_write = _can_write(user, settings, board)
        is_admin = bool(user and user["role"] in ("admin", "owner"))
        return render_template(
            "kanban/history_entry.html",
            board=board,
            entry=entry,
            snapshot=snapshot,
            can_revert=can_write,
            is_admin=is_admin,
            description_html=render_markdown(entry["description"] or ""),
        )

    @app.route("/kanban/<int:board_id>/revert/<int:entry_id>", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def kanban_board_revert(board_id, entry_id):
        """Revert a kanban board to a previous history entry."""
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        user = get_current_user()
        settings = db.get_site_settings()
        if not _can_write(user, settings, board):
            flash(t("flash.you_do_not_have_the_required_permissions_to_c0ae90"), "error")
            return redirect(url_for("kanban_board_history", board_id=board_id))
        entry = db.kanban_get_board_history_entry(entry_id)
        if not entry or entry["board_id"] != board_id:
            abort(404)
        orphaned = db.kanban_restore_board_from_snapshot(board_id, entry["snapshot"])
        _remove_kanban_attachment_files(orphaned)
        db.kanban_add_activity_log(
            board_id, user["id"], "board_reverted",
            f"Reverted board to revision #{entry_id}",
        )
        _kanban_record_history(
            board_id,
            f"Reverted to revision #{entry_id}",
            user=user,
            is_revert=True,
        )
        _kanban_emit(board_id, "board_reverted",
                     payload={"entry_id": entry_id}, user=user)
        log_action("kanban_revert_board", request, user=user,
                   board_id=board_id, entry_id=entry_id)
        flash(t("flash.kanban_board_reverted"), "success")
        return redirect(url_for("kanban_board", board_id=board_id))

    @app.route("/kanban/<int:board_id>/history/<int:entry_id>/delete", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def kanban_board_delete_history_entry(board_id, entry_id):
        """Delete a single board-history entry (admin-only)."""
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        user = get_current_user()
        if not user or user["role"] not in ("admin", "owner"):
            flash(t("flash.you_do_not_have_the_required_permissions_to_c0ae90"), "error")
            return redirect(url_for("kanban_board_history", board_id=board_id))
        entry = db.kanban_get_board_history_entry(entry_id)
        if not entry or entry["board_id"] != board_id:
            abort(404)
        db.kanban_delete_board_history_entry(entry_id)
        log_action("kanban_delete_history_entry", request, user=user,
                   board_id=board_id, entry_id=entry_id)
        flash(t("flash.kanban_history_entry_deleted"), "success")
        return redirect(url_for("kanban_board_history", board_id=board_id))

    @app.route("/kanban/<int:board_id>/history/clear", methods=["POST"])
    @_kanban_access_required
    @rate_limit()
    def kanban_board_clear_history(board_id):
        """Delete every history entry attached to a board (admin-only)."""
        board = db.kanban_get_board(board_id)
        if not board:
            abort(404)
        user = get_current_user()
        if not user or user["role"] not in ("admin", "owner"):
            flash(t("flash.you_do_not_have_the_required_permissions_to_c0ae90"), "error")
            return redirect(url_for("kanban_board_history", board_id=board_id))
        db.kanban_clear_board_history(board_id)
        log_action("kanban_clear_history", request, user=user, board_id=board_id)
        flash(t("flash.kanban_history_cleared"), "success")
        return redirect(url_for("kanban_board_history", board_id=board_id))


def user_has_kanban_sidebar_access(user, settings):
    """Return True if the kanban link should be visible in the sidebar for *user*."""
    if not user:
        return False
    if not db.has_permission(user, "kanban.view"):
        return False
    if _has_global_kanban_access(user, settings):
        return True
    return db.kanban_user_has_any_share(user["id"])
