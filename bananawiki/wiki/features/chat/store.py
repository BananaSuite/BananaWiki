"""Messages and attachments, shared by direct messages and group chats.

Direct messages live in ``chat_messages`` / ``chat_attachments`` (parent
column ``chat_id``), group messages in ``group_messages`` /
``group_attachments`` (``group_id``). Both have the same shape, so one set of
functions serves both through a :class:`Store` description.

Reading is incremental: the chat page shows the latest page of messages and
then asks only for messages after the newest id it has (and pages backwards
with "load older"). Attachments for a batch are fetched with one query.

Deleting is real: deleting a message erases its text and attachments (files
on disk and the legacy ``file_blobs`` copies) and leaves a "deleted" marker so
the conversation keeps its shape. Clearing a chat, retention cleanup and
deleting a group remove rows, files and blobs together.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from flask import url_for

from ....core.timeutil import now_sql
from ... import storage
from ...db import db
from ...i18n import t
from ...templating import format_datetime, human_bytes

FOLDER = "chat_attachments"
PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
SYSTEM_KEY_PREFIX = "chat.system."
# Tables whose rows may point at a legacy ``file_blobs`` copy.
BLOB_REFERENCES = (
    ("page_attachments", "blob_id"),
    ("chat_attachments", "blob_id"),
    ("group_attachments", "blob_id"),
    ("kanban_ticket_attachments", "blob_id"),
    ("custom_page_files", "blob_id"),
)


@dataclass(frozen=True)
class Store:
    kind: str
    messages: str
    attachments: str
    parent: str
    download_endpoint: str
    has_system: bool


DM = Store("dm", "chat_messages", "chat_attachments", "chat_id", "chat.dm_attachment", False)
GROUP = Store("group", "group_messages", "group_attachments", "group_id", "chat.group_attachment", True)


# ── System messages ──────────────────────────────────────────────────────────


def encode_system(key: str, **values: Any) -> str:
    """Stored form of a group system message, translated when displayed."""
    return json.dumps({"t": key, "v": {name: str(value) for name, value in values.items()}}, ensure_ascii=False)


def system_text(content: str) -> str:
    """Translate a stored system message; 1.4 stored plain English text."""
    if content.startswith('{"t"'):
        try:
            data = json.loads(content)
        except ValueError:
            return content
        key = data.get("t") if isinstance(data, dict) else None
        values = data.get("v") if isinstance(data, dict) else None
        if isinstance(key, str) and key.startswith(SYSTEM_KEY_PREFIX) and isinstance(values, dict):
            return t(key, **{str(name): str(value) for name, value in values.items()})
    return content


# ── Reading ──────────────────────────────────────────────────────────────────


def _select(store: Store) -> str:
    system = "m.is_system" if store.has_system else "0"
    return (
        f"SELECT m.id, m.sender_id, m.content, m.is_deleted, m.created_at, m.ip_address, {system} AS is_system, "
        f"u.username AS sender_name FROM {store.messages} m LEFT JOIN users u ON u.id = m.sender_id "
        f"WHERE m.{store.parent} = ?"
    )


def _attach(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``attachments`` to each row with a single query for the whole batch."""
    ids = [row["id"] for row in rows if not row["is_deleted"]]
    grouped: dict[int, list[dict[str, Any]]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for item in db.all(
            f"SELECT id, message_id, filename, original_name, file_size, blob_id FROM {store.attachments} "
            f"WHERE message_id IN ({marks}) ORDER BY id",
            ids,
        ):
            grouped.setdefault(item["message_id"], []).append(item)
    for row in rows:
        row["attachments"] = grouped.get(row["id"], [])
    return rows


def clamp_limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return PAGE_SIZE
    return max(1, min(number, MAX_PAGE_SIZE))


def after(store: Store, parent_id: int, after_id: int, limit: int = MAX_PAGE_SIZE) -> list[dict[str, Any]]:
    """Messages newer than *after_id*, oldest first."""
    rows = db.all(_select(store) + " AND m.id > ? ORDER BY m.id LIMIT ?", (parent_id, after_id, limit))
    return _attach(store, rows)


def before(store: Store, parent_id: int, before_id: int | None, limit: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """The *limit* messages before *before_id* (or the latest ones), oldest first."""
    if before_id:
        rows = db.all(_select(store) + " AND m.id < ? ORDER BY m.id DESC LIMIT ?", (parent_id, before_id, limit))
    else:
        rows = db.all(_select(store) + " ORDER BY m.id DESC LIMIT ?", (parent_id, limit))
    rows.reverse()
    return _attach(store, rows)


def iterate(store: Store, parent_id: int, batch: int = 500) -> Iterator[dict[str, Any]]:
    """Every message of a conversation, oldest first, in bounded batches (exports)."""
    last = 0
    while True:
        rows = after(store, parent_id, last, batch)
        if not rows:
            return
        yield from rows
        last = rows[-1]["id"]


def first_id(store: Store, parent_id: int) -> int | None:
    return db.scalar(f"SELECT MIN(id) FROM {store.messages} WHERE {store.parent} = ?", (parent_id,))


def has_older(store: Store, parent_id: int, oldest_id: int | None) -> bool:
    if not oldest_id:
        return False
    return db.scalar(
        f"SELECT 1 FROM {store.messages} WHERE {store.parent} = ? AND id < ? LIMIT 1", (parent_id, oldest_id)
    ) is not None


def deleted_since(store: Store, parent_id: int, since: str) -> list[int]:
    """Ids of messages deleted at or after *since* (so open pages can hide them)."""
    return db.column(
        f"SELECT id FROM {store.messages} WHERE {store.parent} = ? AND is_deleted = 1 AND deleted_at >= ?",
        (parent_id, since),
    )


def count(store: Store, parent_id: int) -> tuple[int, int]:
    """(messages, attachments) in a conversation."""
    row = db.one(
        f"SELECT (SELECT COUNT(*) FROM {store.messages} WHERE {store.parent} = ?) AS messages, "
        f"(SELECT COUNT(*) FROM {store.attachments} a JOIN {store.messages} m ON m.id = a.message_id "
        f"WHERE m.{store.parent} = ?) AS attachments",
        (parent_id, parent_id),
    ) or {}
    return int(row.get("messages") or 0), int(row.get("attachments") or 0)


def message(store: Store, message_id: int) -> dict[str, Any] | None:
    system = "is_system" if store.has_system else "0"
    return db.one(
        f"SELECT id, {store.parent} AS parent_id, sender_id, is_deleted, {system} AS is_system "
        f"FROM {store.messages} WHERE id = ?",
        (message_id,),
    )


def attachment(store: Store, attachment_id: int) -> dict[str, Any] | None:
    return db.one(
        f"SELECT a.id, a.filename, a.original_name, a.file_size, a.blob_id, m.{store.parent} AS parent_id, "
        f"m.is_deleted FROM {store.attachments} a JOIN {store.messages} m ON m.id = a.message_id WHERE a.id = ?",
        (attachment_id,),
    )


def attachments_sent_since(user_id: str, since: str) -> int:
    """Attachments *user_id* sent in direct messages and groups since *since*."""
    return int(db.scalar(
        "SELECT (SELECT COUNT(*) FROM chat_attachments a JOIN chat_messages m ON m.id = a.message_id "
        "WHERE m.sender_id = ? AND a.created_at >= ?) + "
        "(SELECT COUNT(*) FROM group_attachments a JOIN group_messages m ON m.id = a.message_id "
        "WHERE m.sender_id = ? AND a.created_at >= ?)",
        (user_id, since, user_id, since),
        default=0,
    ) or 0)


# ── Presentation ─────────────────────────────────────────────────────────────


def serialize(
    store: Store,
    row: dict[str, Any],
    *,
    viewer_id: str | None,
    can_delete_any: bool = False,
    with_ip: bool = False,
) -> dict[str, Any]:
    """A message as the page (template and script) shows it to *viewer_id*."""
    system = bool(row["is_system"])
    deleted = bool(row["is_deleted"]) and not system
    mine = viewer_id is not None and row["sender_id"] == viewer_id
    if system:
        text = system_text(row["content"])
    else:
        text = "" if deleted else row["content"]
    item: dict[str, Any] = {
        "id": row["id"],
        "sender": row["sender_name"],
        "mine": mine,
        "system": system,
        "deleted": deleted,
        "text": text,
        "created_at": row["created_at"],
        "time": format_datetime(row["created_at"]),
        "attachments": [
            {"id": a["id"], "name": a["original_name"], "size": human_bytes(a["file_size"]),
             "url": url_for(store.download_endpoint, attachment_id=a["id"])}
            for a in row.get("attachments", [])
        ] if not deleted else [],
        "can_delete": not deleted and not system and (mine or can_delete_any),
    }
    if with_ip:
        item["ip"] = row["ip_address"] or ""
    return item


# ── Writing ──────────────────────────────────────────────────────────────────


def insert(
    store: Store,
    parent_id: int,
    sender_id: str | None,
    content: str,
    *,
    ip_address: str = "",
    stored: storage.StoredFile | None = None,
    system: bool = False,
) -> int:
    """Add a message (and its attachment); call inside the caller's transaction."""
    now = now_sql()
    values: dict[str, Any] = {store.parent: parent_id, "sender_id": sender_id, "content": content,
                              "ip_address": ip_address, "created_at": now}
    if store.has_system:
        values["is_system"] = 1 if system else 0
    message_id = db.insert(store.messages, values)
    if stored is not None:
        db.insert(store.attachments, {"message_id": message_id, "filename": stored.filename,
                                      "original_name": stored.original_name, "file_size": stored.size,
                                      "created_at": now})
    return message_id


def _remove_attachments(store: Store, where: str, params: Iterable[Any]) -> list[str]:
    """Delete attachment rows matching *where* and their blob copies; return the file names.

    Must run inside a transaction; remove the files with :func:`remove_files`
    after it commits.
    """
    params = tuple(params)
    rows = db.all(f"SELECT filename, blob_id FROM {store.attachments} WHERE {where}", params)
    if not rows:
        return []
    blob_ids = [row["blob_id"] for row in rows if row["blob_id"]]
    db.execute(f"DELETE FROM {store.attachments} WHERE {where}", params)
    for start in range(0, len(blob_ids), 500):
        chunk = blob_ids[start:start + 500]
        db.execute(f"DELETE FROM file_blobs WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return [row["filename"] for row in rows]


def remove_files(filenames: Iterable[str]) -> None:
    for name in filenames:
        storage.delete(FOLDER, name)


def wipe(store: Store, message_id: int) -> None:
    """Delete a message for everyone: text, attachments, files and blobs."""
    with db.transaction():
        files = _remove_attachments(store, "message_id = ?", (message_id,))
        db.execute(
            f"UPDATE {store.messages} SET content = '', is_deleted = 1, deleted_at = ? WHERE id = ?",
            (now_sql(), message_id),
        )
    remove_files(files)


def clear(store: Store, parent_id: int) -> list[str]:
    """Delete every message of a conversation; call inside a transaction, then remove the files."""
    files = _remove_attachments(
        store, f"message_id IN (SELECT id FROM {store.messages} WHERE {store.parent} = ?)", (parent_id,)
    )
    db.execute(f"DELETE FROM {store.messages} WHERE {store.parent} = ?", (parent_id,))
    return files


def delete_messages_before(store: Store, cutoff: str) -> tuple[int, list[str]]:
    """Retention: delete messages created before *cutoff*; call inside a transaction."""
    files = _remove_attachments(
        store, f"message_id IN (SELECT id FROM {store.messages} WHERE created_at < ?)", (cutoff,)
    )
    deleted = db.execute(f"DELETE FROM {store.messages} WHERE created_at < ?", (cutoff,)).rowcount
    return deleted, files


def delete_attachments_before(store: Store, cutoff: str) -> list[str]:
    """Retention: delete attachments uploaded before *cutoff*; call inside a transaction."""
    return _remove_attachments(store, "created_at < ?", (cutoff,))


def purge_deleted(store: Store) -> list[str]:
    """Erase what 1.4 kept of deleted messages (it only hid them); call inside a transaction."""
    files = _remove_attachments(
        store, f"message_id IN (SELECT id FROM {store.messages} WHERE is_deleted = 1)", ()
    )
    db.execute(f"UPDATE {store.messages} SET content = '' WHERE is_deleted = 1 AND content != ''")
    return files


def unreferenced_blob_filter() -> str:
    """SQL condition true for ``file_blobs`` rows no attachment table points at."""
    existing = set(db.column("SELECT name FROM sqlite_master WHERE type = 'table'"))
    conditions = [
        f"NOT EXISTS (SELECT 1 FROM {table} r WHERE r.{column} = file_blobs.id)"
        for table, column in BLOB_REFERENCES if table in existing
    ]
    return " AND ".join(conditions) or "1"
