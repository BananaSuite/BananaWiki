"""Fixtures for the hosting portal tests.

* ``portal``   - a fresh portal (temporary hosting.db, FakeRuntime, CSRF off).
* ``runtime``  - the portal's :class:`FakeRuntime`.
* ``web``      - a test client for the portal.
* ``make_account`` / ``login`` / ``make_wiki`` - build state quickly.
* ``query``    - run SQL against hosting.db inside the portal's app context.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.runtime.fake import FakeRuntime

from .hosting_support import PASSWORD, build_portal, counter


@pytest.fixture
def portal(tmp_path):
    return build_portal(tmp_path)


@pytest.fixture
def runtime(portal) -> FakeRuntime:
    return portal.extensions["bananawiki.hosting.runtime"]


@pytest.fixture
def web(portal):
    return portal.test_client()


@pytest.fixture
def ctx(portal) -> Iterator[None]:
    """An app context with a database session, for calling services directly."""
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        yield


@pytest.fixture
def query(portal):
    def run(sql: str, params: tuple = (), *, one: bool = False) -> Any:
        with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
            from bananawiki.hosting.db import db

            if sql.lstrip().upper().startswith("SELECT"):
                return db.one(sql, params) if one else db.all(sql, params)
            db.execute(sql, params)
            return None

    return run


@pytest.fixture
def make_account(portal):
    def make(username: str | None = None, *, admin: bool = False, email: str = "", **fields: Any) -> dict[str, Any]:
        username = username or f"user{next(counter)}"
        with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
            from bananawiki.hosting import accounts
            from bananawiki.hosting.db import db

            account = accounts.create(username, PASSWORD, is_admin=admin, email=email)
            if fields:
                db.update("accounts", fields, "id = ?", (account["id"],))
            return accounts.get(account["id"])

    return make


@pytest.fixture
def login(portal):
    def sign_in(client, account: dict[str, Any], password: str = PASSWORD):
        response = client.post("/login", data={"username": account["username"], "password": password})
        assert response.status_code == 302, response.get_data(as_text=True)[:500]
        return response

    return sign_in


@pytest.fixture
def make_wiki(portal):
    def make(owner: dict[str, Any], slug: str | None = None, **kwargs: Any) -> dict[str, Any]:
        slug = slug or f"wiki{next(counter)}"
        with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
            from bananawiki.hosting import instances

            inst, _username, _password = instances.create(owner, slug, **kwargs)
            return inst

    return make
