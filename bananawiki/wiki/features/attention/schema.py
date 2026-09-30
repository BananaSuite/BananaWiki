"""Schema additions of the attention feature (run by migration 4)."""

from __future__ import annotations

import sqlite3

from ....core import attention
from ....core.sqlite import add_columns

# Indexes that keep every queue count a single indexed lookup.
_QUEUE_INDEXES = {
    "idx_users_pending_approval": "users(created_at) WHERE approval_status = 'pending'",
    "idx_reservation_quota_requests_status": "reservation_quota_requests(status, created_at)",
    "idx_contribution_quota_requests_status": "contribution_quota_requests(status, created_at)",
    "idx_account_merge_requests_status": "account_merge_requests(status, created_at)",
    "idx_pages_pending_deletion": "pages(pending_deletion_at) WHERE pending_deletion = 1",
}


def upgrade_v4(conn: sqlite3.Connection) -> None:
    # An optional address for notifications (1.4 had no email at all) and the
    # person's own choices about which emails they want.
    add_columns(conn, "users", {
        "email": "TEXT",
        "attention_emails": "INTEGER NOT NULL DEFAULT 1",
        "decision_emails": "INTEGER NOT NULL DEFAULT 1",
    })
    add_columns(conn, "site_settings", {
        "attention_email_enabled": "INTEGER NOT NULL DEFAULT 0",
        "attention_email_mode": "TEXT NOT NULL DEFAULT 'digest'",
        "attention_email_interval_minutes": "INTEGER NOT NULL DEFAULT 60",
        "attention_email_daily_hour": "INTEGER NOT NULL DEFAULT 8",
        "decision_email_enabled": "INTEGER NOT NULL DEFAULT 0",
        "public_base_url": "TEXT NOT NULL DEFAULT ''",
        "mail_provider": "TEXT NOT NULL DEFAULT ''",
        "mail_from": "TEXT NOT NULL DEFAULT ''",
        "mail_reply_to": "TEXT NOT NULL DEFAULT ''",
        "mail_api_key": "TEXT NOT NULL DEFAULT ''",
        "mail_smtp_host": "TEXT NOT NULL DEFAULT ''",
        "mail_smtp_port": "INTEGER NOT NULL DEFAULT 587",
        "mail_smtp_security": "TEXT NOT NULL DEFAULT 'starttls'",
        "mail_smtp_username": "TEXT NOT NULL DEFAULT ''",
        "mail_smtp_password": "TEXT NOT NULL DEFAULT ''",
    })
    attention.create_tables(conn, "attention_events", "attention_recipients")
    # Decisions on someone's own request ("your quota request was approved").
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_notices ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "source_id TEXT NOT NULL, outcome TEXT NOT NULL, object_id TEXT NOT NULL DEFAULT '', "
        "endpoint TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, dismissed_at TEXT, emailed_at TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_notices_user ON user_notices(user_id, dismissed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_notices_email ON user_notices(emailed_at, created_at)")
    for name, target in _QUEUE_INDEXES.items():
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
