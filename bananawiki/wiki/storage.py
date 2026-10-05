"""Uploaded files on disk.

Every stored file gets a random name (``<32 hex>.<ext>``) inside one of the
configured folders; the original name is kept only in the database. Files are
streamed to a temporary file in the same folder and renamed into place, so a
failed upload never leaves a partial file behind.

1.4 also copied attachments into the ``file_blobs`` table. 1.6 stores files on
disk only; when a file is missing on disk but its blob exists, the blob is
written back to disk on first access (see :func:`resolve`).
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from filelock import FileLock, Timeout
from flask import Response, abort, current_app, send_file
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from ..core.sqlite import DatabaseUnavailable
from . import settings
from .db import db

CHUNK = 1024 * 1024
FOLDERS = (
    "uploads", "favicons", "attachments", "chat_attachments", "kanban_attachments", "custom_page_files", "tts",
)
# Types that browsers may display inline without risk of script execution.
INLINE_SAFE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/mp4", "video/mp4", "video/webm", "application/pdf",
    "text/plain",
})
_IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP"}


class UploadError(ValueError):
    """The upload was refused; ``key`` is a translation key for the reason."""

    def __init__(self, key: str, **values: object):
        super().__init__(key)
        self.key = key
        self.values = values


@dataclass(frozen=True)
class StoredFile:
    folder: str
    filename: str
    original_name: str
    size: int
    mime_type: str
    sha256: str


def folder_path(folder: str) -> Path:
    if folder not in FOLDERS:
        raise ValueError(f"Unknown storage folder {folder!r}")
    path = Path(getattr(current_app.config["BW"].folders, folder))
    path.mkdir(parents=True, exist_ok=True)
    return path


def extension(name: str | None) -> str:
    name = name or ""
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def _csv(value: str | None) -> set[str]:
    return {item.strip().lower().lstrip(".") for item in (value or "").split(",") if item.strip()}


def extension_allowed(ext: str, allowed: set[str] | frozenset[str] | None) -> bool:
    """Apply the feature's list, the site upload policy and the host's deny list."""
    if not ext:
        return False
    cfg = current_app.config["BW"]
    if ext in cfg.platform_upload_blacklist or ext in _csv(settings.get("platform_upload_blacklist")):
        return False
    if allowed is not None and ext not in allowed:
        return False
    mode = settings.get("upload_mode") or "allow_all"
    if mode == "whitelist":
        return ext in _csv(settings.get("upload_whitelist"))
    if mode == "blacklist":
        return ext not in _csv(settings.get("upload_blacklist"))
    return True


def max_upload_bytes(default: int) -> int:
    configured = settings.get("upload_max_size_mb")
    if configured:
        return min(default, int(configured) * 1024 * 1024)
    return default


# ── Storage quota (managed hosting) ──────────────────────────────────────────

_usage_cache: dict[str, tuple[float, int]] = {}


def storage_used(ttl: float = 60.0) -> int:
    cfg = current_app.config["BW"]
    cached = _usage_cache.get(cfg.instance_dir)
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    total = 0
    roots = {Path(getattr(cfg.folders, name)) for name in FOLDERS} | {Path(cfg.database_path).parent}
    seen: set[tuple[int, int]] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                # Pending writes have their own byte limits. Count them only
                # when published, so concurrent uploads do not reject each
                # other before either file is actually kept.
                if name.startswith((".upload-", ".write-")):
                    continue
                try:
                    info = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                key = (info.st_dev, info.st_ino)
                if key not in seen:
                    seen.add(key)
                    total += info.st_size
    _usage_cache[cfg.instance_dir] = (time.monotonic(), total)
    return total


def quota_allows(extra_bytes: int) -> bool:
    limit = current_app.config["BW"].storage_limit_bytes
    return not limit or storage_used() + max(0, extra_bytes) <= limit


def _note_usage(delta: int) -> None:
    cfg = current_app.config["BW"]
    cached = _usage_cache.get(cfg.instance_dir)
    if cached:
        _usage_cache[cfg.instance_dir] = (cached[0], max(0, cached[1] + delta))


# ── Saving ────────────────────────────────────────────────────────────────────


# Largest image (in pixels) an upload may decode to: 40 megapixels, beyond any camera photo.
MAX_IMAGE_PIXELS = 40_000_000
# A decoded picture takes up to four bytes per pixel, so each worker process
# decodes one picture at a time: simultaneous uploads wait for their turn
# instead of adding up to more memory than a small instance has.
IMAGE_SLOT_TIMEOUT = 30
_image_slot = threading.BoundedSemaphore(1)


def _new_name(ext: str) -> str:
    return f"{secrets.token_hex(16)}.{ext}" if ext else secrets.token_hex(16)


@contextmanager
def image_slot() -> Iterator[None]:
    """Hold this process's turn to decode or re-encode a picture."""
    if not _image_slot.acquire(timeout=IMAGE_SLOT_TIMEOUT):
        raise UploadError("upload.error.busy")
    try:
        yield
    finally:
        _image_slot.release()


