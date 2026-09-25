"""Validate and restore a platform backup onto an empty hosting installation."""

import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile

from .. import config
from ._connection import get_hosting_db_context


def _check_database(path, platform=False):
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("A database in the backup is damaged.")
        if platform:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"accounts", "instances", "hosting_settings"}.issubset(tables):
                raise ValueError("The archive does not contain a hosting platform database.")


def _destination(base, relative):
    destination = base / relative
    if destination.is_symlink() or not destination.resolve().is_relative_to(base.resolve()):
        raise ValueError("A restore destination points outside the instance directory.")
    return destination


def _storage_aliases(staged_instances, instances_root):
    """Plan legacy storage links omitted by ZIP exports, without replacing files."""
    aliases = []
    if staged_instances.is_dir():
        for tenant in staged_instances.iterdir():
            for name in ("uploads", "attachments", "chat_attachments",
                         "kanban_attachments", "custom_page_files"):
                if (tenant / "storage" / name).is_dir() and not (tenant / name).exists():
                    destination = _destination(instances_root, Path(tenant.name) / name)
                    if not destination.exists():
                        aliases.append(destination)
    return aliases


def restore_hosting_from_backup_zips(zip_files):
    """Restore validated archives without writing through archive or local symlinks.

    Online restoration is restricted to an empty installation. Restoring over
    active tenants requires an offline migration, so running wikis cannot write
    to files while they are being replaced. The platform database is installed
    last through SQLite's backup API, which handles its existing WAL safely.
    """
    with get_hosting_db_context() as conn:
        if conn.execute("SELECT 1 FROM instances LIMIT 1").fetchone():
            raise ValueError("Restore onto a fresh hosting installation with no instances. Back up the current installation before migrating.")
    data_dir = Path(config.HOSTING_DATABASE_PATH).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    total_size = 0
    count = 0
    seen = set()
    instances_root = Path(config.INSTANCES_DIR)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=data_dir) as temporary:
        stage = Path(temporary)
        for archive in zip_files:
            for entry in archive.infolist():
                count += 1
                total_size += entry.file_size
                if count > config.HOSTING_IMPORT_MAX_MEMBERS or total_size > config.HOSTING_IMPORT_MAX_EXTRACTED_BYTES:
                    raise ValueError("The backup exceeds the configured extraction limits.")
                name = entry.filename
                parts = PurePosixPath(name).parts
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if "\\" in name or "\x00" in name or name.startswith("/") or any(p in {"..", "."} or ":" in p for p in parts):
                    raise ValueError("The backup contains an unsafe path.")
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError("The backup contains a symlink or special file.")
                if entry.is_dir():
                    continue
                if name in {"hosting.db", "hosting_db/hosting.db", "hosting/hosting.db"}:
                    relative = Path("hosting.db")
                elif name in {"secret_key", ".secret_key", "hosting_config/.secret_key", "hosting/.secret_key"}:
                    relative = Path("secret_key")
                elif name == "hosting/.backup_encryption_key":
                    relative = Path("backup_key")
                elif len(parts) >= 4 and parts[:2] == ("hosting", "instances"):
                    relative = Path(*parts[1:])
                    _destination(instances_root, Path(*parts[2:]))
                elif len(parts) >= 3 and parts[0] == "instances":
                    relative = Path(*parts)
                    _destination(instances_root, Path(*parts[1:]))
                else:
                    continue
                if str(relative) in seen:
                    raise ValueError("The backup contains duplicate files.")
                seen.add(str(relative))
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod(0o600)
                if target.name == "bananawiki.db":
                    _check_database(target)
        database = stage / "hosting.db"
        if not database.is_file():
            raise ValueError("The backup does not contain a hosting database.")
        _check_database(database, platform=True)
        secret = stage / "secret_key"
        if secret.is_file():
            value = secret.read_text().strip()
            if len(value) < 32 or len(value) > 4096:
                raise ValueError("The backup contains an invalid session key.")
        backup_key = stage / "backup_key"
        if backup_key.is_file() and backup_key.stat().st_size != 32:
            raise ValueError("The backup encryption key must contain exactly 32 bytes.")
        # All archive validation finishes before any destination is changed.
        staged_instances = stage / "instances"
        aliases = _storage_aliases(staged_instances, instances_root)
        if staged_instances.is_dir():
            for source in staged_instances.rglob("*"):
                if source.is_file():
                    destination = _destination(instances_root, source.relative_to(staged_instances))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, pending = tempfile.mkstemp(dir=destination.parent, prefix=".restore-")
                    try:
                        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                            shutil.copyfileobj(input_file, output, 1024 * 1024)
                        os.replace(pending, destination)
                    finally:
                        Path(pending).unlink(missing_ok=True)
        for alias in aliases:
            alias.symlink_to(Path("storage") / alias.name, target_is_directory=True)
        for source_key, key_path in ((secret, config._SECRET_KEY_PATH),
                                     (backup_key, config.HOSTING_BACKUP_KEY_PATH)):
            if not source_key.is_file():
                continue
            destination = Path(key_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, pending = tempfile.mkstemp(dir=destination.parent, prefix=".restore-key-")
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(source_key.read_bytes())
                os.replace(pending, destination)
            finally:
                Path(pending).unlink(missing_ok=True)
        with sqlite3.connect(database) as source, sqlite3.connect(config.HOSTING_DATABASE_PATH, timeout=20) as target:
            source.backup(target)
