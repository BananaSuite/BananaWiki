"""Version 8: one portal account across the platform's wikis.

* ``hosting_oauth_grants``: the wikis an account allowed to know who it is.
  Platform sign-in asks for consent once per wiki (the owner's own wikis
  never ask); later sign-ins go straight through. ``last_used_at`` is the
  last time the wiki redeemed a code for the account.
* ``hosting_oauth_signouts``: sign-outs the wikis fetch with their client
  credentials (``/oauth/signouts``). A wiki ends the platform sessions of the
  linked wiki account that started before ``created_at``. ``account_id`` has
  no foreign key: the event must outlive an account that is purged. Rows
  older than :data:`bananawiki.hosting.signouts.RETENTION_DAYS` are pruned by
  the maintenance service.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hosting_oauth_grants ("
        "account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, "
        "instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE, "
        "granted_at TEXT NOT NULL, "
        "last_used_at TEXT, "
        "PRIMARY KEY (account_id, instance_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_oauth_grants_instance ON hosting_oauth_grants (instance_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hosting_oauth_signouts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE, "
        "account_id TEXT NOT NULL, "
        "wiki_user_id TEXT NOT NULL DEFAULT '', "
        "reason TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_oauth_signouts_instance ON hosting_oauth_signouts (instance_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_oauth_signouts_created ON hosting_oauth_signouts (created_at)")
