"""Schema additions for contributions (run by migration 4)."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns, table_exists


def upgrade_v4(conn: sqlite3.Connection) -> None:
    add_columns(conn, "pending_contributions", {"base_revision": "INTEGER"})
    # In 1.4 contribution approval also needed the page_governance plugin, and
    # disabling that plugin reset the setting. Keep that outcome for rows
    # where the plugin was off, now that the setting alone is the switch.
    if table_exists(conn, "plugins"):
        conn.execute(
            "UPDATE site_settings SET contribution_approval_enabled = 0 "
            "WHERE EXISTS (SELECT 1 FROM plugins WHERE id = 'page_governance' AND enabled = 0)"
        )
