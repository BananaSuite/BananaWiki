"""Tests for ``inject_globals`` defensive side-query handling.

Regression coverage for the "random 500 that goes away after a page reload"
pattern.  ``inject_globals`` is invoked for every HTML response and runs a
handful of optional side-queries (badge counts, unread DMs, sidebar people,
beta invites, ...).  Previously, a transient failure in any one of those
queries: most commonly ``sqlite3.OperationalError: database is locked`` or
a single-feature glitch: turned the entire page into a 500 even though the
page itself was fine.

The fix wraps each side-query in :func:`app._safe_globals_call` so a
broken widget just falls back to its default and logs the error.  The
critical state (``settings``, ``current_user``, ``enabled_plugins``) is
still un-wrapped, because without those the request cannot meaningfully
be served at all.
"""

import sqlite3

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def logged_in_admin_with_setup(client):
    """A client logged in as admin on a fully-set-up site."""
    import db
    db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    rv = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert rv.status_code in (200, 302), rv.data[:200]
    return client


def test_transient_badge_query_failure_does_not_500(monkeypatch, logged_in_admin_with_setup):
    """A transient OperationalError in get_unnotified_badges must not 500 the page."""
    import db

    def boom(*_a, **_kw):
        """Simulate a transient SQLite contention error."""
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "get_unnotified_badges", boom)

    rv = logged_in_admin_with_setup.get("/")
    # We don't care which page (re)directs to which, just that it isn't
    # a 500 from the broken side-query.
    assert rv.status_code != 500, rv.data[:200]


def test_transient_unread_dm_failure_does_not_500(monkeypatch, logged_in_admin_with_setup):
    """A glitch in the chat unread counter must not break the wiki home page."""
    import app as app_mod

    def boom(*_a, **_kw):
        """Simulate a transient SQLite contention error."""
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app_mod, "get_request_unread_dm_count", boom)

    rv = logged_in_admin_with_setup.get("/")
    assert rv.status_code != 500, rv.data[:200]


