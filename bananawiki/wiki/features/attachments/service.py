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

import logging
import os
import re
import tempfile
import time
import zipfile
from typing import IO, Any

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
_UNSAFE_NAME = re.compile(r"[^\w.()' -]+", re.UNICODE)


class AttachmentError(ValueError):
    """A refused attachment action; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Reading ──────────────────────────────────────────────────────────────────


def for_page(page_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT a.id, a.page_id, a.filename, a.original_name, a.file_size, a.uploaded_by, a.uploaded_at, "
        "a.blob_id, u.username AS uploader FROM page_attachments a LEFT JOIN users u ON u.id = a.uploaded_by "
        "WHERE a.page_id = ? ORDER BY a.uploaded_at, a.id",
        (page_id,),
    )


def get(attachment_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM page_attachments WHERE id = ?", (attachment_id,))


# ── Permissions ──────────────────────────────────────────────────────────────


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
    if not _may_change(page, user):
        return False
    if auth.has_permission("attachment.delete_any", user):
        return True
    return attachment.get("uploaded_by") == user["id"] and auth.has_permission("attachment.delete_own", user)


def may_delete_some(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    """Whether *user* could delete at least their own attachments (shows the delete buttons)."""
    user = _user(user)
    return _may_change(page, user) and (
        auth.has_permission("attachment.delete_any", user) or auth.has_permission("attachment.delete_own", user)
    )


# ── Writing ──────────────────────────────────────────────────────────────────


def max_bytes() -> int:
    return storage.max_upload_bytes(current_app.config["BW"].max_attachment_size)


def add(page: dict[str, Any], upload: FileStorage | None, user: dict[str, Any]) -> dict[str, Any]:
    """Store *upload* and attach it to *page*.

    Raises :class:`storage.UploadError` (``key`` is a translation key) when the
    file is refused: type policy, size, storage quota or the per-user daily
    upload quota shared with page images.
    """
    # The daily quota is shared with page images (``pages.uploads``); a
    # count limit can be checked before reading the file.
    page_uploads._check_quota(user, 0)
    stored = storage.save(upload, FOLDER, allowed=None, max_bytes=max_bytes())
    try:
        with db.transaction():
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


def delete(attachment: dict[str, Any]) -> None:
    """Remove the row, the file on disk and any 1.4 copy in ``file_blobs``."""
    with db.transaction():
        db.execute("DELETE FROM page_attachments WHERE id = ?", (attachment["id"],))
        if attachment.get("blob_id"):
            db.execute("DELETE FROM file_blobs WHERE id = ?", (attachment["blob_id"],))
    storage.delete(FOLDER, attachment["filename"])


# ── Downloads ────────────────────────────────────────────────────────────────


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


def zip_archive(attachments: list[dict[str, Any]]) -> IO[bytes]:
    """Every available attachment in a ZIP, written to an anonymous temporary file.

    The archive is built on disk (never in memory) and the returned file
    disappears when it is closed.
    """
    archive = tempfile.TemporaryFile(dir=_work_dir())  # noqa: SIM115 - handed to send_file, which closes it
    try:
        used: set[str] = set()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for attachment in attachments:
                path = resolve(attachment)
                if path is None:
                    continue
                name = unique_name(safe_name(attachment["original_name"], attachment["filename"]), used)
                zf.write(path, name)
        archive.seek(0)
    except BaseException:
        archive.close()
        raise
    return archive


def _work_dir() -> str:
    folder = current_app.config["BW"].folders.exports
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return folder


# ── Clean-up ─────────────────────────────────────────────────────────────────


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
    """Delete ``file_blobs`` rows that no table references any more (1.4 left many behind)."""
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
