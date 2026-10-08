"""Taking over a BananaWiki 1.4 installation in place.

Two things happen on the first start of 1.6:

* **Backup.** Before the schema is migrated, an online copy of the database is
  written to ``<instance>/backups/pre-upgrade-v<old>-<time>.db``.
* **Data folders.** 1.4 kept some user files inside the source tree by default
  (``app/static/uploads``, custom favicons in ``app/static/favicons``, uploaded
  languages in ``translations/``). When the matching ``BW_*`` variable is not
  set, those files are moved to the instance directory, where 1.6 keeps all
  persistent data. Files are moved, never overwritten.

Under Gunicorn both happen in the master process before any worker starts
(see :mod:`bananawiki.ops.gunicorn_conf`): a worker has a timeout, the
master has none.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from ..core.sqlite import Database, _fsync_directory, schema_version, tuples
from .config import REPO_ROOT, Config

log = logging.getLogger("bananawiki.takeover")

_BUNDLED_LANGUAGES = {"en.json", "it.json"}


def _legacy_locations(cfg: Config) -> list[tuple[str, Path, Path, str]]:
    """(env var, legacy dir, new dir, glob) for each relocatable folder."""
    legacy_instance = REPO_ROOT / "instance"
    return [
        ("BW_UPLOAD_FOLDER", REPO_ROOT / "app" / "static" / "uploads", Path(cfg.folders.uploads), "**/*"),
        ("BW_FAVICON_UPLOAD_FOLDER", REPO_ROOT / "app" / "static" / "favicons", Path(cfg.folders.favicons),
         "custom_*"),
        ("BW_ATTACHMENT_FOLDER", legacy_instance / "attachments", Path(cfg.folders.attachments), "**/*"),
        ("BW_CHAT_ATTACHMENT_FOLDER", legacy_instance / "chat_attachments", Path(cfg.folders.chat_attachments),
         "**/*"),
        ("BW_KANBAN_ATTACHMENT_FOLDER", legacy_instance / "kanban_attachments",
         Path(cfg.folders.kanban_attachments), "**/*"),
        ("BW_CUSTOM_PAGE_FILES_FOLDER", legacy_instance / "custom_page_files",
         Path(cfg.folders.custom_page_files), "**/*"),
    ]


def relocate_legacy_folders(cfg: Config) -> None:
    if cfg.testing:
        return
    for env_name, legacy, target, pattern in _legacy_locations(cfg):
        if os.environ.get(env_name) or not legacy.is_dir():
            continue
        try:
            if legacy.resolve() == target.resolve():
                continue
        except OSError:
            continue
        _move_tree(legacy, target, pattern)
    languages = REPO_ROOT / "translations"
    if languages.is_dir():
        custom = Path(cfg.instance_dir) / "translations"
        for path in languages.glob("*.json"):
            if path.name not in _BUNDLED_LANGUAGES:
                _move_file(path, custom / path.name)


def _move_tree(source: Path, target: Path, pattern: str) -> None:
    moved = 0
    for path in sorted(source.glob(pattern)):
        if path.is_file() and not path.is_symlink() and not path.name.startswith(".git"):
            if _move_file(path, target / path.relative_to(source)):
                moved += 1
    if moved:
        log.warning("Moved %d files from %s to %s (BananaWiki 1.6 keeps data in the instance directory).",
                    moved, source, target)


def _move_file(source: Path, destination: Path) -> bool:
    if destination.exists():
        return False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return True
    except OSError as error:
        log.error("Could not move %s to %s: %s. Set the matching BW_* folder variable to keep using the "
                  "old location.", source, destination, error)
        return False


def prepare_storage(cfg: Config) -> Database:
    """Move the 1.4 data folders, then create or upgrade the database (copying it aside first)."""
    from .app import open_database

    relocate_legacy_folders(cfg)
    database = open_database(cfg)
    database.initialize(before=lambda conn: backup_before_upgrade(cfg, database, conn))
    return database


def backup_before_upgrade(cfg: Config, database: Database, conn: sqlite3.Connection) -> Path | None:
    """Copy the database aside when it is about to be migrated; return the copy.

    The copy is written to a temporary file and then renamed, so a
    ``pre-upgrade-*.db`` is always complete. An upgrade that failed or was
    interrupted is tried again at the next start: when the database has not
    changed since the newest copy of the same version was written, that copy
    is kept instead of writing another one each time.
    """
    try:
        _app_id, version = schema_version(conn)
    except sqlite3.DatabaseError:
        return None
    if version == 0 or version >= database.latest_version:
        return None
    backups = Path(cfg.instance_dir) / "backups"
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    for leftover in backups.glob(".pre-upgrade-*"):  # an interrupted copy: the schema lock is ours
        leftover.unlink(missing_ok=True)
    unchanged = _unchanged_copy(conn, database.path, backups, version)
    if unchanged is not None:
        log.warning("Database schema %s will be upgraded to %s; the copy %s, saved before an earlier attempt, "
                    "still matches it.", version, database.latest_version, unchanged)
        return unchanged
    target = backups / f"pre-upgrade-v{version}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    fd, temporary = tempfile.mkstemp(prefix=f".pre-upgrade-v{version}-", suffix=".db", dir=backups)
    os.close(fd)
    try:
        destination = sqlite3.connect(temporary)
        try:
            conn.backup(destination)
        finally:
            destination.close()
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        with open(temporary, "rb+") as copy:
            os.fsync(copy.fileno())
        os.replace(temporary, target)
        _fsync_directory(backups)
    finally:
        Path(temporary).unlink(missing_ok=True)
    log.warning("Database schema %s will be upgraded to %s; a copy was saved to %s.",
                version, database.latest_version, target)
    return target


def _unchanged_copy(conn: sqlite3.Connection, path: Path, backups: Path, version: int) -> Path | None:
    """The newest ``pre-upgrade-v<version>-*.db`` the database has not changed since, if any.

    After a complete WAL checkpoint every committed change is in the database
    file. An upgrade that was rolled back, or killed, never wrote it. So the
    database is unchanged when its file is older than the copy and as large.
    """
    busy = tuples(conn, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0]
    if busy:
        return None  # another process has the database open and may write to it
    copies = [item for item in backups.glob(f"pre-upgrade-v{version}-*.db")
              if item.is_file() and not item.is_symlink() and _complete_copy(item, version)]
    if not copies:
        return None
    newest = max(copies, key=lambda item: item.stat().st_mtime_ns)
    size = tuples(conn, "PRAGMA page_count")[0][0] * tuples(conn, "PRAGMA page_size")[0][0]
    current, copy = path.stat(), newest.stat()
    if current.st_mtime_ns < copy.st_mtime_ns and current.st_size == size == copy.st_size:
        return newest
    return None


def _complete_copy(path: Path, version: int) -> bool:
    """Whether *path* is a whole database of schema *version*, read from its header without opening it.

    Earlier releases wrote the copy under its final name: one interrupted
    meanwhile has a journal beside it, and opening it would roll it back.
    """
    if any(os.path.lexists(f"{path}{suffix}") for suffix in ("-journal", "-wal")):
        return False
    try:
        with open(path, "rb") as handle:
            header = handle.read(100)
    except OSError:  # for example written by root with ``bananawiki migrate``: not one this process can reuse
        return False
    return header[:16] == b"SQLite format 3\x00" and int.from_bytes(header[60:64], "big") == version
