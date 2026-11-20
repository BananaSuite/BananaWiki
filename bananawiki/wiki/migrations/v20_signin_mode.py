"""Schema version 20: how members sign in, ``site_settings.signin_mode``.

``local`` (the wiki's own accounts only), ``both`` (its own accounts and the
hosting portal's, the behaviour before this version and the default) or
``platform`` (the hosting portal's accounts only). It only matters while
platform sign-in is configured; ``BW_PLATFORM_SIGNIN_MODE`` overrides it (see
:mod:`bananawiki.wiki.features.auth.signin_modes`).

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "site_settings"):
        add_columns(conn, "site_settings", {"signin_mode": "TEXT NOT NULL DEFAULT 'both'"})
