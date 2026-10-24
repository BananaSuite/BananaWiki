"""Schema version 15: the wiki's easter eggs can be switched off one by one.

``site_settings.easter_eggs`` is a JSON object of egg id -> 0/1 (see
:mod:`bananawiki.wiki.features.mascot.service`); an egg it does not name is
on, so ``'{}'`` keeps every egg as before.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "site_settings"):
        add_columns(conn, "site_settings", {"easter_eggs": "TEXT NOT NULL DEFAULT '{}'"})
