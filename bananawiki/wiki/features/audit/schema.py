"""Schema additions of the audit log (run by migration 4)."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns


def upgrade_v4(conn: sqlite3.Connection) -> None:
    # One row per security-relevant action. ``actor_id`` and ``target_id``
    # are plain values, not foreign keys: entries must outlive the accounts,
    # pages and categories they describe.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL, "
        "actor_id TEXT, "
        "action TEXT NOT NULL, "
        "target_type TEXT, "
        "target_id TEXT, "
        "ip TEXT, "
        "details TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_id, created_at)")
    add_columns(conn, "site_settings", {"audit_log_retention_days": "INTEGER NOT NULL DEFAULT 365"})
