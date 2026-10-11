# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Schema version 10: members block other members in chats.

1. ``chat__blocks``: one row per account and account it blocked (see
   :mod:`bananawiki.wiki.features.chat.blocks`). Deleting either account
   deletes the row; an account never blocks itself. Sending a message looks
   up who blocked the sender, hence the index.
2. ``chat_messages.withheld``: 1 for a direct message sent to someone who had
   blocked its sender. Only its sender (and the administrators) see it, so the
   sender is not told they were blocked; it is never delivered, also after an
   unblock.

Nothing changes for existing accounts and messages: nobody is blocked and
nothing is withheld (0). The DDL is frozen here: later schema changes belong
in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat__blocks ("
        "blocker_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "blocked_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "created_at TEXT NOT NULL, "
        "PRIMARY KEY (blocker_id, blocked_id), "
        "CHECK (blocker_id != blocked_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_blocks_blocked ON chat__blocks(blocked_id)")
    if table_exists(conn, "chat_messages"):
        add_columns(conn, "chat_messages", {"withheld": "INTEGER NOT NULL DEFAULT 0"})
