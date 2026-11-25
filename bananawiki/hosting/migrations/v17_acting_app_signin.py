"""Version 17: administrators acting as an account can sign in to its wikis and apps unless switched off.

``hosting_settings.admin_app_signin`` started as 0 (version 15), which
refused every sign-in to a wiki or app while acting. It becomes 1; the box
stays in the settings (**Administrators acting as an account**) for
platforms that want it off.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE hosting_settings SET admin_app_signin = 1")
