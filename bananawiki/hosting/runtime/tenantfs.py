"""Tenant data directories, handled as hostile file trees.

A tenant can create any file, link or FIFO inside its own data directory
(plugins run there), and the portal opens paths in it with the service
account's rights. Everything here therefore walks with directory descriptors,
opens with ``O_NOFOLLOW | O_NONBLOCK``, checks what it opened and never
follows a link: a link the tenant planted can neither make the portal read
the platform database nor write into another tenant.

The directory layout is 1.4's (audit C12): ``INSTANCES_DIR/<slug>[__apex]``
with the asset folders under ``storage/<name>`` and relative
``<name> -> storage/<name>`` links, which the updater's backup accepts.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from collections.abc import Iterator
from pathlib import Path

from . import RuntimeFailure

ASSET_FOLDERS = ("uploads", "attachments", "chat_attachments", "kanban_attachments", "custom_page_files")
DATA_DIR_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:__apex)?")
EXCHANGE = ".bw-host"
DATABASE_FILES = ("bananawiki.db", "bananawiki.db-wal", "bananawiki.db-shm", "bananawiki.db-journal",
                  "bananawiki.db.initialized", "bananawiki.db.schema.lock")
STALE_STATE_FILES = ("bananawiki.pid", "tts_worker.pid", ".starting", ".port", "gunicorn.pid", ".maintenance.lock")
_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW


def tenant_path(instances_dir: str | os.PathLike[str], name: str) -> Path:
    """``INSTANCES_DIR/<name>`` after validating *name* (never trust the caller)."""
    if not isinstance(name, str) or not DATA_DIR_NAME.fullmatch(name) or len(name) > 120:
        raise RuntimeFailure("invalid", f"unsafe data directory name {str(name)[:80]!r}")
    return Path(instances_dir) / name


def is_tenant_dir(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir()


# ── Descriptor-based access ───────────────────────────────────────────────────


def open_dir(root: Path, relative: str = "") -> int:
    """A descriptor for ``root/relative``, opening each component without following links."""
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise RuntimeFailure("invalid", "path escapes the tenant directory")
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(dir_fd: int, name: str, *, max_bytes: int | None = None) -> int:
    """A read descriptor for a regular file *name* in *dir_fd* (no links, FIFOs or devices)."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeFailure("db_unsafe", f"{name} is not a regular file")
        if max_bytes is not None and info.st_size > max_bytes:
            raise RuntimeFailure("too_large", f"{name} is too large")
        return fd
    except BaseException:
        os.close(fd)
        raise


def iter_files(dir_fd: int, *, skip_dirs: frozenset[str] = frozenset(),
               skip_hidden: bool = False) -> Iterator[tuple[str, int, os.stat_result]]:
    """Yield ``(relative path, fd, stat)`` for each regular file below *dir_fd*; the caller closes *fd*.

    ``os.fwalk`` does not enter linked directories, and files are opened
    relative to their directory's descriptor; links and special files are
    skipped silently.
    """
    for dirpath, dirnames, filenames, walk_fd in os.fwalk(".", dir_fd=dir_fd, follow_symlinks=False):
        dirnames[:] = [name for name in dirnames
                       if name not in skip_dirs and not (skip_hidden and name.startswith("."))]
        for name in filenames:
            try:
                fd = os.open(name, _FILE_FLAGS, dir_fd=walk_fd)
            except OSError:
                continue
            try:
                info = os.fstat(fd)
            except OSError:
                os.close(fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                continue
            yield os.path.normpath(os.path.join(dirpath, name)).replace(os.sep, "/"), fd, info


def read_bytes(root: Path, relative: str, *, max_bytes: int) -> bytes:
    parent, _, name = relative.rpartition("/")
    dir_fd = open_dir(root, parent)
    try:
        fd = open_file(dir_fd, name, max_bytes=max_bytes)
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(max_bytes + 1)[:max_bytes]


def copy_out(root: Path, relative: str, destination: Path) -> int:
    """Copy one tenant file to a host path (created exclusively); returns its size."""
    parent, _, name = relative.rpartition("/")
    dir_fd = open_dir(root, parent)
    try:
        fd = open_file(dir_fd, name)
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as source, open(destination, "xb") as target:
        os.fchmod(target.fileno(), 0o600)
        shutil.copyfileobj(source, target, 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
        return target.tell()


def write_into(root: Path, relative_dir: str, name: str, source: Path) -> None:
    """Copy host file *source* to ``root/relative_dir/name``; the name must be new."""
    dir_fd = open_dir(root, relative_dir)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "wb") as target, open(source, "rb") as handle:
        shutil.copyfileobj(handle, target, 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())


def unlink(root: Path, relative: str) -> None:
    """Remove one entry (a link is removed as a link); missing entries are fine."""
    parent, _, name = relative.rpartition("/")
    try:
        dir_fd = open_dir(root, parent)
    except OSError:
        return
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            _remove_tree_at(dir_fd, name)
        else:
            os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(dir_fd)


def _remove_tree_at(dir_fd: int, name: str) -> None:
    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        for entry in os.listdir(fd):
            info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_tree_at(fd, entry)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dir_fd)


