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
(no absolute or ``..`` paths, backslashes, links, special files, duplicates
or members compressed other than stored or deflated; member count, unpacked
size and compression ratio bounded by ``HOSTING_IMPORT_*``; free disk
space), members are streamed to new files in
a staging folder, and only the database and the data folders are taken:
code (``external_plugins/``, ``config.py``), secret keys, logs and anything
else in the archive is ignored.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import stat
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from ... import __version__
from ...core.json import loads as safe_json_loads
from ..config import ArchiveSettings
from . import RuntimeFailure, tenantfs

log = logging.getLogger("bananawiki.hosting.runtime.archives")

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
# zipfile bounds each read of a deflated member, but hands bzip2 and LZMA data
# to decompressors without an output limit: a few hundred bytes expand to
# gigabytes in memory before any size check sees them. BananaWiki only ever
# writes stored and deflated members.
READABLE_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
# Restores and imports unpack below folders of their own, within Linux's
# 4096-byte path limit: a longer tenant path would make the whole archive
# fail to unpack.
MAX_NAME_BYTES = 3072
# An export's manifest names at most this many of the files it left out:
# a tenant can create any number, and a manifest over MAX_MANIFEST_BYTES
# would make the import refuse the archive.
MAX_LISTED_FILES = 50
# Imports strip these from member paths (the hosting import storage/ after
# assets/): an exported file below them lands where the wiki's own goes.
ALIAS_PREFIXES = ("assets/", "storage/")
# What a member adds to an archive besides its data and its name (stored
# twice): the local and central headers with their ZIP64 fields.
MEMBER_OVERHEAD = 128


@dataclass(frozen=True)
class Unpacked:
    has_database: bool
    has_json_dump: bool
    skipped: int


@dataclass(frozen=True)
class DatabaseCopy:
    """A database copy the tenant task made inside the tenant directory (*path*), with the size and SHA-256 the
    task reported for it."""

    path: str
    size: int
    sha256: str


# ── Member names ──────────────────────────────────────────────────────────────


def unsafe_name(name: str) -> bool:
    """A member path that imports and platform restores refuse: absolute, with
    ``.`` or ``..`` parts, a NUL, a backslash or a colon (a drive or stream on Windows)."""
    return ("\\" in name or "\x00" in name or name.startswith("/")
            or any(part in ("", ".", "..") or ":" in part for part in PurePosixPath(name).parts))


def storable_name(name: str) -> bool:
    """Whether an export or backup may write *name*: every reader must take it back.

    A tenant can create any file name, and one refused member fails the whole
    import or restore.
    """
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        # Bytes that are not UTF-8: a ZIP stores names as UTF-8 only.
        return False
    return 0 < size <= MAX_NAME_BYTES and not unsafe_name(name)


# ── Writing ───────────────────────────────────────────────────────────────────


def zip_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    """A ZIP timestamp clamped to what ZIP can store (tenants set their own file times)."""
    try:
        stamp = datetime.fromtimestamp(mtime).timetuple()[:6]
    except (OverflowError, OSError, ValueError):
        stamp = (1980, 1, 1, 0, 0, 0)
    return min(max(stamp, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))  # type: ignore[return-value]


class _Hashing:
    """A member being written, feeding *digest* with every block on the way."""

    def __init__(self, target: BinaryIO, digest: Any):
        self._target = target
        self._digest = digest

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        return self._target.write(data)


def write_fd(archive: zipfile.ZipFile, fd: int, info: os.stat_result, name: str, settings: ArchiveSettings, *,
             digest: Any = None) -> None:
    """Stream an opened file into *archive* (takes ownership of *fd*); *digest* (a hashlib object) sees what is
    written."""
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
            output = target if digest is None else _Hashing(target, digest)
            # A tenant can keep appending while its files are exported. Read
            # only the size observed when opening it, so the host finishes.
            tenantfs.copy_observed(source, output, info.st_size)  # type: ignore[arg-type]


def open_copy(root: Path, copy: DatabaseCopy, *, max_bytes: int | None = None) -> tuple[int, os.stat_result]:
    """Open the database copy *copy* in *root* as its tenant task reported it.

    The copy lies in the tenant directory, which the wiki (its plugins) can
    write meanwhile: once the task reported it, the wiki can extend it to a
    huge sparse file, which an archive would hold at its full length. A copy
    whose size is not the reported one is refused (``db_unsafe``), and so is
    one larger than *max_bytes* (``too_large``).
    """
    parent, _, name = copy.path.rpartition("/")
    dir_fd = tenantfs.open_dir(root, parent)
    try:
        fd = tenantfs.open_file(dir_fd, name)
    finally:
        os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if info.st_size != copy.size:
            raise RuntimeFailure("db_unsafe", f"the database copy changed once its sandbox made it: {info.st_size} "
                                              f"bytes, {copy.size} reported")
        if max_bytes is not None and info.st_size > max_bytes:
            raise RuntimeFailure("too_large", f"the database copy takes more than the {max_bytes // 1024 ** 2} MiB "
                                              f"its storage limit allows in an archive")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def write_copy(archive: zipfile.ZipFile, fd: int, info: os.stat_result, copy: DatabaseCopy, name: str,
               settings: ArchiveSettings) -> None:
    """Stream a copy :func:`open_copy` opened into *archive* (takes ownership of *fd*), then check that what was
    written has the SHA-256 the task reported (``db_unsafe`` otherwise; the member stays in *archive*)."""
    digest = hashlib.sha256()
    write_fd(archive, fd, info, name, settings, digest=digest)
    if digest.hexdigest() != copy.sha256:
        raise RuntimeFailure("db_unsafe", "the database copy changed once its sandbox made it: its content differs")


