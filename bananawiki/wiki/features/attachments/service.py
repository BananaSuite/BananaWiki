"""Page attachments: files attached to a wiki page (``page_attachments``).

Files live in the ``attachments`` storage folder under random names; the
original name is kept in the row. Installations upgraded from 1.4 may still
have a copy in ``file_blobs`` (``blob_id``): downloads fall back to it when
the file is missing on disk, and deleting an attachment removes it too.

Who may do what
---------------
* download: read access to the page and ``attachment.view``;
* upload: edit rights on the page, the page not frozen, and ``attachment.upload``;
* delete: edit rights on the page, the page not frozen, and
  ``attachment.delete_any`` (or ``attachment.delete_own`` for one's own files).
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.timeutil import now_sql
from ... import auth, storage
from ...db import db
from ..pages import access as page_access
from ..pages import service as pages
from ..pages import uploads as page_uploads

log = logging.getLogger("bananawiki.attachments")

FOLDER = "attachments"
ORPHAN_MIN_AGE_SECONDS = 3600
#: "Download all" archives one process sends at once (each holds a request thread while it is read).
MAX_ZIP_STREAMS = 2
#: Files one page holds; the page view and the editor list them all on every view.
MAX_PER_PAGE = 500
_ZIP_CHUNK = 1024 * 1024
_zip_streams = threading.BoundedSemaphore(MAX_ZIP_STREAMS)
_UNSAFE_NAME = re.compile(r"[^\w.()' -]+", re.UNICODE)


# Reading


def for_page(page_id: int, limit: int = -1) -> list[dict[str, Any]]:
    """The page's attachments, oldest first (at most *limit* of them; -1 for all)."""
    return db.all(
        "SELECT a.id, a.page_id, a.filename, a.original_name, a.file_size, a.uploaded_by, a.uploaded_at, "
        "a.blob_id, u.username AS uploader FROM page_attachments a LEFT JOIN users u ON u.id = a.uploaded_by "
        "WHERE a.page_id = ? ORDER BY a.uploaded_at, a.id LIMIT ?",
        (page_id, limit),
    )


