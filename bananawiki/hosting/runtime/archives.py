"""Portable wiki archives: export a tenant, validate and unpack an uploaded one.

The format is 1.4's unified BananaWiki archive, which a self-hosted wiki's
site migration page also imports::

    manifest.json   {"format_version": 1, "source": "hosting", ...}
    bananawiki.db   consistent snapshot (made inside the tenant container)
    uploads/ attachments/ chat_attachments/ kanban_attachments/ custom_page_files/
    favicons/ translations/ ...

Imports also accept the older 1.4 shapes: everything under one ``<slug>/``
folder (hosting archives, with ``storage/<name>/`` asset paths) and
``site_export.json`` plus ``assets/<name>/`` (early standalone exports; the
JSON dump is turned into a database inside the tenant container).

Import safety: the whole member list is checked before anything is written
(no absolute or ``..`` paths, backslashes, links, special files or
duplicates; member count, unpacked size and compression ratio bounded by
``HOSTING_IMPORT_*``; free disk space), members are streamed to new files in
a staging folder, and only the database and the data folders are taken:
code (``external_plugins/``, ``config.py``), secret keys, logs and anything
else in the archive is ignored.
"""

from __future__ import annotations

import errno
import json
import lzma
import os
import shutil
import stat
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ... import __version__
from ..config import ArchiveSettings
from . import RuntimeFailure, tenantfs

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
RAW_DB = "bananawiki.db"
JSON_DUMP = "site_export.json"
IMPORTED_FOLDERS = frozenset({"favicons", "translations"})
SKIPPED_DIRS = frozenset({"tts", "backups", "tmp_exports", "logs", "piper-voices", "__pycache__"})
SKIPPED_FILES = frozenset({"bananawiki.pid", "tts_worker.pid", "error.log", "access.log", ".secret_key",
                           *tenantfs.DATABASE_FILES, *tenantfs.STALE_STATE_FILES})
MAX_RATIO = 200
MAX_MANIFEST_BYTES = 1024 * 1024
# ZipInfo.compress_level is public from Python 3.13; 3.11 and 3.12 read _compresslevel.
_LEVEL_ATTRIBUTE = "compress_level" if hasattr(zipfile.ZipInfo, "compress_level") else "_compresslevel"


@dataclass(frozen=True)
class Unpacked:
    has_database: bool
    has_json_dump: bool
    skipped: int


# ── Writing ───────────────────────────────────────────────────────────────────


def zip_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    """A ZIP timestamp clamped to what ZIP can store (tenants set their own file times)."""
    try:
        stamp = datetime.fromtimestamp(mtime).timetuple()[:6]
    except (OverflowError, OSError, ValueError):
        stamp = (1980, 1, 1, 0, 0, 0)
    return min(max(stamp, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))  # type: ignore[return-value]


def write_fd(archive: zipfile.ZipFile, fd: int, info: os.stat_result, name: str, settings: ArchiveSettings) -> None:
    """Stream an opened file into *archive* (takes ownership of *fd*)."""
    with os.fdopen(fd, "rb") as source:
        entry = zipfile.ZipInfo(name, date_time=zip_time(info.st_mtime))
        entry.external_attr = (stat.S_IMODE(info.st_mode) | stat.S_IFREG) << 16
        stored = (os.path.splitext(name)[1].lower() in settings.export_store_extensions
                  or 0 < settings.export_store_file_bytes <= info.st_size)
        entry.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
        if not stored:
            setattr(entry, _LEVEL_ATTRIBUTE, settings.export_compress_level)
        entry.file_size = info.st_size
        with archive.open(entry, "w", force_zip64=True) as target:
            # A tenant can keep appending while its files are exported. Read
            # only the size observed when opening it, so the host finishes.
            tenantfs.copy_observed(source, target, info.st_size)


