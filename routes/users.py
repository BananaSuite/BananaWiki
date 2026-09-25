"""
BananaWiki: User account and profile routes.
"""

from flask import (render_template, request, redirect, url_for, session, flash, send_file, abort)
import io
import json
import logging
import os
import uuid
import zipfile

_logger = logging.getLogger("bananawiki")
from urllib.parse import urlparse
from PIL import Image
import db
import config
from helpers import (
    login_required, editor_required, get_current_user,
    allowed_file, _is_valid_username, rate_limit, _safe_referrer, ROLE_LABELS, user_can_view_page,
    _safe_ext, safe_unlink_in,
    MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH,
    normalize_birth_date, is_birthday_today,
    t,
)
from helpers._passwords import generate_password_hash, check_password_hash
from helpers._auth_sessions import current_user_session_id
from helpers._session_metadata import describe_session_user_agent
from wiki_logger import log_action
from sync import notify_change, notify_file_upload, notify_file_deleted


def _zip_json_default(value):
    """JSON fallback for export values that SQLite may return as bytes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "_binary": True,
            "size": len(value),
        }
    return str(value)


def _row_to_dict(row):
    """Convert a sqlite Row or mapping-like object into a plain dict."""
    return dict(row) if row is not None else None


def _rows_to_dicts(rows):
    """Convert sqlite rows to JSON-serialisable dictionaries."""
    return [_row_to_dict(row) for row in rows or []]


def _write_json(zf, path, data):
    """Write formatted JSON into the ZIP."""
    zf.writestr(path, json.dumps(data, indent=2, default=_zip_json_default))


def _table_columns(conn, table_name):
    """Return a set of column names for an existing SQLite table."""
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except Exception:
        return set()


def _query_dicts(conn, sql, params=()):
    """Run a read query and return dictionaries, tolerating absent optional tables."""
    try:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())
    except Exception:
        return []


def _query_user_table(conn, table_name, user_id, *, user_column="user_id",
                      select_columns="*", order_by=None):
    """Return rows from an optional user-owned table."""
    cols = _table_columns(conn, table_name)
    if not cols or user_column not in cols:
        return []
    sql = f"SELECT {select_columns} FROM {table_name} WHERE {user_column}=?"
    if order_by:
        sql += f" ORDER BY {order_by}"
    return _query_dicts(conn, sql, (user_id,))


def _write_attachment_file(zf, *, arcname, filename, blob_id=None, disk_root=None):
    """Add an attachment's bytes to the ZIP from blob storage or disk."""
    content = None
    if blob_id:
        try:
            content = db.get_blob_content(blob_id)
        except Exception:
            content = None
    if content:
        zf.writestr(arcname, content)
        return True
    if not (disk_root and filename):
        return False
    root = os.path.abspath(disk_root)
    path = os.path.abspath(os.path.normpath(os.path.join(root, filename)))
    try:
        if os.path.commonpath([root, path]) != root:
            return False
    except ValueError:
        return False
    if not os.path.isfile(path):
        return False
    zf.write(path, arcname=arcname)
    return True


def _safe_export_name(value, fallback):
    """Return a conservative filename segment for generated export members."""
    raw = str(value or fallback).strip().lower()
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in raw)
    safe = safe.strip("-._")
    return safe or fallback


def _export_canvas_data(conn, zf, user):
    """Export canvas/mind-map layouts and user activity.

    As with kanban boards, a layout's full contents are only exported while
    the user can still view it; the user's own history entries, events and
    permission rows are exported either way.
    """
    user_id = user["id"]
    layout_rows = _query_dicts(
        conn,
        """
        SELECT DISTINCT l.*
        FROM canvas__layouts l
        LEFT JOIN canvas__permissions cp ON cp.layout_id = l.id
        LEFT JOIN canvas__history h ON h.layout_id = l.id
        LEFT JOIN canvas__events e ON e.layout_id = l.id
        WHERE l.creator_id=? OR cp.user_id=? OR h.edited_by=? OR e.by_user_id=?
        ORDER BY l.updated_at DESC, l.id DESC
        """,
        (user_id, user_id, user_id, user_id),
    )
    from routes.canvas import user_has_canvas_sidebar_access

    # The same two checks as /canvas/<slug>: the canvas access gate (which is
    # what the sidebar link uses too), then the layout's own permissions.
    settings = db.get_site_settings() or {}
    open_access = bool(settings.get("canvas_open_access"))
    can_use_canvas = user_has_canvas_sidebar_access(user, settings)
    layout_rows = [
        layout for layout in layout_rows
        if can_use_canvas and db.canvas_user_can_view(layout["id"], user, open_access=open_access)
    ]
    layout_ids = [row["id"] for row in layout_rows]
    histories = _query_user_table(
        conn,
        "canvas__history",
        user_id,
        user_column="edited_by",
        order_by="created_at DESC, id DESC",
    )
    events = _query_user_table(
        conn,
        "canvas__events",
        user_id,
        user_column="by_user_id",
        order_by="created_at DESC, id DESC",
    )
    permissions = _query_user_table(
        conn,
        "canvas__permissions",
        user_id,
        order_by="created_at DESC, id DESC",
    )

    _write_json(
        zf,
        "canvas/index.json",
        {
            "description": "Canvas layouts are the BananaWiki mind-map/visual knowledge maps touched by this user.",
            "layouts": layout_rows,
            "history_entries_by_user": histories,
            "events_by_user": events,
            "explicit_permissions": permissions,
        },
    )

    upload_root = os.path.abspath(config.UPLOAD_FOLDER)
    seen_assets = set()
    for layout in layout_rows:
        try:
            data = json.loads(layout.get("data") or "{}")
        except (TypeError, ValueError):
            data = {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}
        export_obj = {
            "title": layout.get("title") or "",
            "description": layout.get("description") or "",
            "version": layout.get("version"),
            "visibility": layout.get("visibility", "private"),
            "created_at": layout.get("created_at"),
            "updated_at": layout.get("updated_at"),
            "data": data,
        }
        basename = _safe_export_name(layout.get("slug"), f"canvas-{layout.get('id')}")
        _write_json(zf, f"canvas/mind_maps/{basename}.canvas.json", export_obj)
        if isinstance(data, dict):
            for asset_name in _collect_canvas_assets(data):
                asset_key = (layout.get("id"), asset_name)
                if asset_key in seen_assets:
                    continue
                seen_assets.add(asset_key)
                _write_attachment_file(
                    zf,
                    arcname=f"canvas/mind_maps/assets/{basename}/{asset_name}",
                    filename=asset_name,
                    disk_root=upload_root,
                )
    return {"canvas_layouts": len(layout_ids), "canvas_history_entries": len(histories)}


def _collect_canvas_assets(canvas_data):
    """Collect local upload filenames referenced by canvas nodes."""
    out = set()
    if not isinstance(canvas_data, dict):
        return out
    for node in canvas_data.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        url = (node.get("url") or "").strip()
        prefix = "/static/uploads/"
        if url.startswith(prefix):
            name = url[len(prefix):]
            if name and "/" not in name and "\\" not in name:
                out.add(name)
    return out


