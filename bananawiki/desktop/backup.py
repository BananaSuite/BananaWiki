"""Back up a data folder to one ZIP file and restore it.

A backup holds a consistent copy of the database made with SQLite's online
backup API (so it can be taken while the wiki is running), the secret key,
custom languages and every uploaded file. The format is the one the 1.4
launcher wrote ("Export All"), so its archives restore here and the other
way round::

    bananawiki.db                  the database
    instance/.secret_key           sessions and the setup code
    instance/translations/*.json   uploaded languages
    uploads/... attachments/... (every asset folder)
    manifest.json                  informational

A restore needs the server stopped. It validates and unpacks the whole
archive next to the data first, then swaps the folders in; the replaced data
is kept under ``previous/<time>/``. A journal makes the swap all-or-nothing:
if the computer stops half-way, the next start puts the old data back.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from . import DesktopError
from .datafolder import ASSET_DIRECTORIES, DATABASE_NAME, RESTORE_JOURNAL, DataFolder, write_private

ARCHIVE_DATABASE = DATABASE_NAME
ARCHIVE_KEY = "instance/.secret_key"
ARCHIVE_TRANSLATIONS = "instance/translations"
METADATA_FILES = frozenset({"manifest.json", "site_export.json"})
RESTORED = ("instance", *ASSET_DIRECTORIES)
REQUIRED_TABLES = frozenset({"users", "pages", "site_settings"})
MAX_FILES = 100_000
MAX_BYTES = 128 * 1024 ** 3
SPACE_MARGIN = 64 * 1024 ** 2
CHUNK = 1024 * 1024
_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                             *(f"LPT{i}" for i in range(1, 10))})
_STAGED_NAME = re.compile(r"\.restore-[A-Za-z0-9_]+")
_PREVIOUS_NAME = re.compile(r"\d{8}-\d{6}(?:-\d+)?")


# ── Database checks ──────────────────────────────────────────────────────────


def check_database(path: Path) -> None:
    """Refuse anything but an intact BananaWiki database this version can open."""
    from ..wiki.migrations import APPLICATION_ID, LATEST

    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise DesktopError("archive_no_database")
    try:
        conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DesktopError("archive_damaged")
            if conn.execute("PRAGMA application_id").fetchone()[0] not in (0, APPLICATION_ID):
                raise DesktopError("archive_foreign")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not tables >= REQUIRED_TABLES:
                raise DesktopError("archive_foreign")
            if conn.execute("PRAGMA user_version").fetchone()[0] > LATEST:
                raise DesktopError("archive_too_new")
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        raise DesktopError("archive_damaged", str(error)) from error


def snapshot_database(source: Path, destination: Path) -> Path:
    """A consistent copy of a live database (SQLite online backup)."""
    src = sqlite3.connect(f"{source.absolute().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(destination))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if os.name == "posix":
        os.chmod(destination, 0o600)
    return destination


# ── Archive validation ───────────────────────────────────────────────────────


def _safe_parts(name: str) -> list[str]:
    parts = name.rstrip("/").split("/")
    if (not name or name.startswith("/") or "\\" in name or "\0" in name
            or any(part in {"", ".", ".."} or ":" in part or part.endswith((".", " ")) for part in parts)):
        raise DesktopError("archive_unsafe", name)
    if any(part.split(".")[0].upper() in _RESERVED_NAMES for part in parts):
        raise DesktopError("archive_unsafe", name)
    return parts


def _target(name: str, parts: list[str], size: int) -> str | None:
    """Where an archive member goes inside the data folder (None: skipped metadata)."""
    if name == ARCHIVE_DATABASE:
        return f"instance/{DATABASE_NAME}"
    if name == ARCHIVE_KEY:
        if size > 4096:
            raise DesktopError("archive_unsafe", name)
        return name
    if name.startswith(ARCHIVE_TRANSLATIONS + "/") and len(parts) == 3 and name.endswith(".json"):
        return name
    if parts[0] in ASSET_DIRECTORIES and len(parts) > 1:
        return name
    if name in METADATA_FILES:
        return None
    raise DesktopError("archive_unexpected", name)


def archive_entries(archive: zipfile.ZipFile) -> tuple[list[tuple[zipfile.ZipInfo, str]], int]:
    """Validate every member; return (member, target path) pairs and the unpacked size."""
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise DesktopError("archive_too_large")
    seen: set[str] = set()
    accepted: list[tuple[zipfile.ZipInfo, str]] = []
    total = 0
    for info in infos:
        parts = _safe_parts(info.filename)
        kind = (info.external_attr >> 16) & 0o170000
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 0x1:
            raise DesktopError("archive_unsafe", info.filename)
        key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if key in seen:
            raise DesktopError("archive_unsafe", info.filename)
        seen.add(key)
        total += info.file_size
        if total > MAX_BYTES:
            raise DesktopError("archive_too_large")
        if info.is_dir():
            continue
        target = _target(info.filename, parts, info.file_size)
        if target is not None:
            accepted.append((info, target))
    if not any(target == f"instance/{DATABASE_NAME}" for _info, target in accepted):
        raise DesktopError("archive_no_database")
    return accepted, total


# ── Backup ───────────────────────────────────────────────────────────────────


def _regular_files(folder: DataFolder, directory: str) -> Iterator[tuple[Path, str]]:
    base = folder.root / directory
    if not base.is_dir() or base.is_symlink():
        return
    for current, directories, files in os.walk(base, followlinks=False):
        for name in sorted((*directories, *files)):
            path = Path(current) / name
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise DesktopError("unsafe_file", str(path))
            if path.is_file():
                yield path, path.relative_to(folder.root).as_posix()


def _backup_inputs(folder: DataFolder, database_copy: Path) -> list[tuple[Path, str]]:
    inputs = [(database_copy, ARCHIVE_DATABASE)]
    key = folder.secret_key_file
    if key.is_file() and not key.is_symlink():
        inputs.append((key, ARCHIVE_KEY))
    languages = folder.instance / "translations"
    if languages.is_dir() and not languages.is_symlink():
        for path in sorted(languages.glob("*.json")):
            if path.is_file() and not path.is_symlink():
                inputs.append((path, f"{ARCHIVE_TRANSLATIONS}/{path.name}"))
    for directory in ASSET_DIRECTORIES:
        inputs.extend(_regular_files(folder, directory))
    return inputs


def _publish(completed: Path, destination: Path) -> None:
    """Move the finished file into place without ever replacing an existing file."""
    if os.name == "nt":
        os.rename(completed, destination)
        return
    os.link(completed, destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def create_backup(folder: DataFolder, destination: str | os.PathLike[str]) -> Path:
    """Write a ZIP backup of *folder* to *destination* (a new file outside the folder).

    Safe while the wiki is running: the database is copied with SQLite's
    online backup, which sees one consistent state.
    """
    destination = Path(destination).expanduser().absolute()
    folder.check()
    if destination.is_relative_to(folder.root):
        raise DesktopError("backup_inside_folder", str(destination))
    if destination.exists() or destination.is_symlink():
        raise DesktopError("backup_exists", str(destination))
    if not folder.database.is_file():
        raise DesktopError("nothing_to_back_up")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bananawiki-backup-", dir=destination.parent) as scratch:
        work = Path(scratch)
        database_copy = snapshot_database(folder.database, work / DATABASE_NAME)
        check_database(database_copy)
        inputs = _backup_inputs(folder, database_copy)
        size = sum(path.stat().st_size for path, _name in inputs)
        if len(inputs) > MAX_FILES or size > MAX_BYTES:
            raise DesktopError("archive_too_large")
        if shutil.disk_usage(work).free < size + SPACE_MARGIN:
            raise DesktopError("disk_full")
        archive_path = work / "backup.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path, name in inputs:
                archive.write(path, name)
            archive.writestr("manifest.json", json.dumps({
                "format_version": 1, "product": "BananaWiki", "source": "desktop",
                "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "has_raw_db": True, "includes_application_key": any(n == ARCHIVE_KEY for _p, n in inputs),
            }))
        if os.name == "posix":
            os.chmod(archive_path, 0o600)
        with zipfile.ZipFile(archive_path) as archive:
            archive_entries(archive)
            if archive.testzip() is not None:
                raise DesktopError("archive_damaged")
        # Windows only flushes a handle opened for writing.
        with archive_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        _publish(archive_path, destination)
    return destination


# ── Restore ──────────────────────────────────────────────────────────────────


def _extract(archive: zipfile.ZipFile, entries: list[tuple[zipfile.ZipInfo, str]], staged: Path) -> None:
    for info, target in entries:
        path = staged / target
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        copied = 0
        with archive.open(info) as source, path.open("xb") as output:
            if os.name == "posix":
                os.chmod(path, 0o600)
            while chunk := source.read(CHUNK):
                copied += len(chunk)
                if copied > info.file_size:
                    raise DesktopError("archive_damaged", info.filename)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if copied != info.file_size:
            raise DesktopError("archive_damaged", info.filename)


def _journal_path(folder: DataFolder) -> Path:
    return folder.root / RESTORE_JOURNAL


def _new_previous(folder: DataFolder) -> Path:
    folder.previous.mkdir(mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate, n = folder.previous / stamp, 1
    while candidate.exists():
        candidate, n = folder.previous / f"{stamp}-{n}", n + 1
    candidate.mkdir(mode=0o700)
    return candidate


def _commit(folder: DataFolder, staged: Path, *, discard_previous: bool = False) -> Path:
    """Swap the staged folders in; the replaced ones go to ``previous/``."""
    previous = _new_previous(folder)
    record = {"staged": staged.name, "previous": previous.name, "phase": "prepared"}
    journal = _journal_path(folder)
    write_private(journal, json.dumps(record).encode())
    try:
        for name in RESTORED:
            current, incoming = folder.root / name, staged / name
            if current.exists():
                os.replace(current, previous / name)
            if incoming.exists():
                os.replace(incoming, current)
            else:
                current.mkdir(mode=0o700)
        record["phase"] = "committed"
        write_private(journal, json.dumps(record).encode())
    except BaseException:
        recover(folder)
        raise
    recover(folder)
    if discard_previous:
        shutil.rmtree(previous)
        with contextlib.suppress(OSError):
            folder.previous.rmdir()
    return previous


def recover(folder: DataFolder) -> None:
    """Finish or undo a restore that was interrupted (call with the folder locked)."""
    journal = _journal_path(folder)
    if not journal.exists() and not journal.is_symlink():
        return
    if journal.is_symlink() or not journal.is_file() or journal.stat().st_size > 4096:
        raise DesktopError("restore_journal_invalid", str(journal))
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
    except ValueError as error:
        raise DesktopError("restore_journal_invalid", str(journal)) from error
    staged_name = str(record.get("staged", "")) if isinstance(record, dict) else ""
    previous_name = str(record.get("previous", "")) if isinstance(record, dict) else ""
    phase = record.get("phase") if isinstance(record, dict) else None
    if (not _STAGED_NAME.fullmatch(staged_name) or not _PREVIOUS_NAME.fullmatch(previous_name)
            or phase not in {"prepared", "committed"}):
        raise DesktopError("restore_journal_invalid", str(journal))
    staged, previous = folder.root / staged_name, folder.previous / previous_name
    if phase == "prepared" and previous.is_dir():
        for name in RESTORED:
            original, current = previous / name, folder.root / name
            if not original.exists():
                continue  # never moved: the folder in place is still the original
            if current.exists():
                shutil.rmtree(current)
            os.replace(original, current)
        with contextlib.suppress(OSError):
            previous.rmdir()
    if staged.exists():
        shutil.rmtree(staged)
    journal.unlink()


def _staging(folder: DataFolder) -> Path:
    return Path(tempfile.mkdtemp(prefix=".restore-", dir=folder.root))


def restore_backup(folder: DataFolder, archive_path: str | os.PathLike[str]) -> Path:
    """Replace the folder's data with a backup; returns where the old data was kept.

    The server must be stopped (the folder lock is required). When the
    archive has no secret key the current one is kept, so existing sessions
    and the setup code stay valid.
    """
    archive_path = Path(archive_path).expanduser().absolute()
    with folder.lock():
        recover(folder)
        folder.prepare()
        old_key = folder.secret_key_file.read_bytes() if folder.secret_key_file.is_file() else None
        staged = _staging(folder)
        try:
            try:
                archive = zipfile.ZipFile(archive_path)
            except (OSError, zipfile.BadZipFile) as error:
                raise DesktopError("archive_invalid", str(error)) from error
            with archive:
                entries, total = archive_entries(archive)
                if shutil.disk_usage(staged).free < total + SPACE_MARGIN:
                    raise DesktopError("disk_full")
                try:
                    _extract(archive, entries, staged)
                except (zipfile.BadZipFile, zlib.error, EOFError) as error:
                    raise DesktopError("archive_damaged", str(error)) from error
            check_database(staged / "instance" / DATABASE_NAME)
            key_path = staged / ARCHIVE_KEY
            if not key_path.exists() and old_key:
                write_private(key_path, old_key)
            return _commit(folder, staged)
        finally:
            if staged.exists() and not _journal_path(folder).exists():
                shutil.rmtree(staged)


def reset_data(folder: DataFolder) -> None:
    """Delete every page, account and file, keeping only the secret key.

    Used by "Delete all data" after the user confirmed it; the server must be
    stopped.
    """
    with folder.lock():
        recover(folder)
        folder.prepare()
        old_key = folder.secret_key_file.read_bytes() if folder.secret_key_file.is_file() else None
        staged = _staging(folder)
        try:
            if old_key:
                (staged / "instance").mkdir(mode=0o700)
                write_private(staged / ARCHIVE_KEY, old_key)
            _commit(folder, staged, discard_previous=True)
        finally:
            if staged.exists() and not _journal_path(folder).exists():
                shutil.rmtree(staged)
