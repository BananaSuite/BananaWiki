# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Schema version 8: issued tokens and accountable conversations.

1. ``api_service__tokens.parent_id``: the token that issued a token through
   the API (``POST /tokens``). Revoking a token revokes the tokens it issued;
   earlier tokens have none.
2. ``chats.cleared_upto_user1`` / ``cleared_upto_user2``: the newest message
   each participant cleared from their own view of a direct conversation
   (0: nothing), so clearing hides it for that participant only.
3. ``chat__reports``: messages reported to the administrators, with a copy of
   the reported text so a report outlives the message.

Existing conversations start uncleared (0) for both participants. The DDL is
frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "api_service__tokens"):
        add_columns(conn, "api_service__tokens", {"parent_id": "INTEGER"})
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_service_tokens_parent ON api_service__tokens(parent_id)")
    if table_exists(conn, "chats"):
        add_columns(conn, "chats", {
            "cleared_upto_user1": "INTEGER NOT NULL DEFAULT 0",
            "cleared_upto_user2": "INTEGER NOT NULL DEFAULT 0",
        })
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat__reports ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "kind TEXT NOT NULL, "  # dm | group
        "parent_id INTEGER NOT NULL, "  # chats.id or group_chats.id
        "message_id INTEGER NOT NULL, "
        "reporter_id TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "reported_user_id TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "content TEXT NOT NULL DEFAULT '', "
        "attachment_names TEXT NOT NULL DEFAULT '', "
        "message_created_at TEXT, "
        "status TEXT NOT NULL DEFAULT 'open', "  # open | resolved
        "created_at TEXT NOT NULL, "
        "resolved_by TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "resolved_at TEXT, "
        "UNIQUE (kind, message_id, reporter_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_reports_status ON chat__reports(status, id)")
