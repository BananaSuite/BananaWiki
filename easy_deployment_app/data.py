"""Portable Wiki data management: private snapshots and reversible restoration."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
import uuid
import zipfile

from filelock import FileLock, Timeout
from sqlite_snapshot import snapshot

ASSET_DIRECTORIES = ("uploads", "attachments", "chat_attachments", "kanban_attachments",
                     "custom_page_files", "favicons", "tts", "plugins")
LAYOUT_DIRECTORIES = ("instance", *ASSET_DIRECTORIES, "logs", "tmp_exports")
MARKER = ".portable-data.json"
MAX_FILES = 100000
MAX_BYTES = 128 * 1024 ** 3
_HELD_ROOTS = ContextVar("portable_data_locks", default=frozenset())


def wiki_database_path(root: Path) -> Path:
    return Path(root) / "instance" / "bananawiki.db"


def _sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_private(path, content):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Data-management metadata cannot be a symbolic link.")
    descriptor, temporary = tempfile.mkstemp(prefix=".portable-write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_database(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("The archive needs a regular, nonempty Wiki database.")
    conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("The archived database failed its integrity check.")
        application_id = conn.execute("PRAGMA application_id").fetchone()[0]
        if application_id not in (0, 0x42574B49):
            raise ValueError("This database belongs to another application.")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"users", "pages", "site_settings"} <= tables:
            raise ValueError("The archive does not contain a supported Wiki database.")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("The archived database contains invalid references.")
    finally:
        conn.close()


def _root(path):
    root = Path(path).absolute()
    if not root.name or root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("Select a dedicated regular data directory.")
    return root


def _ensure_data_layout(root: Path) -> None:
    root = _root(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / MARKER
    if marker.exists():
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            raise ValueError("Invalid portable data marker.")
        if json.loads(marker.read_text()) != {"product": "BananaWiki", "schema": 1}:
            raise ValueError("This folder belongs to another application.")
    elif any(root.iterdir()):
        # Old launchers did not write a marker. Accept their dedicated layout,
        # never an arbitrary nonempty folder selected by mistake.
        allowed = set(LAYOUT_DIRECTORIES) | {"_startup_marker.txt"}
        if any(item.name not in allowed for item in root.iterdir()):
            _check_database(wiki_database_path(root))
    for name in LAYOUT_DIRECTORIES:
        directory = root / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("Portable data directories cannot be links or special files.")
        directory.mkdir(mode=0o700, exist_ok=True)
    if not marker.exists():
        _write_private(marker, b'{"product":"BananaWiki","schema":1}\n')


def _journal_path(root):
    return root.parent / ("." + root.name + ".restore.json")


def _recover(root):
    journal = _journal_path(root)
    if not journal.exists():
        return
    if journal.is_symlink() or not journal.is_file() or journal.stat().st_size > 4096:
        raise ValueError("The restore journal needs operator inspection.")
    record = json.loads(journal.read_text())
    if not isinstance(record, dict):
        raise ValueError("Invalid restore journal")
    previous_name, staged_name = record.get("previous", ""), record.get("staged", "")
    if not re.fullmatch(re.escape(root.name) + r"\.previous-[0-9a-f]{12}", previous_name):
        raise ValueError("Invalid restore recovery path.")
    if not re.fullmatch(r"\." + re.escape(root.name) + r"\.restore-[A-Za-z0-9_\-]+", staged_name):
        raise ValueError("Invalid restore staging path.")
    previous, staged = root.parent / previous_name, root.parent / staged_name
    if any(path.is_symlink() for path in (root, previous, staged)):
        raise ValueError("Restore recovery paths cannot be symbolic links.")
    phase = record.get("phase")
    if phase not in {"prepared", "committed"}:
        raise ValueError("Unknown restore recovery phase.")
    if phase == "prepared" and previous.exists():
        if root.exists():
            if staged.exists():
                raise ValueError("Ambiguous restore state; preserve both folders for inspection.")
            os.replace(root, staged)
        os.replace(previous, root)
        _sync_directory(root.parent)
    if phase == "committed" and not root.exists():
        raise ValueError("The restored data folder is missing. The previous folder is preserved.")
    if staged.exists():
        shutil.rmtree(staged)
    if phase == "committed" and record.get("delete_previous") and previous.exists():
        shutil.rmtree(previous)
    journal.unlink()
    _sync_directory(root.parent)


@contextmanager
def data_lock(root):
    """A server and a restore/delete operation cannot use the same folder."""
    root = _root(root)
    if root in _HELD_ROOTS.get():
        yield root
        return
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root.parent / ("." + root.name + ".portable.lock")
    if path.is_symlink():
        raise ValueError("The data lock cannot be a symbolic link.")
    try:
        with FileLock(path, timeout=0, mode=0o600):
            _recover(root)
            token = _HELD_ROOTS.set(_HELD_ROOTS.get() | {root})
            try:
                yield root
            finally:
                _HELD_ROOTS.reset(token)
    except Timeout as error:
        raise RuntimeError("Stop every Wiki server using this data folder before managing its data.") from error


def ensure_data_layout(root: Path) -> None:
    """Recover an interrupted operation before creating any missing directories."""
    with data_lock(root) as root:
        _ensure_data_layout(root)


def _commit(root, staged, *, delete_previous=False):
    previous = root.parent / (root.name + ".previous-" + uuid.uuid4().hex[:12])
    journal = _journal_path(root)
    record = {"previous": previous.name, "staged": staged.name,
              "phase": "prepared", "delete_previous": delete_previous}
    for directory, _, _ in os.walk(staged, topdown=False):
        _sync_directory(Path(directory))
    _write_private(journal, json.dumps(record).encode())
    try:
        if root.exists():
            os.replace(root, previous)
        os.replace(staged, root)
        _sync_directory(root.parent)
        record["phase"] = "committed"
        _write_private(journal, json.dumps(record).encode())
        _recover(root)
    except BaseException:
        _recover(root)
        raise
    return previous


def _secret(root):
    instance = root / "instance"
    path = instance / ".secret_key"
    if instance.is_symlink() or path.is_symlink():
        raise ValueError("The application key cannot be a symbolic link.")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Invalid application key file.")
    return path.read_bytes()


def clear_all_data(root: Path) -> None:
    """Perform an explicitly confirmed reset while retaining the local app key."""
    with data_lock(root) as root:
        _ensure_data_layout(root)
        key = _secret(root)
        staged = Path(tempfile.mkdtemp(prefix="." + root.name + ".restore-", dir=root.parent))
        try:
            _ensure_data_layout(staged)
            if key:
                _write_private(staged / "instance" / ".secret_key", key)
            _commit(root, staged, delete_previous=True)
        finally:
            if staged.exists() and not _journal_path(root).exists():
                shutil.rmtree(staged)


def _entries(archive):
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise ValueError("Archive contains too many files.")
    seen, total, accepted = set(), 0, []
    for info in infos:
        name = info.filename
        parts = name.rstrip("/").split("/")
        if (not name or name.startswith("/") or "\\" in name or "\0" in name
                or any(part in {"", ".", ".."} or ":" in part or part.endswith((".", " ")) for part in parts)):
            raise ValueError("Archive contains an unsafe path.")
        for part in parts:
            stem = part.split(".")[0].upper()
            if stem in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}:
                raise ValueError("Archive contains a reserved filename.")
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or bool(info.flag_bits & 1):
            raise ValueError("Archive links, special files and encrypted ZIP entries are not supported.")
        key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if key in seen:
            raise ValueError("Archive contains duplicate or ambiguous paths.")
        seen.add(key)
        total += info.file_size
        if info.file_size < 0 or total > MAX_BYTES:
            raise ValueError("Archive exceeds the extracted-size limit.")
        if info.is_dir():
            continue
        if name == "bananawiki.db":
            target = "instance/bananawiki.db"
        elif name == "instance/.secret_key":
            if info.file_size > 4096:
                raise ValueError("Invalid archived application key.")
            target = name
        elif parts[0] in ASSET_DIRECTORIES and len(parts) > 1:
            target = name
        elif name in {"manifest.json", "site_export.json"}:
            target = None  # Interoperability metadata, never executable source.
        else:
            raise ValueError("Archive contains unsupported files.")
        accepted.append((info, target))
    if not any(target == "instance/bananawiki.db" for _, target in accepted):
        raise ValueError("The archive does not contain a Wiki database.")
    return accepted, total


def extract_data_archive(archive_path: Path, root: Path) -> Path:
    """Validate and stage everything first, then replace the stopped installation.

    A private sibling folder retains the complete previous installation, including
    its application key. A crash before commit restores that previous folder.
    """
    with data_lock(root) as root:
        _ensure_data_layout(root)
        old_key = _secret(root)
        staged = Path(tempfile.mkdtemp(prefix="." + root.name + ".restore-", dir=root.parent))
        try:
            _ensure_data_layout(staged)
            with zipfile.ZipFile(archive_path) as archive:
                entries, total = _entries(archive)
                if shutil.disk_usage(staged).free < total + 64 * 1024 * 1024:
                    raise OSError("Not enough free space to validate and stage the import.")
                for info, target in entries:
                    if target is None:
                        continue
                    path = staged / target
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    copied = 0
                    with archive.open(info) as source, path.open("xb") as destination:
                        os.chmod(path, 0o600)
                        while chunk := source.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > info.file_size:
                                raise ValueError("Archive entry exceeds its declared size.")
                            destination.write(chunk)
                        if copied != info.file_size:
                            raise ValueError("Archive entry is truncated.")
                        destination.flush()
                        os.fsync(destination.fileno())
            _check_database(wiki_database_path(staged))
            if not _secret(staged) and old_key:
                _write_private(staged / "instance" / ".secret_key", old_key)
            return _commit(root, staged)
        finally:
            if staged.exists() and not _journal_path(root).exists():
                shutil.rmtree(staged)


def create_data_archive(root: Path, output_path: Path) -> None:
    """Export a verified DB, application key and assets as a private ZIP."""
    output_path = Path(output_path).absolute()
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError("Choose a new export filename; previous exports are preserved.")
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_path.is_relative_to(_root(root)):
        raise ValueError("Save the portable archive outside the data folder")
    with data_lock(root) as root:
        _ensure_data_layout(root)
        with tempfile.TemporaryDirectory(prefix="portable-export-", dir=output_path.parent) as temporary:
            temporary = Path(temporary)
            database = snapshot(wiki_database_path(root), temporary / "snapshot.db")
            _check_database(database)
            files = [(database, "bananawiki.db")]
            key = _secret(root)
            if key:
                files.append((root / "instance/.secret_key", "instance/.secret_key"))
            for name in ASSET_DIRECTORIES:
                for current, directories, filenames in os.walk(root / name, followlinks=False):
                    for child in (*directories, *filenames):
                        path = Path(current) / child
                        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                            raise ValueError("Exports require regular files; linked or special assets cannot be backed up safely.")
                        if path.is_file():
                            files.append((path, path.relative_to(root).as_posix()))
            total = sum(path.stat().st_size for path, _ in files)
            if len(files) > MAX_FILES or total > MAX_BYTES:
                raise ValueError("The installation exceeds portable archive limits.")
            if shutil.disk_usage(temporary).free < total + 64 * 1024 * 1024:
                raise OSError("Not enough space for the completed export.")
            archive_path = temporary / "export.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                os.chmod(archive_path, 0o600)
                for path, name in files:
                    archive.write(path, name)
                manifest = {"format_version": 1, "source": "standalone", "product": "BananaWiki",
                            "exported_at": datetime.now(timezone.utc).isoformat(), "has_raw_db": True,
                            "has_site_export_json": False, "includes_application_key": bool(key)}
                archive.writestr("manifest.json", json.dumps(manifest))
            # Verify paths and CRCs before making the ZIP visible to its user.
            with zipfile.ZipFile(archive_path) as archive:
                _entries(archive)
                if archive.testzip() is not None:
                    raise ValueError("The completed export failed its integrity check.")
            with archive_path.open("rb") as completed:
                os.fsync(completed.fileno())
            if os.name == "nt":
                os.rename(archive_path, output_path)
            else:
                os.link(archive_path, output_path)
                _sync_directory(output_path.parent)
