"""Shared fixtures for the test suite.

* ``app``      - a fresh wiki in a temporary instance directory, setup done.
* ``client``   - a test client for that wiki (CSRF checks off; see ``csrf_client``).
* ``make_user``- create accounts: ``make_user("bob", role="editor")``.
* ``login``    - sign a client in: ``login(client, user)``.
* ``db``       - run queries against the wiki database inside the test.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bananawiki.wiki.app import create_app
from bananawiki.wiki.config import load_config
from bananawiki.wiki.db import connection_scope

PASSWORD = "correct horse battery"
_counter = itertools.count(1)


def make_config(tmp_path, **overrides: Any):
    environ = {
        "BW_ENV": "test",
        "BW_INSTANCE_DIR": str(tmp_path / "instance"),
        "BW_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000",
        "BW_SETUP_TOKEN": "test-setup-token",
        "BW_BACKGROUND_JOBS": "0",
    }
    environ.update({k: str(v) for k, v in overrides.pop("environ", {}).items()})
    return load_config(environ, secret_key="test-secret-key-" + "x" * 32, **overrides)


def _probe_blueprint():
    """Routes that exercise the request pipeline without depending on a feature."""
    from flask import Blueprint

    from bananawiki.wiki import auth

    bp = Blueprint("probe", __name__)

    @bp.route("/_probe/private", methods=["GET", "POST"])
    def private():
        return "private"

    @bp.route("/_probe/public-read")
    @auth.public_read
    def public_read():
        return "public-read"

    @bp.route("/_probe/admin")
    @auth.admin_required
    def admin_only():
        return "admin"

    return bp


def _cache_url_builders() -> None:
    """Compile each URL rule's builder once per test process, not once per wiki.

    Werkzeug turns every rule into Python code when an application is made,
    the larger half of making one (and most tests make one). The code takes
    the rule as an argument and depends only on its parsed form and defaults,
    so wikis with the same routes can share it. Tests only.
    """
    from werkzeug.routing import rules

    original = rules.Rule._compile_builder
    compiled: dict[Any, Any] = {}

    def cached(self, append_unknown: bool = True):
        try:
            key = (self.rule, tuple(self._trace), repr(sorted((self.defaults or {}).items())), append_unknown)
            hash(key)
        except TypeError:
            return original(self, append_unknown)
        builder = compiled.get(key)
        if builder is None:
            builder = compiled[key] = original(self, append_unknown)
        return builder

    rules.Rule._compile_builder = cached


_cache_url_builders()


# A new wiki's database, made once per test process for each set of overrides and copied into the
# instance folder of every later wiki built with the same overrides: creating it (every migration since
# the 1.4 schema) was a third of the time of a typical test.
_DATABASE_TEMPLATES: dict[str, bytes] = {}


def _template_key(overrides: dict[str, Any]) -> str | None:
    try:
        return repr(sorted((key, repr(value)) for key, value in overrides.items()))
    except TypeError:
        return None


@pytest.fixture
def app_factory(tmp_path):
    """Build extra wikis (or the same one twice) with custom environment variables."""

    def build(*, setup_done: bool = True, **overrides: Any):
        config = make_config(tmp_path, **overrides)
        database = Path(config.database_path)
        key = _template_key(overrides)
        fresh = not database.exists()
        if fresh and key in _DATABASE_TEMPLATES:
            database.parent.mkdir(parents=True, exist_ok=True)
            database.write_bytes(_DATABASE_TEMPLATES[key])
            Path(str(database) + ".initialized").write_bytes(b"BananaSuite SQLite initialized\n")
        application = create_app(config)
        if fresh and key is not None and key not in _DATABASE_TEMPLATES:
            with sqlite3.connect(database) as source:
                copy = sqlite3.connect(":memory:")
                source.backup(copy)
                _DATABASE_TEMPLATES[key] = copy.serialize()
                copy.close()
        application.config["CSRF_DISABLED"] = True
        application.register_blueprint(_probe_blueprint())
        if setup_done:
            with application.app_context(), connection_scope() as session:
                session.execute("UPDATE site_settings SET setup_done = 1 WHERE id = 1")
        return application

    return build


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf_client(app):
    """A client with CSRF enforcement switched back on."""
    app.config["CSRF_DISABLED"] = False
    return app.test_client()


@pytest.fixture
def db(app) -> Iterator[Any]:
    """A separate connection for arranging and checking data (autocommit)."""
    from bananawiki.core.sqlite import Session

    session = Session(app.extensions["bananawiki.database"].connect())
    try:
        yield session
    finally:
        session.conn.close()


@pytest.fixture
def make_user(app):
    def create(username: str | None = None, *, role: str = "user", password: str = PASSWORD, **extra: Any):
        from bananawiki.wiki import accounts

        name = username or f"user{next(_counter)}"
        with app.test_request_context(), connection_scope():
            return accounts.create(name, password, role=role, extra=extra or None, emit_event=False)

    return create


@pytest.fixture
def login():
    def sign_in(client, user: dict[str, Any], password: str = PASSWORD, remember: bool = False):
        response = client.post("/login", data={"username": user["username"], "password": password,
                                               "remember_me": "1" if remember else ""})
        assert response.status_code == 302, response.data[:500]
        return response

    return sign_in


@pytest.fixture
def frozen_limiter_clock(monkeypatch):
    """Stop the clock of the in-memory rate limiter (``MemoryLimiter``).

    A test that sends N+1 requests to see the last one refused relies on all
    of them landing in one window. On a loaded host N requests can take longer
    than the window itself, the oldest hits slide out and the last request is
    allowed. With the clock stopped the window never moves, so the test checks
    the limit, not the host's speed.
    """
    from types import SimpleNamespace

    from bananawiki.core import ratelimit

    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: 1_000.0))


@pytest.fixture
def admin(make_user):
    return make_user("admin_user", role="admin")


@pytest.fixture
def admin_client(client, admin, login):
    login(client, admin)
    return client


def csrf_token_from(response) -> str:
    match = re.search(rb'name="csrf-token" content="([^"]+)"', response.data)
    assert match, "page has no CSRF meta tag"
    return match.group(1).decode()