def _export_kanban_data(conn, zf, user):
    """Export Kanban boards, tickets, comments, assignments, and uploads.

    Boards are found through everything the user ever did on them, but a
    full copy of a board (all columns, tickets, comments and attachments,
    including other people's) is only written for boards the user can still
    open now.  Otherwise a removed share, a single old comment or being
    assigned a ticket would keep handing out the board's current contents.
    The user's own comments, history entries, activity, assignments and
    uploads are exported separately either way.
    """
    from routes.kanban import user_can_view_kanban_board

    user_id = user["id"]
    boards = _query_dicts(
        conn,
        """
        SELECT DISTINCT b.*
        FROM kanban_boards b
        LEFT JOIN kanban_columns c ON c.board_id = b.id
        LEFT JOIN kanban_tickets t ON t.column_id = c.id
        LEFT JOIN kanban_ticket_assignees ta ON ta.ticket_id = t.id
        LEFT JOIN kanban_ticket_comments tc ON tc.ticket_id = t.id
        LEFT JOIN kanban_ticket_history th ON th.ticket_id = t.id
        LEFT JOIN kanban_ticket_attachments katt ON katt.ticket_id = t.id
        LEFT JOIN kanban_activity_log al ON al.board_id = b.id
        LEFT JOIN kanban_events ev ON ev.board_id = b.id
        LEFT JOIN kanban_board_shares bs ON bs.board_id = b.id
        LEFT JOIN kanban_board_history bh ON bh.board_id = b.id
        WHERE b.created_by=? OR t.created_by=? OR t.assigned_to=?
           OR ta.user_id=? OR tc.user_id=? OR th.changed_by=?
           OR katt.uploaded_by=? OR al.user_id=? OR ev.by_user_id=?
           OR (bs.share_type='user' AND bs.target=?) OR bh.edited_by=?
        ORDER BY b.created_at DESC, b.id DESC
        """,
        (user_id, user_id, user_id, user_id, user_id, user_id,
         user_id, user_id, user_id, user_id, user_id),
    )
    settings = db.get_site_settings()
    boards = [board for board in boards if user_can_view_kanban_board(user, board, settings)]
    board_ids = [row["id"] for row in boards]
    comments = _query_user_table(
        conn,
        "kanban_ticket_comments",
        user_id,
        order_by="created_at DESC, id DESC",
    )
    ticket_history = _query_user_table(
        conn,
        "kanban_ticket_history",
        user_id,
        user_column="changed_by",
        order_by="created_at DESC, id DESC",
    )
    activity = _query_user_table(
        conn,
        "kanban_activity_log",
        user_id,
        order_by="created_at DESC, id DESC",
    )
    events = _query_user_table(
        conn,
        "kanban_events",
        user_id,
        user_column="by_user_id",
        order_by="created_at DESC, id DESC",
    )
    assignments = _query_user_table(
        conn,
        "kanban_ticket_assignees",
        user_id,
        order_by="assigned_at DESC",
    )
    uploaded_attachments = _query_user_table(
        conn,
        "kanban_ticket_attachments",
        user_id,
        user_column="uploaded_by",
        order_by="uploaded_at DESC, id DESC",
    )

    board_exports = []
    for board in boards:
        columns = _query_dicts(
            conn,
            "SELECT * FROM kanban_columns WHERE board_id=? ORDER BY sort_order, id",
            (board["id"],),
        )
        columns_data = []
        for column in columns:
            tickets = _query_dicts(
                conn,
                "SELECT * FROM kanban_tickets WHERE column_id=? ORDER BY sort_order, id",
                (column["id"],),
            )
            for ticket in tickets:
                ticket["assignees"] = _query_dicts(
                    conn,
                    "SELECT ta.*, u.username FROM kanban_ticket_assignees ta "
                    "LEFT JOIN users u ON u.id=ta.user_id WHERE ta.ticket_id=? "
                    "ORDER BY ta.assigned_at ASC",
                    (ticket["id"],),
                )
                ticket["comments"] = _query_dicts(
                    conn,
                    "SELECT * FROM kanban_ticket_comments WHERE ticket_id=? ORDER BY created_at ASC, id ASC",
                    (ticket["id"],),
                )
                ticket["attachments"] = _query_dicts(
                    conn,
                    "SELECT id, ticket_id, filename, original_name, file_size, uploaded_by, uploaded_at "
                    "FROM kanban_ticket_attachments WHERE ticket_id=? ORDER BY uploaded_at ASC, id ASC",
                    (ticket["id"],),
                )
            column["tickets"] = tickets
            columns_data.append(column)
        board_exports.append({**board, "columns": columns_data})
        _write_json(
            zf,
            f"kanban/boards/{board['id']}-{_safe_export_name(board.get('title'), 'board')}.kanban.json",
            {**board, "columns": columns_data},
        )

    for attachment in uploaded_attachments:
        attachment_name = _safe_export_name(
            attachment.get("original_name") or attachment.get("filename"),
            f"attachment-{attachment['id']}",
        )
        _write_attachment_file(
            zf,
            arcname=f"kanban/uploaded_attachments/{attachment['id']}-{attachment_name}",
            filename=attachment.get("filename"),
            blob_id=attachment.get("blob_id"),
            disk_root=config.KANBAN_ATTACHMENT_FOLDER,
        )

    _write_json(
        zf,
        "kanban/index.json",
        {
            "boards": boards,
            "board_exports": board_exports,
            "comments_by_user": comments,
            "ticket_history_by_user": ticket_history,
            "activity_by_user": activity,
            "events_by_user": events,
            "assignments": assignments,
            "uploaded_attachments": uploaded_attachments,
        },
    )
    return {"kanban_boards": len(board_ids), "kanban_uploaded_attachments": len(uploaded_attachments)}


