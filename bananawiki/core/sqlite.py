"""SQLite storage: safe connection lifecycle, versioned migrations, query helpers.

Design rules
------------
* Only :meth:`Database.initialize` may create a database file. Ordinary
  connections refuse to open a missing or empty database once it has been
  initialised before (the ``.initialized`` marker or a WAL sidecar exists), so
  a lost volume is reported instead of silently replaced by an empty wiki.
* Schema versions live in ``PRAGMA application_id`` and ``PRAGMA user_version``.
  Each migration runs in its own ``BEGIN IMMEDIATE`` transaction with foreign
  keys disabled; the version is written inside the same transaction and a
  whole-database ``foreign_key_check`` must pass before commit. A database
  with a newer version than the code knows is refused.
* Connections run in autocommit mode. Multi-statement writes use
  :meth:`Database.transaction`, which issues ``BEGIN IMMEDIATE`` so writers
  queue on SQLite's lock instead of failing half-way with ``SQLITE_BUSY``.

These conventions are byte-compatible with BananaWiki 1.4 databases: the
application ids, marker files and lock files are unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

Migration = Callable[[sqlite3.Connection], None]

log = logging.getLogger("bananawiki.database")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BOOT_LOCK_NOTICE = 60.0  # seconds between "still waiting" log lines
_UNAVAILABLE_CODES = {
    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CORRUPT,
    sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_FULL,
    sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY,
}
_MARKER_CONTENT = b"BananaSuite SQLite initialized\n"


class DatabaseUnavailable(sqlite3.OperationalError):
    """Storage needs operator attention (missing, damaged, busy or too new)."""


def is_unavailable(error: BaseException) -> bool:
    """Return True when *error* means storage is unusable rather than a bug."""
    if isinstance(error, DatabaseUnavailable):
        return True
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in _UNAVAILABLE_CODES
    if not isinstance(error, sqlite3.DatabaseError):
        return False
    text = str(error).lower()
    return any(fragment in text for fragment in (
        "database is locked", "database is busy", "database table is locked",
        "database disk image is malformed", "file is not a database",
        "disk i/o error", "database or disk is full", "unable to open database",
        "attempt to write a readonly database",
    ))


def quote_identifier(name: str) -> str:
    """Quote a trusted SQL identifier after validating its shape."""
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def dict_row(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    """Row factory returning plain dictionaries (JSON- and template-friendly)."""
    return {column[0]: value for column, value in zip(cursor.description, row, strict=True)}


# ── Schema helpers used by migrations ─────────────────────────────────────────


def tuples(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
    """Run *sql* returning plain tuples whatever the connection's row factory is."""
    cursor = conn.cursor()
    cursor.row_factory = None
    return cursor.execute(sql, params).fetchall()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(tuples(conn, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)))


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in tuples(conn, f"PRAGMA table_info({quote_identifier(table)})")}


def add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    """``ALTER TABLE ADD COLUMN`` for every column that does not exist yet."""
    existing = column_names(conn, table)
    for name, ddl in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {quote_identifier(table)} ADD COLUMN {quote_identifier(name)} {ddl}")


def execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Run a multi-statement script without ``executescript``'s implicit COMMIT."""
    buffer: list[str] = []
    for line in script.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            if candidate.strip():
                conn.execute(candidate)
            buffer.clear()
    remainder = "".join(buffer).strip()
    if remainder:
        conn.execute(remainder)


# ── File lifecycle guard ──────────────────────────────────────────────────────


def _marker(path: Path) -> Path:
    return Path(str(path) + ".initialized")


def _check_path(path: Path, *, initializing: bool = False) -> None:
    marker = _marker(path)
    if path.is_symlink() or marker.is_symlink():
        raise DatabaseUnavailable("Database and initialization marker must be regular files, not symlinks.")
    if path.exists() and not path.is_file():
        raise DatabaseUnavailable(f"The configured database {path} is not a regular file.")
    missing = not path.exists()
    empty = not missing and path.stat().st_size == 0
    if missing or empty:
        sidecars = any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
        if not initializing or marker.exists() or sidecars:
            raise DatabaseUnavailable(
                f"The database {path} is missing or empty although it was initialised before. "
                "Preserve the directory and restore a verified backup."
            )


