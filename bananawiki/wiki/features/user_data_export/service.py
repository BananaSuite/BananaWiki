"""Personal data export as a streamed ZIP archive.

The archive holds the account, profile and preferences, the account's own
rows of the tables listed in :data:`EXPORTED` (``data/<table>.json``), and
the files the account uploaded. It is an allow-list: every table with a
foreign key to ``users(id)`` is either in :data:`EXPORTED`, with the columns
and rows that are the account's own, or in :data:`NOT_EXPORTED` with the
reason (a test keeps the two complete, so a new feature has to decide).

What is never exported:

* credentials: password hashes, token and session digests, invite codes,
  webhook secrets, stored API answers (``api_service__idempotency``);
* what administrators keep about the account without showing it: hidden
  suspension reasons and end times, impersonation records, who changed a
  role or awarded a badge, internal tags;
* what the account did to others as an administrator (those rows describe
  the other accounts or the site);
* content the account can no longer read: page, draft and proposal text,
  attachment names and files, board, ticket, comment and canvas content, and
  group messages are left out (the row keeps its ids and times) when the
  page, board, canvas or group is no longer readable by the account, checked
  with the same functions the web interface uses. History rows of boards and
  canvases are exported as ids and times only.

Nothing is built in memory: rows and files are written to the ZIP stream as
they are read.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....core.sqlite import quote_identifier, tuples
from ....core.timeutil import now_sql
from ... import storage
from ...db import db
from ..canvas import access as canvas_access
from ..chat import groups as chat_groups
from ..kanban import access as kanban_access
from ..pages import service as pages
from ..users import preferences
from ..users import service as users

FORMAT = "bananawiki-user-data-export"
VERSION = "3.1"
CHUNK = 256 * 1024
ACCOUNT_COLUMNS = ("id", "username", "role", "suspended", "suspended_until", "approval_status", "invite_code",
                   "is_superuser", "created_at", "last_login_at", "chat_disabled", "userbot_enabled")


@dataclass(frozen=True)
class Rows:
    """``data/<table>.json``: what *sql* selects (``?`` stands for the account id).

    With *resource* (``page``, ``board``, ``canvas`` or ``group``) the column
    *resource_id* holds the id of what the row belongs to, and the *content*
    columns are emptied when the account can no longer read it. A row whose
    resource id is NULL (a draft of a page not created yet) belongs to nothing.
    """

    table: str
    sql: str
    resource: str | None = None
    resource_id: str = ""
    content: tuple[str, ...] = ()


_TICKET_BOARD = ("JOIN kanban_tickets t ON t.id = {alias}.ticket_id "
                 "JOIN kanban_columns c ON c.id = t.column_id")

EXPORTED: tuple[Rows, ...] = (
    # Account
    Rows("username_history", "SELECT id, old_username, new_username, changed_at FROM username_history "
                             "WHERE user_id = ? ORDER BY id"),
    Rows("user_sessions", "SELECT created_at, last_seen_at, expires_at, remember_me, auth_method, ip_address, last_ip, "
                          "user_agent, revoked_at, revoked_reason FROM user_sessions WHERE user_id = ? "
                          "ORDER BY created_at"),
    Rows("role_history", "SELECT id, old_role, new_role, changed_at FROM role_history WHERE user_id = ? ORDER BY id"),
    Rows("suspension_audit", "SELECT id, action, CASE WHEN reason_visible = 1 THEN reason END AS reason, "
                             "CASE WHEN time_visible = 1 THEN duration END AS duration, "
                             "CASE WHEN time_visible = 1 THEN suspended_until END AS suspended_until, created_at "
                             "FROM suspension_audit WHERE user_id = ? ORDER BY id"),
    Rows("temp_roles", "SELECT original_role, CASE WHEN show_countdown = 1 THEN expires_at END AS expires_at, "
                       "created_at FROM temp_roles WHERE user_id = ?"),
    Rows("temp_users", "SELECT CASE WHEN show_countdown = 1 THEN expires_at END AS expires_at, created_at "
                       "FROM temp_users WHERE user_id = ?"),
    Rows("user_permissions", "SELECT permission_key FROM user_permissions WHERE user_id = ? ORDER BY permission_key"),
    Rows("user_category_access", "SELECT access_type, restricted FROM user_category_access WHERE user_id = ?"),
    Rows("user_allowed_categories", "SELECT category_id, access_type FROM user_allowed_categories WHERE user_id = ?"),
    Rows("editor_category_access", "SELECT restricted FROM editor_category_access WHERE user_id = ?"),
    Rows("editor_allowed_categories", "SELECT category_id FROM editor_allowed_categories WHERE user_id = ?"),
    Rows("invite_code_usage", "SELECT invite_code_id, used_at FROM invite_code_usage WHERE user_id = ?"),
    Rows("invite_codes", "SELECT id, created_at, expires_at, max_uses, use_count, deleted, deleted_at, assigned_role "
                         "FROM invite_codes WHERE created_by = ? ORDER BY id"),
    Rows("user_notices", "SELECT id, source_id, outcome, object_id, endpoint, created_at, dismissed_at, emailed_at "
                         "FROM user_notices WHERE user_id = ? ORDER BY id"),
    Rows("platform_oauth_links", "SELECT account_id, portal_username, linked_at FROM platform_oauth_links "
                                 "WHERE user_id = ?"),
    Rows("account_merge_requests", "SELECT id, source_user_id, target_user_id, status, source_approved, "
                                   "target_approved, request_reason, completed_at, created_at FROM account_merge_requests "
                                   "WHERE source_user_id = ? OR target_user_id = ? ORDER BY id"),
    Rows("account_merge_logs", "SELECT id, source_user_id, target_user_id, data_transferred, created_at "
                               "FROM account_merge_logs WHERE source_user_id = ? OR target_user_id = ? ORDER BY id"),
    Rows("api_service__tokens", "SELECT id, name, permissions, last_used_at, expires_at, active, created_at "
                                "FROM api_service__tokens WHERE user_id = ? ORDER BY id"),
    Rows("api_service__audit_log", "SELECT id, endpoint, method, status_code, ip_address, duration_ms, created_at "
                                   "FROM api_service__audit_log WHERE user_id = ? ORDER BY id"),
    Rows("user_badges", "SELECT id, badge_type_id, earned_at, revoked, revoked_at FROM user_badges "
                        "WHERE user_id = ? ORDER BY id"),
    Rows("badge_notifications", "SELECT id, badge_type_id, notified, created_at FROM badge_notifications "
                                "WHERE user_id = ? ORDER BY id"),
    Rows("badge_reading_time", "SELECT seconds, updated_at FROM badge_reading_time WHERE user_id = ?"),
    Rows("profile_group_badges", "SELECT group_id, visible, updated_at FROM profile_group_badges WHERE user_id = ?"),
    Rows("announcement_audience_users", "SELECT announcement_id FROM announcement_audience_users WHERE user_id = ?"),
    Rows("assessment_attempts", "SELECT id, assessment_id, attempt_number, total_points, max_points, submitted_at "
                                "FROM assessment_attempts WHERE user_id = ? ORDER BY id"),
    # Pages
    Rows("page_history", "SELECT id, page_id, title, content, builder_json, builder_public, edit_message, is_revert, "
                         "created_at FROM page_history WHERE edited_by = ? ORDER BY id",
         "page", "page_id", ("title", "content", "builder_json", "edit_message")),
    Rows("drafts", "SELECT id, page_id, title, content, updated_at FROM drafts WHERE user_id = ? ORDER BY id",
         "page", "page_id", ("title", "content")),
    Rows("page_builder_drafts", "SELECT page_id, builder_json, updated_at, base_revision FROM page_builder_drafts "
                                "WHERE user_id = ?", "page", "page_id", ("builder_json",)),
    Rows("pending_contributions", "SELECT id, page_id, title, content, reason, status, reviewed_by, review_reason, "
                                  "review_source, reviewed_at, created_at, updated_at, base_revision "
                                  "FROM pending_contributions WHERE user_id = ? ORDER BY id",
         "page", "page_id", ("title", "content", "reason", "review_reason")),
    Rows("contribution_quota_requests", "SELECT id, requested_quota, reason, status, review_reason, review_source, "
                                        "reviewed_at, created_at FROM contribution_quota_requests WHERE user_id = ? "
                                        "ORDER BY id"),
    Rows("reservation_quota_requests", "SELECT id, requested_quota, reason, status, review_reason, review_source, "
                                       "reviewed_at, created_at FROM reservation_quota_requests WHERE user_id = ? "
                                       "ORDER BY id"),
    Rows("page_reservations", "SELECT id, page_id, reserved_at, expires_at, released_at FROM page_reservations "
                              "WHERE user_id = ? ORDER BY id"),
    Rows("page_attachments", "SELECT id, page_id, original_name, file_size, uploaded_at FROM page_attachments "
                             "WHERE uploaded_by = ? ORDER BY id", "page", "page_id", ("original_name",)),
    Rows("page_builder_sections", "SELECT id, name, builder_json, created_at FROM page_builder_sections "
                                  "WHERE created_by = ? ORDER BY id"),
    # Kanban
    Rows("kanban_boards", "SELECT id, title, description, visibility, created_at, archived_at FROM kanban_boards "
                          "WHERE created_by = ? ORDER BY id", "board", "id", ("title", "description")),
    Rows("kanban_tickets", "SELECT t.id, c.board_id, t.column_id, t.title, t.description, t.priority, t.due_date, "
                           "t.labels, t.color, t.created_by = ? AS created_by_me, t.assigned_to = ? AS assigned_to_me, "
                           "t.created_at, t.archived_at FROM kanban_tickets t "
                           "JOIN kanban_columns c ON c.id = t.column_id "
                           "WHERE t.created_by = ? OR t.assigned_to = ? ORDER BY t.id",
         "board", "board_id", ("title", "description", "labels")),
    Rows("kanban_ticket_assignees", "SELECT a.ticket_id, c.board_id, a.assigned_at FROM kanban_ticket_assignees a "
                                    + _TICKET_BOARD.format(alias="a") + " WHERE a.user_id = ? ORDER BY a.ticket_id"),
    Rows("kanban_ticket_comments", "SELECT m.id, m.ticket_id, c.board_id, m.content, m.created_at, m.updated_at "
                                   "FROM kanban_ticket_comments m " + _TICKET_BOARD.format(alias="m")
                                   + " WHERE m.user_id = ? ORDER BY m.id", "board", "board_id", ("content",)),
    Rows("kanban_ticket_attachments", "SELECT a.id, a.ticket_id, c.board_id, a.original_name, a.file_size, "
                                      "a.uploaded_at FROM kanban_ticket_attachments a "
                                      + _TICKET_BOARD.format(alias="a") + " WHERE a.uploaded_by = ? ORDER BY a.id",
         "board", "board_id", ("original_name",)),
    Rows("kanban_ticket_history", "SELECT h.id, h.ticket_id, c.board_id, h.created_at FROM kanban_ticket_history h "
                                  + _TICKET_BOARD.format(alias="h") + " WHERE h.changed_by = ? ORDER BY h.id"),
    Rows("kanban_activity_log", "SELECT id, board_id, action, details, created_at FROM kanban_activity_log "
                                "WHERE user_id = ? ORDER BY id", "board", "board_id", ("details",)),
    Rows("kanban_board_history", "SELECT id, board_id, edit_message, is_revert, created_at FROM kanban_board_history "
                                 "WHERE edited_by = ? ORDER BY id", "board", "board_id", ("edit_message",)),
    # Canvases
    Rows("canvas__layouts", "SELECT id, slug, title, description, category_id, visibility, is_published, is_archived, "
                            "data, version, created_at, updated_at FROM canvas__layouts WHERE creator_id = ? ORDER BY id",
         "canvas", "id", ("slug", "title", "description", "data")),
    Rows("canvas__history", "SELECT id, layout_id, edit_message, is_revert, created_at FROM canvas__history "
                            "WHERE edited_by = ? ORDER BY id", "canvas", "layout_id", ("edit_message",)),
    Rows("canvas__permissions", "SELECT layout_id, permission, created_at FROM canvas__permissions WHERE user_id = ?"),
    # Chat
    Rows("chats", "SELECT id, user1_id, user2_id, created_at FROM chats WHERE user1_id = ? OR user2_id = ? "
                  "ORDER BY id"),
    Rows("chat_messages", "SELECT id, chat_id, CASE WHEN is_deleted = 0 THEN content END AS content, is_deleted, "
                          "deleted_at, ip_address, created_at FROM chat_messages WHERE sender_id = ? ORDER BY id"),
    Rows("group_members", "SELECT group_id, role, timed_out_until, joined_at, banned FROM group_members "
                          "WHERE user_id = ?"),
    Rows("group_chats", "SELECT id, name, description, is_global, is_active, created_at FROM group_chats "
                        "WHERE creator_id = ? ORDER BY id", "group", "id", ("name", "description")),
    Rows("group_messages", "SELECT id, group_id, CASE WHEN is_deleted = 0 THEN content END AS content, is_system, "
                           "is_deleted, deleted_at, ip_address, created_at FROM group_messages WHERE sender_id = ? "
                           "ORDER BY id", "group", "group_id", ("content",)),
)

# Tables with a foreign key to users(id) that are left out, and why.
NOT_EXPORTED = {
    "chat__upload_usage": "short-lived upload rate limiting (24-hour window); technical source ids, no account content",
    "users": "the account itself: account.json",
    "pages": "site content; the account's own edits are in page_history",
    "user_profiles": "profile/profile.json",
    "user_custom_tags": "labels administrators keep about the account, not shown to it",
    "impersonation_logs": "administrators' records of viewing the wiki as the account",
    "api_service__idempotency": "stored API answers may hold secrets; a short-lived retry cache",
    "api_service__webhooks": "site configuration",
    "announcements": "site content",
    "assessments": "site content",
    "badge_types": "site configuration",
    "custom_pages": "site content",
    "custom_roles": "site configuration",
    "federation_shares": "site configuration",
    "temp_pages": "an administrator's schedule for a page",
    "temp_page_index_state": "an administrator's schedule for a page",
    "tts_generations": "a cache of generated audio",
    "editing_sessions": "short-lived editor presence",
    "user_page_cooldowns": "short-lived rate limiting",
    "kanban_events": "the synchronisation log of boards (content of everyone's changes)",
    "canvas__events": "the synchronisation log of canvases (content of everyone's changes)",
}

_ResourceCheck = Callable[[dict[str, Any], Any], bool]


def _page_readable(user: dict[str, Any], page_id: Any) -> bool:
    return pages.can_view(db.one("SELECT * FROM pages WHERE id = ?", (page_id,)), user)


def _board_readable(user: dict[str, Any], board_id: Any) -> bool:
    return kanban_access.can_view(user, db.one("SELECT * FROM kanban_boards WHERE id = ?", (board_id,)))


def _canvas_readable(user: dict[str, Any], layout_id: Any) -> bool:
    return canvas_access.can_view(user, db.one("SELECT * FROM canvas__layouts WHERE id = ?", (layout_id,)))


def _group_readable(user: dict[str, Any], group_id: Any) -> bool:
    return chat_groups.member(group_id, user["id"]) is not None


_READABLE: dict[str, _ResourceCheck] = {
    "page": _page_readable, "board": _board_readable, "canvas": _canvas_readable, "group": _group_readable,
}


class _Readable:
    """Whether the exported account can still read a page, board, canvas or group (asked once per object)."""

    def __init__(self, user: dict[str, Any]) -> None:
        self.user = user
        self._known: dict[tuple[str, Any], bool] = {}

    def __call__(self, kind: str, object_id: Any) -> bool:
        if object_id is None:
            return True
        key = (kind, object_id)
        if key not in self._known:
            self._known[key] = _READABLE[kind](self.user, object_id)
        return self._known[key]

    def redactor(self, spec: Rows) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
        if spec.resource is None:
            return None
        kind = spec.resource

        def redact(row: dict[str, Any]) -> dict[str, Any]:
            if not self(kind, row.get(spec.resource_id)):
                row.update(dict.fromkeys(spec.content))
            return row

        return redact


def _table_exists(table: str) -> bool:
    return bool(db.scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)))


class _Sink:
    """A write-only stream for :class:`zipfile.ZipFile` whose bytes are collected and drained."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        return data


