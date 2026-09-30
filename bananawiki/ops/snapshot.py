"""Frozen copies of ``data/``, ``site/`` and the configuration, taken mostly online.

1.4 stopped every service and then tarred all data, so an update's downtime
grew with the size of the installation (every tenant in hosting mode). A
snapshot is prepared while the site is still serving:

* SQLite databases are copied with SQLite's online backup API;
* append-only or rewritten-in-place files (logs, locks, small files) are copied;
* everything else (uploads, attachments: written once, replaced atomically,
  never modified in place) is hard-linked, which costs no space or time.

After the services stop, :meth:`Snapshot.refresh` re-copies only databases
and files whose inode, size or modification time changed, so the downtime is
proportional to the database size plus what changed meanwhile. The portable
package is then written from the snapshot after the services are back up.

A snapshot directory has the same layout as an extracted package
(``data/``, ``site/``, ``config/``), so the same restore code handles both.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .files import MAINTENANCE_MARKER, read_json, regular_files, write_json

CONFIG_FILES = ("installation.json", "source.json", "app.env", "repo.token", "repo.key", "repo.known_hosts",
                "updates.json")
_DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_SIDECARS = ("-wal", "-shm", "-journal")
_MUTABLE_SUFFIXES = {".log", ".jsonl", ".lock", ".pid", ".tmp"}
_SMALL = 64 * 1024


def is_database(path: Path) -> bool:
    return path.suffix in _DATABASE_SUFFIXES


def is_sidecar(path: Path) -> bool:
    return any(path.name.endswith(suffix) and is_database(Path(path.name[: -len(suffix)])) for suffix in _SIDECARS)


def _mutable(path: Path, relative: str) -> bool:
    parts = relative.split("/")
    return (path.suffix in _MUTABLE_SUFFIXES or ".log." in path.name or "logs" in parts[:-1]
            or path.stat().st_size < _SMALL)


def _fix_sidecars(database: Path) -> None:
    """Opening a WAL database may create -wal/-shm files; give them back to the database's owner."""
    info = database.stat()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.is_file() and not sidecar.is_symlink() and (sidecar.stat().st_uid, sidecar.stat().st_gid) != (
                info.st_uid, info.st_gid):
            os.chown(sidecar, info.st_uid, info.st_gid, follow_symlinks=False)