def _mark_initialized(path: Path) -> None:
    marker = _marker(path)
    if marker.exists():
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(marker, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(_MARKER_CONTENT)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(marker.parent)


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DatabaseUnavailable("The database must be a regular file without hard links.")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


# ── Migrations ────────────────────────────────────────────────────────────────


@contextmanager
def _migration_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            violations = tuples(conn, "PRAGMA foreign_key_check")
            if violations:
                table = violations[0][0]
                raise DatabaseUnavailable(
                    f"Migration left an invalid foreign-key reference in table {table!r}. "
                    "The upgrade was rolled back; the database is unchanged."
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def schema_version(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return ``(application_id, user_version)``."""
    return (
        int(tuples(conn, "PRAGMA application_id")[0][0]),
        int(tuples(conn, "PRAGMA user_version")[0][0]),
    )


def apply_migrations(
    conn: sqlite3.Connection,
    application_id: int,
    migrations: Sequence[Migration],
    *,
    baseline: int = 0,
    bootstrap: Migration | None = None,
) -> int:
    """Bring the database to version ``baseline + len(migrations)``.

    ``migrations[i]`` upgrades version ``baseline + i`` to ``baseline + i + 1``.
    A brand-new (empty) database is created by *bootstrap*, which must build
    the latest schema directly; without one, an empty database runs every
    migration from ``baseline``. Databases older than *baseline* were written
    by releases whose upgrade code is no longer shipped and are refused.
    Returns the number of steps applied (bootstrap counts as one).
    """
    if conn.in_transaction:
        raise RuntimeError("Migrations must start outside a transaction.")
    actual_id, version = schema_version(conn)
    latest = baseline + len(migrations)
    if actual_id not in (0, application_id):
        raise DatabaseUnavailable("This database belongs to a different application.")
    if version and actual_id != application_id:
        raise DatabaseUnavailable("The database has a schema version but no matching application id.")
    if version > latest:
        raise DatabaseUnavailable(
            f"This database has schema version {version}, newer than this release supports ({latest}). "
            "Start the matching newer release, or restore the backup taken before the upgrade."
        )
    if version == 0 and not _has_user_tables(conn):
        if bootstrap is None and baseline:
            raise DatabaseUnavailable("No bootstrap schema is available for a new database.")
        if bootstrap is not None:
            with _migration_transaction(conn):
                bootstrap(conn)
                conn.execute(f"PRAGMA application_id={int(application_id)}")
                conn.execute(f"PRAGMA user_version={latest}")
            return 1
    elif version < baseline:
        raise DatabaseUnavailable(
            f"This database has schema version {version}; this release upgrades version {baseline} "
            "or newer. Start the last 1.4 release once to finish its own upgrade, then this one. "
            "See UPGRADING.md."
        )
    applied = 0
    for target in range(max(version, baseline), latest):
        step = migrations[target - baseline]
        with _migration_transaction(conn):
            step(conn)
            conn.execute(f"PRAGMA application_id={int(application_id)}")
            conn.execute(f"PRAGMA user_version={target + 1}")
        applied += 1
    return applied


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    return bool(tuples(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"))


# ── Database ──────────────────────────────────────────────────────────────────


class Database:
    """One SQLite database file with its connection policy and migrations."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        application_id: int,
        migrations: Sequence[Migration] = (),
        baseline: int = 0,
        bootstrap: Migration | None = None,
        env_prefix: str = "BW",
        busy_timeout_ms: int = 5000,
        cache_kib: int = 4096,
        synchronous: str = "FULL",
    ):
        self.path = Path(path).absolute()
        self.application_id = application_id
        self.migrations = tuple(migrations)
        self.baseline = baseline
        self.bootstrap = bootstrap
        self.env_prefix = env_prefix
        self.busy_timeout_ms = busy_timeout_ms
        self.cache_kib = cache_kib
        if synchronous.upper() not in {"FULL", "NORMAL"}:
            raise ValueError("synchronous must be FULL or NORMAL")
        self.synchronous = synchronous.upper()
        self._local = threading.local()

    @property
    def latest_version(self) -> int:
        return self.baseline + len(self.migrations)

    # Connections --------------------------------------------------------

    def connect(self, *, _initializing: bool = False, readonly: bool = False) -> sqlite3.Connection:
        """Open a new autocommit connection with BananaWiki's pragmas."""
        _check_path(self.path, initializing=_initializing)
        if _initializing:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _create_file(self.path)
        mode = "ro" if readonly else "rw"
        conn = sqlite3.connect(
            f"{self.path.as_uri()}?mode={mode}",
            uri=True,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = dict_row
        try:
            if not readonly:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            conn.execute(f"PRAGMA synchronous={self.synchronous}")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute(f"PRAGMA cache_size={-int(self.cache_kib)}")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def boot_lock(self, what: str = "Database initialisation") -> Iterator[None]:
        """Serialise first-boot work across processes, waiting for as long as another process holds it.

        An upgrade can outlast any fixed timeout (a large 1.4 database on a
        slow disk). Giving up made a Gunicorn worker fail to boot, which
        stopped the server and killed the worker that was migrating: the
        upgrade was rolled back at every restart.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = Path(str(self.path) + ".schema.lock")
        if lock_path.is_symlink():
            raise DatabaseUnavailable("The schema lock file cannot be a symbolic link.")
        lock = FileLock(str(lock_path), mode=0o600)
        while True:
            try:
                lock.acquire(timeout=_BOOT_LOCK_NOTICE)
                break
            except Timeout:
                log.warning("%s is still running in another process; waiting for it to finish.", what)
        try:
            yield
        finally:
            lock.release()

    def initialize(self, *, before: Callable[[sqlite3.Connection], None] | None = None) -> int:
        """Create or upgrade the database. Safe to call from every worker.

        *before* runs (outside any transaction) after the lock is held and
        before migrations are applied: used for pre-upgrade backups.
        Returns the number of migrations applied.
        """
        with self.boot_lock():
            conn = self.connect(_initializing=True)
            try:
                if before is not None:
                    before(conn)
                applied = apply_migrations(
                    conn, self.application_id, self.migrations,
                    baseline=self.baseline, bootstrap=self.bootstrap,
                )
            finally:
                conn.close()
            _check_path(self.path)
            _mark_initialized(self.path)
            return applied

    def version(self) -> tuple[int, int]:
        conn = self.connect(readonly=True)
        try:
            return schema_version(conn)
        finally:
            conn.close()

    def integrity_check(self) -> str:
        """Return ``"ok"`` or SQLite's description of the damage."""
        try:
            conn = self.connect(readonly=True)
        except sqlite3.DatabaseError as error:
            return str(error)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return str(next(iter(row.values()))) if row else "quick_check returned nothing"
        except sqlite3.DatabaseError as error:
            return str(error)
        finally:
            conn.close()

    def backup_to(self, destination: str | os.PathLike[str]) -> Path:
        """Write a consistent online copy of the database to *destination*."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        try:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        if os.name == "posix":
            os.chmod(target, 0o600)
        return target


class Session:
    """Query helpers bound to one connection (a request or a background job)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._depth = 0

    def all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return default
        return next(iter(row.values()))

    def column(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[Any]:
        return [next(iter(row.values())) for row in self.conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, rows)

    def insert(self, table: str, values: dict[str, Any]) -> int:
        """INSERT a row from a dict of trusted column names; return its rowid."""
        columns = ", ".join(quote_identifier(name) for name in values)
        marks = ", ".join("?" for _ in values)
        cursor = self.conn.execute(
            f"INSERT INTO {quote_identifier(table)} ({columns}) VALUES ({marks})", tuple(values.values())
        )
        return int(cursor.lastrowid or 0)

    def update(self, table: str, values: dict[str, Any], where: str, params: Sequence[Any] = ()) -> int:
        """UPDATE trusted columns; *where* is a SQL fragment with ``?`` params."""
        if not values:
            return 0
        assignments = ", ".join(f"{quote_identifier(name)} = ?" for name in values)
        cursor = self.conn.execute(
            f"UPDATE {quote_identifier(table)} SET {assignments} WHERE {where}",
            (*values.values(), *params),
        )
        return cursor.rowcount

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; nested calls become savepoints."""
        if self.conn.in_transaction:
            self._depth += 1
            name = f"sp_{self._depth}"
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield self
                self.conn.execute(f"RELEASE {name}")
            except BaseException:
                self.conn.execute(f"ROLLBACK TO {name}")
                self.conn.execute(f"RELEASE {name}")
                raise
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
