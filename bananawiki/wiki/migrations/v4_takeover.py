"""Schema version 4: BananaWiki 1.6 takes over a 1.4 database.

Everything here is idempotent and keeps 1.4 data. In order:

1. Repair dangling references. 1.4 wrote ``-1`` ("system") into user
   columns with foreign keys switched off, which makes every later
   migration's integrity check fail. Nullable references become NULL; rows
   whose required parent is gone are removed, as ``ON DELETE CASCADE``
   would have done.
2. Store every timestamp in one shape (``YYYY-MM-DD HH:MM:SS``, UTC) so text
   comparisons are correct. 1.4 mixed that with ISO ``…T…+00:00``.
3. Fold the pre-custom-role editor category ACL into the current one.
4. Wipe secrets of retired features (the Telegram bot token was stored in
   plain text).
5. Add ``pages.revision`` for edit-conflict detection and ``job_runs``
   (leases so each background job runs in one worker at a time).
6. Add indexes for foreign keys used on hot paths and in cascades.
7. Add a full-text search index over pages (when SQLite has FTS5).
8. Let each feature add what it needs (``features/*/schema.py``).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
import sqlite3

from ...core.sqlite import add_columns, column_names, quote_identifier, table_exists, tuples

log = logging.getLogger("bananawiki.migrations")

_TIMESTAMP_COLUMN = re.compile(r"(_at|_until)$|^upload_window_start$")
_RETIRED_SECRET_COLUMNS = ("telegram_sync_token", "feedback_bot_token", "feedback_telegram_userids")

_INDEXES = {
    "idx_page_attachments_page": "page_attachments(page_id)",
    "idx_page_history_page": "page_history(page_id, id)",
    "idx_page_history_editor": "page_history(edited_by)",
    "idx_pages_category": "pages(category_id, sort_order)",
    "idx_pages_last_edited_by": "pages(last_edited_by)",
    "idx_categories_parent": "categories(parent_id, sort_order)",
    "idx_chats_user2": "chats(user2_id)",
    "idx_chat_messages_chat": "chat_messages(chat_id, id)",
    "idx_chat_messages_sender": "chat_messages(sender_id)",
    "idx_chat_attachments_message": "chat_attachments(message_id)",
    "idx_group_members_user": "group_members(user_id)",
    "idx_group_messages_group": "group_messages(group_id, id)",
    "idx_group_attachments_message": "group_attachments(message_id)",
    "idx_user_badges_type": "user_badges(badge_type_id)",
    "idx_user_badges_user": "user_badges(user_id)",
    "idx_custom_page_files_page": "custom_page_files(custom_page_id)",
    "idx_assessment_answers_question": "assessment_answers(question_id)",
    "idx_pending_contributions_user": "pending_contributions(user_id)",
    "idx_page_reservations_user": "page_reservations(user_id)",
    "idx_user_sessions_user": "user_sessions(user_id)",
    "idx_user_sessions_expiry": "user_sessions(expires_at)",
    "idx_rate_limit_hits_lookup": "rate_limit_hits(ip, bucket, hit_at)",
    "idx_rate_limit_hits_time": "rate_limit_hits(hit_at)",
    "idx_kanban_tickets_column": "kanban_tickets(column_id, sort_order)",
    "idx_kanban_columns_board": "kanban_columns(board_id, sort_order)",
    "idx_federation_shares_page": "federation_shares(page_id)",
    "idx_drafts_user": "drafts(user_id)",
    "idx_users_custom_role": "users(custom_role_id)",
}

_BLOB_COLUMNS = (
    ("page_attachments", "blob_id"),
    ("chat_attachments", "blob_id"),
    ("group_attachments", "blob_id"),
    ("kanban_ticket_attachments", "blob_id"),
    ("custom_page_files", "blob_id"),
)


def upgrade(conn: sqlite3.Connection) -> None:
    repair_dangling_references(conn)
    normalize_timestamps(conn)
    merge_legacy_editor_acl(conn)
    scrub_retired_secrets(conn)
    add_columns(conn, "pages", {"revision": "INTEGER NOT NULL DEFAULT 0"})
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_runs ("
        "name TEXT PRIMARY KEY, held_by TEXT, lease_until TEXT, last_run_at TEXT, "
        "last_status TEXT, last_error TEXT)"
    )
    add_indexes(conn)
    add_page_search(conn)
    upgrade_features(conn)


def _foreign_keys(conn: sqlite3.Connection, table: str) -> dict[int, list[str]]:
    keys: dict[int, list[str]] = {}
    for fk_id, _seq, _parent, source, *_ in tuples(conn, f"PRAGMA foreign_key_list({quote_identifier(table)})"):
        keys.setdefault(fk_id, []).append(source)
    return keys


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, bool]]:
    """(name, declared type, required) for each column of *table*."""
    return [
        (name, (declared or "").upper(), bool(notnull) or bool(pk))
        for _cid, name, declared, notnull, _default, pk in tuples(conn, f"PRAGMA table_info({quote_identifier(table)})")
    ]


def repair_dangling_references(conn: sqlite3.Connection) -> None:
    violations = tuples(conn, "PRAGMA foreign_key_check")
    if not violations:
        return
    fixed = removed = 0
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
    log.warning("Repaired %d dangling references and removed %d orphaned rows.", fixed, removed)


def normalize_timestamps(conn: sqlite3.Connection) -> None:
    tables = [row[0] for row in tuples(
        conn,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'",
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


def merge_legacy_editor_acl(conn: sqlite3.Connection) -> None:
    if not (table_exists(conn, "editor_category_access") and table_exists(conn, "user_category_access")):
        return
    from ..permissions import defaults

    rows = tuples(
        conn,
        "SELECT e.user_id, u.role FROM editor_category_access e JOIN users u ON u.id = e.user_id "
        "WHERE e.restricted = 1 AND u.custom_role_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM user_category_access a WHERE a.user_id = e.user_id)",
    )
    for user_id, role in rows:
        for key in sorted(defaults(role)):
            conn.execute(
                "INSERT OR IGNORE INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (user_id, key)
            )
        conn.execute(
            "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', 1)",
            (user_id,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_allowed_categories (user_id, category_id, access_type) "
            "SELECT user_id, category_id, 'write' FROM editor_allowed_categories WHERE user_id = ?",
            (user_id,),
        )


def scrub_retired_secrets(conn: sqlite3.Connection) -> None:
    existing = column_names(conn, "site_settings")
    for column in _RETIRED_SECRET_COLUMNS:
        if column in existing:
            conn.execute(f"UPDATE site_settings SET {quote_identifier(column)} = ''")


def add_indexes(conn: sqlite3.Connection) -> None:
    for name, target in _INDEXES.items():
        table = target.split("(", 1)[0]
        if table_exists(conn, table):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    for table, column in _BLOB_COLUMNS:
        if table_exists(conn, table) and column in column_names(conn, table):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{column} ON {table}({column})")


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def add_page_search(conn: sqlite3.Connection) -> None:
    """External-content FTS5 index over page titles and Markdown, kept in sync by triggers."""
    if table_exists(conn, "pages_fts") or not fts5_available(conn):
        return
    conn.execute(
        "CREATE VIRTUAL TABLE pages_fts USING fts5("
        "title, content, content='pages', content_rowid='id', "
        "tokenize='unicode61 remove_diacritics 2', prefix='2 3')"
    )
    conn.execute(
        "CREATE TRIGGER pages_fts_insert AFTER INSERT ON pages BEGIN "
        "INSERT INTO pages_fts (rowid, title, content) VALUES (new.id, new.title, new.content); END"
    )
    conn.execute(
        "CREATE TRIGGER pages_fts_delete AFTER DELETE ON pages BEGIN "
        "INSERT INTO pages_fts (pages_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER pages_fts_update AFTER UPDATE OF title, content ON pages BEGIN "
        "INSERT INTO pages_fts (pages_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content); "
        "INSERT INTO pages_fts (rowid, title, content) VALUES (new.id, new.title, new.content); END"
    )
    conn.execute("INSERT INTO pages_fts (pages_fts) VALUES ('rebuild')")


def upgrade_features(conn: sqlite3.Connection) -> None:
    """Run ``upgrade_v4(conn)`` from every ``features/<name>/schema.py``, in name order."""
    from .. import features

    for module in sorted(pkgutil.iter_modules(features.__path__), key=lambda m: m.name):
        if not module.ispkg:
            continue
        try:
            schema = importlib.import_module(f"{features.__name__}.{module.name}.schema")
        except ModuleNotFoundError as error:
            if error.name and error.name.endswith(f"{module.name}.schema"):
                continue
            raise
        hook = getattr(schema, "upgrade_v4", None)
        if hook is not None:
            hook(conn)
