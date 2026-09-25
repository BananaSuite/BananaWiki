"""Database connection helper."""

import logging
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps

import config
import sqlite_runtime
from ops_observability import (
    get_observability_snapshot,
    record_sql_timing,
    record_sqlite_retry,
)

SYSTEM_USER_ID = -1

_logger = logging.getLogger("bananawiki.db")


# Retry complete read-only/idempotent operations after transient SQLite
# contention. Connection deadlines and memory limits live in sqlite_runtime.
_BUSY_RETRY_ATTEMPTS = 4
_BUSY_RETRY_BASE_SLEEP = 0.05  # seconds; jittered backoff multiplier
_TRANSIENT_BUSY_TOKENS = (
    "database is locked",
    "database is busy",
    "locking protocol",
)
_OBSERVABILITY_ENABLED = os.environ.get("BW_DB_OBSERVABILITY", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

# Optional in-process serialization for callers with measured write contention.
_WRITE_LOCK = threading.Lock()


def integrity_check():
    """Report existing database integrity without replacing damaged data."""
    return sqlite_runtime.integrity_check(config.DATABASE_PATH)


@contextmanager
def write_serialized():
    """Queue writers in this process; transactions still need BEGIN IMMEDIATE."""
    _WRITE_LOCK.acquire()
    try:
        yield
    finally:
        _WRITE_LOCK.release()


class _ObservedConnection(sqlite3.Connection):
    """SQLite connection subclass that records query timing buckets."""

    def execute(self, sql, parameters=(), /):
        """Execute *sql* and record its elapsed time in the SQL timing bucket."""
        started = time.perf_counter()
        try:
            return super().execute(sql, parameters)
        finally:
            if _OBSERVABILITY_ENABLED:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                record_sql_timing(sql, elapsed_ms)

    def executemany(self, sql, seq_of_parameters, /):
        """Execute *sql* once per parameter set; record elapsed time as a single sample."""
        started = time.perf_counter()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            if _OBSERVABILITY_ENABLED:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                record_sql_timing(sql, elapsed_ms)

    def executescript(self, sql_script, /):
        """Execute a multi-statement SQL script and record total elapsed time."""
        started = time.perf_counter()
        try:
            return super().executescript(sql_script)
        finally:
            if _OBSERVABILITY_ENABLED:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                record_sql_timing(sql_script, elapsed_ms)


def _is_transient_busy_error(exc):
    """Return True if *exc* looks like a transient SQLite contention error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    msg = str(exc).lower()
    return any(token in msg for token in _TRANSIENT_BUSY_TOKENS)


def retry_on_busy(fn):
    """Retry read-only or idempotent operations after transient SQLite contention.

    Retrying a partially completed write can duplicate its effects. Writers
    should use BEGIN IMMEDIATE and the connection's busy timeout instead.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        """Re-run the call while SQLite reports contention, then give up."""
        last_exc = None
        retries = 0
        started = time.perf_counter()
        for attempt in range(_BUSY_RETRY_ATTEMPTS):
            try:
                result = fn(*args, **kwargs)
                if retries and _OBSERVABILITY_ENABLED:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    record_sqlite_retry(fn.__name__, retries, elapsed_ms, exhausted=False)
                return result
            except sqlite3.OperationalError as exc:
                if not _is_transient_busy_error(exc):
                    raise
                last_exc = exc
                if attempt == _BUSY_RETRY_ATTEMPTS - 1:
                    break
                retries += 1
                # Jittered exponential backoff: 50 ms, 100 ms, 200 ms …
                sleep_s = _BUSY_RETRY_BASE_SLEEP * (2 ** attempt)
                sleep_s *= 0.5 + random.random()
                time.sleep(sleep_s)
        if _OBSERVABILITY_ENABLED:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            record_sqlite_retry(fn.__name__, retries, elapsed_ms, exhausted=True)
        raise last_exc
    return wrapper


class _SafeRow(sqlite3.Row):
    """SQLite rows with the dict-style get method used by application helpers."""

    def get(self, key, default=None):
        """Return self[key] or *default* without raising."""
        try:
            return self[key]
        except (KeyError, IndexError):
            return default


def get_db():
    """Open an existing database; only init_db may create fresh storage."""
    return sqlite_runtime.connect(config.DATABASE_PATH, prefix="BW",
                                  factory=_ObservedConnection, row_factory=_SafeRow)


def get_db_observability_snapshot():
    """Return current database observability counters and timing buckets."""
    return get_observability_snapshot()


@contextmanager
def get_db_context():
    """Context manager that yields a database connection and guarantees close.

    Usage::

        with get_db_context() as conn:
            conn.execute("SELECT ...")
    """
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def create_consistent_backup(dest_path):
    """Publish a private, consistent snapshot after verifying its integrity."""
    from sqlite_snapshot import snapshot
    return snapshot(config.DATABASE_PATH, dest_path)
