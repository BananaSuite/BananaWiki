# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Schema version 11: kanban columns that count as done.

``kanban_columns.is_done``: 1 for a column whose tickets are finished work, so
their due dates no longer make them overdue or due soon (on the board, in the
due-date filters and on "My tickets"). It is a column setting, off (0) for
every existing and new column until someone turns it on.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "kanban_columns"):
        add_columns(conn, "kanban_columns", {"is_done": "INTEGER NOT NULL DEFAULT 0"})
