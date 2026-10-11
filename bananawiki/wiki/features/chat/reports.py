"""Messages reported to the administrators (``chat__reports``).

A participant of a direct conversation, or a member of a group, may report
a message someone else sent. The report keeps a copy of its text and the
names of its attachments, so it outlives the message: the sender deleting
it, or retention clearing it, does not erase what was reported. Each person
reports a message once. Administrators see open reports under
**Admin → Chats → Reports** and mark them resolved; both are audited.
"""

from __future__ import annotations

from typing import Any

from ....core.timeutil import now_sql
from ... import attention
from ...db import db
from . import store
from .store import Store

MAX_CONTENT = 10_000


class ReportRefused(ValueError):
    def __init__(self, key: str, status: int = 400):
        super().__init__(key)
        self.key = key
        self.status = status


def file(target: Store, parent_id: int, message_id: int, reporter_id: str, *, floor: int = 0) -> None:
    """Report *message_id* of conversation *parent_id*; raise :class:`ReportRefused` when it cannot be."""
    system = "is_system" if target.has_system else "0"
    withheld = "withheld" if target.has_withheld else "0"
    row = db.one(
        f"SELECT id, sender_id, content, is_deleted, created_at, {system} AS is_system, {withheld} AS withheld "
        f"FROM {target.messages} WHERE id = ? AND {target.parent} = ? AND id > ?",
        (message_id, parent_id, floor),
    )
    if row is None or row["is_deleted"] or store.withheld_from(row, reporter_id):
        raise ReportRefused("chat.error.message_not_found", 404)
    if row["is_system"] or row["sender_id"] == reporter_id:
        raise ReportRefused("chat.report.error.own")
    names = db.column(f"SELECT original_name FROM {target.attachments} WHERE message_id = ? ORDER BY id",
                      (message_id,))
    cursor = db.execute(
        "INSERT OR IGNORE INTO chat__reports (kind, parent_id, message_id, reporter_id, reported_user_id, content, "
        "attachment_names, message_created_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (target.kind, parent_id, message_id, reporter_id, row["sender_id"], (row["content"] or "")[:MAX_CONTENT],
         "\n".join(names), row["created_at"], now_sql()),
    )
    if cursor.rowcount:  # a new report (not the same person reporting the same message again)
        attention.created("chat.reports", cursor.lastrowid)


def open_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM chat__reports WHERE status = 'open'", default=0) or 0)


def page(*, status: str, page: int, per_page: int) -> tuple[list[dict[str, Any]], int]:
    """Reports with *status* (newest first) and their total."""
    total = int(db.scalar("SELECT COUNT(*) FROM chat__reports WHERE status = ?", (status,), default=0) or 0)
    rows = db.all(
        "SELECT r.*, reporter.username AS reporter_name, reported.username AS reported_name, "
        "resolver.username AS resolver_name FROM chat__reports r "
        "LEFT JOIN users reporter ON reporter.id = r.reporter_id "
        "LEFT JOIN users reported ON reported.id = r.reported_user_id "
        "LEFT JOIN users resolver ON resolver.id = r.resolved_by "
        "WHERE r.status = ? ORDER BY r.id DESC LIMIT ? OFFSET ?",
        (status, per_page, (page - 1) * per_page),
    )
    for row in rows:
        row["attachments"] = [name for name in (row["attachment_names"] or "").split("\n") if name]
    return rows, total


def get(report_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM chat__reports WHERE id = ?", (report_id,))


def resolve(report_id: int, actor_id: str) -> bool:
    return db.execute(
        "UPDATE chat__reports SET status = 'resolved', resolved_by = ?, resolved_at = ? "
        "WHERE id = ? AND status = 'open'",
        (actor_id, now_sql(), report_id),
    ).rowcount > 0