def _sanitize_image(path: Path, ext: str, max_pixels: int) -> None:
    """Verify an image and drop its metadata (EXIF/GPS) by re-encoding stills.

    The size in the header is checked before anything is decoded: Pillow only
    warns up to twice ``MAX_IMAGE_PIXELS``, so a small file announcing a huge
    canvas could otherwise make the worker allocate gigabytes. *max_pixels* is
    the (lower) limit of the picture's use, such as an avatar.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(path) as probe:
            width, height = probe.size
            if width * height > MAX_IMAGE_PIXELS:
                raise UploadError("upload.error.not_an_image")
            if width * height > max_pixels:
                raise UploadError("upload.error.too_many_pixels", limit_mp=f"{max_pixels / 1_000_000:g}")
            probe.verify()
        with image_slot(), Image.open(path) as image:
            fmt = _IMAGE_FORMATS[ext]
            accepted = {"JPEG", "MPO"} if fmt == "JPEG" else {fmt}
            if (image.format or "").upper() not in accepted:
                raise UploadError("upload.error.not_an_image")
            if fmt == "GIF" or getattr(image, "is_animated", False):
                return
            # In place: an upright picture is not copied, a turned one replaces the original.
            ImageOps.exif_transpose(image, in_place=True)
            cleaned = image
            if fmt == "JPEG" and cleaned.mode not in ("RGB", "L"):
                cleaned = cleaned.convert("RGB")
            buffer = io.BytesIO()
            save_args = {"quality": 90, "optimize": True} if fmt == "JPEG" else {}
            cleaned.save(buffer, format=fmt, **save_args)
        path.write_bytes(buffer.getvalue())
    except UploadError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise UploadError("upload.error.not_an_image") from error


def save(
    upload: FileStorage | None,
    folder: str,
    *,
    allowed: set[str] | frozenset[str] | None,
    max_bytes: int,
    images_only: bool = False,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> StoredFile:
    """Validate and store an uploaded file; raise :class:`UploadError` if refused.

    Images larger than *max_pixels* are refused from their header, before decoding.
    """
    if upload is None or not upload.filename:
        raise UploadError("upload.error.no_file")
    original = secure_filename(upload.filename) or "file"
    ext = extension(original)
    if images_only and ext not in _IMAGE_FORMATS:
        raise UploadError("upload.error.not_an_image")
    if not extension_allowed(ext, allowed):
        raise UploadError("upload.error.type_not_allowed", ext=ext or "?")
    if not quota_allows(upload.content_length or 0):
        raise UploadError("upload.error.quota")
    target_dir = folder_path(folder)
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=".upload-")
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = upload.stream.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadError("upload.error.too_large", limit_mb=max(1, max_bytes // (1024 * 1024)))
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise UploadError("upload.error.empty")
        if ext in _IMAGE_FORMATS:
            _sanitize_image(tmp, ext, max_pixels)
            size = tmp.stat().st_size
            if size > max_bytes:
                raise UploadError("upload.error.too_large", limit_mb=max(1, max_bytes // (1024 * 1024)))
            digest = hashlib.sha256(tmp.read_bytes())
        name = _new_name(ext)
        cfg = current_app.config["BW"]
        guard = (FileLock(str(Path(cfg.instance_dir) / ".storage-quota.lock"), timeout=20, mode=0o600)
                 if cfg.storage_limit_bytes else nullcontext())
        try:
            with guard:
                # Usage caches are private to each worker. Refresh under one
                # shared lock before publishing so their upload allowances
                # cannot overlap or overlook files another worker just kept.
                if cfg.storage_limit_bytes and storage_used(ttl=0) + size > cfg.storage_limit_bytes:
                    raise UploadError("upload.error.quota")
                os.chmod(tmp, 0o640)
                os.replace(tmp, target_dir / name)
                _note_usage(size)
        except Timeout as error:
            raise DatabaseUnavailable("Upload storage is busy. Try again shortly.") from error
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    mime = mimetypes.guess_type(original)[0] or "application/octet-stream"
    return StoredFile(folder, name, original, size, mime, digest.hexdigest())


def save_bytes(data: bytes, folder: str, ext: str) -> str:
    """Store generated content (exports, thumbnails, audio); return its name."""
    target_dir = folder_path(folder)
    name = _new_name(ext)
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=".write-")
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(tmp_name, target_dir / name)
    _note_usage(len(data))
    return name


# ── Reading ───────────────────────────────────────────────────────────────────


def resolve(folder: str, filename: str, *, blob_id: int | None = None) -> Path | None:
    """Absolute path of a stored file, or None. Refuses anything outside *folder*."""
    if not isinstance(filename, str) or not filename or "\x00" in filename \
            or filename != os.path.normpath(filename) or filename.startswith(("/", "..")):
        return None
    root = folder_path(folder).resolve()
    try:
        candidate = (root / filename).resolve()
        if root not in candidate.parents:
            return None
        if candidate.is_file():
            return candidate
    except (OSError, ValueError, RuntimeError):
        # Invalid path lengths, malformed names and symlink loops are missing
        # resources, rather than an application error on a public file route.
        return None
    if blob_id:
        row = db.one("SELECT content FROM file_blobs WHERE id = ?", (blob_id,))
        if row and row["content"] is not None:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            # Different workers may restore the same legacy blob together.
            # Each writer needs its own staging inode so one rename cannot
            # remove or expose another writer's unfinished file.
            fd, tmp_name = tempfile.mkstemp(dir=candidate.parent, prefix=".restore-")
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(row["content"])
                os.replace(tmp, candidate)
            finally:
                tmp.unlink(missing_ok=True)
            return candidate
    return None


def delete(folder: str, filename: str | None) -> None:
    if not filename:
        return
    path = resolve(folder, filename)
    if path is not None:
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        _note_usage(-size)


def send(
    folder: str,
    filename: str,
    *,
    download_name: str | None = None,
    inline: bool = False,
    blob_id: int | None = None,
    max_age: int = 3600,
) -> Response:
    """Serve a stored file. Only media types in INLINE_SAFE_TYPES may render inline."""
    path = resolve(folder, filename, blob_id=blob_id)
    if path is None:
        abort(404)
    guessed = mimetypes.guess_type(download_name or filename)[0] or "application/octet-stream"
    show_inline = inline and guessed in INLINE_SAFE_TYPES
    try:
        response = send_file(
            path,
            mimetype=guessed if show_inline else "application/octet-stream",
            as_attachment=not show_inline,
            download_name=download_name or filename,
            max_age=max_age,
            conditional=True,
        )
    except FileNotFoundError:
        # Deletion may commit after resolve() and before send_file opens it.
        abort(404)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response.headers["Cache-Control"] = f"private, max-age={max_age}"
    return response


def copy_into(folder: str, source: BinaryIO, ext: str) -> str:
    target_dir = folder_path(folder)
    name = _new_name(ext)
    with open(target_dir / name, "wb") as out:
        shutil.copyfileobj(source, out, CHUNK)
    return name
