"""Board export and import (the 1.4 ``.kanban.json`` / ``.kanban.zip`` formats).

A board without attachments exports as JSON; with attachments as a ZIP that
holds ``board.json`` and ``attachments/<ticket>/<file>``, written to a
temporary file (never held in memory) and refused when it would exceed what
an import accepts. Imports accept both. Every limit of a ZIP is checked
before anything is unpacked: ``board.json`` is held to ``MAX_JSON`` like a
plain JSON upload, and only the files tickets refer to are streamed into
storage, through the normal upload checks, within ``MAX_UNCOMPRESSED`` and
the storage left under a quota. 1.6 exports add each
column's ``wip_limit``, each ticket's ``checklist``, ``"archived": true`` on
archived tickets and on an archived board (imported as archived again);
documents without them import as before.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import IO, Any, BinaryIO

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.json import loads as safe_json_loads
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
MIB = 1024 * 1024
MAX_UNCOMPRESSED = 1024 * MIB
MAX_JSON = 20 * MIB
MAX_RATIO = 200  # largest unpacked/packed ratio of a bundled file over 1 MiB
# zipfile turns every central directory entry into an object: a directory
# larger than this per allowed member is refused before it is read.
_DIRECTORY_PER_MEMBER = 512
# General purpose flags zipfile cannot unpack: encryption (bit 0), patched data (5), strong encryption (6).
_UNREADABLE_FLAGS = 0x61
# What zipfile raises on an archive it cannot read (OverflowError: an offset
# past any a file can seek to); OSError as well while the archive or a member
# is opened, not while a member is saved (then it is a storage fault).
_ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, ValueError, NotImplementedError, OverflowError)
# Attachment types that are compressed already: exported as they are.
_COMPRESSED = frozenset({"docx", "xlsx", "pptx", "zip", "gz", "png", "jpg", "jpeg", "gif", "webp", "mp4", "webm",
                         "mp3", "ogg"})


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


def export_zip(document: dict[str, Any], bundle: list[tuple[str, str]]) -> IO[bytes]:
    """The ZIP export in an anonymous temporary file, which disappears once closed (``send_file`` does).

    The archive is never held in memory. Files that are already compressed are
    stored as they are. A bundle the import would refuse for its attachments
    (more than ``MAX_MEMBERS`` entries or ``MAX_UNCOMPRESSED`` bytes) is
    refused here; a document over ``MAX_JSON`` is not, as its JSON export
    is not either (both still serve as a copy).
    """
    body = json.dumps(document, indent=2, ensure_ascii=False).encode()
    folder = storage.folder_path(store.FOLDER)
    files = [(folder / filename, path) for filename, path in bundle]
    total = len(body) + sum(source.stat().st_size for source, _path in files)
    if len(files) >= MAX_MEMBERS or total > MAX_UNCOMPRESSED:
        raise KanbanError("kanban.error.export_too_large", files=MAX_MEMBERS - 1, size=MAX_UNCOMPRESSED // MIB)
    archive = tempfile.TemporaryFile(dir=_work_dir())  # noqa: SIM115 - handed to send_file, which closes it
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundled:
            bundled.writestr("board.json", body)
            for source, path in files:
                stored = storage.extension(source.name) in _COMPRESSED
                bundled.write(source, arcname=path, compress_type=zipfile.ZIP_STORED if stored else None)
        archive.seek(0)
    except BaseException:
        archive.close()
        raise
    return archive


def _work_dir() -> str:
    folder = current_app.config["BW"].folders.exports
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return folder


# ── Import ────────────────────────────────────────────────────────────────────


@dataclass
class _Parsed:
    document: dict[str, Any]
    archive: zipfile.ZipFile | None = None
    members: dict[str, zipfile.ZipInfo] = field(default_factory=dict)  # attachments by bundle path


def _directory_size(stream: BinaryIO) -> int | None:
    """The central directory size :class:`zipfile.ZipFile` will read (ZIP64 included), or None.

    Read by zipfile's own end record lookup, so both always agree, but before
    it turns every entry into an object. None when there is no end record
    (ZipFile reports it) or where a future Python lacks the private names
    used here (the member count is still checked once the archive is open).
    """
    end_record, size = (getattr(zipfile, name, None) for name in ("_EndRecData", "_ECD_SIZE"))
    if end_record is None or size is None:
        return None
    try:
        end = end_record(stream)
    finally:
        stream.seek(0)
    return end[size] if end else None


def _readable(info: zipfile.ZipInfo) -> bool:
    """Stored or deflated, neither encrypted nor patched: the only members unpacked in bounded memory."""
    return (info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            and not info.flag_bits & _UNREADABLE_FLAGS)


def _read(upload: BinaryIO) -> _Parsed:
    """The document, and for a ZIP the open archive with its attachment entries (none unpacked yet)."""
    head = upload.read(4)
    upload.seek(0)
    if head != b"PK\x03\x04":
        raw = upload.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise KanbanError("kanban.error.import_invalid")
        return _Parsed(_decode(raw))
    try:
        directory = _directory_size(upload)
    except (*_ZIP_ERRORS, OSError) as error:
        raise KanbanError("kanban.error.import_invalid") from error
    if directory is not None and directory > MAX_MEMBERS * _DIRECTORY_PER_MEMBER:
        raise KanbanError("kanban.error.import_invalid")
    try:
        archive = zipfile.ZipFile(upload)
    except (*_ZIP_ERRORS, OSError) as error:
        raise KanbanError("kanban.error.import_invalid") from error
    try:
        document, members = _contents(archive)
    except BaseException:
        archive.close()
        raise
    return _Parsed(document, archive, members)


def _contents(archive: zipfile.ZipFile) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    """``board.json`` (at most ``MAX_JSON`` bytes, checked before it is unpacked) and the attachment entries."""
    members = [info for info in archive.infolist() if not info.is_dir()]
    if len(members) > MAX_MEMBERS or sum(info.file_size for info in members) > MAX_UNCOMPRESSED:
        raise KanbanError("kanban.error.import_invalid")
    document: zipfile.ZipInfo | None = None
    attachments: dict[str, zipfile.ZipInfo] = {}
    for info in members:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise KanbanError("kanban.error.import_invalid")
        if name == "board.json":
            document = info
        elif name.startswith("attachments/"):
            attachments[name] = info
    if document is None or document.file_size > MAX_JSON or not _readable(document):
        raise KanbanError("kanban.error.import_invalid")
    try:
        with archive.open(document) as source:
            raw = source.read(MAX_JSON + 1)
    except (*_ZIP_ERRORS, OSError) as error:
        raise KanbanError("kanban.error.import_invalid") from error
    if len(raw) > MAX_JSON:
        raise KanbanError("kanban.error.import_invalid")
    return _decode(raw), attachments


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        data = safe_json_loads(raw.decode("utf-8-sig"))
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


def _unpack_budget() -> int:
    """Bytes an import may unpack: ``MAX_UNCOMPRESSED``, and no more than the storage left under a quota."""
    quota = current_app.config["BW"].storage_limit_bytes
    if not quota:
        return MAX_UNCOMPRESSED
    return max(0, min(MAX_UNCOMPRESSED, quota - storage.storage_used()))


def _store_bundled(parsed: _Parsed, columns: list[dict[str, Any]], saved: dict[str, storage.StoredFile]) -> None:
    """Save every referenced bundled file into *saved*, through the normal upload checks.

    Only files a ticket refers to are unpacked, each streamed from the
    archive into storage. Files the upload checks refuse, and files beyond
    the import's budget (:func:`_unpack_budget`), are skipped; a member
    packed more tightly than ``MAX_RATIO`` makes the archive invalid.
    """
    if parsed.archive is None:
        return
    limit = storage.max_upload_bytes(current_app.config["BW"].max_kanban_attachment_size)
    budget = _unpack_budget()
    for column in columns:
        for item in column["tickets"]:
            for attachment in item.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                path = str(attachment.get("bundle_path") or "")
                info = parsed.members.get(path)
                if info is None or path in saved or info.file_size > min(limit, budget):
                    continue
                if not _readable(info) or (info.file_size > MIB
                                           and info.file_size > MAX_RATIO * max(info.compress_size, 1)):
                    raise KanbanError("kanban.error.import_invalid")
                budget -= info.file_size
                name = str(attachment.get("original_name") or "file")[:255]
                try:  # reads the member's local header, wherever the directory says it is
                    source = parsed.archive.open(info)
                except (*_ZIP_ERRORS, OSError) as error:
                    raise KanbanError("kanban.error.import_invalid") from error
                try:
                    with source:
                        saved[path] = storage.save(FileStorage(source, filename=name), store.FOLDER,
                                                   allowed=extras.ATTACHMENT_EXTENSIONS, max_bytes=limit)
                except storage.UploadError:
                    continue
                except _ZIP_ERRORS as error:
                    raise KanbanError("kanban.error.import_invalid") from error


def import_board(user: dict[str, Any], upload: FileStorage | None) -> dict[str, Any]:
    if upload is None or not upload.filename:
        raise KanbanError("kanban.error.import_invalid")
    parsed = _read(upload.stream)
    saved: dict[str, storage.StoredFile] = {}
    try:
        columns = []
        for item in parsed.document.get("columns", [])[:MAX_COLUMNS]:
            if isinstance(item, dict):
                tickets = [ticket for ticket in item.get("tickets") or [] if isinstance(ticket, dict)]
                columns.append({"title": item.get("title"), "wip_limit": item.get("wip_limit"), "tickets": tickets})
        if sum(len(column["tickets"]) for column in columns) > MAX_TICKETS:
            raise KanbanError("kanban.error.import_invalid")
        _store_bundled(parsed, columns, saved)
        return _create(user, parsed.document, columns, saved)
    except BaseException:
        for stored in saved.values():
            storage.delete(store.FOLDER, stored.filename)
        raise
    finally:
        if parsed.archive is not None:
            parsed.archive.close()


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
