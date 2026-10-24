"""Schema version 14: BM mode, the easy mode of a wiki (see :mod:`bananawiki.wiki.bm`).

``site_settings.bm_mode`` is the administrators' choice (NULL: none yet, the
operator's ``BW_EASY_WIKI`` applies), ``bm_mode_env`` the operator's value
when they chose (a later change by the operator applies again) and
``bm_choices`` the features and simplifications shown or hidden in BM mode
(JSON). While it is off for the wiki, each account can be in BM mode on its
own: ``users.bm_mode`` (0/1) and ``users.bm_self`` (``""`` the wiki's rule,
``allow`` or ``deny``: whether the account may switch it itself);
``site_settings.bm_self_service`` is the wiki's rule and
``bm_new_accounts`` whether new accounts start in BM mode.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "site_settings"):
        add_columns(conn, "site_settings", {"bm_mode": "INTEGER", "bm_mode_env": "INTEGER",
                                            "bm_choices": "TEXT NOT NULL DEFAULT '{}'",
                                            "bm_self_service": "INTEGER NOT NULL DEFAULT 1",
                                            "bm_new_accounts": "INTEGER NOT NULL DEFAULT 0"})
    if table_exists(conn, "users"):
        add_columns(conn, "users", {"bm_mode": "INTEGER NOT NULL DEFAULT 0",
                                    "bm_self": "TEXT NOT NULL DEFAULT ''"})
