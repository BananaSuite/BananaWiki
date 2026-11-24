"""Version 13: easter eggs administrators design, who found them, and the other BananaSuite apps.

* ``hosting_settings.easter_egg_configs``: the portal's key-sequence easter
  eggs in the BananaSuite format ``banana-eggs/v1``
  (:mod:`bananawiki.core.eggs`); NULL means "never saved": the default egg
  (the Konami code) applies. ``easter_konami`` (version 9) stays the switch
  for all of them.
* ``account_egg_finds``: the first time each account found each egg.
  Visitors who are not signed in keep their finds in their browser.
* ``hosting_settings.goto_chat_url`` and ``goto_git_url``: the addresses of
  the platform's BananaChat and BananaGit, listed in the top bar's "Go to"
  menu ('' leaves an app out).

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns


def upgrade(conn: sqlite3.Connection) -> None:
    add_columns(conn, "hosting_settings", {"easter_egg_configs": "TEXT",
                                           "goto_chat_url": "TEXT NOT NULL DEFAULT ''",
                                           "goto_git_url": "TEXT NOT NULL DEFAULT ''"})
    conn.execute(
        "CREATE TABLE IF NOT EXISTS account_egg_finds ("
        "account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, "
        "egg_id TEXT NOT NULL, "
        "found_at TEXT NOT NULL, "
        "PRIMARY KEY (account_id, egg_id))"
    )
