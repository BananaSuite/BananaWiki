"""Personal data export as a streamed ZIP archive.

The archive holds the account, profile, preferences and every row of every
table that refers to the account through a foreign key to ``users(id)``
(discovered from the schema, so tables of features added later are covered),
plus the files the account uploaded. Credential columns (password hashes,
tokens, secrets) are never exported. Nothing is built in memory: rows and
files are written to the ZIP stream as they are read.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ....core.sqlite import quote_identifier, tuples
from ....core.timeutil import now_sql
from ... import storage
from ...db import db
from ..users import preferences
from ..users import service as users

FORMAT = "bananawiki-user-data-export"
VERSION = "3.0"
CHUNK = 256 * 1024
SECRET_COLUMN = re.compile(r"password|token|secret|hash|session_key", re.IGNORECASE)
# Tables whose rows describe someone else or the site rather than this account.
SKIP_TABLES = frozenset({"users", "pages", "file_blobs", "site_settings"})
ACCOUNT_COLUMNS = ("id", "username", "role", "suspended", "suspended_until", "approval_status", "invite_code",
                   "is_superuser", "created_at", "last_login_at", "chat_disabled", "userbot_enabled")
# (table, user column, storage folder) for files the account uploaded.
UPLOADED_FILES = (
    ("page_attachments", "uploaded_by", "attachments"),
    ("kanban_ticket_attachments", "uploaded_by", "kanban_attachments"),
)


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
        if table in SKIP_TABLES:
            continue
        columns = sorted({row[3] for row in tuples(db.conn, f"PRAGMA foreign_key_list({quote_identifier(table)})")
                          if row[2] == "users"})
        if columns:
            found.append((table, columns))
    return found


def _exported_columns(table: str) -> list[str]:
    return [row[1] for row in tuples(db.conn, f"PRAGMA table_info({quote_identifier(table)})")
            if not SECRET_COLUMN.search(row[1])]


class _Writer:
    def __init__(self) -> None:
        self.sink = _Sink()
        self.zip = zipfile.ZipFile(self.sink, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
        self.summary: dict[str, int] = {}

    def json(self, name: str, data: Any) -> Iterator[bytes]:
        with self.zip.open(name, "w", force_zip64=True) as out:
            out.write(json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8"))
        yield self.sink.drain()

    def rows(self, name: str, sql: str, params: list[Any]) -> Iterator[bytes]:
        """Stream the rows of *sql* as a JSON array."""
        count = 0
        with self.zip.open(name, "w", force_zip64=True) as out:
            out.write(b"[")
            for row in db.execute(sql, params):
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
    return {key: user.get(key) for key in ACCOUNT_COLUMNS}


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
    for table, columns in user_tables():
        selected = ", ".join(quote_identifier(c) for c in _exported_columns(table))
        condition = " OR ".join(f"{quote_identifier(c)} = ?" for c in columns)
        yield from writer.rows(f"data/{table}.json", f"SELECT {selected} FROM {quote_identifier(table)} "
                               f"WHERE {condition}", [user["id"]] * len(columns))
    for name, folder in ((profile.get("avatar_filename"), "uploads"),
                         (preferences.stored(user).get("background_image"), "uploads")):
        path = storage.resolve(folder, name) if name else None
        if path is not None:
            yield from writer.file(f"files/{name}", path)
    for table, column, folder in UPLOADED_FILES:
        if not db.scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)):
            continue
        for row in db.all(f"SELECT id, filename, original_name, blob_id FROM {quote_identifier(table)} "
                          f"WHERE {quote_identifier(column)} = ? ORDER BY id", (user["id"],)):
            path = storage.resolve(folder, row["filename"], blob_id=row["blob_id"])
            if path is not None:
                yield from writer.file(f"files/{table}/{row['id']}-{_safe_name(row['original_name'], 'file')}", path)
    yield from writer.json("summary.json", {"rows": writer.summary})
    yield from writer.close()


def filename(user: dict[str, Any]) -> str:
    return f"userdata_{_safe_name(user['username'], 'account')}.zip"
