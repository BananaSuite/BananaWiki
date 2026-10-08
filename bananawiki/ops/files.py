"""Private configuration files, locks and bounded portable packages.

Package format (unchanged from 1.4, so packages stay readable in both
directions): a gzip tarball of regular files plus ``manifest.json`` with
``schema: 1``, ``product``, ``mode``, ``revision``, ``created_at``,
``old_root`` and ``files: {name: {sha256, size}}``. The manifest is written
last, from checksums of the bytes that went into the archive.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

MAINTENANCE_MARKER = ".banana-maintenance"
# Files a portable package may hold (``BANANA_PACKAGE_MAX_FILES``); 1.4 and 1.6.0 restore at most 100,000.
DEFAULT_MAX_FILES = 1_000_000
_JSON_BYTES = 16 * 1024 * 1024
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Refused(RuntimeError):
    """An operation refused to begin, before it changed anything: trying again later is safe."""


def absolute_path(value: str | os.PathLike[str]) -> Path:
    """Validate a managed root: absolute, simple characters, no symlinks, not ``/`` or ``/opt``."""
    path = Path(value).absolute()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)) or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("Use an absolute installation path below a dedicated directory, without spaces or '..'.")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Managed installation paths cannot traverse symlinks.")
    return path


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: str | os.PathLike[str], content: str | bytes, mode: int = 0o600) -> None:
    """Replace *path* atomically and durably; never follow a symlink at the destination."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content.encode() if isinstance(content, str) else content)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _remove_entry_at(dir_fd: int, name: str) -> None:
    """Remove *name* under *dir_fd* whatever it is (a link as a link, a directory as a tree)."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        for entry in os.listdir(fd):
            _remove_entry_at(fd, entry)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dir_fd)


def untrusted_marker(directory: str | os.PathLike[str], name: str, content: str | None) -> None:
    """Create (*content*) or remove (None) the file *name* in a directory someone else controls.

    For tenant directories: the tenant may have put a link, a FIFO or a
    directory under that name. Whatever is there is removed without being
    followed, and the file is created by descriptor with ``O_EXCL``.
    """
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _remove_entry_at(dir_fd, name)
        if content is not None:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o644)
                handle.write(content)
    finally:
        os.close(dir_fd)


def open_directory(root: str | os.PathLike[str], relative: str = "") -> int:
    """A descriptor for ``root/relative``, opening each component without following links.

    For trees someone else writes (tenant directories): a component swapped
    for a link while root walks the tree fails with ``ELOOP``/``ENOTDIR``
    instead of redirecting root elsewhere.
    """
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ValueError("Path escapes its directory.")
    fd = os.open(root, _DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_regular(name: str | os.PathLike[str], dir_fd: int | None = None) -> int:
    """A read descriptor for a regular file: never through a link, never blocking on a FIFO."""
    fd = os.open(name, _READ, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Not a regular file: {os.fsdecode(name)}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_json(path: str | os.PathLike[str], value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: str | os.PathLike[str], default: Any = None, *, max_bytes: int = _JSON_BYTES) -> Any:
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return default
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError(f"Invalid configuration file: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_limit(files: int) -> int:
    """The largest manifest of a package of *files* files (16 MiB, as in 1.4, plus 1 KiB a file)."""
    return _JSON_BYTES + 1024 * files


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as target:
        target.write(json.dumps(value, sort_keys=True) + "\n")


def read_environment(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a systemd ``EnvironmentFile`` written by :func:`write_environment` (or by hand)."""
    output: dict[str, str] = {}
    path = Path(path)
    if not path.exists():
        return output
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not _ENV_KEY.fullmatch(key):
            raise ValueError("Environment files use one NAME=value assignment per line.")
        output[key] = " ".join(shlex.split(value, comments=False))
    return output


