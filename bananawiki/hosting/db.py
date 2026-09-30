"""Database access for the hosting portal.

``db`` works like :data:`bananawiki.wiki.db.db`: inside a request it uses one
connection for the whole request (opened lazily, closed at teardown); the
maintenance loop and scripts wrap their work in :func:`connection_scope`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from flask import current_app, g, has_app_context

from ..core.sqlite import Database, Session
from . import migrations

EXTENSION = "bananawiki.hosting.database"
_thread = threading.local()


def open_database(cfg: Any) -> Database:
    return Database(
        cfg.database_path,
        application_id=migrations.APPLICATION_ID,
        migrations=migrations.MIGRATIONS,
        baseline=migrations.BASELINE,
        bootstrap=migrations.bootstrap_for(cfg.default_signup_mode),
        env_prefix="HOSTING",
        busy_timeout_ms=cfg.db_busy_timeout_ms,
    )


def get_database() -> Database:
    return current_app.extensions[EXTENSION]


def _session() -> Session:
    scoped = getattr(_thread, "session", None)
    if scoped is not None:
        return scoped
    if not has_app_context():
        raise RuntimeError("Database access needs an application context or connection_scope().")
    session = g.get("_hosting_db_session")
    if session is None:
        session = Session(get_database().connect())
        g._hosting_db_session = session
    return session


def close_request_session(_exc: BaseException | None = None) -> None:
    session = g.pop("_hosting_db_session", None)
    if session is not None:
        if session.conn.in_transaction:
            session.conn.rollback()
        session.conn.close()


@contextmanager
def connection_scope(database: Database | None = None) -> Iterator[Session]:
    """Give the current thread its own connection (maintenance, CLI)."""
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
    def __getattr__(self, name: str) -> Any:
        return getattr(_session(), name)


db: Session = _DbProxy()  # type: ignore[assignment]
