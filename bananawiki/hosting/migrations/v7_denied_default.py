"""Version 7: a denied account waits one day before it is deleted, as documented.

``hosting_settings.hosting_activation_denied_timeout_seconds`` came from 1.4
with ``DEFAULT 0``, which the maintenance pass reads as "delete a denied
account at once", while the portal treats one day as the default
(``settings.denied_timeout_seconds``) and documents 0/0/0 as the choice to
delete at once. Until 1.7 the approval form showed 0/0/0 whatever was stored
and saved what it showed, so a stored 0 is that old default rather than a
choice: it becomes 86400. "Never" (-1) and every other value are kept. A new
platform gets the same day, because this runs after the baseline creates the
settings row.
"""

from __future__ import annotations

import sqlite3

DEFAULT_SECONDS = 86400


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE hosting_settings SET hosting_activation_denied_timeout_seconds = ? "
        "WHERE hosting_activation_denied_timeout_seconds = 0",
        (DEFAULT_SECONDS,),
    )
