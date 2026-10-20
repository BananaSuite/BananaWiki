"""Schema additions for contributions (run by migration 4)."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns, table_exists


def upgrade_v4(conn: sqlite3.Connection) -> None:
    add_columns(conn, "pending_contributions", {"base_revision": "INTEGER"})
    # 1.4 also required the page_governance plugin, and disabling that plugin
    # reset the setting. Where the plugin was off, the setting stays off: here
    # the setting alone is the switch.
    if table_exists(conn, "plugins"):
        conn.execute(
            "UPDATE site_settings SET contribution_approval_enabled = 0 "
            "WHERE EXISTS (SELECT 1 FROM plugins WHERE id = 'page_governance' AND enabled = 0)"
        )