def backup_database(source: Path, destination: Path) -> bool:
    """Consistent online copy via the SQLite backup API; False when *source* is not a database."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        connection = sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            target = sqlite3.connect(str(destination))
            try:
                connection.backup(target)
            finally:
                target.close()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        destination.unlink(missing_ok=True)
        return False
    finally:
        _fix_sidecars(source)
    destination.chmod(0o600)
    return True


def link_or_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        os.link(source, destination, follow_symlinks=False)
        return "link"
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP}:
            raise
    shutil.copy2(source, destination, follow_symlinks=False)
    return "copy"


def _copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)
    return "copy"


def _signature(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


class Snapshot:
    """``staging/snapshot-<id>/`` with ``index.json`` recording what was captured and how."""

    def __init__(self, path: Path, root: Path, allowed_link: Callable[[Path], bool] | None = None):
        self.path = Path(path)
        self.root = Path(root)
        self.allowed_link = allowed_link

    @classmethod
    def create(cls, root: Path, allowed_link: Callable[[Path], bool] | None = None) -> Snapshot:
        staging = Path(root) / "staging"
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = staging / ("snapshot-" + secrets.token_hex(6))
        path.mkdir(mode=0o700)
        snapshot = cls(path, root, allowed_link)
        try:
            snapshot.capture()
        except BaseException:
            snapshot.remove()
            raise
        return snapshot

    @classmethod
    def open(cls, path: str | os.PathLike[str], root: Path,
             allowed_link: Callable[[Path], bool] | None = None) -> Snapshot:
        path = Path(path)
        if path.parent != Path(root) / "staging" or not path.name.startswith("snapshot-") or path.is_symlink():
            raise ValueError("The journal names an unexpected snapshot directory.")
        if not (path / "index.json").is_file():
            raise ValueError("The snapshot recorded in the journal is incomplete or missing.")
        return cls(path, root, allowed_link)

    # Capture ------------------------------------------------------------

    def _sources(self) -> dict[str, Path]:
        output: dict[str, Path] = {}
        data = self.root / "data"
        for path, name in regular_files(data, allowed_link=self.allowed_link):
            if not is_sidecar(path):
                output["data/" + name] = path
        for path, name in regular_files(self.root / "site"):
            output["site/" + name] = path
        for name in CONFIG_FILES:
            path = self.root / "config" / name
            if path.is_file() and not path.is_symlink():
                output["config/" + name] = path
        return output

    def _add(self, name: str, source: Path) -> dict[str, Any]:
        destination = self.path / name
        signature = _signature(source)
        if is_database(source) and backup_database(source, destination):
            method = "sqlite"
        elif _mutable(source, name):
            method = _copy(source, destination)
        else:
            method = link_or_copy(source, destination)
        return {"method": method, "signature": signature}

    def _check_space(self, sources: dict[str, Path]) -> None:
        needed = sum(path.stat().st_size for name, path in sources.items()
                     if is_database(path) or _mutable(path, name))
        if shutil.disk_usage(self.path).free < needed + 128 * 1024 * 1024:
            raise ValueError("Not enough free space for a pre-update snapshot.")

    def capture(self) -> None:
        sources = self._sources()
        self._check_space(sources)
        index = {name: self._add(name, path) for name, path in sources.items()}
        (self.path / "data").mkdir(mode=0o700, exist_ok=True)
        (self.path / "site").mkdir(mode=0o700, exist_ok=True)
        write_json(self.path / "index.json", {"schema": 1, "files": index, "complete": False})

    def refresh(self) -> dict[str, int]:
        """Bring the snapshot up to date once nothing writes any more; return change counts."""
        record = read_json(self.path / "index.json")
        index: dict[str, Any] = record["files"]
        sources = self._sources()
        counts = {"kept": 0, "updated": 0, "removed": 0}
        for name in sorted(set(index) - set(sources)):
            (self.path / name).unlink(missing_ok=True)
            del index[name]
            counts["removed"] += 1
        for name, path in sources.items():
            entry = index.get(name)
            if entry and entry["method"] != "sqlite" and entry["signature"] == _signature(path):
                counts["kept"] += 1
                continue
            index[name] = self._add(name, path)
            counts["updated"] += 1
        write_json(self.path / "index.json", {"schema": 1, "files": index, "complete": True})
        return counts

    # Use ----------------------------------------------------------------

    def inputs(self) -> list[tuple[Path, str]]:
        """Package entries (``data/…``, ``site/…``, ``config/…``) in a stable order."""
        index = read_json(self.path / "index.json")["files"]
        return [(self.path / name, name) for name in sorted(index)]

    def copy_tree(self, name: str, destination: Path) -> None:
        """Materialise ``data`` or ``site`` at *destination* (hard links where the snapshot linked)."""
        index = read_json(self.path / "index.json")["files"]
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        prefix = name + "/"
        for entry_name, entry in index.items():
            if not entry_name.startswith(prefix):
                continue
            source = self.path / entry_name
            target = destination / entry_name[len(prefix):]
            if entry["method"] == "link":
                link_or_copy(source, target)
            else:
                _copy(source, target)

    def remove(self) -> None:
        if self.path.is_dir() and not self.path.is_symlink():
            shutil.rmtree(self.path)


def stale_snapshots(root: Path, keep: str | None) -> list[Path]:
    staging = Path(root) / "staging"
    if not staging.is_dir():
        return []
    return [path for path in staging.glob("snapshot-*")
            if path.is_dir() and not path.is_symlink() and str(path) != keep and path.name != MAINTENANCE_MARKER]
