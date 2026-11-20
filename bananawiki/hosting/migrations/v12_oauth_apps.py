"""Version 12: sign in to other apps with a hosting account, and sign-in modes per wiki.

* ``hosting_oauth_apps``: OAuth clients any hosting account registers
  (**Account → Developer apps**): BananaChat, BananaGit or any other app.
  ``status`` is ``pending`` until an administrator approves it (only its
  owner and administrators can sign in to it meanwhile), then ``approved``;
  ``denied`` and ``suspended`` apps sign nobody in. ``redirect_uris`` is a
  JSON list compared exactly. The client secret is stored as the salted
  digest wikis' secrets use (shown once to the owner, never stored).
* ``hosting_oauth_app_grants``: the apps an account allowed to know who it
  is, with the scopes it allowed (``profile`` or ``profile email``).
* ``hosting_oauth_authorization_codes.scope``: the scopes a code was issued
  for (empty for wikis, which always get ``openid profile``).
* ``instances.platform_signin_mode``: how members sign in to the wiki, set by
  an administrator over the wiki's own choice (``local``, ``both`` or
  ``platform``); empty lets the wiki's administrators decide.
* ``hosting_settings.oauth_apps_enabled`` (accounts may register apps) and
  ``oauth_apps_per_account`` (how many each, administrators excepted).

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hosting_oauth_apps ("
        "id TEXT PRIMARY KEY, "
        "account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, "
        "name TEXT NOT NULL, "
        "description TEXT NOT NULL DEFAULT '', "
        "homepage_url TEXT NOT NULL DEFAULT '', "
        "redirect_uris TEXT NOT NULL DEFAULT '[]', "
        "client_id TEXT NOT NULL UNIQUE, "
        "client_secret_hash TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'denied', 'suspended')), "
        "review_note TEXT NOT NULL DEFAULT '', "
        "reviewed_by TEXT REFERENCES accounts(id) ON DELETE SET NULL, "
        "reviewed_at TEXT, "
        "created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, "
        "last_used_at TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_oauth_apps_account ON hosting_oauth_apps (account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_oauth_apps_status ON hosting_oauth_apps (status, created_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hosting_oauth_app_grants ("
        "account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, "
        "app_id TEXT NOT NULL REFERENCES hosting_oauth_apps(id) ON DELETE CASCADE, "
        "scope TEXT NOT NULL DEFAULT 'profile', "
        "granted_at TEXT NOT NULL, "
        "last_used_at TEXT, "
        "PRIMARY KEY (account_id, app_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_oauth_app_grants_app ON hosting_oauth_app_grants (app_id)")
    add_columns(conn, "hosting_oauth_authorization_codes", {"scope": "TEXT NOT NULL DEFAULT ''"})
    add_columns(conn, "instances", {"platform_signin_mode": "TEXT NOT NULL DEFAULT ''"})
    add_columns(conn, "hosting_settings", {"oauth_apps_enabled": "INTEGER NOT NULL DEFAULT 1",
                                           "oauth_apps_per_account": "INTEGER NOT NULL DEFAULT 5"})
