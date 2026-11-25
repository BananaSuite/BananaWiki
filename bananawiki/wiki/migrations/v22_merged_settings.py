"""Schema version 22: options that decided the same thing as another one.

* ``site_settings.favicon_enabled`` is retired. Switched off, it showed the
  default icon (the yellow banana) whatever was selected, so a wiki where it
  was off now has that icon selected, and the chosen icon is always used.
  The column stays and is set to 1, so an older release reading it shows
  the same icon.

The statements are frozen here: later changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import column_names, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "site_settings") or "favicon_enabled" not in column_names(conn, "site_settings"):
        return
    conn.execute("UPDATE site_settings SET favicon_type = 'yellow', favicon_custom = '' WHERE favicon_enabled = 0")
    conn.execute("UPDATE site_settings SET favicon_enabled = 1")
