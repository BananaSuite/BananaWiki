"""Schema additions of the page builder."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns


def upgrade_v4(conn: sqlite3.Connection) -> None:
    """Draft base revisions (1.4 drafts: 0, like 1.4 pages) and the site's saved sections."""
    add_columns(conn, "page_builder_drafts", {"base_revision": "INTEGER NOT NULL DEFAULT 0"})
    conn.execute(
        "CREATE TABLE IF NOT EXISTS page_builder_sections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, builder_json TEXT NOT NULL, "
        "created_by TEXT REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL)"
    )
