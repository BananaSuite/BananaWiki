"""Per-revision statistics for the contributor leaderboard.

One row per ``page_history`` entry, filled in by a background job: the size of
the revision and how many characters it added or removed compared with the
page's previous revision. The leaderboard then aggregates this small table
instead of reading every revision's content. Rows disappear with their
history entry (and so with their page).
"""

from __future__ import annotations

import sqlite3


def upgrade_v4(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leaderboard_revisions ("
        "history_id INTEGER PRIMARY KEY REFERENCES page_history(id) ON DELETE CASCADE, "
        "page_id INTEGER NOT NULL, size INTEGER NOT NULL, added INTEGER NOT NULL, removed INTEGER NOT NULL, "
        "is_first INTEGER NOT NULL, is_revert INTEGER NOT NULL, created_at TEXT NOT NULL, day TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leaderboard_revisions_page ON leaderboard_revisions(page_id, history_id)"
    )