def remove_tree(path: Path) -> None:
    """Delete a directory tree without following links (``shutil.rmtree`` is fd-based on Linux)."""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


# ── Layout ────────────────────────────────────────────────────────────────────


def create(root: Path) -> None:
    """Create a new tenant directory with the 1.4 layout; refuses an existing one."""
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        raise RuntimeFailure("data_exists", root.name) from None
    ensure_layout(root)


def ensure_layout(root: Path) -> None:
    """Add missing ``storage/<name>`` folders and ``<name>`` links (restored or 1.4 data).

    Works on directory descriptors: a ``storage`` (or asset folder) the
    tenant replaced with a link is refused rather than followed, so the
    portal never creates folders wherever such a link points.
    """
    root_fd = open_dir(root)
    try:
        storage_fd = _mkdir_at(root_fd, "storage")
        try:
            for name in ASSET_FOLDERS:
                os.close(_mkdir_at(storage_fd, name))
                try:
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.symlink(f"storage/{name}", name, target_is_directory=True, dir_fd=root_fd)
        finally:
            os.close(storage_fd)
        os.close(_mkdir_at(root_fd, "external_plugins"))
    finally:
        os.close(root_fd)


def _mkdir_at(dir_fd: int, name: str) -> int:
    """Create *name* (0700) below *dir_fd* when missing and open it; links and files are refused."""
    try:
        os.mkdir(name, 0o700, dir_fd=dir_fd)
    except FileExistsError:
        pass
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise RuntimeFailure("db_unsafe", f"{name} is not a directory") from None


def ensure_exchange(root: Path) -> None:
    """The ``.bw-host`` folder the host and the tenant task swap database copies through."""
    dir_fd = open_dir(root)
    try:
        try:
            info = os.stat(EXCHANGE, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISDIR(info.st_mode):
            os.unlink(EXCHANGE, dir_fd=dir_fd)
            info = None
        if info is None:
            os.mkdir(EXCHANGE, 0o700, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def clear_stale_state(root: Path) -> None:
    """Files 1.4's process runtime left behind (PID files, start locks)."""
    for name in STALE_STATE_FILES:
        unlink(root, name)


def reset_content(root: Path) -> None:
    """Delete the database and the asset folders of a stopped tenant; keep the layout."""
    for name in (*DATABASE_FILES, EXCHANGE, "tts", "tmp_exports", "favicons"):
        unlink(root, name)
    for name in ASSET_FOLDERS:
        unlink(root, f"storage/{name}")
        alias = root / name
        if not alias.is_symlink() or os.readlink(alias) != f"storage/{name}":
            unlink(root, name)
    ensure_layout(root)


def relocate(instances_dir: str, old_name: str, new_name: str) -> None:
    source = tenant_path(instances_dir, old_name)
    target = tenant_path(instances_dir, new_name)
    if old_name == new_name:
        return
    if os.path.lexists(target):
        raise RuntimeFailure("data_exists", new_name)
    if not is_tenant_dir(source):
        raise RuntimeFailure("not_found", old_name)
    os.rename(source, target)


def usage(root: Path, *, deadline_seconds: float = 10.0) -> int:
    """Bytes used below *root*, not following links; stops counting at the deadline."""
    total = 0
    deadline = time.monotonic() + deadline_seconds
    stack = [str(root)]
    while stack and time.monotonic() < deadline:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


def copy_tree(source_root: Path, relative: str, target: Path) -> None:
    """Copy the regular files of a tenant folder into a host folder (links and special files skipped)."""
    try:
        dir_fd = open_dir(source_root, relative)
    except OSError:
        return
    try:
        for name, fd, _info in iter_files(dir_fd):
            destination = target / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with os.fdopen(fd, "rb") as source:
                try:
                    output = open(destination, "xb")  # noqa: SIM115 - closed below
                except FileExistsError:
                    continue
                with output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
    finally:
        os.close(dir_fd)
