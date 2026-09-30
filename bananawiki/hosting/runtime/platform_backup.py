"""Whole-platform backups: the encrypted export and the restore onto a fresh installation.

The export (1.4 ``hosting/backups.py``) holds a consistent copy of
``hosting.db``, the portal's secret key and every tenant directory, with each
tenant database copied inside its own container. It is written straight
into :class:`~.crypto.EncryptedOutput`, so no plaintext archive exists on
disk at any point (1.4 wrote one to ``/tmp`` first).

The restore (1.4 ``db.restore_hosting_from_backup_zips``) accepts the
encrypted ``.bwenc`` files and plain 1.4 ``.zip`` parts, checks every member
before changing anything, and only restores onto an installation that has no
wikis yet: replacing live tenants needs them offline, which a web request
cannot guarantee.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ..config import HostingConfig
from . import RuntimeFailure, archives, crypto, tenantfs

log = logging.getLogger("bananawiki.hosting.runtime.platform")

BACKUP_PREFIX = "bananawiki_hosting_backup_"
PLATFORM_TABLES = frozenset({"accounts", "instances", "hosting_settings"})
DATABASE_NAMES = frozenset({"hosting.db", "hosting_db/hosting.db", "hosting/hosting.db"})
SECRET_NAMES = frozenset({"secret_key", ".secret_key", "hosting_config/.secret_key", "hosting/.secret_key"})
BACKUP_KEY_NAME = "hosting/.backup_encryption_key"

Snapshot = Callable[[str], str | None]


def tenant_names(instances_dir: str) -> list[str]:
    base = Path(instances_dir)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir()
                  if tenantfs.DATA_DIR_NAME.fullmatch(entry.name) and tenantfs.is_tenant_dir(entry))


def _snapshot_hosting_db(cfg: HostingConfig, target: Path) -> None:
    source = sqlite3.connect(f"{Path(cfg.database_path).absolute().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        copy = sqlite3.connect(str(target))
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()


def export(cfg: HostingConfig, destination_dir: Path, key: bytes, snapshot: Snapshot,
           release: Callable[[str, str], None]) -> Path:
    """Write the encrypted backup into *destination_dir*.

    ``snapshot(tenant)`` returns the path (inside the tenant directory) of a
    fresh database copy, or None when the tenant has no usable database;
    ``release(tenant, path)`` removes it again.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    output = crypto.EncryptedOutput(Path(destination_dir) / f"{BACKUP_PREFIX}{stamp}.zip.bwenc", key)
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:  # type: ignore[arg-type]
            database = Path(destination_dir) / ".hosting-snapshot.db"
            try:
                _snapshot_hosting_db(cfg, database)
                archive.write(database, "hosting.db")
            finally:
                database.unlink(missing_ok=True)
            archive.writestr("secret_key", cfg.secret_key)
            for tenant in tenant_names(cfg.instances_dir):
                _add_tenant(archive, cfg, tenant, snapshot, release)
        return output.finish()
    except BaseException:
        output.abort()
        raise


def _add_tenant(archive: zipfile.ZipFile, cfg: HostingConfig, tenant: str, snapshot: Snapshot,
                release: Callable[[str, str], None]) -> None:
    root = tenantfs.tenant_path(cfg.instances_dir, tenant)
    copy = snapshot(tenant)
    try:
        if copy:
            parent, _, name = copy.rpartition("/")
            dir_fd = tenantfs.open_dir(root, parent)
            try:
                fd = tenantfs.open_file(dir_fd, name)
            finally:
                os.close(dir_fd)
            archives.write_fd(archive, fd, os.fstat(fd), f"instances/{tenant}/bananawiki.db", cfg.archives)
        archives.add_tenant_tree(archive, root, f"instances/{tenant}/", cfg.archives, portable=False)
    finally:
        if copy:
            release(tenant, copy)


# ── Restore ───────────────────────────────────────────────────────────────────


def _staged_name(name: str) -> PurePosixPath | None:
    """Where a backup member goes in the staging tree, or None to ignore it."""
    parts = PurePosixPath(name).parts
    if "\\" in name or "\x00" in name or name.startswith("/") or any(p in ("..", ".") or ":" in p for p in parts):
        raise RuntimeFailure("archive_invalid", "the backup contains an unsafe path")
    if name in DATABASE_NAMES:
        return PurePosixPath("hosting.db")
    if name in SECRET_NAMES:
        return PurePosixPath("secret_key")
    if name == BACKUP_KEY_NAME:
        return PurePosixPath("backup_key")
    if len(parts) >= 4 and parts[:2] == ("hosting", "instances"):
        parts = parts[1:]
    if len(parts) >= 3 and parts[0] == "instances":
        tenantfs.tenant_path("/", parts[1])
        return PurePosixPath(*parts)
    return None


