"""Version 4: keep unfinished tenant initialization out of recovery.

The legacy status constraint is preserved; a pending initialization reserves
a stopped row until the runtime operation completes. Existing wikis are ready.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns


def upgrade(conn: sqlite3.Connection) -> None:
    add_columns(conn, "instances", {
        "provisioning_state": "TEXT NOT NULL DEFAULT 'ready' "
        "CHECK (provisioning_state IN ('pending', 'ready', 'failed'))",
    })
