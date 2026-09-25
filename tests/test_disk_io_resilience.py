"""Tests for resilience to transient SQLite ``disk I/O error``.

Regression coverage for the production incident where Hetzner Cloud
volume stalls (and similar brief storage hiccups) surfaced as
``sqlite3.OperationalError: disk I/O error`` and turned every request
into a 500 -- including the styled 500 page itself, because
``inject_globals`` re-issues a DB read for ``current_user``.

The three layers being verified here:

1. :func:`db.retry_on_busy` retries lock contention but propagates
   storage errors immediately: an I/O failure does not establish whether
   a partially completed write is safe to repeat.
2. :func:`helpers.get_current_user` catches a post-retry
   ``OperationalError`` and degrades the visitor to anonymous instead
   of letting the exception bubble up as a 500.
3. The 500 error handler in :mod:`routes.errors` renders a minimal
   hard-coded HTML page when even the styled template can't render
   (e.g. because the DB-backed context processor crashes), so the
   visitor sees a recognisable error page rather than gunicorn's bare
   ``Internal Server Error`` body.
"""

import sqlite3

import pytest

import db
from db import _connection


def test_retry_on_busy_does_not_repeat_disk_io_failure(monkeypatch):
    """An I/O failure must not cause an operation to run a second time."""
    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)
    calls = {"n": 0}

    @db.retry_on_busy
    def flaky():
        """Simulate a storage error after work may already have occurred."""
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("disk I/O error")
        return "ok"

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        flaky()
    assert calls["n"] == 1


def test_retry_on_busy_disk_io_error_eventually_gives_up(monkeypatch):
    """If the storage stays unhealthy, retry_on_busy still gives up."""
    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)
    monkeypatch.setattr(_connection, "_BUSY_RETRY_ATTEMPTS", 3)

    @db.retry_on_busy
    def always_bad():
        """Simulate persistent disk failure."""
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        always_bad()


def test_retry_on_busy_does_not_swallow_real_io_errors(monkeypatch):
    """Non-transient OperationalErrors must still propagate immediately."""
    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)
    calls = {"n": 0}

    @db.retry_on_busy
    def broken_table():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: definitely_not_real")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        broken_table()
    assert calls["n"] == 1  # no retries for non-transient errors


def test_failed_scheduler_claims_do_not_authorize_background_work():
    with db.get_db_context() as connection:
        connection.execute("CREATE TRIGGER reject_scheduler_claim BEFORE UPDATE ON site_settings BEGIN SELECT RAISE(ABORT, 'fixture storage failure'); END")
        connection.commit()
    assert db.check_and_set_backup_sent(0) is False
    assert db.check_and_claim_chat_cleanup(0) is False


def test_get_current_user_returns_none_on_persistent_db_failure(monkeypatch, client):
    """If db.get_user_by_id keeps failing, the request degrades to anonymous.

    The retry layer is already exercised above; this test verifies the
    *final* safety net inside :func:`helpers.get_current_user`.
    """
    import db as db_mod
    from werkzeug.security import generate_password_hash
    db_mod.create_user("admin", generate_password_hash("admin123"), role="admin")
    db_mod.update_site_settings(setup_done=1)
    rv = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert rv.status_code in (200, 302), rv.data[:200]

    def boom(*_a, **_kw):
        """Simulate a Hetzner volume stall long enough to exhaust retries."""
        raise sqlite3.OperationalError("disk I/O error")

    # Patch the *underlying* DB function -- the retry wrapper around it
    # will still be invoked, but every retry will fail too, so the
    # OperationalError reaches helpers.get_current_user where the
    # belt-and-braces ``except`` should catch it.
    monkeypatch.setattr(db_mod, "get_user_by_id", boom)

    # The home page should still render (as anonymous) rather than 500.
    rv = client.get("/")
    assert rv.status_code != 500, rv.data[:300]


def test_get_current_user_swallows_db_error_inside_request_context(monkeypatch, client):
    """The safety net handles the DB error path even with a session set.

    Hits the function directly inside a synthetic request context with a
    ``user_id`` in the session so the DB lookup path is actually taken,
    then verifies that a persistent OperationalError is swallowed and
    None is returned.
    """
    import db as db_mod
    from helpers._auth import get_current_user

    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db_mod, "get_user_by_id", boom)

    from app import app as flask_app
    with flask_app.test_request_context("/"):
        from flask import session as flask_session
        flask_session["user_id"] = "00000000"  # arbitrary -- DB call will raise
        try:
            result = get_current_user()
        except sqlite3.OperationalError:
            pytest.fail("OperationalError must not escape get_current_user()")
    assert result is None


def test_error_handler_falls_back_when_template_render_fails(monkeypatch, client):
    """If render_template itself crashes, we still return styled HTML.

    Simulates the production failure mode: the DB is unreachable -> the
    styled 500.html template's context processor raises -> we must not
    let gunicorn emit its bare ``Internal Server Error`` body.
    """
    import db as db_mod
    from werkzeug.security import generate_password_hash
    db_mod.create_user("admin", generate_password_hash("admin123"), role="admin")
    db_mod.update_site_settings(setup_done=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    import routes.errors as errors_mod

    def boom_render(*_a, **_kw):
        """Pretend the template engine itself failed."""
        raise RuntimeError("template engine exploded")

    monkeypatch.setattr(errors_mod, "render_template", boom_render)

    # Hitting an unknown URL routes through the 404 handler, which now
    # calls ``_safe_render`` -- the patched ``render_template`` will
    # detonate, and we verify the handler falls back to the minimal
    # HTML response instead of letting the visitor see a bare gunicorn
    # body.
    rv = client.get("/this-page-does-not-exist-please-404-me")
    assert rv.status_code == 404, rv.data[:300]
    body = rv.data.decode("utf-8", errors="replace")
    assert "Return home" in body, body[:400]
    assert "<html" in body.lower()
    assert "404" in body


def test_minimal_error_response_has_no_db_or_template_deps():
    """The fallback HTML must be a pure string with no external lookups.

    Defence-in-depth: if someone later refactors the fallback to use
    ``url_for`` or pull a string from the DB, the whole point of the
    fallback collapses.  This test pins that down.
    """
    from routes.errors import _minimal_error_response

    body, status, headers = _minimal_error_response(
        500, "Internal server error", "Something went wrong.",
    )
    assert status == 500
    assert headers["Content-Type"].startswith("text/html")
    assert "Internal server error" in body
    assert "Something went wrong." in body
    # Must not embed any URL helpers / template syntax that would
    # require Flask app context to resolve.
    assert "{{" not in body and "{%" not in body
