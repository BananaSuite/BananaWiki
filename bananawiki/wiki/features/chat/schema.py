"""Indexes the messaging feature needs on top of the 1.4 tables."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import table_exists
from ....core.timeutil import sql_in

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


def upgrade_v5(conn: sqlite3.Connection) -> None:
    """Keep daily upload usage when a message, attachment or conversation is deleted."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat__upload_usage ("
        "source TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_upload_usage_user_created "
                 "ON chat__upload_usage(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_upload_usage_created ON chat__upload_usage(created_at)")
    cutoff = sql_in(days=-1)
    for kind, attachments, messages in (("dm", "chat_attachments", "chat_messages"),
                                        ("group", "group_attachments", "group_messages")):
        if table_exists(conn, attachments) and table_exists(conn, messages):
            # Existing files preserve the sliding 24-hour allowance during an
            # upgrade. Already-deleted uploads cannot be recovered from v4.
            conn.execute(
                f"INSERT OR IGNORE INTO chat__upload_usage (source, user_id, created_at) "
                f"SELECT ? || ':' || a.id, m.sender_id, a.created_at FROM {attachments} a "
                f"JOIN {messages} m ON m.id = a.message_id JOIN users u ON u.id = m.sender_id "
                "WHERE a.created_at >= ?", (kind, cutoff),
            )
