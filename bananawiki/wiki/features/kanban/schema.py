"""Schema additions for kanban on top of the 1.4 tables.

* indexes for the 1.6 queries;
* ``kanban_columns.wip_limit``: an optional work-in-progress limit per column
  (``NULL`` means no limit);
* ``kanban_ticket_checklist``: checklist items (subtasks) of a ticket;
* ``archived_at`` / ``archived_by`` on ``kanban_tickets`` and ``kanban_boards``:
  when (and by whom) a ticket or board was archived (``NULL``: active).
"""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns, table_exists

_INDEXES = {
    "idx_kanban_board_shares_target": "kanban_board_shares(share_type, target)",
    "idx_kanban_boards_creator": "kanban_boards(created_by)",
    "idx_kanban_events_created": "kanban_events(created_at)",
    "idx_kanban_board_history_board_id": "kanban_board_history(board_id, id)",
    "idx_kanban_ticket_checklist_ticket": "kanban_ticket_checklist(ticket_id, sort_order)",
    "idx_kanban_tickets_column_archived": "kanban_tickets(column_id, archived_at)",
}

_CHECKLIST = """
CREATE TABLE IF NOT EXISTS kanban_ticket_checklist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    text        TEXT    NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

_ARCHIVE = {"archived_at": "TEXT DEFAULT NULL", "archived_by": "TEXT DEFAULT NULL"}


def upgrade_v4(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "kanban_columns"):
        add_columns(conn, "kanban_columns", {"wip_limit": "INTEGER DEFAULT NULL"})
    if table_exists(conn, "kanban_tickets"):
        add_columns(conn, "kanban_tickets", _ARCHIVE)
        conn.execute(_CHECKLIST)
    if table_exists(conn, "kanban_boards"):
        add_columns(conn, "kanban_boards", _ARCHIVE)
    for name, target in _INDEXES.items():
        if table_exists(conn, target.split("(", 1)[0]):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
