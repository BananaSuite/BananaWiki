"""Frozen copies of ``data/``, ``site/`` and the configuration, taken mostly online.

1.4 stopped every service and then tarred all data, so an update's downtime
grew with the size of the installation (every tenant in hosting mode). A
snapshot is prepared while the site is still serving:

* SQLite databases are copied (with their WAL or journal) by descriptor into
  a private directory and turned into one file there with SQLite's backup
  API, so SQLite never opens a path in a tree someone else writes;
* append-only or rewritten-in-place files (logs, locks, small files) are copied;
* files tenants control (hosting: ``data/instances/<tenant>/…``, marked by
  *allowed_link*) are always copied: the package is written after the
  tenants run again, and a hard link would let a tenant rewrite the staged
  file under the package writer;
* everything else (uploads, attachments: written once, replaced atomically,
  never modified in place) is hard-linked, which costs no space or time.

Every file is opened by descriptor, one path component at a time, without
following links (``O_NOFOLLOW``), without blocking on a FIFO
(``O_NONBLOCK``), and only captured when ``fstat`` says it is a regular file:
tenants keep writing their directories while root captures them. An entry a
tenant removed or replaced meanwhile is left out rather than failing the
snapshot for every tenant.

After the services stop, :meth:`Snapshot.refresh` re-copies databases and
files whose inode, size or modification time changed, so the downtime is
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
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from .files import MAINTENANCE_MARKER, open_directory, open_regular, read_json, regular_files, write_json

CONFIG_FILES = ("installation.json", "source.json", "app.env", "repo.token", "repo.key", "repo.known_hosts",
                "repo.allowed_signers", "updates.json")
_DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_SIDECARS = ("-wal", "-shm", "-journal")
_MUTABLE_SUFFIXES = {".log", ".jsonl", ".lock", ".pid", ".tmp"}
_SMALL = 64 * 1024


def is_database(path: Path) -> bool:
    return path.suffix in _DATABASE_SUFFIXES


def is_sidecar(path: Path) -> bool:
    return any(path.name.endswith(suffix) and is_database(Path(path.name[: -len(suffix)])) for suffix in _SIDECARS)


def _mutable(relative: str, size: int) -> bool:
    path = Path(relative)
    return (path.suffix in _MUTABLE_SUFFIXES or ".log." in path.name or "logs" in path.parts[:-1]
            or size < _SMALL)


def backup_database(source: Path, destination: Path) -> bool:
    """Copy a *private* database copy with the SQLite backup API; False when *source* is not a database.

    Opening *source* read-write lets SQLite recover the WAL or hot journal
    copied next to it; only ever pass a file in a directory nobody else writes.
    """
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        connection = sqlite3.connect(str(source), timeout=30)
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
    destination.chmod(0o600)
    return True


def _write_from(source: BinaryIO, destination: Path, mode: int = 0o600) -> None:
    """Copy an open file into a new private file (never through a link at *destination*)."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as target:
        # A running tenant may keep appending indefinitely. Capture only the
        # observed length; the offline refresh captures any later changes.
        remaining = os.fstat(source.fileno()).st_size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("A file shrank while its snapshot was captured; retry the operation.")
            target.write(chunk)
            remaining -= len(chunk)
        os.fchmod(target.fileno(), mode & 0o777)


def _copy_database(directory: int, name: str, source: BinaryIO, destination: Path, scratch: Path) -> bool:
    """A consistent copy of the database open as *source*; False when it is not a database.

    The file and its ``-wal``/``-journal`` (opened in the same *directory*
    descriptor; anything else under those names is ignored) are copied into
    a private directory first, and SQLite only opens that copy.
    """
    with tempfile.TemporaryDirectory(prefix=".database-", dir=scratch) as private:
        copy = Path(private) / "database.db"
        _write_from(source, copy)
        for suffix in ("-wal", "-journal"):
            try:
                fd = open_regular(name + suffix, directory)
            except (OSError, ValueError):
                continue
            with os.fdopen(fd, "rb") as sidecar:
                _write_from(sidecar, Path(str(copy) + suffix))
        return backup_database(copy, destination)


def _link_descriptor(directory: int, fd: int, source: BinaryIO, destination: Path, mode: int) -> str:
    """Hard-link exactly the inode open as *fd* (not whatever its name points to now); copy if that fails."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        # linkat(AT_SYMLINK_FOLLOW) on the descriptor's /proc entry; Python only uses
        # linkat when a directory descriptor is given (ignored for the absolute path).
        os.link(f"/proc/self/fd/{fd}", destination, src_dir_fd=directory, follow_symlinks=True)
        return "link"
    except OSError:
        pass
    _write_from(source, destination, mode)
    return "copy"


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


def _signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _current(path: Path) -> os.stat_result | None:
    """The entry's own metadata (a link is not followed); None when it is gone."""
    try:
        return path.lstat()
    except OSError:
        return None


class Snapshot:
    """``staging/snapshot-<id>/`` with ``index.json`` recording what was captured and how.

    *allowed_link* marks the entries tenants control: links and special files
    there are skipped, and their files are copied, never hard-linked.
    """

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

    def _untrusted(self, name: str) -> bool:
        return self.allowed_link is not None and self.allowed_link(self.root / name)

    def _add(self, name: str) -> dict[str, Any] | None:
        """Capture ``root/name`` by descriptor; None when a tenant's entry vanished or is no regular file."""
        destination = self.path / name
        parent, _, leaf = name.rpartition("/")
        try:
            directory = open_directory(self.root, parent)
            try:
                fd = open_regular(leaf, directory)
            except BaseException:
                os.close(directory)
                raise
        except (OSError, ValueError):
            if self._untrusted(name):
                return None
            raise
        try:
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(fd)
                if is_database(Path(name)) and _copy_database(directory, leaf, source, destination, self.path):
                    method = "sqlite"
                else:
                    source.seek(0)
                    if self._untrusted(name) or _mutable(name, info.st_size):
                        _write_from(source, destination, info.st_mode)
                        method = "copy"
                    else:
                        method = _link_descriptor(directory, fd, source, destination, info.st_mode)
        finally:
            os.close(directory)
        return {"method": method, "signature": _signature(info)}

    def _check_space(self, sources: dict[str, Path]) -> None:
        needed = 0
        for name, path in sources.items():
            info = _current(path)
            size = info.st_size if info else 0
            if is_database(path):
                needed += 2 * size  # the private copy and the consolidated one
            elif self._untrusted(name) or _mutable(name, size):
                needed += size
        if shutil.disk_usage(self.path).free < needed + 128 * 1024 * 1024:
            raise ValueError("Not enough free space for a pre-update snapshot.")

    def capture(self) -> None:
        sources = self._sources()
        self._check_space(sources)
        index = {}
        for name in sources:
            entry = self._add(name)
            if entry is not None:
                index[name] = entry
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
            info = _current(path)
            if (entry and entry["method"] != "sqlite" and not is_database(path) and info is not None
                    and entry["signature"] == _signature(info)):
                counts["kept"] += 1
                continue
            added = self._add(name)
            if added is None:
                (self.path / name).unlink(missing_ok=True)
                if index.pop(name, None) is not None:
                    counts["removed"] += 1
                continue
            index[name] = added
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