def count(page_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM page_attachments WHERE page_id = ?", (page_id,), default=0))


def get(attachment_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM page_attachments WHERE id = ?", (attachment_id,))


# Permissions


def _user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    return auth.current_user() if user is None else user


def can_download(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return bool(user) and pages.can_view(page, user) and auth.has_permission("attachment.view", user)


def _may_change(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    return bool(user) and pages.can_edit(page, user) and not page_access.edit_blocked(page, user)


def can_upload(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return _may_change(page, user) and auth.has_permission("attachment.upload", user)


def can_delete(attachment: dict[str, Any], page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return attachment["id"] in deletable([attachment], page, user)


def deletable(attachments: list[dict[str, Any]], page: dict[str, Any],
              user: dict[str, Any] | None = None) -> set[int]:
    """Ids of the *attachments* of *page* that *user* may delete (the page is checked once)."""
    user = _user(user)
    if not attachments or not _may_change(page, user):
        return set()
    if auth.has_permission("attachment.delete_any", user):
        return {a["id"] for a in attachments}
    if not auth.has_permission("attachment.delete_own", user):
        return set()
    return {a["id"] for a in attachments if a.get("uploaded_by") == user["id"]}


# Writing


def max_bytes() -> int:
    return storage.max_upload_bytes(current_app.config["BW"].max_attachment_size)


def add(page: dict[str, Any], upload: FileStorage | None, user: dict[str, Any]) -> dict[str, Any]:
    """Store *upload* and attach it to *page*.

    Raises :class:`storage.UploadError` (``key`` is a translation key) when the
    file is refused: type policy, size, storage quota, the per-user daily
    upload quota shared with page images, or a page that already holds
    :data:`MAX_PER_PAGE` files.
    """
    # The daily quota is shared with page images (``pages.uploads``); a
    # count limit can be checked before reading the file.
    page_uploads._check_quota(user, 0)
    _check_room(page)
    stored = storage.save(upload, FOLDER, allowed=None, max_bytes=max_bytes())
    try:
        with db.transaction():
            _check_room(page)
            counters = page_uploads._check_quota(user, stored.size)
            db.update("users", counters, "id = ?", (user["id"],))
            attachment_id = db.insert("page_attachments", {
                "page_id": page["id"],
                "filename": stored.filename,
                "original_name": stored.original_name,
                "file_size": stored.size,
                "uploaded_by": user["id"],
                "uploaded_at": now_sql(),
            })
    except BaseException:
        storage.delete(FOLDER, stored.filename)
        raise
    attachment = get(attachment_id)
    assert attachment is not None
    return attachment


def _check_room(page: dict[str, Any]) -> None:
    if count(page["id"]) >= MAX_PER_PAGE:
        raise storage.UploadError("attachments.error.page_full", count=MAX_PER_PAGE)


def delete(attachment: dict[str, Any]) -> None:
    """Remove the row, the file on disk and any 1.4 copy in ``file_blobs``."""
    with db.transaction():
        db.execute("DELETE FROM page_attachments WHERE id = ?", (attachment["id"],))
        if attachment.get("blob_id"):
            db.execute("DELETE FROM file_blobs WHERE id = ?", (attachment["blob_id"],))
    storage.delete(FOLDER, attachment["filename"])


# Downloads


def safe_name(name: str | None, fallback: str) -> str:
    """A file name safe to use inside a ZIP archive."""
    cleaned = _UNSAFE_NAME.sub("_", os.path.basename(str(name or "")).replace("\x00", "")).strip(" .")
    return cleaned[:180] or fallback


def unique_name(name: str, used: set[str]) -> str:
    """*name*, or *name* with a ``-2``, ``-3``… suffix when it is already in *used*."""
    candidate, n = name, 2
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    while candidate.casefold() in used:
        candidate = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
        n += 1
    used.add(candidate.casefold())
    return candidate


def resolve(attachment: dict[str, Any]):
    """Path of the attachment's file (restored from its 1.4 blob if needed), or None."""
    return storage.resolve(FOLDER, attachment["filename"], blob_id=attachment.get("blob_id"))


class Busy(Exception):
    """This process already sends :data:`MAX_ZIP_STREAMS` archives."""


class _Sink(io.RawIOBase):
    """Where the archive is written: handed out piece by piece while it is made."""

    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:  # type: ignore[override]
        self._pieces.append(bytes(data))
        return len(data)

    def take(self) -> bytes:
        data = b"".join(self._pieces)
        self._pieces.clear()
        return data


def zip_files(attachments: list[dict[str, Any]]) -> list[tuple[Path, str]]:
    """The available files of *attachments* with their unique names inside the archive."""
    used: set[str] = set()
    files = []
    for attachment in attachments:
        path = resolve(attachment)
        if path is not None:
            files.append((path, unique_name(safe_name(attachment["original_name"], attachment["filename"]), used)))
    return files


def zip_stream(files: list[tuple[Path, str]]) -> Iterator[bytes]:
    """A ZIP of *files* (from :func:`zip_files`), made while it is sent.

    Files are stored, not compressed again (most are compressed already),
    and nothing is written to disk. Raises :class:`Busy` before anything is
    sent when this process already sends :data:`MAX_ZIP_STREAMS` archives.
    """
    if not _zip_streams.acquire(blocking=False):
        raise Busy
    released = threading.Lock()

    def release() -> None:
        # Exactly once: when the archive ends, or when the response is closed without being read
        # (a HEAD request never starts the generator, so its own finally would never run).
        if released.acquire(blocking=False):
            _zip_streams.release()

    def pieces() -> Iterator[bytes]:
        try:
            sink = _Sink()
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as archive:
                for path, name in files:
                    try:
                        source = open(path, "rb")  # noqa: SIM115 - closed below
                        info = zipfile.ZipInfo.from_file(path, name)
                    except OSError:
                        continue
                    with source, archive.open(info, "w") as target:
                        while chunk := source.read(_ZIP_CHUNK):
                            target.write(chunk)
                            yield sink.take()
                    yield sink.take()
            yield sink.take()
        finally:
            release()

    return _Closing(pieces(), release)


class _Closing:
    """An iterable that runs *on_close* when the server closes it, read or not."""

    def __init__(self, iterator: Iterator[bytes], on_close: Callable[[], None]):
        self._iterator = iterator
        self._on_close = on_close

    def __iter__(self) -> Iterator[bytes]:
        return self._iterator

    def close(self) -> None:
        try:
            self._iterator.close()  # type: ignore[attr-defined]
        finally:
            self._on_close()


# Clean-up


def _blob_reference_columns() -> list[tuple[str, str]]:
    """``(table, column)`` pairs that point at ``file_blobs`` rows."""
    tables = db.column("SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'file_blobs'")
    pairs = []
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        if any(col["name"] == "blob_id" for col in db.all(f"PRAGMA table_info({quoted})")):
            pairs.append((quoted, '"blob_id"'))
    return pairs


def delete_orphan_blobs() -> int:
    """Delete ``file_blobs`` rows that no table references any more."""
    if not db.scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'file_blobs'"):
        return 0
    conditions = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {table} WHERE {column} = file_blobs.id)"
        for table, column in _blob_reference_columns()
    ) or "1"
    return db.execute(f"DELETE FROM file_blobs WHERE {conditions}").rowcount


def delete_orphan_files(min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS) -> int:
    """Remove files in the attachments folder that no row refers to.

    Files younger than *min_age_seconds* are kept: an upload writes its file
    just before inserting the row.
    """
    folder = storage.folder_path(FOLDER)
    cutoff = time.time() - min_age_seconds
    known = set(db.column("SELECT filename FROM page_attachments"))
    removed = 0
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith(".") or entry.name in known or not entry.is_file(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                continue
            storage.delete(FOLDER, entry.name)
            removed += 1
    return removed


def cleanup() -> None:
    """Background job: orphaned attachment files and 1.4 blobs."""
    files = delete_orphan_files()
    blobs = delete_orphan_blobs()
    if files or blobs:
        log.info("Removed %d orphaned attachment files and %d orphaned blobs", files, blobs)


def on_page_deleted(page: dict[str, Any], **_: Any) -> None:
    """The page's rows are gone (cascade) and its files deleted; drop the 1.4 blobs too."""
    delete_orphan_blobs()
