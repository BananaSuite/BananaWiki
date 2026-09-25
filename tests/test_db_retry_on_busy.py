"""Tests for the SQLite-busy retry helper.

These tests verify that :func:`db.retry_on_busy` transparently retries
:class:`sqlite3.OperationalError("database is locked")` (and friends) and
gives up after a small number of attempts.  The intent is that transient
contention errors no longer surface to user code as random 500s.
"""

import sqlite3

import pytest

import db
from db import _connection


def test_retry_on_busy_succeeds_after_a_few_failures(monkeypatch):
    """retry_on_busy retries until the wrapped fn succeeds."""
    # Make backoff effectively zero so the test runs quickly.
    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)

    calls = {"n": 0}

    @db.retry_on_busy
    def flaky():
        """Fail twice then succeed."""
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_on_busy_gives_up_and_reraises(monkeypatch):
    """retry_on_busy re-raises the original error after exhausting attempts."""
    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)
    monkeypatch.setattr(_connection, "_BUSY_RETRY_ATTEMPTS", 3)

    @db.retry_on_busy
    def always_locked():
        """Always raise a transient busy error."""
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        always_locked()


def test_retry_on_busy_does_not_swallow_other_errors(monkeypatch):
    """Non-busy OperationalErrors should propagate on the first try."""
    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)

    calls = {"n": 0}

    @db.retry_on_busy
    def broken_sql():
        """Raise a non-transient OperationalError."""
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        broken_sql()
    assert calls["n"] == 1  # no retry for non-transient errors


def test_get_site_settings_is_retry_wrapped():
    """get_site_settings is wrapped so transient locks don't break /."""
    # The decorated function carries the inner __wrapped__ attribute via
    # functools.wraps, so we can sanity-check that the decorator was
    # actually applied.
    assert hasattr(db.get_site_settings, "__wrapped__"), (
        "db.get_site_settings must be wrapped by retry_on_busy "
        "so transient SQLITE_BUSY errors do not cause random 500s on /"
    )


# Every entry below names a ``db.*`` function that runs on essentially
# every request (``before_request_hook`` + ``inject_globals``).  Without
# the ``retry_on_busy`` wrapper, a single ``database is locked`` from a
# concurrent writer (periodic cleanup, nightly backup, another worker
# committing a page edit) would surface to the user as a 500. The
# "random 500 that goes away after a reload" pattern.
@pytest.mark.parametrize(
    "fn_name",
    [
        "get_user_by_id",         # every authenticated request (get_current_user)
        "get_user_by_username",   # login flow
        "list_plugins",           # every request (plugin-path gating)
        "list_categories",        # every page render (sidebar)
        "get_category_tree",      # every page render (sidebar)
        "get_active_announcements",  # every page render with announcements plugin
        "get_user_permissions",   # every page render for non-admin users (sidebar filter)
        "get_user_accessibility", # every authenticated request (interface language resolution)
    ],
)
def test_hot_path_read_is_retry_wrapped(fn_name):
    """Each hot-path read must be wrapped by ``retry_on_busy``."""
    fn = getattr(db, fn_name)
    assert hasattr(fn, "__wrapped__"), (
        f"db.{fn_name} must be wrapped by retry_on_busy so transient "
        f"SQLITE_BUSY errors do not surface as a 500 to the user."
    )


def test_hot_path_reads_recover_after_transient_busy(monkeypatch):
    """A single transient SQLITE_BUSY in a wrapped read must auto-recover.

    End-to-end sanity check for the wrappers above: simulate the lock
    clearing after one retry and assert the wrapped function still
    returns the underlying result instead of bubbling the
    OperationalError.
    """
    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)

    calls = {"n": 0}
    original = db.list_plugins.__wrapped__

    def flaky_inner(*args, **kwargs):
        """Fail once, then delegate to the real implementation."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    # Re-wrap so the retry decorator sees the flaky inner.
    flaky = db.retry_on_busy(flaky_inner)
    monkeypatch.setattr(db, "list_plugins", flaky)

    # Should not raise: the first attempt fails, the second succeeds.
    result = db.list_plugins()
    assert calls["n"] == 2
    # ``list_plugins`` returns either rows or an empty list depending on
    # whether the schema has been initialised in this test context; we
    # only care that no exception escaped the retry wrapper.
    assert result is not None or result == []
