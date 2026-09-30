"""Schema version 3: the 1.6 hosting portal takes over a 1.4 ``hosting.db``.

Everything here is idempotent and keeps 1.4 data. In order:

1. Repair dangling references. 1.4 wrote placeholder values such as ``''``
   or ``'system:storage-quota'`` into columns that reference ``accounts``,
   which would make every later integrity check fail. Nullable references
   become NULL; rows whose required parent is gone are removed, as
   ``ON DELETE CASCADE`` would have done.
2. Store every timestamp in one shape (``YYYY-MM-DD HH:MM:SS``, UTC) so text
   comparisons are correct. 1.4 wrote ISO ``…T…+00:00`` values.
3. Drop OAuth authorisation codes and access tokens issued by 1.4: codes are
   now stored as digests and access tokens are opaque, so the old rows can
   never match again (they lived for five minutes and one hour).
4. Add the columns 1.6 needs: PKCE on authorisation codes, the encrypted
   OAuth client secret of each wiki (so a restart can hand it to the wiki
   again) and the optional two-step requirement for administrators.
5. Add indexes for the lookups the portal does on every request.
6. Add the "needs your attention" state: throttling tables for administrator
   emails, notices about decisions for owners, each account's email opt-out
   and language, and the notification schedule (the 1.4 digest interval in
   hours becomes minutes).

The table ``instances(subdomain, domain_mode, status)`` keeps its shape: the
1.4 updater reads it to check that tenants came back after an update.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from ...core import attention
from ...core.sqlite import add_columns, column_names, quote_identifier, table_exists, tuples

log = logging.getLogger("bananawiki.hosting.migrations")

_TIMESTAMP_COLUMN = re.compile(r"(_at|_until)$")

_INDEXES = {
    "ix_instances_account": "instances(account_id, status)",
    "ix_instances_status": "instances(status, expires_at)",
    "ix_instances_oauth_client": "instances(oauth_client_id)",
    "ix_accounts_approval": "accounts(approval_status, created_at)",
    "ix_account_suspension_audit_account": "account_suspension_audit(account_id, created_at)",
    "ix_instance_suspension_audit_instance": "instance_suspension_audit(instance_id, created_at)",
    "ix_hosting_rate_limit_hits_time": "hosting_rate_limit_hits(hit_at)",
    "ix_hosting_account_sessions_expiry": "hosting_account_sessions(expires_at)",
    "ix_oauth_auth_codes_expiry": "hosting_oauth_authorization_codes(expires_at)",
    "ix_oauth_links_wiki_user": "hosting_oauth_account_links(instance_id, wiki_user_id)",
    "ix_impersonation_logs_admin": "hosting_impersonation_logs(admin_account_id)",
}


def upgrade(conn: sqlite3.Connection) -> None:
    repair_dangling_references(conn)
    normalize_timestamps(conn)
    drop_legacy_oauth_grants(conn)
    add_columns(conn, "hosting_oauth_authorization_codes", {
        "code_challenge": "TEXT NOT NULL DEFAULT ''",
        "code_challenge_method": "TEXT NOT NULL DEFAULT ''",
    })
    add_columns(conn, "instances", {"oauth_client_secret_encrypted": "TEXT NOT NULL DEFAULT ''"})
    add_columns(conn, "hosting_settings", {"admin_mfa_required": "INTEGER NOT NULL DEFAULT 0"})
    for name, target in _INDEXES.items():
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    add_attention(conn)


def add_attention(conn: sqlite3.Connection) -> None:
    attention.create_tables(conn, "hosting_attention_events", "hosting_attention_recipients")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hosting_account_notices ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, "
        "source_id TEXT NOT NULL, outcome TEXT NOT NULL, object_id TEXT NOT NULL DEFAULT '', "
        "detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, dismissed_at TEXT, emailed_at TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_hosting_account_notices_account "
                 "ON hosting_account_notices(account_id, dismissed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_hosting_account_notices_email "
                 "ON hosting_account_notices(emailed_at, created_at)")
    add_columns(conn, "accounts", {
        "attention_emails": "INTEGER NOT NULL DEFAULT 1",
        "language": "TEXT NOT NULL DEFAULT ''",
    })
    had_interval = "approval_notify_interval_minutes" in column_names(conn, "hosting_settings")
    add_columns(conn, "hosting_settings", {
        "approval_notify_admins": "INTEGER NOT NULL DEFAULT 0",
        "approval_notify_interval_minutes": "INTEGER NOT NULL DEFAULT 360",
        "approval_notify_daily_hour": "INTEGER NOT NULL DEFAULT 8",
    })
    if not had_interval:
        conn.execute("UPDATE hosting_settings SET approval_notify_interval_minutes = "
                     "MIN(4320, MAX(5, COALESCE(approval_notify_digest_hours, 6) * 60))")


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, bool]]:
    return [
        (name, (declared or "").upper(), bool(notnull) or bool(pk))
        for _cid, name, declared, notnull, _default, pk in tuples(conn, f"PRAGMA table_info({quote_identifier(table)})")
    ]


def _foreign_keys(conn: sqlite3.Connection, table: str) -> dict[int, list[str]]:
    keys: dict[int, list[str]] = {}
    for fk_id, _seq, _parent, source, *_ in tuples(conn, f"PRAGMA foreign_key_list({quote_identifier(table)})"):
        keys.setdefault(fk_id, []).append(source)
    return keys


def repair_dangling_references(conn: sqlite3.Connection) -> None:
    fixed = removed = 0
    # Deleting a row can orphan its children, so repeat until clean.
    for _round in range(8):
        violations = tuples(conn, "PRAGMA foreign_key_check")
        if not violations:
            break
        for table, rowid, _parent, fk_id in violations:
            if rowid is None:
                continue
            columns = _foreign_keys(conn, table).get(fk_id, [])
            required = {name for name, _type, req in _columns(conn, table) if req}
            if columns and not required.intersection(columns):
                assignments = ", ".join(f"{quote_identifier(c)} = NULL" for c in columns)
                conn.execute(f"UPDATE {quote_identifier(table)} SET {assignments} WHERE rowid = ?", (rowid,))
                fixed += 1
            else:
                conn.execute(f"DELETE FROM {quote_identifier(table)} WHERE rowid = ?", (rowid,))
                removed += 1
    if fixed or removed:
        log.warning("Repaired %d dangling references and removed %d orphaned rows.", fixed, removed)


def normalize_timestamps(conn: sqlite3.Connection) -> None:
    tables = [row[0] for row in tuples(
        conn, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
    )]
    for table in tables:
        for name, declared, _required in _columns(conn, table):
            if "TEXT" not in declared or not _TIMESTAMP_COLUMN.search(name):
                continue
            column = quote_identifier(name)
            conn.execute(
                f"UPDATE {quote_identifier(table)} SET {column} = strftime('%Y-%m-%d %H:%M:%S', {column}) "
                f"WHERE {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*' "
                f"AND strftime('%Y-%m-%d %H:%M:%S', {column}) IS NOT NULL"
            )


def drop_legacy_oauth_grants(conn: sqlite3.Connection) -> None:
    for table in ("hosting_oauth_authorization_codes", "hosting_oauth_access_tokens"):
        if table_exists(conn, table):
            conn.execute(f"DELETE FROM {quote_identifier(table)}")
