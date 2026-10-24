"""Version 11: who may use custom domains, and asking for one.

* ``hosting_settings.custom_domains_policy``: ``admins`` (only administrators'
  wikis, the default), ``selected`` (also the accounts an administrator
  allows) or ``everyone``.
* ``accounts.custom_domains_allowed``: the account may use custom domains
  while the policy is ``selected``.
* ``instance_feature_requests.feature`` also accepts ``custom_domain``, so an
  owner can ask for one like for public access or the page builder; approving
  it sets the wiki's own grant, ``instances.custom_domain_allowed``, which
  keeps working as before. SQLite cannot change a CHECK constraint in place,
  so the table is rebuilt (rows keep their ids, the AUTOINCREMENT counter
  keeps its value and the indexes are recreated), as in version 5.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, column_names, quote_identifier, tuples

_REQUESTS = """CREATE TABLE {name} (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            feature         TEXT NOT NULL CHECK(feature IN ('public_access', 'page_builder', 'custom_domain')),
            requested_by    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            reason          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'approved', 'denied', 'cancelled')),
            reviewed_by     TEXT REFERENCES accounts(id) ON DELETE SET NULL,
            review_note     TEXT NOT NULL DEFAULT '',
            review_source   TEXT NOT NULL DEFAULT 'manual'
                                CHECK(review_source IN ('manual', 'automatic')),
            requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
            reviewed_at     TEXT,
            cancelled_at    TEXT
        )"""


def upgrade(conn: sqlite3.Connection) -> None:
    add_columns(conn, "hosting_settings", {"custom_domains_policy": "TEXT NOT NULL DEFAULT 'admins'"})
    add_columns(conn, "accounts", {"custom_domains_allowed": "INTEGER NOT NULL DEFAULT 0"})
    ddl = tuples(conn, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'instance_feature_requests'")
    if ddl and "'custom_domain'" not in (ddl[0][0] or ""):
        _rebuild(conn, "instance_feature_requests", _REQUESTS)


def _rebuild(conn: sqlite3.Connection, table: str, ddl: str) -> None:
    """Recreate *table* from *ddl*, keeping its rows, indexes, triggers and AUTOINCREMENT counter."""
    name, staging = quote_identifier(table), quote_identifier(f"{table}_v11")
    extras = [row[0] for row in tuples(
        conn, "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
        "ORDER BY type, name", (table,))]
    counter = tuples(conn, "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,))
    conn.execute(ddl.format(name=staging))
    kept = ", ".join(quote_identifier(column) for column in sorted(column_names(conn, table))
                     if column in column_names(conn, f"{table}_v11"))
    conn.execute(f"INSERT INTO {staging} ({kept}) SELECT {kept} FROM {name}")
    conn.execute(f"DROP TABLE {name}")
    conn.execute(f"ALTER TABLE {staging} RENAME TO {name}")
    for sql in extras:
        conn.execute(sql)
    if counter:
        changed = conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (counter[0][0], table))
        if not changed.rowcount:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, counter[0][0]))
