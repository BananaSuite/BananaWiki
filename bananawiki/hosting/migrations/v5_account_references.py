# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Version 5: purging an account no longer deletes rows it is only named in.

``instance_collaborators.invited_by`` and ``hosting_account_merge_logs.merged_by``
were ``NOT NULL ... ON DELETE CASCADE``. When the maintenance service purged a
deleted account's tombstone, SQLite also deleted every collaborator that
account had invited, on wikis other people now own, and every merge it had
carried out as an administrator. Both columns become nullable
``ON DELETE SET NULL``.

SQLite cannot change a column's constraints in place, so each table is
rebuilt as https://www.sqlite.org/lang_altertable.html describes: the
migration runner has already switched foreign keys off and opened the
transaction, and checks every foreign key before it commits. Rows keep their
ids, the ``AUTOINCREMENT`` counter keeps its value and the indexes are
recreated.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import column_names, quote_identifier, tuples

_TABLES = {
    "instance_collaborators": ("invited_by", """CREATE TABLE {name} (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            role            TEXT NOT NULL DEFAULT 'custom'
                                CHECK(role IN ('full_access', 'custom')),
            permissions     TEXT NOT NULL DEFAULT '[]',
            invited_by      TEXT REFERENCES accounts(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(instance_id, account_id)
        )"""),
    "hosting_account_merge_logs": ("merged_by", """CREATE TABLE {name} (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        source_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        merged_by       TEXT REFERENCES accounts(id) ON DELETE SET NULL,
        instances_transferred INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )"""),
}


def upgrade(conn: sqlite3.Connection) -> None:
    for table, (column, ddl) in _TABLES.items():
        if not _sets_null(conn, table, column):
            _rebuild(conn, table, ddl)


def _sets_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[3] == column and str(row[6]).upper() == "SET NULL"
               for row in tuples(conn, f"PRAGMA foreign_key_list({quote_identifier(table)})"))


def _rebuild(conn: sqlite3.Connection, table: str, ddl: str) -> None:
    """Recreate *table* from *ddl*, keeping its rows, indexes, triggers and AUTOINCREMENT counter."""
    name, staging = quote_identifier(table), quote_identifier(f"{table}_v5")
    extras = [row[0] for row in tuples(
        conn, "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
        "ORDER BY type, name", (table,))]
    counter = tuples(conn, "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,))
    conn.execute(ddl.format(name=staging))
    kept = ", ".join(quote_identifier(column) for column in sorted(column_names(conn, table))
                     if column in column_names(conn, f"{table}_v5"))
    conn.execute(f"INSERT INTO {staging} ({kept}) SELECT {kept} FROM {name}")
    conn.execute(f"DROP TABLE {name}")
    conn.execute(f"ALTER TABLE {staging} RENAME TO {name}")
    for sql in extras:
        conn.execute(sql)
    if counter:
        changed = conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (counter[0][0], table))
        if not changed.rowcount:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, counter[0][0]))
