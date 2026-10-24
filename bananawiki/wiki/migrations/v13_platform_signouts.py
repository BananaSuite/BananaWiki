"""Schema version 13: single sign-out from the hosting portal.

``platform_oauth_state`` (one row) remembers the last sign-out the wiki
fetched from the portal (``signout_cursor``) and when it last asked
(``signouts_checked_at``). The ``auth.platform_signouts`` job reads the
portal's ``/oauth/signouts`` after that id and ends the platform sessions of
the wiki accounts it names.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS platform_oauth_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "signout_cursor INTEGER NOT NULL DEFAULT 0, signouts_checked_at TEXT)"
    )