def add_tenant_tree(archive: zipfile.ZipFile, root: Path, prefix: str, settings: ArchiveSettings, *,
                    portable: bool) -> None:
    """Add a tenant's files below *prefix*.

    *portable* (exports): asset folders are flattened from ``storage/<name>``
    to ``<name>``, hidden folders and secrets are left out. Otherwise
    (platform backups) the tree is kept as it is, including the tenant's
    ``.secret_key`` so sessions survive a restore.
    """
    dir_fd = tenantfs.open_dir(root)
    try:
        for relative, fd, info in tenantfs.iter_files(dir_fd, skip_dirs=SKIPPED_DIRS | {tenantfs.EXCHANGE},
                                                      skip_hidden=portable):
            name = relative.rsplit("/", 1)[-1]
            skipped = name in SKIPPED_FILES and not (name == ".secret_key" and not portable)
            if skipped or name.endswith((".lock", "-wal", "-shm", "-journal")):
                os.close(fd)
                continue
            if portable and relative.startswith("storage/"):
                relative = relative[len("storage/"):]
            write_fd(archive, fd, info, f"{prefix}{relative}", settings)
    finally:
        os.close(dir_fd)


def export(root: Path, snapshot: str, destination_dir: Path, slug: str, settings: ArchiveSettings) -> Path:
    """Write ``bananawiki-<slug>-<time>.zip``; *snapshot* is the database copy inside *root*."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in slug if c.isalnum() or c in "-_") or "wiki"
    path = Path(destination_dir) / f"bananawiki-{safe}-{stamp}.zip"
    manifest = {"format_version": FORMAT_VERSION, "source": "hosting", "exported_at": datetime.now(UTC).isoformat(),
                "has_raw_db": True, "has_site_export_json": False, "original_subdomain": slug,
                "bananawiki_version": __version__}
    # Opening before the cleanup scope ensures a name collision cannot delete
    # someone else's completed export.
    archive = zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED, compresslevel=settings.export_compress_level,
                              allowZip64=True)
    try:
        with archive:
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
            parent, _, name = snapshot.rpartition("/")
            dir_fd = tenantfs.open_dir(root, parent)
            try:
                fd = tenantfs.open_file(dir_fd, name)
            finally:
                os.close(dir_fd)
            write_fd(archive, fd, os.fstat(fd), RAW_DB, settings)
            add_tenant_tree(archive, root, "", settings, portable=True)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


# ── Reading ───────────────────────────────────────────────────────────────────


def _layout_prefix(names: set[str]) -> str:
    """"" for unified and standalone archives, "<slug>/" for 1.4 hosting archives."""
    if MANIFEST in names or RAW_DB in names or JSON_DUMP in names or any(n.startswith("assets/") for n in names):
        return ""
    tops = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(tops) == 1:
        prefix = next(iter(tops)) + "/"
        if prefix + RAW_DB in names:
            return prefix
    return ""


def _safe_name(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise RuntimeFailure("archive_invalid", "unsafe member path")
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") or ":" in part for part in path.parts):
        raise RuntimeFailure("archive_invalid", "unsafe member path")
    return path


def destination(name: str, prefix: str) -> str | None:
    """Where an archive member goes inside the tenant directory, or None to skip it."""
    if prefix and name.startswith(prefix):
        name = name[len(prefix):]
    if name.startswith("assets/"):
        name = name[len("assets/"):]
    parts = PurePosixPath(name).parts
    if parts == (RAW_DB,):
        return RAW_DB
    if parts == (JSON_DUMP,):
        return f"{tenantfs.EXCHANGE}/{JSON_DUMP}"
    if parts and parts[0] == "storage":
        parts = parts[1:]
    if len(parts) > 1 and parts[0] in tenantfs.ASSET_FOLDERS:
        return "/".join(("storage", *parts))
    if len(parts) == 2 and parts[0] == "favicons" and parts[1].startswith("custom_"):
        return "/".join(parts)
    if len(parts) == 2 and parts[0] == "translations" and parts[1].endswith(".json"):
        return "/".join(parts)
    return None


def _check_manifest(archive: zipfile.ZipFile, names: set[str]) -> None:
    if MANIFEST not in names:
        return
    if archive.getinfo(MANIFEST).file_size > MAX_MANIFEST_BYTES:
        raise RuntimeFailure("archive_invalid", "manifest.json is too large")
    try:
        with archive.open(MANIFEST) as source:
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RuntimeFailure("archive_invalid", "manifest.json is too large")
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error, lzma.LZMAError, OSError):
        raise RuntimeFailure("archive_invalid", "manifest.json is not JSON") from None
    version = manifest.get("format_version") if isinstance(manifest, dict) else None
    if type(version) is not int or not 1 <= version <= FORMAT_VERSION:
        raise RuntimeFailure("archive_invalid", "unsupported archive format version")


def plan(archive: zipfile.ZipFile, settings: ArchiveSettings, free_bytes: int) -> tuple[dict[str, zipfile.ZipInfo], int]:
    """Check every member before anything is written; returns ``(destination -> member, skipped)``."""
    members = archive.infolist()
    if len(members) > settings.import_max_members:
        raise RuntimeFailure("too_large", "too many files in the archive")
    files = [info for info in members if not info.is_dir()]
    total = 0
    for info in members:
        _safe_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
        if info.flag_bits & 1 or info.compress_type not in (
                zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            raise RuntimeFailure("archive_invalid", "encrypted or unsupported ZIP members cannot be imported")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise RuntimeFailure("archive_invalid", "the archive contains links or special files")
        total += info.file_size
        if total > settings.import_max_extracted_bytes:
            raise RuntimeFailure("too_large", "the archive unpacks beyond the configured limit")
        if info.file_size > 10 * 1024 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise RuntimeFailure("archive_invalid", "suspicious compression ratio")
    if free_bytes < total + settings.import_min_free_bytes:
        raise RuntimeFailure("no_space", "not enough free disk space for this archive")
    names = {info.filename for info in files}
    _check_manifest(archive, names)
    prefix = _layout_prefix(names)
    chosen: dict[str, zipfile.ZipInfo] = {}
    skipped = 0
    for info in files:
        target = destination(info.filename, prefix)
        if target is None:
            skipped += 1
            continue
        if target in chosen:
            raise RuntimeFailure("archive_invalid", "the archive contains duplicate files")
        chosen[target] = info
    if RAW_DB not in chosen and f"{tenantfs.EXCHANGE}/{JSON_DUMP}" not in chosen:
        raise RuntimeFailure("archive_invalid", "the archive holds no BananaWiki database")
    for target in chosen:
        if any(str(parent) in chosen for parent in PurePosixPath(target).parents):
            raise RuntimeFailure("archive_invalid", "the archive uses the same path as a file and a directory")
    if RAW_DB in chosen:
        chosen.pop(f"{tenantfs.EXCHANGE}/{JSON_DUMP}", None)
    return chosen, skipped


def unpack(archive_path: Path, staging: Path, settings: ArchiveSettings) -> Unpacked:
    """Validate *archive_path* and write the members it keeps below the (new, empty) *staging* folder."""
    try:
        size = archive_path.stat().st_size
    except OSError:
        raise RuntimeFailure("archive_invalid", "the archive is missing") from None
    if size > settings.import_max_bytes:
        raise RuntimeFailure("too_large", "the archive is larger than HOSTING_IMPORT_MAX_BYTES")
    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError):
        raise RuntimeFailure("archive_invalid", "not a ZIP archive") from None
    with archive:
        chosen, skipped = plan(archive, settings, shutil.disk_usage(staging.parent).free)
        try:
            for target, info in chosen.items():
                path = staging / target
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(info) as source, open(path, "xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    shutil.copyfileobj(source, output, 1024 * 1024)
        except (zipfile.BadZipFile, EOFError, ValueError, RuntimeError, zlib.error, lzma.LZMAError) as error:
            raise RuntimeFailure("archive_invalid", f"damaged archive: {error}") from None
        except OSError as error:
            if error.errno in (errno.ENOSPC, errno.EDQUOT):
                raise RuntimeFailure("no_space", "not enough free space to unpack this archive") from None
            raise RuntimeFailure("archive_invalid", "an archive member could not be extracted") from None
    return Unpacked(RAW_DB in chosen, f"{tenantfs.EXCHANGE}/{JSON_DUMP}" in chosen, skipped)