def _json_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"binary_bytes": len(value)}
    return value


def _safe_name(value: Any, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return text[:80] or fallback


def user_tables() -> list[tuple[str, list[str]]]:
    """(table, user columns) for every table with a foreign key to ``users``."""
    found = []
    for (table,) in tuples(db.conn, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                    "ORDER BY name"):
        columns = sorted({row[3] for row in tuples(db.conn, f"PRAGMA foreign_key_list({quote_identifier(table)})")
                          if row[2] == "users"})
        if columns:
            found.append((table, columns))
    return found


class _Writer:
    def __init__(self) -> None:
        self.sink = _Sink()
        self.zip = zipfile.ZipFile(self.sink, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
        self.summary: dict[str, int] = {}

    def json(self, name: str, data: Any) -> Iterator[bytes]:
        with self.zip.open(name, "w", force_zip64=True) as out:
            out.write(json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8"))
        yield self.sink.drain()

    def rows(self, name: str, sql: str, params: list[Any],
             redact: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> Iterator[bytes]:
        """Stream the rows of *sql* as a JSON array, each passed through *redact* first."""
        count = 0
        with self.zip.open(name, "w", force_zip64=True) as out:
            out.write(b"[")
            for row in db.execute(sql, params):
                if redact is not None:
                    row = redact(row)
                out.write((b",\n" if count else b"\n") + json.dumps(
                    {key: _json_value(value) for key, value in row.items()}, ensure_ascii=False, default=str
                ).encode("utf-8"))
                count += 1
                if count % 200 == 0:
                    yield self.sink.drain()
            out.write(b"\n]\n")
        self.summary[name] = count
        yield self.sink.drain()

    def file(self, name: str, path: Path) -> Iterator[bytes]:
        with self.zip.open(name, "w", force_zip64=True) as out, path.open("rb") as source:
            while chunk := source.read(CHUNK):
                out.write(chunk)
                yield self.sink.drain()
        yield self.sink.drain()

    def close(self) -> Iterator[bytes]:
        self.zip.close()
        yield self.sink.drain()


def _account(user: dict[str, Any]) -> dict[str, Any]:
    account = {key: user.get(key) for key in ACCOUNT_COLUMNS}
    if not user.get("suspend_time_visible"):
        account["suspended_until"] = None  # the administrator chose not to show when it ends
    return account


# (table, rows with ``resource_id``, storage folder, resource kind) for files the account uploaded.
UPLOADED_FILES = (
    ("page_attachments", "SELECT id, filename, original_name, blob_id, page_id AS resource_id FROM page_attachments "
                         "WHERE uploaded_by = ? ORDER BY id", "attachments", "page"),
    ("kanban_ticket_attachments", "SELECT a.id, a.filename, a.original_name, a.blob_id, c.board_id AS resource_id "
                                  "FROM kanban_ticket_attachments a " + _TICKET_BOARD.format(alias="a")
                                  + " WHERE a.uploaded_by = ? ORDER BY a.id", "kanban_attachments", "board"),
)


def stream(user: dict[str, Any]) -> Iterator[bytes]:
    """Yield the ZIP archive of everything *user* owns, chunk by chunk."""
    writer = _Writer()
    yield from writer.json("manifest.json", {
        "format": FORMAT, "version": VERSION, "exported_at": now_sql(), "account_id": user["id"],
        "username": user["username"],
    })
    yield from writer.json("account.json", _account(user))
    profile = users.get_profile(user["id"]) or {}
    yield from writer.json("profile/profile.json", profile)
    yield from writer.json("preferences.json", preferences.parse(user.get("accessibility")))
    if db.scalar("SELECT 1 FROM sqlite_master WHERE name = 'user_profile_fields__values'"):
        yield from writer.rows(
            "profile/fields.json",
            "SELECT d.key, d.label, v.value, v.visible, v.updated_at FROM user_profile_fields__values v "
            "JOIN user_profile_fields__definitions d ON d.id = v.field_id WHERE v.user_id = ? ORDER BY d.sort_order",
            [user["id"]],
        )
    readable = _Readable(user)
    for spec in EXPORTED:
        if _table_exists(spec.table):
            yield from writer.rows(f"data/{spec.table}.json", spec.sql, [user["id"]] * spec.sql.count("?"),
                                   readable.redactor(spec))
    for name, folder in ((profile.get("avatar_filename"), "uploads"),
                         (preferences.stored(user).get("background_image"), "uploads")):
        path = storage.resolve(folder, name) if name else None
        if path is not None:
            yield from writer.file(f"files/{name}", path)
    for table, sql, folder, kind in UPLOADED_FILES:
        if not _table_exists(table):
            continue
        for row in db.all(sql, (user["id"],)):
            if not readable(kind, row["resource_id"]):
                continue
            path = storage.resolve(folder, row["filename"], blob_id=row["blob_id"])
            if path is not None:
                yield from writer.file(f"files/{table}/{row['id']}-{_safe_name(row['original_name'], 'file')}", path)
    yield from writer.json("summary.json", {"rows": writer.summary})
    yield from writer.close()


def filename(user: dict[str, Any]) -> str:
    return f"userdata_{_safe_name(user['username'], 'account')}.zip"
