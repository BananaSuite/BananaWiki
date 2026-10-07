"""Ticket comments, attachments and description history."""

from __future__ import annotations

import time
from typing import Any

from flask import current_app
from markupsafe import Markup
from werkzeug.datastructures import FileStorage

from ....core.timeutil import now_sql
from ... import storage
from ...db import db
from ...i18n import t
from ...markdown import render
from ..pages import diff
from . import access, events, fields, signals, store

# The 1.4 list of attachment types (the site's upload policy applies on top).
ATTACHMENT_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "json", "xml", "zip", "tar", "gz",
    "png", "jpg", "jpeg", "gif", "webp", "mp4", "webm", "mp3", "ogg", "py", "js", "ts", "html", "css", "sh",
})


def _touch_card(board_id: int, ticket_id: int, user_id: str | None) -> None:
    """Tell other viewers the ticket's counters changed."""
    card = store.ticket_card(ticket_id)
    if card is not None:
        events.append(board_id, "ticket_upsert", {"ticket": card, "columns": {}}, user_id)


# ── Comments ──────────────────────────────────────────────────────────────────

_COMMENT_SELECT = (
    "SELECT c.*, u.username AS author_name FROM kanban_ticket_comments c LEFT JOIN users u ON u.id = c.user_id"
)


def comment_payload(row: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    allowed = access.can_moderate_comment(user, row)
    return {
        "id": row["id"], "user_id": row["user_id"], "author_name": row["author_name"] or "",
        "content": row["content"], "content_html": render(row["content"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "edited": row["updated_at"] != row["created_at"], "can_edit": allowed, "can_delete": allowed,
    }


def comments(ticket_id: int) -> list[dict[str, Any]]:
    return db.all(f"{_COMMENT_SELECT} WHERE c.ticket_id = ? ORDER BY c.created_at, c.id", (ticket_id,))


def get_comment(comment_id: int) -> dict[str, Any] | None:
    return db.one(f"{_COMMENT_SELECT} WHERE c.id = ?", (comment_id,))


def add_comment(ticket: dict[str, Any], user: dict[str, Any], content: Any) -> dict[str, Any]:
    text = fields.comment(content)
    with db.transaction():
        now = now_sql()
        comment_id = db.insert("kanban_ticket_comments", {"ticket_id": ticket["id"], "user_id": user["id"],
                                                          "content": text, "created_at": now, "updated_at": now})
        events.log_activity(ticket["board_id"], user["id"], "comment_added",
                            t("kanban.log.comment_added", title=ticket["title"]))
        _touch_card(ticket["board_id"], ticket["id"], user["id"])
    row = get_comment(comment_id)
    assert row is not None
    signals.comment(row, store.get_ticket(ticket["id"]) or ticket, user)
    return row


def edit_comment(comment: dict[str, Any], content: Any) -> None:
    db.update("kanban_ticket_comments", {"content": fields.comment(content), "updated_at": now_sql()},
              "id = ?", (comment["id"],))


def delete_comment(ticket: dict[str, Any], comment: dict[str, Any], user: dict[str, Any]) -> None:
    with db.transaction():
        db.execute("DELETE FROM kanban_ticket_comments WHERE id = ?", (comment["id"],))
        _touch_card(ticket["board_id"], ticket["id"], user["id"])


# ── Attachments ───────────────────────────────────────────────────────────────


def attachments(ticket_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT a.id, a.original_name, a.file_size, a.uploaded_at, a.uploaded_by, u.username AS uploader_name "
        "FROM kanban_ticket_attachments a LEFT JOIN users u ON u.id = a.uploaded_by WHERE a.ticket_id = ? "
        "ORDER BY a.uploaded_at, a.id",
        (ticket_id,),
    )


def get_attachment(attachment_id: int) -> dict[str, Any] | None:
    """The attachment with its ticket's ``board_id``."""
    return db.one(
        "SELECT a.*, c.board_id FROM kanban_ticket_attachments a JOIN kanban_tickets t ON t.id = a.ticket_id "
        "JOIN kanban_columns c ON c.id = t.column_id WHERE a.id = ?",
        (attachment_id,),
    )


def add_attachment(ticket: dict[str, Any], user: dict[str, Any], upload: FileStorage | None) -> dict[str, Any]:
    """Store an upload (raises ``storage.UploadError``) and attach it to *ticket*."""
    limit = storage.max_upload_bytes(current_app.config["BW"].max_kanban_attachment_size)
    stored = storage.save(upload, store.FOLDER, allowed=ATTACHMENT_EXTENSIONS, max_bytes=limit)
    try:
        with db.transaction():
            attachment_id = db.insert("kanban_ticket_attachments", {
                "ticket_id": ticket["id"], "filename": stored.filename, "original_name": stored.original_name[:255],
                "file_size": stored.size, "uploaded_by": user["id"], "uploaded_at": now_sql(),
            })
            events.log_activity(ticket["board_id"], user["id"], "attachment_added",
                                t("kanban.log.attachment_added", name=stored.original_name, title=ticket["title"]))
            _touch_card(ticket["board_id"], ticket["id"], user["id"])
    except BaseException:
        storage.delete(store.FOLDER, stored.filename)
        raise
    signals.tickets_changed("updated", [ticket["id"]], ticket["board_id"], user)
    return {"id": attachment_id, "original_name": stored.original_name, "file_size": stored.size}


def delete_attachment(attachment: dict[str, Any], user: dict[str, Any]) -> None:
    with db.transaction():
        db.execute("DELETE FROM kanban_ticket_attachments WHERE id = ?", (attachment["id"],))
        store.delete_blobs([attachment])
        events.log_activity(attachment["board_id"], user["id"], "attachment_deleted",
                            t("kanban.log.attachment_deleted", name=attachment["original_name"]))
        _touch_card(attachment["board_id"], attachment["ticket_id"], user["id"])
    store.delete_files([attachment])
    signals.tickets_changed("updated", [attachment["ticket_id"]], attachment["board_id"], user)


def sweep_orphan_files(min_age_seconds: int = 3600) -> int:
    """Background job: remove files no attachment row refers to (e.g. after an account was deleted)."""
    folder = storage.folder_path(store.FOLDER)
    known = set(db.column("SELECT filename FROM kanban_ticket_attachments"))
    removed = 0
    cutoff = time.time() - min_age_seconds
    for path in folder.iterdir():
        if not path.is_file() or path.name.startswith(".") or path.name in known:
            continue
        if path.stat().st_mtime < cutoff:
            storage.delete(store.FOLDER, path.name)
            removed += 1
    return removed


# ── Description history ───────────────────────────────────────────────────────


def description_history(ticket_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT h.id, h.created_at, u.username AS editor_name FROM kanban_ticket_history h "
        "LEFT JOIN users u ON u.id = h.changed_by WHERE h.ticket_id = ? ORDER BY h.created_at DESC, h.id DESC",
        (ticket_id,),
    )


def get_history_entry(entry_id: int) -> dict[str, Any] | None:
    return db.one(
        "SELECT h.*, u.username AS editor_name FROM kanban_ticket_history h "
        "LEFT JOIN users u ON u.id = h.changed_by WHERE h.id = ?",
        (entry_id,),
    )


def diff_html(old: str, new: str) -> Markup:
    """A line diff with every line escaped: ``<del>`` removed, ``<ins>`` added.

    Compared within the work budget of the pages diff; a note says when the
    two descriptions were too different to compare line by line.
    """
    before, after = (old or "").splitlines(), (new or "").splitlines()
    ops, complete = diff.opcodes(before, after, diff.Budget())
    parts = [] if complete else [Markup('<span class="muted">{}</span>').format(t("kanban.diff.description_coarse"))]
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            parts.extend(Markup("<span>{}</span>").format(line) for line in after[j1:j2])
            continue
        parts.extend(Markup('<del class="diff-del">{}</del>').format(line) for line in before[i1:i2])
        parts.extend(Markup('<ins class="diff-add">{}</ins>').format(line) for line in after[j1:j2])
    return Markup("\n").join(parts)
