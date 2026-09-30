"""Indexes the messaging feature needs on top of the 1.4 tables."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import table_exists

_INDEXES = {
    "idx_group_messages_sender": "group_messages(sender_id)",
    "idx_chat_attachments_created": "chat_attachments(created_at)",
    "idx_group_attachments_created": "group_attachments(created_at)",
    "idx_chat_messages_created": "chat_messages(created_at)",
    "idx_group_messages_created": "group_messages(created_at)",
}


def upgrade_v4(conn: sqlite3.Connection) -> None:
    """Per-sender daily attachment limits and retention cleanup look rows up by these columns.

    The global room is created here, so opening ``/groups/global`` (a GET)
    never has to write.
    """
    for name, target in _INDEXES.items():
        if table_exists(conn, target.split("(", 1)[0]):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    if table_exists(conn, "group_chats") and conn.execute(
            "SELECT 1 FROM group_chats WHERE is_global = 1 LIMIT 1").fetchone() is None:
        from .groups import GLOBAL_NAME, new_invite_code

        conn.execute("INSERT INTO group_chats (name, invite_code, is_global, created_at) "
                     "VALUES (?, ?, 1, strftime('%Y-%m-%d %H:%M:%S', 'now'))", (GLOBAL_NAME, new_invite_code()))