def _parents(member: str) -> Iterable[str]:
    parts = member.split("/")
    return ("/".join(parts[:end]) for end in range(1, len(parts)))


def _in_asset_folder(relative: str) -> bool:
    """Whether a tenant path lies below ``storage/<asset folder>/``, the folders exports flatten."""
    parts = relative.split("/", 2)
    return len(parts) == 3 and parts[0] == "storage" and parts[1] in tenantfs.ASSET_FOLDERS


def _shown(relative: str) -> str:
    """A tenant path as text a page can show: file names may hold bytes that are not UTF-8."""
    return os.fsencode(relative).decode("utf-8", "backslashreplace")


def member_cost(member: str, size: int) -> int:
    """What a member of *size* bytes adds to an archive: its data, its name twice and its headers."""
    return size + 2 * len(member.encode("utf-8")) + MEMBER_OVERHEAD


def add_tenant_tree(archive: zipfile.ZipFile, root: Path, prefix: str, settings: ArchiveSettings, *,
                    portable: bool, reserved: Iterable[str] = (), directories: Iterable[str] = (),
                    budget: int | None = None, links: list[str] | None = None) -> list[str]:
    """Add a tenant's files below *prefix*; returns the files it left out.

    *portable* (exports): the asset folders (:data:`tenantfs.ASSET_FOLDERS`)
    are flattened from ``storage/<name>`` to ``<name>``; anything else below
    ``storage/`` keeps its path. ``storage/`` is walked first, so where a
    top-level asset folder (a tenant can replace the ``<name>`` link with a
    folder of its own) holds a file ``storage/<name>`` holds too, the
    ``storage/`` copy is the one exported, as :meth:`duplicate` prefers it.
    Hidden folders and secrets are left out. Otherwise (platform backups)
    the tree is kept as it is, including the tenant's ``.secret_key`` so
    sessions survive a restore.

    *reserved* names the members the caller writes itself (the database
    copy, the manifest), *directories* the member paths a reader needs as
    folders (the layout a restore prepares). A tenant can create any file
    name, and one member that an import or restore refuses fails all of
    it, so a file is left out when its name an archive cannot hold or a
    reader would refuse (see :func:`storable_name`), when it would turn up
    twice or as both a file and a folder, a reserved member or directory
    included, or when it lies in a folder named like the database or its
    journals (a wiki that swapped its database for a folder once the copy
    was taken). An export also leaves out the files an import would take
    for the wiki's own: below a top-level ``assets/`` folder, or below
    ``storage/`` but outside its asset folders, such as
    ``storage/translations/xx.json`` or ``storage/favicons/custom_*`` (see
    :data:`ALIAS_PREFIXES`): next to the file it names it would turn up
    twice, or as both a file and a folder, once imported. Such paths no
    import maps (``storage/storage/...``) are kept, and ignored by imports.
    The tenant paths of the files left out are returned, as text a page can
    show; the caller decides what an archive without them is worth.

    A tenant's files can cost an archive far more than they cost the
    tenant's storage quota: a sparse file takes its full length, every hard
    link to a file a full copy. *links*, when given, collects the further
    links to a file already archived, which are left out: each file goes in
    once, whatever number of names it has. *budget* bounds what the files
    add to the archive (see :func:`member_cost`, each file at its apparent
    size): the walk stops before a file that would exceed it, with a
    ``too_large`` :class:`RuntimeFailure`.
    """
    refused: list[str] = []
    files = set(reserved)
    folders = set(directories)
    folders.update([parent for name in (*files, *folders) for parent in _parents(name)])
    archived: set[tuple[int, int]] = set()
    used = 0
    dir_fd = tenantfs.open_dir(root)
    walk = tenantfs.iter_files(dir_fd, skip_dirs=SKIPPED_DIRS | {tenantfs.EXCHANGE}, skip_hidden=portable,
                               first=("storage",) if portable else ())
    try:
        for relative, fd, info in walk:
            name = relative.rsplit("/", 1)[-1]
            skipped = name in SKIPPED_FILES and not (name == ".secret_key" and not portable)
            if skipped or name.endswith((".lock", "-wal", "-shm", "-journal")):
                os.close(fd)
                continue
            member = prefix + (relative[len("storage/"):] if portable and _in_asset_folder(relative) else relative)
            in_database = "/" in relative and relative.split("/", 1)[0] in tenantfs.DATABASE_FILES
            clash = member in files or member in folders or any(parent in files for parent in _parents(member))
            flat = member[len(prefix):]
            alias = portable and flat.startswith(ALIAS_PREFIXES) and destination(flat, "") is not None
            if in_database or clash or alias or not storable_name(member):
                os.close(fd)
                refused.append(_shown(relative))
                continue
            identity = (info.st_dev, info.st_ino)
            if links is not None and identity in archived:
                os.close(fd)
                links.append(_shown(relative))
                continue
            used += member_cost(member, info.st_size)
            if budget is not None and used > budget:
                os.close(fd)
                raise RuntimeFailure("too_large", f"its files take more than the {budget // 1024 ** 2} MiB its "
                                                  f"storage limit allows in an archive (a sparse file at its full "
                                                  f"size); stopped at {_shown(relative)}"[:300])
            if links is not None:
                archived.add(identity)
            files.add(member)
            folders.update(_parents(member))
            write_fd(archive, fd, info, member, settings)
    finally:
        # Closing the walk early closes the folder descriptors os.fwalk holds.
        walk.close()
        os.close(dir_fd)
    return refused


