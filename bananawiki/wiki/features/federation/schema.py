"""Federation schema additions (the tables themselves come from 1.4 schema version 3)."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns, table_exists


def upgrade_v4(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "federation_peers"):
        # Peers on a private network must be allowed explicitly (SSRF protection).
        add_columns(conn, "federation_peers", {"allow_private_network": "INTEGER NOT NULL DEFAULT 0"})