def write_environment(path: str | os.PathLike[str], values: dict[str, str]) -> None:
    lines = []
    for key, value in sorted(values.items()):
        text = str(value)
        if not _ENV_KEY.fullmatch(key) or any(char in text for char in "\r\n\0"):
            raise ValueError(f"Invalid environment assignment for {key!r}.")
        lines.append(key + "=" + json.dumps(text, ensure_ascii=False))
    atomic_write(path, "\n".join(lines) + "\n")


@contextmanager
def exclusive_lock(path: Path, message: str) -> Iterator[None]:
    """Non-blocking ``flock`` on *path*; raise RuntimeError(*message*) when held elsewhere."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(message) from None
        yield
    finally:
        os.close(descriptor)


def maintenance_lock(root: Path) -> Any:
    """The lock every lifecycle operation holds (same file as 1.4, so both versions exclude each other)."""
    return exclusive_lock(
        Path(root) / "config" / "maintenance.lock",
        "Another maintenance operation is running. Inspect status and retry when it finishes.",
    )


def digest_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def regular_files(
    directory: Path,
    *,
    allowed_link: Callable[[Path], bool] | None = None,
    excluded: Iterable[str] = (),
) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, relative posix name)`` for every regular file under *directory*.

    Maintenance markers, and links or special files accepted by *allowed_link*
    (hosting: anything a tenant created inside its own directory), are
    skipped; any other symlink or special file is refused so a package is
    always complete and restorable.
    """
    root = Path(directory)
    if not root.exists():
        return
    excluded = tuple(excluded)

    def omitted(relative: str) -> bool:
        return any(relative == item or relative.startswith(item + "/") for item in excluded)

    # By descriptor below *directory*: a directory swapped for a link during the walk is not entered.
    top = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    walk = os.fwalk(".", dir_fd=top, follow_symlinks=False)
    try:
        for current, dirs, files, dir_fd in walk:
            base = root / current
            dirs[:] = sorted(name for name in dirs if not omitted((base / name).relative_to(root).as_posix()))
            for name in (*dirs, *sorted(files)):
                path = base / name
                relative = path.relative_to(root).as_posix()
                if name == MAINTENANCE_MARKER or omitted(relative):
                    continue
                try:
                    mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    mode = 0
                if stat.S_ISDIR(mode):
                    continue
                if not stat.S_ISREG(mode):
                    if allowed_link is not None and allowed_link(path):
                        continue
                    raise ValueError(f"Portable packages require regular files and directories: {relative}")
                yield path, relative
    finally:
        walk.close()
        os.close(top)


class _HashingReader:
    """Hand a file to ``tarfile`` while hashing exactly the bytes it archives."""

    def __init__(self, source: Any):
        self.source = source
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        self.digest.update(chunk)
        self.size += len(chunk)
        return chunk


