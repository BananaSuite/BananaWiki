"""Consistent local platform backups shared by downloads and cloud storage."""

import logging
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from . import config
from .db._connection import create_consistent_db_copy
from .instance_archives import _zip_date_time
from .instance_environment import iter_tenant_files, open_tenant_dir, snapshot_tenant_db

logger = logging.getLogger(__name__)
_VOLATILE = {".pid", ".starting", ".port", "gunicorn.pid", ".maintenance.lock"}
_SKIPPED_DIRS = frozenset({"__pycache__", "tts"})


def notify_change(change_type, description=""):
    logger.info("%s: %s", change_type, description)


def _write_tenant_fd(archive, fd, st, name):
    """Stream an already opened tenant file into *archive*. Takes ownership of *fd*."""
    with os.fdopen(fd, "rb") as source:
        info = zipfile.ZipInfo(name, date_time=_zip_date_time(st.st_mtime))
        info.external_attr = (stat.S_IMODE(st.st_mode) | stat.S_IFREG) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w", force_zip64=True) as output:
            shutil.copyfileobj(source, output, 1024 * 1024)


def _add_tenant(archive, temporary, tenant_dir, prefix):
    """Add one tenant's data directory to *archive* under *prefix*.

    Running tenants can rewrite everything in their directory, so files are
    walked and opened with the no-follow helpers from instance_environment:
    links and special files are skipped, and each ``bananawiki.db`` is copied
    through snapshot_tenant_db, which checks the file SQLite really opened.
    """
    root_fd = open_tenant_dir(tenant_dir, "")
    try:
        for relative, fd, st in iter_tenant_files(root_fd, skip_dirs=_SKIPPED_DIRS):
            name = relative.rsplit("/", 1)[-1]
            if name in _VOLATILE or name.endswith(("-wal", "-shm", "-journal", ".sock", ".lock")):
                os.close(fd)
                continue
            if name == "bananawiki.db":
                os.close(fd)
                snapshot = os.path.join(temporary, "snapshot.db")
                Path(snapshot).unlink(missing_ok=True)
                try:
                    snapshot_tenant_db(tenant_dir, os.path.join(tenant_dir, relative), snapshot)
                except ValueError:
                    # Swapped for a link or a special file after the walk
                    # opened it. Leave it out rather than let one tenant
                    # stop every platform backup.
                    logger.warning("Left %s/%s out of the backup: not a regular file", prefix, relative)
                    continue
                archive.write(snapshot, f"{prefix}/{relative}")
                Path(snapshot).unlink()
            else:
                _write_tenant_fd(archive, fd, st, f"{prefix}/{relative}")
    finally:
        os.close(root_fd)


def write_backup(destination):
    """Write a ZIP containing database snapshots, instance data, and session keys.

    Private website content and the backup decryption key are managed separately.
    Symlinks and transient process files are excluded. A failed snapshot aborts
    the backup instead of silently including a live SQLite file.
    """
    with tempfile.TemporaryDirectory(prefix="bwh-snapshots-") as temporary:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
            database = Path(config.HOSTING_DATABASE_PATH)
            if not database.is_file():
                raise FileNotFoundError("The hosting database does not exist.")
            snapshot = Path(temporary) / "hosting.db"
            create_consistent_db_copy(str(database), str(snapshot))
            archive.write(snapshot, "hosting.db")
            snapshot.unlink()
            # The configured value also covers deployments that use an environment key.
            archive.writestr("secret_key", config.HOSTING_SECRET_KEY)
            base = Path(config.INSTANCES_DIR)
            if base.is_dir():
                for tenant in sorted(os.listdir(base)):
                    tenant_dir = base / tenant
                    if tenant in _SKIPPED_DIRS or tenant_dir.is_symlink() or not tenant_dir.is_dir():
                        continue
                    _add_tenant(archive, temporary, str(tenant_dir), "instances/" + tenant)


def build_full_backup(reason="manual"):
    """Return one ZIP path; the caller owns its cleanup."""
    fd, name = tempfile.mkstemp(prefix="bananawiki-hosting-", suffix=".zip")
    os.close(fd)
    try:
        write_backup(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    logger.info("Created platform backup (%s)", reason)
    return [name]
