"""Uploaded files on disk.

Every stored file gets a random name (``<32 hex>.<ext>``) inside one of the
configured folders; the original name is kept only in the database, as the
user wrote it in any script (see :func:`clean_filename`) and offered back in
``Content-Disposition`` with an RFC 5987 ``filename*`` (:func:`content_disposition`). Files are
streamed to a temporary file in the same folder and renamed into place, so a
failed upload never leaves a partial file behind.

Files are stored on disk only. Databases upgraded from 1.4 may still hold
copies in the ``file_blobs`` table; when a file is missing on disk but its blob exists, the blob is
written back to disk on first access (see :func:`resolve`).
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock, Timeout
from flask import Response, abort, current_app, g, send_file
from werkzeug.datastructures import FileStorage
from werkzeug.http import dump_options_header

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
MAX_NAME_CHARS = 200
_EXTENSION = re.compile(r"[a-z0-9_-]{1,32}")
# Direction overrides and isolates can make "gpj.exe" read as "exe.jpg".
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
# Joiners shape Persian, Indic and emoji names; other format characters go.
_JOINERS = frozenset("\u200c\u200d")
# Characters no Windows file name may contain.
_RESERVED_NAME_CHARS = re.compile(r'[<>:"|?*]')


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
    """The lower-cased ASCII extension of the last segment of *name*, or ``""``."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[1]
    return ext.lower() if ext.isascii() and _EXTENSION.fullmatch(ext.lower()) else ""


def clean_filename(name: str | None, default: str = "file") -> str:
    """An uploaded file's name as the user sees it, safe to store, show and download as.

    Letters of every script are kept (normalised to NFC). Folders, control,
    direction and other invisible characters, characters Windows refuses and
    leading or trailing dots and spaces go; the name is cut to
    :data:`MAX_NAME_CHARS` with its extension kept.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = re.sub(r"\s+", " ", text)
    text = "".join(char for char in text if char in _JOINERS or (
        char not in _BIDI_CONTROLS and not unicodedata.category(char).startswith("C")))
    text = _RESERVED_NAME_CHARS.sub("_", text).strip(" .")
    if len(text) > MAX_NAME_CHARS:
        ext = extension(text)
        keep = MAX_NAME_CHARS - (len(ext) + 1 if ext else 0)
        text = text[:keep].rstrip(" .") + (f".{ext}" if ext else "")
    return text or default


def content_disposition(name: str | None, *, inline: bool = False) -> str:
    """A ``Content-Disposition`` value offering *name*, in any script, as the file name.

    Header values must be Latin-1: names outside ASCII are sent as an RFC 5987
    ``filename*`` with an ASCII ``filename`` for older clients.
    """
    name = clean_filename(name)
    kind = "inline" if inline else "attachment"
    if name.isascii():
        return dump_options_header(kind, {"filename": name})
    ext = extension(name)
    stem = name[:-len(ext) - 1] if ext else name
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r'[^\x20-\x7e]|["\\%]', "", stem).strip(" .")
    if not any(char.isalnum() for char in stem):
        stem = "download"
    fallback = stem + (f".{ext}" if ext else "")
    return dump_options_header(kind, {"filename": fallback,
                                      "filename*": "UTF-8''" + quote(name, safe="!#$&+-.^_`|~")})


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


def client_rules(allowed: set[str] | frozenset[str] | None = None, max_bytes: int = 0) -> dict[str, object]:
    """What :func:`extension_allowed` and the size limit accept, for scripts to check a file before sending it.

    ``{"maxBytes": n, "allow": [...] or absent, "deny": [...]}``; the server
    checks again, so this only spares people a refused upload.
    """
    cfg = current_app.config["BW"]
    deny = set(cfg.platform_upload_blacklist) | _csv(settings.get("platform_upload_blacklist"))
    allow = set(allowed) if allowed is not None else None
    mode = settings.get("upload_mode") or "allow_all"
    if mode == "whitelist":
        listed = _csv(settings.get("upload_whitelist"))
        allow = listed if allow is None else allow & listed
    elif mode == "blacklist":
        deny |= _csv(settings.get("upload_blacklist"))
    rules: dict[str, object] = {"deny": sorted(deny)}
    if allow is not None:
        rules["allow"] = sorted(allow - deny)
    if max_bytes:
        rules["maxBytes"] = int(max_bytes)
    return rules


def max_upload_bytes(default: int) -> int:
    configured = settings.get("upload_max_size_mb")
    if configured:
        return min(default, int(configured) * 1024 * 1024)
    return default


# Storage quota (managed hosting)

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


TEMP_PREFIXES = (".upload-", ".write-", ".restore-")
TEMP_MAX_AGE_SECONDS = 86400


def sweep_stale_temp_files(max_age: int = TEMP_MAX_AGE_SECONDS) -> int:
    """Delete half-written files a worker left behind when it was killed mid-write; returns how many.

    Writes go to ``.upload-*``, ``.write-*`` or ``.restore-*`` files that are
    renamed when complete, so one older than a day belongs to no write any
    more. They are invisible to the wiki (and to its storage total) but take
    disk space, under managed hosting from the wiki's own hard quota.
    """
    cfg = current_app.config["BW"]
    cutoff = time.time() - max_age
    removed = 0
    for name in FOLDERS:
        root = Path(getattr(cfg.folders, name))
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for filename in files:
                if not filename.startswith(TEMP_PREFIXES):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    info = os.lstat(path)
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        os.unlink(path)
                        removed += 1
                except OSError:
                    continue
    return removed


def quota_allows(extra_bytes: int) -> bool:
    limit = current_app.config["BW"].storage_limit_bytes
    return not limit or storage_used() + max(0, extra_bytes) <= limit


def _note_usage(delta: int) -> None:
    cfg = current_app.config["BW"]
    cached = _usage_cache.get(cfg.instance_dir)
    if cached:
        _usage_cache[cfg.instance_dir] = (cached[0], max(0, cached[1] + delta))


# Saving


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
    original = clean_filename(upload.filename)
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


# Reading


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
    deferred = g.get("_deferred_storage_deletions")
    if deferred is not None:
        deferred.append((folder, filename))
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
    # send_file's own ASCII fallback for a name without Latin letters is
    # just its extension.
    response.headers["Content-Disposition"] = content_disposition(download_name or filename, inline=show_inline)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response.headers["Cache-Control"] = f"private, max-age={max_age}"
    return response