def _check_database(path: Path) -> None:
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeFailure("archive_invalid", "the hosting database in the backup is damaged")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not tables >= PLATFORM_TABLES:
            raise RuntimeFailure("archive_invalid", "the backup holds no hosting platform database")
    except sqlite3.DatabaseError:
        raise RuntimeFailure("archive_invalid", "the hosting database in the backup is not readable") from None
    finally:
        conn.close()


def _installation_empty(cfg: HostingConfig) -> bool:
    conn = sqlite3.connect(f"{Path(cfg.database_path).absolute().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute("SELECT 1 FROM instances LIMIT 1").fetchone() is None
    finally:
        conn.close()


def _stage(parts: Sequence[Path], stage: Path, cfg: HostingConfig, key: Callable[[], bytes]) -> None:
    count = total = 0
    seen: set[str] = set()
    for index, part in enumerate(parts):
        path = Path(part)
        if crypto.is_encrypted(path):
            path = crypto.decrypt_file(path, stage / f".part-{index}.zip", key())
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            raise RuntimeFailure("archive_invalid", "a backup part is not a ZIP archive") from None
        with archive:
            for entry in archive.infolist():
                count += 1
                total += entry.file_size
                if count > cfg.archives.import_max_members or total > cfg.archives.import_max_extracted_bytes:
                    raise RuntimeFailure("too_large", "the backup exceeds the configured extraction limits")
                if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise RuntimeFailure("archive_invalid", "the backup contains links or special files")
                relative = None if entry.is_dir() else _staged_name(entry.filename)
                if relative is None:
                    continue
                if str(relative) in seen:
                    raise RuntimeFailure("archive_invalid", "the backup contains duplicate files")
                seen.add(str(relative))
                target = stage / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(entry) as source, open(target, "xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    shutil.copyfileobj(source, output, 1024 * 1024)
        if path != Path(part):
            path.unlink()


def _install_file(source: Path, destination: Path, mode: int = 0o600) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(dir=destination.parent, prefix=".restore-")
    try:
        with os.fdopen(descriptor, "wb") as output, open(source, "rb") as handle:
            shutil.copyfileobj(handle, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(pending, mode)
        os.replace(pending, destination)
    finally:
        Path(pending).unlink(missing_ok=True)


def _refuse_links(root: Path, target: Path) -> None:
    """Never write through a link left in an existing tenant directory."""
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeFailure("invalid", f"a restore target inside {root.name} is a link")


def _install_tenants(staged: Path, cfg: HostingConfig) -> None:
    base = Path(cfg.instances_dir)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    real_base = base.resolve()
    for tenant_dir in sorted(staged.iterdir()):
        root = tenantfs.tenant_path(base, tenant_dir.name)
        if root.is_symlink() or (root.exists() and root.resolve().parent != real_base):
            raise RuntimeFailure("invalid", f"{root} is a link")
        root.mkdir(mode=0o700, exist_ok=True)
        for source in sorted(tenant_dir.rglob("*")):
            if not source.is_file():
                continue
            target = root / source.relative_to(tenant_dir)
            _refuse_links(root, target)
            _install_file(source, target)
        tenantfs.ensure_layout(root)


def restore(cfg: HostingConfig, parts: Sequence[Path], key: Callable[[], bytes], secret_key_path: str) -> None:
    """Validate every part, then install tenants, keys and finally ``hosting.db``."""
    if not _installation_empty(cfg):
        raise RuntimeFailure("data_exists", "restore onto a fresh installation without wikis")
    data_dir = Path(cfg.database_path).parent
    with tempfile.TemporaryDirectory(prefix="restore-", dir=data_dir) as temporary:
        stage = Path(temporary)
        _stage(parts, stage, cfg, key)
        database = stage / "hosting.db"
        if not database.is_file():
            raise RuntimeFailure("archive_invalid", "the backup holds no hosting database")
        _check_database(database)
        secret = stage / "secret_key"
        if secret.is_file() and not 32 <= len(secret.read_bytes().strip()) <= 4096:
            raise RuntimeFailure("archive_invalid", "the backup holds an invalid session key")
        backup_key = stage / "backup_key"
        if backup_key.is_file() and backup_key.stat().st_size != 32:
            raise RuntimeFailure("archive_invalid", "the backup encryption key must be 32 bytes")
        if (stage / "instances").is_dir():
            _install_tenants(stage / "instances", cfg)
        if secret.is_file():
            _install_file(secret, Path(secret_key_path))
        if backup_key.is_file():
            _install_file(backup_key, Path(cfg.backup_key_path))
        source = sqlite3.connect(str(database))
        try:
            target = sqlite3.connect(cfg.database_path, timeout=20)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    log.warning("The hosting platform was restored from a backup; restart the portal.")
