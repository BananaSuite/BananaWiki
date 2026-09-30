"""Database access for the wiki.

``db`` is the object feature code uses::

    from bananawiki.wiki.db import db

    page = db.one("SELECT * FROM pages WHERE slug = ?", (slug,))
    with db.transaction():
        db.execute("UPDATE pages SET title = ? WHERE id = ?", (title, page_id))

Inside a request it uses one connection for the whole request (opened lazily,
closed at teardown). Background jobs and CLI commands wrap their work in
:func:`connection_scope`, which does the same for the current thread.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from flask import current_app, g, has_app_context

from ..core.sqlite import Database, Session

_thread = threading.local()


def get_database() -> Database:
    return current_app.extensions["bananawiki.database"]


def _session() -> Session:
    scoped = getattr(_thread, "session", None)
    if scoped is not None:
        return scoped
    if not has_app_context():
        raise RuntimeError("Database access needs an application context or connection_scope().")
    session = g.get("_db_session")
    if session is None:
        session = Session(get_database().connect())
        g._db_session = session
    return session


def close_request_session(_exc: BaseException | None = None) -> None:
    session = g.pop("_db_session", None)
    if session is not None:
        if session.conn.in_transaction:
            session.conn.rollback()
        session.conn.close()


@contextmanager
def connection_scope(database: Database | None = None) -> Iterator[Session]:
    """Give the current thread its own connection (background jobs, CLI)."""
    previous = getattr(_thread, "session", None)
    database = database or get_database()
    session = Session(database.connect())
    _thread.session = session
    try:
        yield session
    finally:
        _thread.session = previous
        if session.conn.in_transaction:
            session.conn.rollback()
        session.conn.close()


class _DbProxy:
    """Forwards to the connection that belongs to the current request or thread."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_session(), name)

    @property
    def session(self) -> Session:
        return _session()


db: Session = _DbProxy()  # type: ignore[assignment]
