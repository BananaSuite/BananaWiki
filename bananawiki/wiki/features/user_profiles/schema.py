"""Custom profile field tables.

1.4 created these only when the ``user_profiles`` plugin loaded; 1.6 creates
them for every database so the data layer never depends on a feature switch.
"""

from __future__ import annotations

import sqlite3


def upgrade_v4(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_profile_fields__definitions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, label TEXT NOT NULL, "
        "field_type TEXT NOT NULL DEFAULT 'text', sort_order INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_profile_fields__values ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, field_id INTEGER NOT NULL, "
        "value TEXT NOT NULL DEFAULT '', visible INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "FOREIGN KEY (field_id) REFERENCES user_profile_fields__definitions(id) ON DELETE CASCADE, "
        "UNIQUE(user_id, field_id))"
    )
    # 1.4 declared no foreign key on user_id, so values of deleted accounts linger.
    conn.execute(
        "DELETE FROM user_profile_fields__values WHERE user_id NOT IN (SELECT id FROM users)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS user_profile_fields_values_user_deleted AFTER DELETE ON users BEGIN "
        "DELETE FROM user_profile_fields__values WHERE user_id = old.id; END"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_custom_tags_user ON user_custom_tags(user_id, sort_order)"
    )
