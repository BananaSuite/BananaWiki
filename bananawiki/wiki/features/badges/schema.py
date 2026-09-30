"""Reading time tracked for the ``reading_time`` badge trigger (new in 1.6)."""

from __future__ import annotations

import sqlite3


def upgrade_v4(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS badge_reading_time ("
        "user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
        "seconds INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    # The retired "easter_egg" trigger becomes a manual badge.
    conn.execute("UPDATE badge_types SET trigger_type = '', auto_trigger = 0 WHERE trigger_type = 'easter_egg'")
