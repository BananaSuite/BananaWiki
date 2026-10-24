"""Version 10: main wikis.

``instances.is_main`` (0/1) marks the wikis administrators keep for the
platform: they never expire, are never terminated, reset or deleted with
their owner's account until an administrator takes the mark away, and start
first after an update or a reboot. ``main_since`` is when the mark was set.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns


def upgrade(conn: sqlite3.Connection) -> None:
    add_columns(conn, "instances", {"is_main": "INTEGER NOT NULL DEFAULT 0", "main_since": "TEXT"})
    conn.execute("CREATE INDEX IF NOT EXISTS ix_instances_main ON instances(is_main) WHERE is_main = 1")