def build_user_export_zip(user):
    """Build an in-memory ZIP file containing all exported data for a user.

    ``user`` must be a valid user row (as returned by ``db.get_user_by_id``).
    Returns a ``BytesIO`` object ready to be sent as a file download.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Account info (exclude password hash)
        account_data = {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "suspended": bool(user["suspended"]),
            "invite_code": user["invite_code"],
            "created_at": user["created_at"],
            "last_login_at": user["last_login_at"],
            "is_superuser": bool(user["is_superuser"]),
        }
        summary = {
            "account_id": user["id"],
            "username": user["username"],
            "sections": {},
        }
        _write_json(zf, "manifest.json", {
            "format": "bananawiki-user-data-export",
            "version": "2.0",
            "description": "Personal account, profile, contribution, collaboration, and user-created artifact export.",
        })
        _write_json(zf, "account.json", account_data)

        # Username history
        username_history = [dict(r) for r in db.get_username_history(user["id"])]
        _write_json(zf, "username_history.json", username_history)
        summary["sections"]["username_history"] = len(username_history)

        # Contributions (page edits)
        contributions = [dict(r) for r in db.get_user_contributions(user["id"])]
        _write_json(zf, "contributions.json", contributions)
        _write_json(zf, "contributions/page_edits.json", contributions)
        summary["sections"]["page_edits"] = len(contributions)

        # Drafts
        drafts = [dict(r) for r in db.list_user_drafts(user["id"])]
        _write_json(zf, "drafts.json", drafts)
        summary["sections"]["drafts"] = len(drafts)

        # Accessibility preferences
        accessibility = db.get_user_accessibility(user["id"])
        _write_json(zf, "accessibility.json", accessibility)

        # Profile and identity-adjacent data
        profile = _row_to_dict(db.get_user_profile(user["id"]))
        profile_fields = {}
        if db.is_plugin_enabled("user_profiles"):
            try:
                profile_fields = db.get_user_field_values(user["id"])
            except Exception:
                profile_fields = {}
        _write_json(zf, "profile/profile.json", profile or {})
        _write_json(zf, "profile/profile_fields.json", profile_fields or {})
        try:
            _write_json(zf, "profile/group_badges.json", _rows_to_dicts(db.get_user_profile_group_settings(user["id"])))
        except Exception:
            _write_json(zf, "profile/group_badges.json", [])
        if profile and profile.get("avatar_filename"):
            _write_attachment_file(
                zf,
                arcname=f"profile/avatar/{os.path.basename(profile['avatar_filename'])}",
                filename=profile["avatar_filename"],
                disk_root=config.UPLOAD_FOLDER,
            )

        # DM chat messages (all conversations the user participates in)
        if db.is_plugin_enabled("chat"):
            try:
                chats = db.get_user_chats(user["id"])
                chat_ids = [chat["id"] for chat in chats]
                chat_name_map = {chat["id"]: chat["other_username"] for chat in chats}
                all_msgs = db.get_all_messages_for_chats(chat_ids)
                deleted_placeholder = t("chat.deleted_message")
                dm_export = []
                for chat_id, messages in all_msgs.items():
                    for msg in messages:
                        content = msg["content"]
                        # The chat shows the other person's deleted messages
                        # as a placeholder; the export must not bring the
                        # retracted text back.  The user's own deleted
                        # messages keep their text, as they wrote it.
                        if msg["is_deleted"] and msg["sender_id"] != user["id"]:
                            content = deleted_placeholder
                        dm_export.append({
                            "chat_id": chat_id,
                            "other_user": chat_name_map.get(chat_id, ""),
                            "message_id": msg["id"],
                            "sender_id": msg["sender_id"],
                            "sender_name": msg["sender_name"],
                            "content": content,
                            "created_at": msg["created_at"],
                            "is_deleted": bool(msg["is_deleted"]),
                        })
                _write_json(zf, "dm_messages.json", dm_export)
                _write_json(zf, "chat/dm_messages.json", dm_export)
                summary["sections"]["dm_messages"] = len(dm_export)
            except Exception:
                _logger.warning("Failed to export DM messages for user %s", user["id"], exc_info=True)

        # Group chat messages sent by this user
        if db.is_plugin_enabled("chat"):
            try:
                # Only the user's own messages are exported, so groups the
                # user is banned from count too.  db.get_user_groups() leaves
                # those out, because it also returns each group's latest
                # message.
                with db.get_db_context() as group_conn:
                    groups = _query_dicts(
                        group_conn,
                        "SELECT gc.id, gc.name FROM group_chats gc "
                        "JOIN group_members gm ON gm.group_id = gc.id "
                        "WHERE gm.user_id = ?",
                        (user["id"],),
                    )
                group_ids = [group["id"] for group in groups]
                group_name_map = {group["id"]: group["name"] for group in groups}
                all_msgs = db.get_all_messages_for_groups(group_ids)
                group_export = []
                for group_id, messages in all_msgs.items():
                    for msg in messages:
                        if msg.get("sender_id") == user["id"]:
                            group_export.append({
                                "group_id": group_id,
                                "group_name": group_name_map.get(group_id, ""),
                                "message_id": msg["id"],
                                "content": msg["content"],
                                "created_at": msg["created_at"],
                                "is_deleted": bool(msg.get("is_deleted", 0)),
                            })
                _write_json(zf, "group_messages.json", group_export)
                _write_json(zf, "chat/group_messages.json", group_export)
                summary["sections"]["group_messages"] = len(group_export)
            except Exception:
                _logger.warning("Failed to export group messages for user %s", user["id"], exc_info=True)

        with db.get_db_context() as conn:
            uid = user["id"]
            pending_contributions = []
            try:
                pending_contributions = db.list_user_contributions(uid)
            except Exception:
                pending_contributions = []
            contribution_quota_requests = []
            try:
                contribution_quota_requests = db.list_contribution_quota_requests(uid)
            except Exception:
                contribution_quota_requests = []
            _write_json(zf, "contributions/pending_contributions.json", pending_contributions)
            _write_json(zf, "contributions/contribution_quota_requests.json", contribution_quota_requests)
            summary["sections"]["pending_contributions"] = len(pending_contributions)
            summary["sections"]["contribution_quota_requests"] = len(contribution_quota_requests)

            reservation_data = {
                "active_reservations": [],
                "reservation_quota_requests": [],
            }
            try:
                reservation_data["active_reservations"] = db.get_user_reservations(uid)
            except Exception:
                reservation_data["active_reservations"] = []
            try:
                reservation_data["reservation_quota_requests"] = db.list_reservation_quota_requests(uid)
            except Exception:
                reservation_data["reservation_quota_requests"] = []
            _write_json(zf, "reservations.json", reservation_data)
            summary["sections"]["active_reservations"] = len(reservation_data["active_reservations"])

            activity_bundle = {
                "role_history": _rows_to_dicts(db.get_role_history(uid)),
                "suspension_history": _rows_to_dicts(db.get_suspension_history(uid)),
                "custom_tags": _rows_to_dicts(db.get_user_custom_tags(uid)),
                "badges": _rows_to_dicts(db.get_user_badges(uid, include_revoked=True)),
                "badge_notifications": _query_user_table(conn, "badge_notifications", uid, order_by="created_at DESC, id DESC"),
                "user_permissions": _query_user_table(conn, "user_permissions", uid, order_by="permission_key ASC"),
                "editor_category_access": _query_user_table(conn, "editor_category_access", uid),
                "editor_allowed_categories": _query_user_table(conn, "editor_allowed_categories", uid),
                "invite_code_usage": _query_user_table(conn, "invite_code_usage", uid, order_by="used_at DESC, id DESC"),
                "invite_codes_created": _query_user_table(conn, "invite_codes", uid, user_column="created_by", order_by="created_at DESC, id DESC"),
                # The beta programme and feedback plugins no longer ship, but an
                # upgraded database can still hold rows about this person, and
                # a personal data export has to include them.
                "beta_tester": _query_user_table(conn, "beta_testers", uid),
                "beta_invites": _query_user_table(conn, "beta_tester_invites", uid, order_by="created_at DESC, id DESC"),
                "api_token": _query_user_table(conn, "api_tokens", uid, select_columns="id, user_id, created_at, last_used_at"),
                "api_service_tokens": _query_user_table(
                    conn,
                    "api_service__tokens",
                    uid,
                    select_columns="id, user_id, name, permissions, last_used_at, expires_at, active, created_at",
                    order_by="created_at DESC, id DESC",
                ),
                "api_service_audit_log": _query_user_table(conn, "api_service__audit_log", uid, order_by="created_at DESC, id DESC"),
                "impersonation_as_admin": _query_user_table(conn, "impersonation_logs", uid, user_column="admin_id", order_by="started_at DESC, id DESC"),
                "impersonation_as_target": _query_user_table(conn, "impersonation_logs", uid, user_column="target_user_id", order_by="started_at DESC, id DESC"),
                "announcements_created": _query_user_table(conn, "announcements", uid, user_column="created_by", order_by="created_at DESC, id DESC"),
                "feedback_reports": _query_dicts(
                    conn,
                    "SELECT * FROM feedback_reports WHERE user_id=? OR username=? ORDER BY created_at DESC, id DESC",
                    (uid, user["username"]),
                ) + _query_dicts(
                    conn,
                    "SELECT * FROM feedback__reports WHERE user_id=? OR username=? ORDER BY submitted_at DESC, id DESC",
                    (uid, user["username"]),
                ),
            }
            _write_json(zf, "activity.json", activity_bundle)
            summary["sections"]["badges"] = len(activity_bundle["badges"])

            assessment_attempts = _query_user_table(
                conn,
                "assessment_attempts",
                uid,
                order_by="submitted_at DESC, id DESC",
            )
            attempt_ids = [row["id"] for row in assessment_attempts]
            assessment_answers = []
            if attempt_ids and _table_columns(conn, "assessment_answers"):
                placeholders = ",".join("?" * len(attempt_ids))
                assessment_answers = _query_dicts(
                    conn,
                    f"SELECT * FROM assessment_answers WHERE attempt_id IN ({placeholders}) ORDER BY id ASC",
                    tuple(attempt_ids),
                )
            _write_json(zf, "assessments.json", {
                "attempts": assessment_attempts,
                "answers": assessment_answers,
            })
            summary["sections"]["assessment_attempts"] = len(assessment_attempts)

            uploaded_page_attachments = _query_dicts(
                conn,
                "SELECT pa.*, p.title AS page_title, p.slug AS page_slug "
                "FROM page_attachments pa LEFT JOIN pages p ON p.id=pa.page_id "
                "WHERE pa.uploaded_by=? ORDER BY pa.uploaded_at DESC, pa.id DESC",
                (uid,),
            )
            _write_json(zf, "uploads/page_attachments.json", uploaded_page_attachments)
            for attachment in uploaded_page_attachments:
                attachment_name = _safe_export_name(
                    attachment.get("original_name") or attachment.get("filename"),
                    f"attachment-{attachment['id']}",
                )
                _write_attachment_file(
                    zf,
                    arcname=f"uploads/page_attachments/{attachment['id']}-{attachment_name}",
                    filename=attachment.get("filename"),
                    blob_id=attachment.get("blob_id"),
                    disk_root=config.ATTACHMENT_FOLDER,
                )
            summary["sections"]["page_attachments_uploaded"] = len(uploaded_page_attachments)

            canvas_summary = _export_canvas_data(conn, zf, user)
            kanban_summary = _export_kanban_data(conn, zf, user)
            summary["sections"].update(canvas_summary)
            summary["sections"].update(kanban_summary)

            _write_json(zf, "summary.json", summary)

    buf.seek(0)
    return buf


def _revoke_api_tokens_after_credential_change(user_id, reason):
    """Revoke every API token of *user_id* and tell the user when there were any.

    Called whenever the account's password changes or the user signs out of
    all sessions.  Those are the moments someone acts on a suspected leak,
    and a token minted with the old password must not outlive it.  *reason*
    is a fixed description for the server log.
    """
    revoked = db.revoke_user_api_service_tokens(user_id, reason)
    if revoked:
        flash(
            t(
                "flash.api_tokens_revoked_after_credential_change",
                default="{count} API token(s) of this account were revoked. Create new ones if they are still needed.",
                count=revoked,
            ),
            "info",
        )
    return revoked


def _filter_visible_profile_contributions(viewer, contribution_list, year):
    """Return only contribution rows whose current pages are visible to *viewer*."""
    if not isinstance(year, int):
        raise ValueError("year must be an integer representing the contribution year for heatmap filtering")

    visible = []
    visible_by_day = {}
    year_prefix = f"{year}-"

    for contribution in contribution_list:
        # Deleted pages have no live slug/category metadata, so public profile
        # views omit them instead of guessing at stale visibility.
        if not contribution["page_slug"]:
            continue
        if not user_can_view_page(viewer, contribution):
            continue
        visible.append(contribution)
        day = contribution["created_at"][:10]
        if day.startswith(year_prefix):
            visible_by_day[day] = visible_by_day.get(day, 0) + 1

    return visible, visible_by_day


def register_user_routes(app):
    """Register user account and profile routes on the Flask app."""

    def _profile_next(fallback):
        """Return next_url from the current form post if it is a safe same-site path, else fallback."""
        url = request.form.get("next_url", "").strip()
        if not url:
            return fallback
        parsed = urlparse(url)
        # Reject any URL that contains a scheme or network location.
        if parsed.scheme or parsed.netloc:
            return fallback
        safe = parsed.path
        if parsed.query:
            safe = f"{safe}?{parsed.query}"
        # Only accept simple same-site paths: must start with / but not // and contain no backslashes
        if not safe or not safe.startswith("/") or safe.startswith("//") or "\\" in safe:
            return fallback
        return safe

    def _delete_own_account(user, failure_endpoint):
        """Validate and delete the current user, redirecting failures safely."""
        if user["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_deleted"), "error")
            return redirect(url_for(failure_endpoint))
        password = request.form.get("password", "")
        if len(password) > MAX_PASSWORD_LENGTH or not check_password_hash(user["password"], password):
            flash(t("flash.incorrect_password"), "error")
            return redirect(url_for(failure_endpoint))
        if user["role"] in ("admin", "owner"):
            remaining_admins = db.count_admins() - (0 if user["suspended"] else 1)
            if remaining_admins <= 0:
                flash(t("flash.cannot_delete_the_last_admin_account"), "error")
                return redirect(url_for(failure_endpoint))
        log_action("delete_account", request, user=user)
        notify_change("user_delete_account", f"User '{user['username']}' deleted their account")
        db.propagate_mention_deletion(user["username"])
        profile = db.get_user_profile(user["id"])
        try:
            db.delete_user_field_values(user["id"])
        except Exception:
            pass
        db.delete_user(user["id"])
        if profile and profile["avatar_filename"]:
            safe_unlink_in(config.UPLOAD_FOLDER, profile["avatar_filename"])
            notify_file_deleted(profile["avatar_filename"])
        session.clear()
        flash(t("flash.your_account_has_been_deleted"), "info")
        return redirect(url_for("login"))

    @app.route("/badges/notifications")
    @login_required
    def badge_notifications():
        """View and dismiss badge notifications."""
        user = get_current_user()
        unnotified = db.get_unnotified_badges(user["id"])
        return render_template(
            "users/badge_notifications.html",
            unnotified=unnotified,
        )

    @app.route("/badges/notifications/dismiss", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def dismiss_badge_notifications():
        """Dismiss all badge notifications for the current user."""
        user = get_current_user()
        db.mark_badges_notified(user["id"])
        if "badge_notifications" in session:
            del session["badge_notifications"]
        flash(t("flash.badge_notifications_dismissed"), "info")
        return redirect(_safe_referrer() or url_for("home"))

    @app.route("/account")
    @app.route("/account/settings")
    @login_required
    def account_redirect():
        """Redirect legacy account paths to the new settings page."""
        return redirect(url_for("user_settings"), code=301)

    @app.route("/settings/sessions/<session_id>/revoke", methods=["POST"])
    @login_required
    @rate_limit(20, 60)
    def revoke_active_session(session_id):
        """Revoke one persistent login session owned by the current user."""
        user = get_current_user()
        if session.get("impersonator_id"):
            abort(403)
        current_id = current_user_session_id()
        if not db.revoke_user_session(user["id"], session_id):
            abort(404)
        log_action("revoke_session", request, user=user, session_id=session_id)
        if session_id == current_id:
            session.clear()
            flash(t("flash.session_revoked"), "info")
            return redirect(url_for("login"))
        flash(t("flash.session_revoked"), "success")
        return redirect(url_for("user_settings") + "#active-sessions")

    @app.route("/settings/sessions/logout-all", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def logout_all_active_sessions():
        """Revoke every login session for the current account."""
        user = get_current_user()
        if session.get("impersonator_id"):
            abort(403)
        count = db.revoke_all_user_sessions(user["id"])
        db.update_user(user["id"], session_token=None)
        session.clear()
        token_count = _revoke_api_tokens_after_credential_change(
            user["id"], "the user signed out of all sessions"
        )
        log_action("logout_all_sessions", request, user=user, revoked_count=count,
                   revoked_api_tokens=token_count)
        flash(t("flash.all_sessions_revoked"), "info")
        return redirect(url_for("login"))

    @app.route("/settings/sessions/history/clear", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def clear_session_history():
        """Clear revoked and expired session records for the current user."""
        user = get_current_user()
        if session.get("impersonator_id"):
            abort(403)
        count = db.clear_user_session_history(user["id"])
        log_action("clear_session_history", request, user=user, cleared_count=count)
        flash(t("flash.session_history_cleared"), "success")
        return redirect(url_for("user_settings") + "#session-history")

    @app.route("/account-suspended/delete", methods=["POST"])
    @rate_limit(10, 60)
    def suspended_delete_account():
        """Allow password-confirmed self-deletion from the suspension flow."""
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if not user["suspended"] or db.check_suspension_expired(user["id"]):
            return redirect(url_for("user_settings"))
        settings = db.get_site_settings() or {}
        if not settings.get("suspended_account_deletion_enabled"):
            abort(403)
        return _delete_own_account(user, "account_suspended")

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def user_settings():
        """Display and handle the user's account settings page (username, password, profile, avatar)."""
        user = get_current_user()
        action = request.form.get("action", "") if request.method == "POST" else ""

        if action == "change_username":
            if user["is_superuser"]:
                flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
                return redirect(url_for("user_settings"))
            new_username = request.form.get("new_username", "").strip()
            password = request.form.get("password", "")
            if len(password) > MAX_PASSWORD_LENGTH or not check_password_hash(user["password"], password):
                flash(t("flash.incorrect_password"), "error")
            elif len(new_username) < 3:
                flash(t("flash.username_must_be_at_least_3_characters_long"), "error")
            elif len(new_username) > 50:
                flash(t("flash.username_cannot_exceed_50_characters"), "error")
            elif not _is_valid_username(new_username):
                flash(t("flash.username_can_only_contain_letters_digits_underscores_and"), "error")
            elif db.get_user_by_username(new_username) and new_username.lower() != user["username"].lower():
                flash(t("flash.username_already_taken_please_choose_another"), "error")
            else:
                try:
                    db.update_user(user["id"], username=new_username)
                except db.IntegrityError:
                    flash(t("flash.username_already_taken_please_choose_another"), "error")
                    return redirect(url_for("user_settings"))
                else:
                    db.record_username_change(user["id"], user["username"], new_username)
                    db.propagate_mention_rename(user["username"], new_username)
                    log_action("change_username", request, user=user, new_username=new_username)
                    notify_change("user_change_username", f"User '{user['username']}' renamed to '{new_username}'")
                    flash(t("flash.username_updated"), "success")
            return redirect(url_for("user_settings"))

        if action == "change_password":
            if user["is_superuser"]:
                flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
                return redirect(url_for("user_settings"))
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")
            if len(current_pw) > MAX_PASSWORD_LENGTH or not check_password_hash(user["password"], current_pw):
                flash(t("flash.incorrect_current_password"), "error")
            elif new_pw != confirm_pw:
                flash(t("flash.new_passwords_do_not_match"), "error")
            elif len(new_pw) < MIN_PASSWORD_LENGTH:
                flash(t("flash.password_must_contain_at_least_minpasswordlength_characters", MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH), "error")
            elif len(new_pw) > MAX_PASSWORD_LENGTH:
                flash(t("flash.password_cannot_exceed_maxpasswordlength_characters", MAX_PASSWORD_LENGTH=MAX_PASSWORD_LENGTH), "error")
            else:
                # Always rotate the session token on password change so any
                # other active sessions for this user (different browser,
                # mobile, stolen cookie) are invalidated on their next
                # request via the token-mismatch branch in
                # ``before_request_hook``.  This is enforced regardless of
                # the ``session_limit_enabled`` site setting.
                token = uuid.uuid4().hex
                update_fields = {
                    "password": generate_password_hash(new_pw),
                    "session_token": token,
                }
                session["session_token"] = token
                db.update_user(user["id"], **update_fields)
                current_session_id = current_user_session_id()
                if current_session_id:
                    db.revoke_other_user_sessions(user["id"], current_session_id)
                token_count = _revoke_api_tokens_after_credential_change(
                    user["id"], "the user changed the password"
                )
                log_action("change_password", request, user=user, revoked_api_tokens=token_count)
                notify_change("user_change_password", f"User '{user['username']}' changed password")
                flash(t("flash.password_updated_other_active_sessions_for_this_account"), "success")
            return redirect(url_for("user_settings"))

        if action == "delete_account":
            return _delete_own_account(user, "user_settings")

        if action == "toggle_owner":
            if user["role"] not in ("admin", "owner"):
                flash(t("flash.only_admins_can_toggle_owner_status"), "error")
                return redirect(url_for("user_settings"))
            password = request.form.get("password", "")
            if len(password) > MAX_PASSWORD_LENGTH or not check_password_hash(user["password"], password):
                flash(t("flash.incorrect_password"), "error")
                return redirect(url_for("user_settings"))
            if user["role"] == "admin":
                # Same rule as the admin role change: a scheduled role revert
                # has to be removed first.  The temporary-role routes refuse
                # owner accounts, so once this account were an owner nobody
                # could remove the schedule, and the expiry would still take
                # owner status away (it must: skipping owners there would let
                # a temporary admin keep the grant by turning owner on).
                if db.get_role_expiry(user["id"]) is not None:
                    flash(
                        t(
                            "flash.owner_status_blocked_by_temporary_role",
                            default="This account has a temporary role schedule. Owner status can be turned on once an admin removes that schedule.",
                        ),
                        "error",
                    )
                    return redirect(url_for("user_settings"))
                db.update_user(user["id"], role="owner")
                db.record_role_change(user["id"], "admin", "owner", changed_by=user["id"])
                log_action("enable_owner", request, user=user)
                notify_change("user_enable_owner", f"User '{user['username']}' enabled owner status")
                flash(t("flash.owner_status_enabled"), "success")
            else:
                if db.count_owners() <= 1:
                    flash(t("flash.cannot_demote_the_last_owner"), "error")
                    return redirect(url_for("user_settings"))
                db.update_user(user["id"], role="admin")
                db.record_role_change(user["id"], "owner", "admin", changed_by=user["id"])
                log_action("disable_owner", request, user=user)
                notify_change("user_disable_owner", f"User '{user['username']}' disabled owner status")
                flash(t("flash.owner_status_disabled"), "success")
            return redirect(url_for("user_settings"))

        if action == "update_profile":
            real_name = request.form.get("real_name", "").strip()[:100]
            bio = request.form.get("bio", "").strip()[:500]
            try:
                birth_date = normalize_birth_date(request.form.get("birth_date", ""))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(_profile_next(url_for("user_settings")))
            avatar_file = request.files.get("avatar")
            profile = db.get_user_profile(user["id"])
            old_avatar = profile["avatar_filename"] if profile else ""
            new_avatar = old_avatar
            if avatar_file and avatar_file.filename:
                if not allowed_file(avatar_file.filename):
                    flash(t("flash.invalid_avatar_file_type"), "error")
                    return redirect(url_for("user_settings"))
                # 1 MB limit for avatars
                avatar_file.stream.seek(0, 2)
                size = avatar_file.stream.tell()
                avatar_file.stream.seek(0)
                if size > 1 * 1024 * 1024:
                    flash(t("flash.avatar_file_size_cannot_exceed_1_mb"), "error")
                    return redirect(url_for("user_settings"))
                try:
                    img = Image.open(avatar_file.stream)
                    img.verify()
                    avatar_file.stream.seek(0)
                except Exception:
                    flash(t("flash.avatar_is_not_a_valid_image"), "error")
                    return redirect(url_for("user_settings"))
                avatar_dir = os.path.join(config.UPLOAD_FOLDER, "avatars")
                os.makedirs(avatar_dir, exist_ok=True)
                ext = _safe_ext(avatar_file.filename)
                if not ext:
                    flash(t("flash.invalid_file_extension"), "error")
                    return redirect(url_for("user_settings"))
                new_avatar = f"avatars/{uuid.uuid4().hex}.{ext}"
                save_path = os.path.abspath(os.path.join(config.UPLOAD_FOLDER, new_avatar))
                if os.path.commonpath([os.path.abspath(config.UPLOAD_FOLDER), save_path]) != os.path.abspath(config.UPLOAD_FOLDER):
                    flash(t("flash.invalid_upload_path"), "error")
                    return redirect(url_for("user_settings"))
                try:
                    avatar_file.save(save_path)
                except OSError:
                    flash(t("flash.failed_to_save_avatar_file"), "error")
                    return redirect(url_for("user_settings"))
                notify_file_upload(new_avatar, save_path, display_name=f"Avatar for {user['username']}")
                # Remove old avatar file if different
                if old_avatar and old_avatar != new_avatar:
                    safe_unlink_in(config.UPLOAD_FOLDER, old_avatar)
                    notify_file_deleted(old_avatar)
            db.upsert_user_profile(
                user["id"],
                real_name=real_name,
                bio=bio,
                birth_date=birth_date,
                avatar_filename=new_avatar,
            )
            # Also save profile fields if included in the main form
            if db.is_plugin_enabled("user_profiles"):
                profile_fields_data = request.form.getlist("profile_field_key")
                profile_fields_visible = request.form.getlist("profile_field_visible")
                visible_set = set(profile_fields_visible)
                for key in profile_fields_data:
                    value = request.form.get(f"profile_value_{key}", "").strip()
                    visible = key in visible_set
                    try:
                        db.save_user_field_value(user["id"], key, value, visible)
                    except Exception:
                        pass
            log_action("update_profile", request, user=user)
            flash(t("flash.profile_has_been_successfully_updated"), "success")
            return redirect(_profile_next(url_for("user_settings")))

        if action == "remove_avatar":
            profile = db.get_user_profile(user["id"])
            if profile and profile["avatar_filename"]:
                safe_unlink_in(config.UPLOAD_FOLDER, profile["avatar_filename"])
                notify_file_deleted(profile["avatar_filename"])
                db.upsert_user_profile(user["id"], avatar_filename="")
            flash(t("flash.avatar_has_been_successfully_removed"), "success")
            return redirect(_profile_next(url_for("user_settings")))

        if action == "publish_profile":
            profile = db.get_user_profile(user["id"])
            if profile and profile["page_disabled_by_admin"]:
                flash(t("flash.your_profile_page_has_been_disabled_by_an"), "error")
                return redirect(url_for("user_settings"))
            db.upsert_user_profile(user["id"], page_published=True)
            log_action("publish_profile", request, user=user)
            flash(t("flash.your_profile_page_is_now_public"), "success")
            return redirect(_profile_next(url_for("user_settings")))

        if action == "unpublish_profile":
            db.upsert_user_profile(user["id"], page_published=False)
            log_action("unpublish_profile", request, user=user)
            flash(t("flash.your_profile_page_is_now_hidden"), "success")
            return redirect(_profile_next(url_for("user_settings")))

        if action == "delete_profile":
            profile = db.get_user_profile(user["id"])
            if profile and profile["avatar_filename"]:
                safe_unlink_in(config.UPLOAD_FOLDER, profile["avatar_filename"])
                notify_file_deleted(profile["avatar_filename"])
            db.delete_user_profile(user["id"])
            log_action("delete_profile", request, user=user)
            flash(t("flash.your_profile_page_has_been_deleted"), "success")
            return redirect(_profile_next(url_for("user_settings")))

        if action == "toggle_group_badge":
            settings = db.get_site_settings()
            if not (settings and settings.get("profile_group_badges_enabled")):
                flash(t("flash.profile_group_badges_are_not_enabled"), "error")
                return redirect(url_for("user_settings"))
            try:
                group_id = int(request.form.get("group_id", 0))
            except (TypeError, ValueError):
                flash(t("flash.invalid_group"), "error")
                return redirect(url_for("user_settings"))
            if not db.is_group_member(group_id, user["id"]):
                flash(t("flash.you_are_not_a_member_of_this_group"), "error")
                return redirect(url_for("user_settings"))
            visible = request.form.get("visible") == "1"
            db.set_profile_group_badge_visible(user["id"], group_id, visible)
            flash(t("flash.group_badge_visibility_has_been_successfully_updated"), "success")
            return redirect(url_for("user_settings") + "#profile-group-badges")

        if action == "save_profile_fields":
            if not db.is_plugin_enabled("user_profiles"):
                flash(t("flash.feature_not_available"), "error")
                return redirect(url_for("user_settings"))
            profile_fields_data = request.form.getlist("profile_field_key")
            profile_fields_visible = request.form.getlist("profile_field_visible")
            visible_set = set(profile_fields_visible)
            had_error = False
            for key in profile_fields_data:
                value = request.form.get(f"profile_value_{key}", "").strip()
                visible = key in visible_set
                try:
                    ok = db.save_user_field_value(user["id"], key, value, visible)
                    if not ok:
                        had_error = True
                except Exception as exc:
                    _logger.warning(
                        "save_profile_fields failed for user_id=%s field_key=%s: %s",
                        user["id"], key, exc,
                    )
                    had_error = True
            if had_error:
                flash(t("flash.profile_fields_save_error"), "error")
            else:
                flash(t("flash.profile_fields_updated"), "success")
            return redirect(url_for("user_profile", username=user["username"]) + "#profile-fields")

        if action == "self_unsuspend":
            if not user["suspended"]:
                flash(t("flash.your_account_is_not_suspended"), "error")
            elif user["role"] not in ("admin", "owner"):
                flash(t("flash.only_admins_can_reactivate_their_own_account"), "error")
            else:
                db.update_user(
                    user["id"],
                    suspended=0,
                    suspended_until=None,
                    suspend_reason=None,
                    suspend_reason_visible=0,
                    suspend_time_visible=0,
                )
                db.record_suspension_action(
                    user["id"], "unsuspend",
                    performed_by=user["id"],
                )
                log_action("admin_unsuspend", request, user=user,
                           target_user=user["username"])
                notify_change("admin_unsuspend", f"User '{user['username']}' reactivated their own account")
                flash(t("flash.your_account_has_been_reactivated"), "success")
            return redirect(url_for("user_settings"))

        profile = db.get_user_profile(user["id"])
        pending_quota_request = db.get_pending_reservation_quota_request(user["id"])
        # Temporary expiry info
        user_expiry = None
        role_expiry = None
        try:
            user_expiry = db.get_user_expiry(user["id"])
            role_expiry = db.get_role_expiry(user["id"])
        except Exception:
            pass
        # Profile group badge settings for the account settings page
        profile_group_settings = []
        site_settings = db.get_site_settings()
        profile_group_badges_enabled = bool(
            site_settings and site_settings.get("profile_group_badges_enabled")
        )
        if profile_group_badges_enabled:
            profile_group_settings = db.get_user_profile_group_settings(user["id"])
        # Profile fields definitions and values for the settings page
        profile_field_defs = []
        profile_field_values = {}
        if db.is_plugin_enabled("user_profiles"):
            try:
                profile_field_defs = db.get_field_definitions()
                profile_field_values = db.get_user_field_values(user["id"])
            except Exception:
                pass
        new_userbot_token = session.pop("new_userbot_token", None)
        active_sessions = []
        session_history = []
        current_session_id = current_user_session_id()
        if not session.get("impersonator_id"):
            for row in db.list_active_user_sessions(user["id"]):
                item = dict(row)
                item["is_current"] = item["id"] == current_session_id
                item["device_label"] = describe_session_user_agent(item["user_agent"])
                active_sessions.append(item)
            for row in db.list_user_session_history(user["id"]):
                item = dict(row)
                item["device_label"] = describe_session_user_agent(item["user_agent"])
                item["status"] = "ended" if item["revoked_at"] else "expired"
                item["ended_at"] = item["revoked_at"] or item["expires_at"]
                session_history.append(item)
        pending_quota_count = 0
        if user["role"] in ("admin", "owner"):
            pending_quota_count = (
                db.count_pending_reservation_quota_requests()
                + db.count_pending_contribution_quota_requests()
            )
        return render_template(
            "account/settings.html",
            user=user,
            profile=profile,
            new_userbot_token=new_userbot_token,
            reservation_quota=db.get_effective_reserved_pages_quota(user["id"]),
            pending_quota_request=pending_quota_request,
            pending_quota_count=pending_quota_count,
            user_expiry=user_expiry,
            role_expiry=role_expiry,
            profile_group_settings=profile_group_settings,
            profile_group_badges_enabled=profile_group_badges_enabled,
            profile_field_defs=profile_field_defs,
            profile_field_values=profile_field_values,
            active_sessions=active_sessions,
            session_history=session_history,
        )

    @app.route("/account/reservation-quota", methods=["GET", "POST"])
    @login_required
    def account_reservation_quota_redirect():
        """Redirect legacy reservation-quota paths to the new settings page."""
        return redirect(url_for("user_reservation_quota"), code=301)

    @app.route("/settings/reservation-quota", methods=["GET", "POST"])
    @login_required
    @editor_required
    @rate_limit(10, 60)
    def user_reservation_quota():
        """Display and handle the current user's reservation quota requests."""
        user = get_current_user()
        is_admin_user = user["role"] in ("admin", "owner")

        if request.method == "POST":
            action = request.form.get("action", "")

            # Admins set their own quota directly. No request needed
            if action == "set_quota" and is_admin_user:
                unlimited = request.form.get("unlimited") == "1"
                if unlimited:
                    new_quota = -1
                else:
                    new_quota = request.form.get("new_quota", type=int)
                    if new_quota is None or new_quota < 1:
                        flash(t("flash.a_valid_quota_amount_is_required_to_continue"), "error")
                        return redirect(url_for("user_reservation_quota"))
                try:
                    db.set_user_reserved_pages_quota(user["id"], new_quota)
                except Exception as exc:
                    flash(str(exc), "error")
                else:
                    label = "Unlimited" if new_quota == -1 else str(new_quota)
                    log_action(
                        "admin_set_own_quota",
                        request,
                        user=user,
                        new_quota=label,
                    )
                    flash(t("flash.your_reservation_quota_has_been_successfully_updated_to", label=label), "success")
                return redirect(url_for("user_reservation_quota"))

            # Non-admin: cancel a pending quota request
            if action == "cancel_request" and not is_admin_user:
                request_id = request.form.get("request_id", type=int)
                if request_id:
                    try:
                        db.cancel_reservation_quota_request(request_id, user["id"])
                    except ValueError as exc:
                        flash(str(exc), "error")
                    else:
                        log_action(
                            "cancel_reservation_quota_request",
                            request,
                            user=user,
                            request_id=request_id,
                        )
                        flash(t("flash.quota_request_has_been_successfully_cancelled"), "success")
                return redirect(url_for("user_reservation_quota"))

            # Non-admin: submit a new quota request
            if action == "submit_quota_request" and not is_admin_user:
                unlimited = request.form.get("unlimited") == "1"
                if unlimited:
                    requested_quota = -1
                else:
                    requested_quota = request.form.get("requested_quota", type=int)
                    if requested_quota is None:
                        flash(t("flash.requested_quota_is_required_to_continue"), "error")
                        return redirect(url_for("user_reservation_quota"))
                reason = request.form.get("reason", "").strip()
                try:
                    quota_request = db.create_reservation_quota_request(
                        user["id"], requested_quota, reason
                    )
                except ValueError as exc:
                    flash(str(exc), "error")
                else:
                    log_action(
                        "submit_reservation_quota_request",
                        request,
                        user=user,
                        requested_quota=requested_quota,
                    )
                    if quota_request["status"] == "approved":
                        flash(t("flash.quota_request_automatically_approved"), "success")
                    else:
                        notify_change(
                            "reservation_quota_request_submit",
                            f"User '{user['username']}' submitted a reservation quota request",
                        )
                        flash(t("flash.quota_request_has_been_successfully_submitted"), "success")
                return redirect(url_for("user_reservation_quota"))

        current_quota = db.get_effective_reserved_pages_quota(user["id"])
        return render_template(
            "account/reservation_quota.html",
            target_user=user,
            current_quota=current_quota,
            default_quota=db.get_default_reserved_pages_quota(),
            active_reservation_count=db.get_user_active_reservation_count(user["id"]),
            pending_request=db.get_pending_reservation_quota_request(user["id"]),
            quota_requests=db.list_reservation_quota_requests(user["id"]),
            max_reason_length=db.MAX_QUOTA_REQUEST_REASON_LENGTH,
            max_review_reason_length=db.MAX_QUOTA_REVIEW_REASON_LENGTH,
            admin_view=False,
            is_admin_user=is_admin_user,
        )

    @app.route("/account/export")
    @login_required
    def account_export_redirect():
        """Redirect legacy export paths to the new settings page."""
        return redirect(url_for("export_own_data"), code=301)

    @app.route("/settings/export")
    @login_required
    @rate_limit(5, 60, exempt_html_nav=False)
    def export_own_data():
        """Allow a logged-in user to download all their own data as a ZIP file."""
        user = get_current_user()
        buf = build_user_export_zip(user)
        filename = f"userdata_{user['username']}.zip"
        log_action("export_own_data", request, user=user)
        return send_file(buf, mimetype="application/zip",
                         as_attachment=True, download_name=filename)

    @app.route("/users")
    @login_required
    def users_list():
        """Render the People directory; admins see all users, others see published profiles."""
        query = request.args.get("q", "").strip().lower()
        current_user = get_current_user()
        if current_user["role"] in ("admin", "owner"):
            users = db.list_all_users_with_profiles()
        else:
            users = db.list_published_profiles()
            # Normalise column names so template works for both result sets
            users = [dict(u) for u in users]
            for u in users:
                u.setdefault("role", "user")
                u.setdefault("suspended", 0)
                u.setdefault("page_published", 1)
                u.setdefault("page_disabled_by_admin", 0)
        if query:
            users = [u for u in users if query in u["username"].lower()
                     or query in (u["real_name"] or "").lower()]
        return render_template("users/list.html", users=users, query=query)

    @app.route("/users/<string:username>")
    @login_required
    def user_profile(username):
        """Render a user's public profile page with contribution heatmap and role history."""
        target = db.get_user_by_username(username)
        if not target:
            abort(404)
        profile = db.get_user_profile(target["id"])
        current_user = get_current_user()
        is_admin = current_user["role"] in ("admin", "owner")
        is_own = current_user["id"] == target["id"]
        settings = db.get_site_settings()
        contribution_chart_enabled = bool(
            settings.get("profile_contribution_chart_enabled", 1)
        ) if settings else True
        # Only admins and the user themselves can view unpublished/disabled profiles
        if not (is_admin or is_own):
            if not profile or not profile["page_published"] or profile["page_disabled_by_admin"]:
                abort(404)
        contribution_years = db.get_contribution_years(target["id"]) if contribution_chart_enabled else []
        year_query = request.args.get("year", "").strip()
        requested_year = None
        if year_query:
            try:
                requested_year = int(year_query)
            except ValueError:
                requested_year = None
        selected_year = requested_year if requested_year in contribution_years else None
        contrib_year, contributions = db.get_contributions_by_day(
            target["id"],
            year=selected_year,
        )
        if contribution_chart_enabled and contrib_year not in contribution_years:
            contribution_years = [contrib_year] + contribution_years
        contribution_list = db.get_user_contributions(target["id"])
        contribution_year_list = (
            db.get_user_contributions(target["id"], year=contrib_year)
            if contribution_chart_enabled else []
        )
        if not (is_admin or is_own):
            contribution_list, contributions = _filter_visible_profile_contributions(
                current_user, contribution_list, contrib_year
            )
            contribution_year_list = [
                c for c in contribution_year_list
                if c["page_slug"] and user_can_view_page(current_user, c)
            ]
        contribution_day_details = {}
        if contribution_chart_enabled:
            for c in contribution_year_list:
                day = c["created_at"][:10]
                contribution_day_details.setdefault(day, []).append({
                    "created_at": c["created_at"],
                    "page_title": c["page_title"],
                    "page_slug": c["page_slug"],
                    "edit_message": c["edit_message"],
                })
        role_history = db.get_role_history(target["id"])
        custom_tags = db.get_user_custom_tags(target["id"])
        user_badges = db.get_user_badges(target["id"], include_revoked=False)
        all_users = db.list_users() if is_admin else []
        # Temporary expiry info for users and roles
        user_expiry = None
        role_expiry = None
        try:
            user_expiry = db.get_user_expiry(target["id"])
            role_expiry = db.get_role_expiry(target["id"])
        except Exception:
            pass
        # Profile group badges, only fetch when the feature is globally enabled
        profile_group_badges = []
        if settings and settings.get("profile_group_badges_enabled"):
            profile_group_badges = db.get_profile_group_badges(target["id"])
        # Profile fields: shown before bio (merged from former user_profile_fields plugin)
        profile_fields = []
        profile_field_defs = []
        profile_field_values = {}
        if db.is_plugin_enabled("user_profiles"):
            try:
                profile_fields = db.get_visible_user_field_values(target["id"])
                if is_own or is_admin:
                    profile_field_defs = db.get_field_definitions()
                    profile_field_values = db.get_user_field_values(target["id"])
            except Exception:
                pass
        return render_template(
            "users/profile.html",
            target=target,
            profile=profile,
            contributions=contributions,
            contrib_year=contrib_year,
            contribution_list=contribution_list,
            is_own=is_own,
            is_admin=is_admin,
            role_labels=ROLE_LABELS,
            role_history=role_history,
            custom_tags=custom_tags,
            user_badges=user_badges,
            all_users=all_users,
            user_expiry=user_expiry,
            role_expiry=role_expiry,
            profile_group_badges=profile_group_badges,
            contribution_chart_enabled=contribution_chart_enabled,
            contribution_years=contribution_years,
            contribution_day_details=contribution_day_details,
            profile_fields=profile_fields,
            profile_field_defs=profile_field_defs,
            profile_field_values=profile_field_values,
            birthday_celebration=is_birthday_today(profile["birth_date"] if profile else ""),
        )

    @app.route("/account/language", methods=["POST"])
    def account_language_redirect():
        """Redirect legacy language paths to the new settings page."""
        return redirect(url_for("user_change_language"), code=301)

    @app.route("/settings/language", methods=["POST"])
    @rate_limit(30, 60)
    def user_change_language():
        """Update the guest interface language preference in the session."""
        language = request.form.get("language", "default")
        session["interface_language"] = language
        user = get_current_user()
        if user:
            # If logged in, also update user preferences
            try:
                prefs = db.get_user_accessibility(user["id"])
                prefs["interface_language"] = language
                db.save_user_accessibility(user["id"], prefs)
            except Exception:
                pass
        return redirect(_safe_referrer() or url_for("home"))

    @app.route("/force-change-password", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def force_change_password():
        """Force the user to change their password (and optionally username).

        Hosted instances seeded with random credentials set
        ``force_password_change`` on the admin user so that after the
        first login the user is redirected here instead of the home page.
        Once the password has been successfully changed the flag is cleared
        and the user is redirected to the home page.
        """
        user = get_current_user()
        if not user.get("force_password_change"):
            return redirect(url_for("home"))

        if request.method == "POST":
            action = request.form.get("action", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if action == "change_password":
                if new_password != confirm_password:
                    flash(t("flash.new_passwords_do_not_match"), "error")
                elif len(new_password) < MIN_PASSWORD_LENGTH:
                    flash(t("flash.password_must_contain_at_least_minpasswordlength_characters",
                            MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH), "error")
                elif len(new_password) > MAX_PASSWORD_LENGTH:
                    flash(t("flash.password_cannot_exceed_maxpasswordlength_characters",
                            MAX_PASSWORD_LENGTH=MAX_PASSWORD_LENGTH), "error")
                else:
                    if check_password_hash(user["password"], new_password):
                        flash(t("flash.force_password_same_as_current"), "warning")

                    new_username = (request.form.get("new_username", "") or user["username"]).strip()
                    update_fields = {
                        "password": generate_password_hash(new_password),
                        "force_password_change": 0,
                    }
                    if new_username and new_username != user["username"]:
                        if len(new_username) < 3:
                            flash(t("flash.username_must_be_at_least_3_characters_long"), "error")
                            return redirect(url_for("force_change_password"))
                        if len(new_username) > 50:
                            flash(t("flash.username_cannot_exceed_50_characters"), "error")
                            return redirect(url_for("force_change_password"))
                        if not _is_valid_username(new_username):
                            flash(t("flash.username_can_only_contain_letters_digits_underscores_and"), "error")
                            return redirect(url_for("force_change_password"))
                        existing = db.get_user_by_username(new_username)
                        if existing and existing["id"] != user["id"]:
                            flash(t("flash.username_already_taken_please_choose_another"), "error")
                            return redirect(url_for("force_change_password"))
                        update_fields["username"] = new_username
                        db.record_username_change(user["id"], user["username"], new_username)
                        db.propagate_mention_rename(user["username"], new_username)
                        log_action("change_username", request, user=user, new_username=new_username)
                        notify_change("user_change_username",
                                       f"User '{user['username']}' renamed to '{new_username}'")

                    db.update_user(user["id"], **update_fields)
                    current_session_id = current_user_session_id()
                    if current_session_id:
                        db.revoke_other_user_sessions(user["id"], current_session_id)
                    token_count = _revoke_api_tokens_after_credential_change(
                        user["id"], "the user completed a required password change"
                    )
                    log_action("force_change_password", request, user=user,
                               revoked_api_tokens=token_count)
                    notify_change("user_force_change_password",
                                   f"User '{user['username']}' completed forced password change")
                    flash(t("flash.password_updated_other_active_sessions_for_this_account"), "success")
                    refreshed = db.get_user_by_id(user["id"])
                    if refreshed and refreshed.get("onboarding_required"):
                        return redirect(url_for("onboarding_setup"))
                    if refreshed and refreshed.get("intro_required"):
                        return redirect(url_for("onboarding_intro"))
                    return redirect(url_for("home"))
            else:
                flash(t("flash.invalid_action"), "error")
            return redirect(url_for("force_change_password"))

        return render_template("account/force_change_password.html", user=user)

    # Merging is a multi-step flow: the routes below only move the request
    # between states, and the actual data transfer happens in db/_account_merges.

    @app.route("/settings/merge-request", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 300)
    def merge_request():
        """Allow a user to request merging another account into their own.

        POST: Accepts a username to merge INTO and a reason. Creates a merge
        request that can be approved by both parties or by an admin.
        """
        user = get_current_user()

        if request.method == "POST":
            target_username = request.form.get("target_username", "").strip()
            reason = request.form.get("reason", "").strip().strip("\n").strip()

            if not target_username:
                flash(t("flash.please_specify_the_username_to_merge_into_your_account"), "error")
                return redirect(url_for("merge_request"))

            target = db.get_user_by_username(target_username)
            if not target:
                flash(t("flash.no_user_found_with_that_username"), "error")
                return redirect(url_for("merge_request"))

            if target["id"] == user["id"]:
                flash(t("flash.cannot_merge_with_your_own_account"), "error")
                return redirect(url_for("merge_request"))

            # Check for existing requests
            pending = db.get_active_requests_for_user(user["id"])
            is_source = any(r["source_user_id"] == user["id"] for r in pending)
            is_target = any(r["target_user_id"] == user["id"] for r in pending)

            if is_source:
                flash(t("flash.you_already_have_a_pending_merge_request_as_source"), "error")
                return redirect(url_for("merge_pending"))
            if is_target:
                flash(t("flash.you_are_already_the_target_of_a_pending_merge_request"), "error")
                return redirect(url_for("merge_pending"))

            # Check target has no active requests as source
            target_pending = db.get_active_requests_for_user(target["id"])
            if any(r["source_user_id"] == target["id"] for r in target_pending):
                flash(t("flash.this_account_is_already_part_of_a_pending_merge_as_source"), "error")
                return redirect(url_for("merge_request"))

            try:
                db.create_merge_request(target["id"], user["id"], user["id"], reason)
                # Track on target_user as well
                db.update_user(user["id"], pending_merge_target_id=target["id"])
                db.update_user(target["id"], pending_merge_source_id=user["id"])
                log_action("merge_request_created", request, user=user,
                           merge_target=target["username"])
                notify_change("merge_request_created",
                              f"User '{user['username']}' requested to merge account '{target['username']}' into theirs")
                flash(t("flash.merge_request_sent_to_target_account"), "success")
                return redirect(url_for("merge_pending"))
            except ValueError as exc:
                flash(str(exc), "error")

        return render_template("account/merge_request.html")

    @app.route("/settings/merge-request/from/<string:target_username>", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 300)
    def merge_request_from(target_username):
        """Allow a user to request merging a target account INTO their own."""
        user = get_current_user()
        # The target is the account that will be merged FROM (locked/deleted)
        # The user is the account that will survive
        # So we want: source=target_user, target=current_user
        target = db.get_user_by_username(target_username)
        if not target:
            flash(t("flash.no_user_found_with_that_username"), "error")
            return redirect(url_for("merge_request"))
        if target["id"] == user["id"]:
            flash(t("flash.cannot_merge_with_your_own_account"), "error")
            return redirect(url_for("merge_request"))

        if request.method == "POST":
            reason = request.form.get("reason", "").strip().strip("\n").strip()

            pending = db.get_active_requests_for_user(user["id"])
            is_source = any(r["source_user_id"] == user["id"] for r in pending)
            is_target = any(r["target_user_id"] == user["id"] for r in pending)

            if is_source:
                flash(t("flash.you_already_have_a_pending_merge_request_as_source"), "error")
                return redirect(url_for("merge_pending"))
            if is_target:
                flash(t("flash.you_are_already_the_target_of_a_pending_merge_request"), "error")
                return redirect(url_for("merge_pending"))

            try:
                db.create_merge_request(target["id"], user["id"], user["id"], reason)
                db.update_user(user["id"], pending_merge_target_id=target["id"])
                db.update_user(target["id"], pending_merge_source_id=user["id"])
                log_action("merge_request_created", request, user=user,
                           merge_target=target["username"])
                notify_change("merge_request_created",
                              f"User '{user['username']}' requested to merge account '{target['username']}' into theirs")
                flash(t("flash.merge_request_sent_to_target_account"), "success")
                return redirect(url_for("merge_pending"))
            except ValueError as exc:
                flash(str(exc), "error")

        return render_template("account/merge_request.html", target_user=target)

    @app.route("/settings/merge/pending")
    @login_required
    def merge_pending():
        """Display pending and completed merge requests for the current user."""
        user = get_current_user()
        pending = db.get_active_requests_for_user(user["id"])
        # Also get completed/merged requests
        completed = db.get_db().execute(
            "SELECT * FROM account_merge_requests WHERE "
            "(source_user_id = ? OR target_user_id = ?) AND status = 'merged' "
            "ORDER BY completed_at DESC",
            (user["id"], user["id"]),
        ).fetchall()

        # Enrich with user info
        enriched = []
        for req in pending:
            source = db.get_user_by_id(req["source_user_id"])
            target = db.get_user_by_id(req["target_user_id"])
            enriched.append({
                **req,
                "source_username": source["username"] if source else "deleted",
                "target_username": target["username"] if target else "deleted",
                "source_is_current": req["source_user_id"] == user["id"],
                "target_is_current": req["target_user_id"] == user["id"],
            })

        completed_enriched = []
        for req in completed:
            source = db.get_user_by_id(req["source_user_id"])
            target = db.get_user_by_id(req["target_user_id"])
            completed_enriched.append({
                **req,
                "source_username": source["username"] if source else "deleted",
                "target_username": target["username"] if target else "deleted",
            })

        return render_template(
            "account/merge_pending.html",
            pending=sorted(enriched, key=lambda r: r["created_at"], reverse=True),
            completed=completed_enriched,
        )

    @app.route("/settings/merge/approve/<int:merge_id>", methods=["POST"])
    @login_required
    def merge_approve(merge_id):
        """Approve a merge request as either the source or target party."""
        user = get_current_user()
        req = db.get_merge_request(merge_id)
        if not req:
            flash(t("flash.merge_request_not_found"), "error")
            return redirect(url_for("merge_pending"))

        if req["status"] not in ("pending",):
            flash(t("flash.this_merge_request_cannot_be_approved"), "error")
            return redirect(url_for("merge_pending"))

        if req["source_user_id"] == user["id"]:
            db.approve_by_source(merge_id, user["id"])
            log_action("merge_approved_by_source", request, user=user, merge_id=merge_id)
            flash(t("flash.you_have_approved_the_merge_request"), "success")
        elif req["target_user_id"] == user["id"]:
            db.approve_by_target(merge_id, user["id"])
            log_action("merge_approved_by_target", request, user=user, merge_id=merge_id)
            flash(t("flash.you_have_approved_the_merge_request"), "success")
        else:
            flash(t("flash.you_are_not_part_of_this_merge_request"), "error")

        return redirect(url_for("merge_pending"))

    @app.route("/settings/merge/cancel/<int:merge_id>", methods=["POST"])
    @login_required
    def merge_cancel(merge_id):
        """Cancel a merge request."""
        user = get_current_user()
        req = db.get_merge_request(merge_id)
        if not req:
            flash(t("flash.merge_request_not_found"), "error")
            return redirect(url_for("merge_pending"))

        # Only the initiator, the source user, or the target user can cancel
        can_cancel = (
            req["created_by"] == user["id"]
            or req["source_user_id"] == user["id"]
            or req["target_user_id"] == user["id"]
        )
        if not can_cancel:
            flash(t("flash.you_cannot_cancel_this_merge_request"), "error")
            return redirect(url_for("merge_pending"))

        try:
            db.cancel_merge(merge_id, user["id"])
            log_action("merge_cancelled", request, user=user, merge_id=merge_id)
            flash(t("flash.merge_request_has_been_cancelled"), "success")
        except ValueError as exc:
            flash(str(exc), "error")

        return redirect(url_for("merge_pending"))

    @app.route("/settings/merge/deny/<int:merge_id>", methods=["POST"])
    @login_required
    def merge_deny(merge_id):
        """Deny a merge request (as source or target)."""
        user = get_current_user()
        req = db.get_merge_request(merge_id)
        if not req:
            flash(t("flash.merge_request_not_found"), "error")
            return redirect(url_for("merge_pending"))

        if req["source_user_id"] == user["id"]:
            # Source cannot deny directly, they cancel instead
            flash(t("flash.source_account_should_cancel_the_request_instead"), "error")
        elif req["target_user_id"] == user["id"]:
            try:
                db.cancel_merge(merge_id, user["id"])
                log_action("merge_denied_by_target", request, user=user, merge_id=merge_id)
                flash(t("flash.you_have_denied_the_merge_request"), "success")
            except ValueError as exc:
                flash(str(exc), "error")
        else:
            flash(t("flash.you_are_not_part_of_this_merge_request"), "error")

        return redirect(url_for("merge_pending"))
