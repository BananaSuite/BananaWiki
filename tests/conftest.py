"""Shared fixtures for the BananaWiki 1.6 test suite.

* ``app``      - a fresh wiki in a temporary instance directory, setup done.
* ``client``   - a test client for that wiki (CSRF checks off; see ``csrf_client``).
* ``make_user``- create accounts: ``make_user("bob", role="editor")``.
* ``login``    - sign a client in: ``login(client, user)``.
* ``db``       - run queries against the wiki database inside the test.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
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


@pytest.fixture
def app_factory(tmp_path):
    """Build extra wikis (or the same one twice) with custom environment variables."""

    def build(*, setup_done: bool = True, **overrides: Any):
        application = create_app(make_config(tmp_path, **overrides))
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
