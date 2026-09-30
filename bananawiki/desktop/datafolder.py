"""The desktop data folder: everything one local wiki keeps on disk.

The layout is the one the 1.4 "Easy Deployment App" used, so a folder created
by 1.4 is opened in place and upgraded by the wiki's own migrations::

    <folder>/
      .portable-data.json      marks the folder as BananaWiki's
      instance/bananawiki.db   the database
      instance/.secret_key     signs sessions; derives the setup code
      uploads/ attachments/ chat_attachments/ kanban_attachments/
      custom_page_files/ favicons/ tts/ plugins/
      logs/ tmp_exports/
      previous/                data set aside by a restore

A folder is served by one process at a time: :meth:`DataFolder.lock` holds an
exclusive lock file while a server runs or a restore replaces the data.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from . import DesktopError

ASSET_DIRECTORIES = (
    "uploads", "attachments", "chat_attachments", "kanban_attachments",
    "custom_page_files", "favicons", "tts", "plugins",
)
LAYOUT_DIRECTORIES = ("instance", *ASSET_DIRECTORIES, "logs", "tmp_exports")
MARKER = ".portable-data.json"
MARKER_CONTENT = {"product": "BananaWiki", "schema": 1}
LOCK_FILE = ".bananawiki.lock"
RESTORE_JOURNAL = ".restore.json"
PREVIOUS_DIRECTORY = "previous"
LEGACY_LEFTOVERS = ("_startup_marker.txt",)  # 1.4 debug output, removed on open
KNOWN_NAMES = frozenset({
    *LAYOUT_DIRECTORIES, MARKER, LOCK_FILE, RESTORE_JOURNAL, PREVIOUS_DIRECTORY, *LEGACY_LEFTOVERS,
})
DATABASE_NAME = "bananawiki.db"
SECRET_KEY_NAME = ".secret_key"
MAX_KEY_BYTES = 4096


def write_private(path: Path, content: bytes) -> None:
    """Atomically replace *path* with *content*, readable only by the owner."""
    if path.is_symlink():
        raise DesktopError("unsafe_path", str(path))
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class DataFolder:
    """One wiki's data folder, chosen by the user."""

    def __init__(self, path: str | os.PathLike[str]):
        self.root = Path(path).expanduser().absolute()

    def __repr__(self) -> str:
        return f"DataFolder({str(self.root)!r})"

    # Paths ----------------------------------------------------------------

    @property
    def instance(self) -> Path:
        return self.root / "instance"

    @property
    def database(self) -> Path:
        return self.instance / DATABASE_NAME

    @property
    def secret_key_file(self) -> Path:
        return self.instance / SECRET_KEY_NAME

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def previous(self) -> Path:
        return self.root / PREVIOUS_DIRECTORY

    # Validation and layout -------------------------------------------------

    def check(self) -> None:
        """Refuse folders that are not (or cannot become) a BananaWiki data folder.

        An empty or missing folder is fine, as are folders created by this
        launcher or by 1.4. A non-empty folder with other files (the home
        folder, the desktop, a school's shared drive) is refused so the wiki
        never mixes its data with the user's documents.
        """
        root = self.root
        if not root.name or root.is_symlink() or (root.exists() and not root.is_dir()):
            raise DesktopError("folder_invalid", str(root))
        if not root.exists():
            return
        marker = root / MARKER
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
                raise DesktopError("folder_invalid", str(marker))
            try:
                content = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise DesktopError("folder_invalid", str(marker)) from error
            if not isinstance(content, dict) or content.get("product") != "BananaWiki":
                raise DesktopError("folder_foreign", str(root))
            return
        names = {entry.name for entry in root.iterdir()}
        if names and not names <= KNOWN_NAMES:
            raise DesktopError("folder_not_empty", str(root))

    def prepare(self) -> None:
        """Create the layout (after :meth:`check`); safe to call repeatedly."""
        self.check()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in LAYOUT_DIRECTORIES:
            directory = self.root / name
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise DesktopError("unsafe_path", str(directory))
            directory.mkdir(mode=0o700, exist_ok=True)
        marker = self.root / MARKER
        if not marker.exists():
            write_private(marker, json.dumps(MARKER_CONTENT).encode() + b"\n")
        for name in LEGACY_LEFTOVERS:
            leftover = self.root / name
            if leftover.is_file() and not leftover.is_symlink():
                leftover.unlink()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the folder exclusively (one server or one restore at a time)."""
        self.check()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / LOCK_FILE
        if path.is_symlink():
            raise DesktopError("unsafe_path", str(path))
        lock = FileLock(str(path), timeout=0, mode=0o600, thread_local=False)
        try:
            lock.acquire()
        except Timeout as error:
            raise DesktopError("folder_in_use", str(self.root)) from error
        try:
            yield
        finally:
            lock.release()

    # Secrets and state -----------------------------------------------------

    def secret_key(self) -> str:
        """The session key, generated on first use and kept in the folder.

        Keeping it in the folder (never in the environment) means a backup of
        the folder restores working sessions and the same setup code.
        """
        self.instance.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.secret_key_file
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise DesktopError("unsafe_path", str(path))
        if path.exists():
            raw = path.read_bytes()[: MAX_KEY_BYTES + 1]
            key = raw.decode("utf-8", "replace").strip()
            if not key or len(raw) > MAX_KEY_BYTES:
                raise DesktopError("secret_key_invalid", str(path))
            if os.name == "posix" and path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
            return key
        key = secrets.token_hex(32)
        write_private(path, key.encode("ascii"))
        return key

    def setup_done(self) -> bool:
        """Whether the first administrator exists (read-only peek at the database)."""
        if not self.database.is_file():
            return False
        try:
            conn = sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute("SELECT setup_done FROM site_settings WHERE id = 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return bool(row and row[0])

    # Wiki configuration ----------------------------------------------------

    def environ(self, *, host: str, port: int, language: str) -> dict[str, str]:
        """The ``BW_*`` variables that point the wiki at this folder.

        Built from scratch: nothing is inherited from the user's environment,
        so a ``SECRET_KEY`` or ``BW_*`` variable set for another purpose can
        never change which data or key this wiki uses.
        """
        root = self.root
        return {
            "BW_ENV": "production",
            "BW_EASY_DEPLOYMENT": "1",
            "BW_INSTANCE_DIR": str(self.instance),
            "BW_DATABASE_PATH": str(self.database),
            "BW_UPLOAD_FOLDER": str(root / "uploads"),
            "BW_ATTACHMENT_FOLDER": str(root / "attachments"),
            "BW_CHAT_ATTACHMENT_FOLDER": str(root / "chat_attachments"),
            "BW_KANBAN_ATTACHMENT_FOLDER": str(root / "kanban_attachments"),
            "BW_CUSTOM_PAGE_FILES_FOLDER": str(root / "custom_page_files"),
            "BW_FAVICON_UPLOAD_FOLDER": str(root / "favicons"),
            "BW_TTS_FOLDER": str(root / "tts"),
            "BW_EXTERNAL_PLUGINS_DIR": str(root / "plugins"),
            "BW_SITE_EXPORT_TEMP_DIR": str(root / "tmp_exports"),
            "BW_LOG_FILE": str(self.logs / "bananawiki.log"),
            "BW_HOST": host,
            "BW_PORT": str(port),
            "BW_PROXY_MODE": "0",
            "BW_DEFAULT_INTERFACE_LANGUAGE": language,
        }
