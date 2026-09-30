"""Schema additions of the sign-in feature (run by migration 4)."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns


def upgrade_v4(conn: sqlite3.Connection) -> None:
    # Why a session ended, so the single-session limit can explain itself
    # ("signed in somewhere else") instead of a generic "session expired".
    add_columns(conn, "user_sessions", {"revoked_reason": "TEXT"})
    # Hosting-portal accounts linked to wiki accounts, keyed by the portal's
    # stable account id so renames on either side do not break the link.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS platform_oauth_links ("
        "account_id TEXT PRIMARY KEY, "
        "user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE, "
        "portal_username TEXT NOT NULL DEFAULT '', "
        "linked_at TEXT NOT NULL)"
    )
