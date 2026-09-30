"""Board export and import (the 1.4 ``.kanban.json`` / ``.kanban.zip`` formats).

A board without attachments exports as JSON; with attachments as a ZIP that
holds ``board.json`` and ``attachments/<ticket>/<file>``. Imports accept both;
bundled files go through the normal upload checks. 1.6 exports add each
column's ``wip_limit``, each ticket's ``checklist``, ``"archived": true`` on
archived tickets and on an archived board (imported as archived again);
documents without them import as before.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.timeutil import now_sql, utcnow
from ... import storage
from ...db import db
from ...i18n import t
from . import access, events, extras, fields, history, signals, store
from .fields import KanbanError

FORMAT_VERSION = "1.2"
MAX_COLUMNS = 200
MAX_TICKETS = 5000
MAX_MEMBERS = 5000
MAX_UNCOMPRESSED = 1024 * 1024 * 1024
MAX_JSON = 20 * 1024 * 1024


# ── Export ────────────────────────────────────────────────────────────────────


def export(board: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """The board document and the ``(stored filename, bundle path)`` of every attachment."""
    state = history.snapshot(board["id"]) or {"columns": []}
    files = db.all(
        "SELECT a.ticket_id, a.filename, a.original_name, a.file_size, a.blob_id FROM kanban_ticket_attachments a "
        "JOIN kanban_tickets t ON t.id = a.ticket_id JOIN kanban_columns c ON c.id = t.column_id "
        "WHERE c.board_id = ? ORDER BY a.id",
        (board["id"],),
    )
    by_ticket: dict[int, list[dict[str, Any]]] = {}
    bundle: list[tuple[str, str]] = []
    for row in files:
        path = f"attachments/{row['ticket_id']}/{row['filename']}"
        if storage.resolve(store.FOLDER, row["filename"], blob_id=row["blob_id"]) is None:
            continue
        by_ticket.setdefault(row["ticket_id"], []).append({
            "filename": row["filename"], "original_name": row["original_name"], "file_size": row["file_size"],
            "bundle_path": path,
        })
        bundle.append((row["filename"], path))
    document = {
        "title": board["title"],
        "description": board["description"] or "",
        "columns": [
            {"title": column["title"], "wip_limit": column["wip_limit"], "tickets": [
                {"title": ticket["title"], "description": ticket["description"], "priority": ticket["priority"],
                 "due_date": ticket["due_date"], "color": ticket["color"], "labels": ticket["labels"],
                 "assignee_usernames": ticket["assignee_usernames"], "checklist": ticket["checklist"],
                 "archived": bool(ticket.get("archived")), "attachments": by_ticket.get(ticket["id"], [])}
                for ticket in column["tickets"]
            ]}
            for column in state["columns"]
        ],
        "archived": bool(board.get("archived_at")),
        "exported_at": utcnow().isoformat(),
        "version": FORMAT_VERSION,
    }
    return document, bundle


def export_zip(document: dict[str, Any], bundle: list[tuple[str, str]]) -> io.BytesIO:
    buffer = io.BytesIO()
    folder = storage.folder_path(store.FOLDER)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("board.json", json.dumps(document, indent=2, ensure_ascii=False))
        for filename, path in bundle:
            archive.write(folder / filename, arcname=path)
    buffer.seek(0)
    return buffer


# ── Import ────────────────────────────────────────────────────────────────────


@dataclass
class _Parsed:
    document: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)


def _read(upload: BinaryIO) -> _Parsed:
    head = upload.read(4)
    upload.seek(0)
    if head != b"PK\x03\x04":
        raw = upload.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise KanbanError("kanban.error.import_invalid")
        return _Parsed(_decode(raw))
    member_limit = current_app.config["BW"].max_kanban_attachment_size
    try:
        with zipfile.ZipFile(upload) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            if len(members) > MAX_MEMBERS or sum(info.file_size for info in members) > MAX_UNCOMPRESSED:
                raise KanbanError("kanban.error.import_invalid")
            document = None
            files: dict[str, bytes] = {}
            for info in members:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/") or info.file_size > max(member_limit, MAX_JSON):
                    raise KanbanError("kanban.error.import_invalid")
                if name == "board.json":
                    document = _decode(archive.read(info))
                elif name.startswith("attachments/") and info.file_size <= member_limit:
                    files[name] = archive.read(info)
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise KanbanError("kanban.error.import_invalid") from error
    if document is None:
        raise KanbanError("kanban.error.import_invalid")
    return _Parsed(document, files)


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as error:
        raise KanbanError("kanban.error.import_invalid") from error
    if not isinstance(data, dict) or not isinstance(data.get("columns", []), list):
        raise KanbanError("kanban.error.import_invalid")
    return data


def _clean(value: Any, maximum: int, fallback: str) -> str:
    return " ".join(str(value or "").split())[:maximum] or fallback


def _ticket_values(item: dict[str, Any]) -> dict[str, Any]:
    try:
        due = fields.due_date(item.get("due_date"))
    except fields.KanbanError:
        due = None
    try:
        color = fields.color(item.get("color"))
    except fields.KanbanError:
        color = ""
    return {
        "title": _clean(item.get("title"), fields.MAX_TICKET_TITLE, t("kanban.import.untitled_ticket")),
        "description": str(item.get("description") or "")[:fields.MAX_DESCRIPTION],
        "priority": item.get("priority") if item.get("priority") in fields.PRIORITIES else "medium",
        "due_date": due, "color": color,
        "labels": fields.encode_labels(fields.labels(item.get("labels") or [])),
    }


def _store_bundled(parsed: _Parsed, columns: list[dict[str, Any]]) -> dict[str, storage.StoredFile]:
    """Save every referenced bundled file through the normal upload checks (refused ones are skipped)."""
    limit = storage.max_upload_bytes(current_app.config["BW"].max_kanban_attachment_size)
    saved: dict[str, storage.StoredFile] = {}
    for column in columns:
        for item in column["tickets"]:
            for attachment in item.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                path = str(attachment.get("bundle_path") or "")
                if path not in parsed.files or path in saved:
                    continue
                upload = FileStorage(io.BytesIO(parsed.files[path]),
                                     filename=str(attachment.get("original_name") or "file")[:255])
                try:
                    saved[path] = storage.save(upload, store.FOLDER, allowed=extras.ATTACHMENT_EXTENSIONS,
                                               max_bytes=limit)
                except storage.UploadError:
                    continue
    return saved


def import_board(user: dict[str, Any], upload: FileStorage | None) -> dict[str, Any]:
    if upload is None or not upload.filename:
        raise KanbanError("kanban.error.import_invalid")
    parsed = _read(upload.stream)
    columns = []
    for item in parsed.document.get("columns", [])[:MAX_COLUMNS]:
        if isinstance(item, dict):
            tickets = [ticket for ticket in item.get("tickets") or [] if isinstance(ticket, dict)]
            columns.append({"title": item.get("title"), "wip_limit": item.get("wip_limit"), "tickets": tickets})
    if sum(len(column["tickets"]) for column in columns) > MAX_TICKETS:
        raise KanbanError("kanban.error.import_invalid")
    saved = _store_bundled(parsed, columns)
    try:
        return _create(user, parsed.document, columns, saved)
    except BaseException:
        for stored in saved.values():
            storage.delete(store.FOLDER, stored.filename)
        raise


def _create(user: dict[str, Any], document: dict[str, Any], columns: list[dict[str, Any]],
            saved: dict[str, storage.StoredFile]) -> dict[str, Any]:
    title = _clean(document.get("title"), fields.MAX_BOARD_TITLE, t("kanban.import.untitled_board"))
    now = now_sql()
    archived = document.get("archived") is True
    with db.transaction():
        board_id = db.insert("kanban_boards", {
            "title": title, "description": str(document.get("description") or "")[:fields.MAX_DESCRIPTION],
            "created_by": user["id"], "created_at": now, "visibility": "public",
            "archived_at": now if archived else None, "archived_by": user["id"] if archived else None,
        })
        board = store.get_board(board_id)
        assert board is not None
        allowed = access.assignable_ids(board)
        names = {row["username"].lower(): row["id"] for row in db.all("SELECT id, username FROM users")}
        unused = dict(saved)
        for column_index, column in enumerate(columns):
            column_id = db.insert("kanban_columns", {
                "board_id": board_id, "sort_order": column_index, "created_at": now,
                "title": _clean(column["title"], fields.MAX_COLUMN_TITLE, t("kanban.import.untitled_column")),
                "wip_limit": fields.stored_wip_limit(column["wip_limit"]),
            })
            positions = {False: 0, True: 0}
            for item in column["tickets"]:
                shelved = item.get("archived") is True
                ticket_id = db.insert("kanban_tickets", {
                    **_ticket_values(item), "column_id": column_id, "created_by": user["id"],
                    "sort_order": positions[shelved], "created_at": now,
                    "archived_at": now if shelved else None, "archived_by": user["id"] if shelved else None,
                })
                positions[shelved] += 1
                people = [names.get(str(name).lower()) for name in item.get("assignee_usernames") or []]
                store.set_assignees(ticket_id, [person for person in people if person in allowed])
                store.replace_checklist(ticket_id, fields.stored_checklist(item.get("checklist")))
                for attachment in item.get("attachments") or []:
                    path = str(attachment.get("bundle_path") or "") if isinstance(attachment, dict) else ""
                    stored = unused.pop(path, None)
                    if stored is None:
                        continue
                    db.insert("kanban_ticket_attachments", {
                        "ticket_id": ticket_id, "filename": stored.filename,
                        "original_name": stored.original_name, "file_size": stored.size,
                        "uploaded_by": user["id"], "uploaded_at": now,
                    })
        message = t("kanban.log.board_imported", title=title)
        events.append(board_id, "board_reset", {}, user["id"])
        events.log_activity(board_id, user["id"], "board_imported", message)
        history.record(board_id, user["id"], message)
    board = store.get_board(board_id)
    assert board is not None
    signals.board("created", board, user)
    return board
