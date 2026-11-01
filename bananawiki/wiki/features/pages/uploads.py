"""Images embedded in pages: upload, per-user daily quota and clean-up.

Images are stored in the ``uploads`` folder and referenced from Markdown as
``/static/uploads/<name>``. They are shared by pages, their history, drafts,
announcements, custom pages, canvases and more, so an image is only removed
by :func:`cleanup_unused` when *no* text column anywhere in the database
mentions its name and it is older than ``MIN_AGE_HOURS`` (an editor may have
uploaded it seconds ago and not saved yet).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import timedelta
from typing import Any

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.timeutil import now_sql, parse, utcnow
from ... import auth, settings, storage
from ...db import db

log = logging.getLogger("bananawiki.pages.uploads")

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
URL_PREFIX = "/static/uploads/"
MIN_AGE_HOURS = 24
QUOTA_WINDOW = timedelta(hours=24)
# Names BananaWiki generates (random hex) and explicit upload URLs.
_GENERATED_NAME = re.compile(r"[0-9a-f]{32}\.[A-Za-z0-9]{1,8}")
_UPLOAD_URL = re.compile(r"/static/uploads/([^\s)\"'\\<>?#]+)")
_TEXT_TYPES = ("", "TEXT", "VARCHAR", "CHAR", "CLOB", "JSON")
# Full-text index shadow tables repeat page content; scanning pages is enough.
_SKIPPED_TABLE_PREFIXES = ("sqlite_", "pages_fts")


class QuotaExceeded(storage.UploadError):
    pass


def may_upload(user: dict[str, Any] | None = None) -> bool:
    """Editors who can create or edit pages (and administrators) may upload images."""
    user = auth.current_user() if user is None else user
    if not user:
        return False
    if auth.is_admin(user):
        return True
    return auth.has_role("editor", user) and (
        auth.has_permission("page.edit_all", user) or auth.has_permission("page.create", user)
    )


def _check_quota(user: dict[str, Any], size: int, files: int = 1) -> dict[str, Any]:
    """Raise :class:`QuotaExceeded` if *files* more images of *size* bytes break the daily limits; return new
    counters."""
    max_count = int(settings.get("upload_quota_per_day_count", 0) or 0)
    max_bytes = int(settings.get("upload_quota_per_day_bytes", 0) or 0)
    row = db.one("SELECT upload_window_start, upload_window_count, upload_window_bytes FROM users WHERE id = ?",
                 (user["id"],)) or {}
    start = parse(row.get("upload_window_start"))
    if start is None or utcnow() - start >= QUOTA_WINDOW:
        count, used, start_sql = 0, 0, now_sql()
    else:
        count, used, start_sql = int(row.get("upload_window_count") or 0), int(row.get("upload_window_bytes") or 0), \
            row["upload_window_start"]
    if max_count and count + files > max_count:
        raise QuotaExceeded("pages.upload.quota_count", count=max_count)
    if max_bytes and used + size > max_bytes:
        raise QuotaExceeded("pages.upload.quota_bytes", limit_mb=max(1, max_bytes // (1024 * 1024)))
    return {"upload_window_start": start_sql, "upload_window_count": count + files,
            "upload_window_bytes": used + size}


def max_image_bytes() -> int:
    return storage.max_upload_bytes(current_app.config["BW"].max_content_length)


def client_rules() -> dict[str, object]:
    """What :func:`save_image` accepts, for the editor to check a picture before sending it."""
    return storage.client_rules(IMAGE_EXTENSIONS, max_image_bytes())


def save_image(upload: FileStorage | None, user: dict[str, Any]) -> dict[str, str]:
    """Store an uploaded image for *user*; return ``{url, filename}``.

    Raises :class:`storage.UploadError` (``key`` is a translation key).
    """
    stored = save_counted(upload, user, allowed=IMAGE_EXTENSIONS, max_bytes=max_image_bytes())
    return {"url": URL_PREFIX + stored.filename, "filename": stored.filename}


def save_counted(upload: FileStorage | None, user: dict[str, Any], *, allowed: Any, max_bytes: int) -> Any:
    """Store an image in the uploads folder, counted against *user*'s daily quota; the stored file.

    Every feature that lets people put images there (pages, the page builder,
    canvases) goes through this, so the administrator's daily limits hold
    whichever way the picture comes. Raises :class:`storage.UploadError`
    (:class:`QuotaExceeded` when a limit is reached).
    """
    if int(settings.get("upload_quota_per_day_count", 0) or 0):
        _check_quota(user, 0)
    stored = storage.save(upload, "uploads", allowed=allowed, max_bytes=max_bytes, images_only=True)
    try:
        with db.transaction():
            counters = _check_quota(user, stored.size)
            db.update("users", counters, "id = ?", (user["id"],))
    except QuotaExceeded:
        storage.delete("uploads", stored.filename)
        raise
    return stored


def check_room(user: dict[str, Any], files: int) -> None:
    """Raise :class:`QuotaExceeded` if *user* may not upload *files* more images today (before storing any)."""
    _check_quota(user, 0, files)


def count_stored(user: dict[str, Any], files: int, size: int) -> None:
    """Count *files* images of *size* bytes, stored together, against *user*'s daily quota.

    For a batch (a canvas import) that is kept or refused as a whole: raises
    :class:`QuotaExceeded`, counting nothing, and the caller removes the files.
    """
    with db.transaction():
        db.update("users", _check_quota(user, size, files), "id = ?", (user["id"],))


def uncount_stored(user: dict[str, Any], files: int, size: int) -> None:
    """Give back what :func:`count_stored` counted for a batch removed after all (the canvas was not created)."""
    with db.transaction():
        db.execute("UPDATE users SET upload_window_count = MAX(0, COALESCE(upload_window_count, 0) - ?), "
                   "upload_window_bytes = MAX(0, COALESCE(upload_window_bytes, 0) - ?) WHERE id = ?",
                   (files, size, user["id"]))


# Clean-up


def _text_columns(table: str) -> list[str]:
    rows = db.all(f"PRAGMA table_info({_quote(table)})")
    return [row["name"] for row in rows if (row["type"] or "").upper().split("(")[0] in _TEXT_TYPES]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def referenced_names() -> set[str]:
    """Every upload name mentioned in any text column of the database.

    Deliberately broad: the tables of disabled features and of features this
    code does not know about are scanned too, so their images survive.
    """
    found: set[str] = set()
    tables = db.column("SELECT name FROM sqlite_master WHERE type = 'table'")
    for table in tables:
        if table.startswith(_SKIPPED_TABLE_PREFIXES):
            continue
        columns = _text_columns(table)
        if not columns:
            continue
        condition = " OR ".join(f"{_quote(c)} IS NOT NULL" for c in columns)
        cursor = db.execute(f"SELECT {', '.join(_quote(c) for c in columns)} FROM {_quote(table)} WHERE {condition}")
        for row in cursor:
            for value in row.values():
                if isinstance(value, str) and value:
                    found.update(_GENERATED_NAME.findall(value))
                    found.update(name.rsplit("/", 1)[-1] for name in _UPLOAD_URL.findall(value))
    return found


def cleanup_unused(min_age_hours: float = MIN_AGE_HOURS) -> int:
    """Delete images in the uploads folder that nothing references (background job)."""
    folder = storage.folder_path("uploads")
    cutoff = time.time() - min_age_hours * 3600
    candidates = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                continue
            candidates.append(entry.name)
    if not candidates:
        return 0
    referenced = referenced_names()
    removed = 0
    for name in candidates:
        if name in referenced:
            continue
        try:
            storage.delete("uploads", name)
            removed += 1
        except OSError:
            log.warning("Could not remove unused upload %s", name, exc_info=True)
    if removed:
        log.info("Removed %d unused uploaded images", removed)
    return removed


def is_referenced(filename: str) -> bool:
    return filename in referenced_names()
