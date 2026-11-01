"""Schema version 16: canvases indexed in the order of the canvas lists.

``idx_canvas_layouts_updated`` serves ``ORDER BY updated_at DESC, id DESC``,
the order of the canvas lists, so the lists and the page builder's embed
picker read canvases in batches (each one continuing after the last canvas
of the one before) without sorting the whole table for every batch.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "canvas__layouts"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_canvas_layouts_updated ON canvas__layouts(updated_at, id)")