def write_package(destination: Path, manifest: dict[str, Any], inputs: Iterable[tuple[Path, str]], *,
                  max_files: int = DEFAULT_MAX_FILES) -> Path:
    """Write a new package (never overwriting) with a hashed manifest.

    Each file is read once, by descriptor: the checksum in the manifest is of
    the bytes streamed into the archive, so a file changed while the package
    is written cannot make the package disagree with its own manifest.
    """
    destination = Path(destination).absolute()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new backup filename; existing packages are never overwritten.")
    files = list(inputs)
    if len(files) > max_files:
        raise ValueError(f"The installation exceeds the package limit of {max_files:,} files "
                         "(BANANA_PACKAGE_MAX_FILES).")
    total = sum(path.stat().st_size for path, _ in files)
    if shutil.disk_usage(destination.parent).free < total + 128 * 1024 * 1024:
        raise ValueError("Not enough free space for a complete backup.")
    manifest = {**manifest, "schema": 1, "files": {}}
    fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
    os.close(fd)
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=3) as archive:
            for path, name in files:
                if name in manifest["files"]:
                    raise ValueError(f"Duplicate package entry: {name}")
                with os.fdopen(open_regular(path), "rb") as handle:
                    info = archive.gettarinfo(arcname=name, fileobj=handle)
                    reader = _HashingReader(handle)
                    archive.addfile(info, reader)
                if reader.size != info.size:
                    raise ValueError(f"A file changed size while it was packaged: {name}")
                manifest["files"][name] = {"sha256": reader.digest.hexdigest(), "size": info.size}
            raw = json.dumps(manifest, sort_keys=True).encode()
            if len(raw) > manifest_limit(len(files)):
                raise ValueError("Package manifest is too large.")
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(raw), 0o600
            archive.addfile(info, io.BytesIO(raw))
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        destination.chmod(0o600)
        fsync_directory(destination.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def extract_archive(archive_path: Path, destination: Path, *, max_bytes: int = 1024 ** 4,
                    max_files: int = 100000) -> set[str]:
    """Stream-extract into an empty directory, refusing links, special files, duplicates and traversal."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Extract only into an empty staging directory.")
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive_path, "r|*") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if (relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts)
                    or "\\" in member.name or "\0" in member.name or member.name in seen):
                raise ValueError("Unsafe or duplicate archive path.")
            seen.add(member.name)
            if len(seen) > max_files or member.size < 0:
                raise ValueError(f"Archive exceeds file limits ({max_files:,} files).")
            total += member.size
            if total > max_bytes:
                raise ValueError("Archive exceeds the extracted-size limit.")
            path = destination.joinpath(*relative.parts)
            if member.isdir():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("Archive links and special files are not supported.")
            if shutil.disk_usage(destination).free < member.size + 64 * 1024 * 1024:
                raise ValueError("Not enough free space to restore the archive.")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Unreadable archive entry.")
            with source, path.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            if path.stat().st_size != member.size:
                raise ValueError("Truncated archive entry.")
            path.chmod(0o700 if member.mode & 0o111 else 0o600)
    return seen


def check_database_copy(file: Path, scratch: Path) -> None:
    """``quick_check`` a copy of a packaged SQLite database (a read can still rewrite WAL indexes)."""
    with tempfile.TemporaryDirectory(prefix="database-check-", dir=scratch) as directory:
        inputs = [file, *(Path(str(file) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
        size = sum(item.stat().st_size for item in inputs if item.is_file())
        if shutil.disk_usage(directory).free < size + 64 * 1024 * 1024:
            raise ValueError("Not enough free space to validate a packaged database.")
        for item in inputs:
            if item.is_file():
                shutil.copyfile(item, Path(directory) / item.name)
        copy = Path(directory) / file.name
        try:
            connection = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA trusted_schema=OFF")
                result = connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error:
            raise ValueError(f"A packaged database failed its integrity check: {file.name!r}.") from None
        if result != "ok":
            raise ValueError(f"A packaged database failed its integrity check: {file.name!r}.")


@contextmanager
def read_package(path: Path, product: str, staging_parent: Path, *,
                 max_files: int = DEFAULT_MAX_FILES) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Extract and fully verify a package of at most *max_files* files; yield ``(directory, manifest)``."""
    staging_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=staging_parent) as temporary:
        root = Path(temporary)
        members = extract_archive(Path(path), root, max_files=max_files + 1)  # and the manifest
        manifest = read_json(root / "manifest.json", max_bytes=manifest_limit(len(members)))
        if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("product") != product:
            raise ValueError("This package does not match the application or package format.")
        declared = manifest.get("files", {})
        present = {item for item in members if (root / item).is_file()} - {"manifest.json"}
        if not isinstance(declared, dict) or set(declared) != present:
            raise ValueError("The package does not match its manifest.")
        for name, entry in declared.items():
            file = root / name
            if file.stat().st_size != entry.get("size") or digest_file(file) != entry.get("sha256"):
                raise ValueError(f"Package integrity verification failed for {name!r}.")
        for name in declared:
            if Path(name).suffix in _DATABASE_SUFFIXES:
                check_database_copy(root / name, staging_parent)
        yield root, manifest
