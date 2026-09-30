"""Taking over a BananaWiki 1.4 installation in place.

Two things happen on the first start of 1.6:

* **Backup.** Before the schema is migrated, an online copy of the database is
  written to ``<instance>/backups/pre-upgrade-v<old>-<time>.db``.
* **Data folders.** 1.4 kept some user files inside the source tree by default
  (``app/static/uploads``, custom favicons in ``app/static/favicons``, uploaded
  languages in ``translations/``). When the matching ``BW_*`` variable is not
  set, those files are moved to the instance directory, where 1.6 keeps all
  persistent data. Files are moved, never overwritten.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from ..core.sqlite import Database, schema_version
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


def backup_before_upgrade(cfg: Config, database: Database, conn: sqlite3.Connection) -> Path | None:
    """Copy the database aside when it is about to be migrated."""
    try:
        _app_id, version = schema_version(conn)
    except sqlite3.DatabaseError:
        return None
    if version == 0 or version >= database.latest_version:
        return None
    backups = Path(cfg.instance_dir) / "backups"
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = backups / f"pre-upgrade-v{version}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    destination = sqlite3.connect(str(target))
    try:
        conn.backup(destination)
    finally:
        destination.close()
    if os.name == "posix":
        os.chmod(target, 0o600)
    log.warning("Database schema %s will be upgraded to %s; a copy was saved to %s.",
                version, database.latest_version, target)
    return target
