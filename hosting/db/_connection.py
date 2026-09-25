"""SQLite connection factory for the hosting platform database."""

import contextlib
import sqlite3

import sqlite_runtime

from .. import config


class _SafeRow(sqlite3.Row):
    """sqlite3.Row subclass with dict-like .get() method.

    ``sqlite3.Row`` does not support ``.get(key, default)``, so every
    function that receives a Row and wants safe attribute access must
    either convert to ``dict(...)`` or catch ``KeyError``.  This subclass
    adds ``.get()`` so neither is needed: eliminating an entire class of
    ``AttributeError: 'sqlite3.Row' object has no attribute 'get'`` bugs
    that have repeatedly surfaced in production.
    """

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default


def get_hosting_db():
    """Open the existing platform database with the shared storage policy."""
    return sqlite_runtime.connect(config.HOSTING_DATABASE_PATH, prefix="HOSTING",
                                  row_factory=_SafeRow)


@contextlib.contextmanager
def get_hosting_db_context():
    """Context manager that yields a hosting DB connection and closes it."""
    conn = get_hosting_db()
    try:
        yield conn
    finally:
        conn.close()


def create_consistent_db_copy(src_path, dest_path):
    """Publish a private, consistent snapshot after verifying its integrity."""
    from sqlite_snapshot import snapshot
    return snapshot(src_path, dest_path)
