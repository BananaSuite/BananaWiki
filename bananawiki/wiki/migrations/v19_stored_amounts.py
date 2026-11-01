"""Schema version 19: amounts in the shared rate-limit counters.

``rate_limit_hits.amount`` lets one row count more than one hit, so a
budget of an amount (the bytes an account stores in Kanban boards or
canvases a day, see :meth:`bananawiki.core.ratelimit.SqlLimiter.reserve_amount`)
is counted exactly in a row a minute instead of a row per unit. Every other
counter keeps one row per hit, with the default ``1``.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "rate_limit_hits"):
        add_columns(conn, "rate_limit_hits", {"amount": "INTEGER NOT NULL DEFAULT 1"})