def refused_detail(refused: list[str]) -> str:
    """What to report about the files :func:`add_tenant_tree` left out."""
    return (f"{len(refused)} file(s) left out: an archive cannot hold their names, or a restore would refuse "
            f"them (not UTF-8, a ':' or '\\', too long, or clashing with another file, the database or a folder "
            f"the wiki needs): {', '.join(refused[:5])}")[:300]


def export(root: Path, snapshot: DatabaseCopy, destination_dir: Path, slug: str, settings: ArchiveSettings, *,
           budget: int | None = None) -> Path:
    """Write ``bananawiki-<slug>-<time>.zip``; *snapshot* is the database copy inside *root*.

    A copy that is no longer the one its task reported fails the export
    (see :func:`open_copy`). Tenant files no import would accept, or would
    take for the wiki's own, are left out, logged and listed in
    ``manifest.json`` (``omitted_files``, the first
    :data:`MAX_LISTED_FILES`, and ``omitted_file_count``): the rest of the
    wiki still moves.

    Exports of every wiki share one folder, and a sparse file or many hard
    links to one file cost the wiki's quota next to nothing: each file goes
    in once, further links are left out and listed like the files above,
    and with a *budget* (bytes, see :func:`add_tenant_tree`) the database
    copy and the files may each add at most that much. Over it, the export
    fails (``too_large``) and no archive is left.
    """
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
            fd, info = open_copy(root, snapshot, max_bytes=budget)
            write_copy(archive, fd, info, snapshot, RAW_DB, settings)
            # The manifest goes last, so that it can list what was left out:
            # its name is reserved, a tenant file cannot take its place. Nor
            # can a file named like an asset folder take the place of that
            # folder's files.
            links: list[str] = []
            refused = add_tenant_tree(archive, root, "", settings, portable=True, reserved=(MANIFEST, RAW_DB),
                                      directories=tenantfs.ASSET_FOLDERS, budget=budget, links=links)
            if refused:
                log.warning("The export of %s left out %d file(s) no import would accept or keep apart from the "
                            "wiki's own: %s", slug, len(refused), ", ".join(refused[:5])[:300])
            if links:
                log.warning("The export of %s left out %d further hard link(s) to files already in it: %s", slug,
                            len(links), ", ".join(links[:5])[:300])
            omitted = refused + links
            manifest.update(omitted_files=[item[:300] for item in omitted[:MAX_LISTED_FILES]],
                            omitted_file_count=len(omitted))
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


# ── Reading ───────────────────────────────────────────────────────────────────


def readable_member(info: zipfile.ZipInfo) -> bool:
    """Neither encrypted nor compressed with a method whose output zipfile cannot bound."""
    return not info.flag_bits & 1 and info.compress_type in READABLE_COMPRESSION


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
    if not name or unsafe_name(name):
        raise RuntimeFailure("archive_invalid", "unsafe member path")
    return PurePosixPath(name)


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
        manifest = safe_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error, OSError):
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
        if not readable_member(info):
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
        except (zipfile.BadZipFile, EOFError, ValueError, RuntimeError, zlib.error) as error:
            raise RuntimeFailure("archive_invalid", f"damaged archive: {error}") from None
        except OSError as error:
            if error.errno in (errno.ENOSPC, errno.EDQUOT):
                raise RuntimeFailure("no_space", "not enough free space to unpack this archive") from None
            raise RuntimeFailure("archive_invalid", "an archive member could not be extracted") from None
    return Unpacked(RAW_DB in chosen, f"{tenantfs.EXCHANGE}/{JSON_DUMP}" in chosen, skipped)
