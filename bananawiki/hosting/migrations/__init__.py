"""Hosting database schema versions (``hosting.db``).

Version history
---------------
1-2  BananaWiki Hosting 1.4 (legacy import, REST API tokens and approval
     notifications). ``baseline_v2.sql`` is that schema, verbatim.
3    BananaWiki 1.6 takeover. See :mod:`.v3_takeover`.
4    Pending tenant initialization. See :mod:`.v4_provisioning`.
5    Purging an account keeps the collaborators it invited and the merges
     it carried out. See :mod:`.v5_account_references`.

A 1.4 database at version 2 upgrades in place. A new database is created from
the baseline followed by every later migration, so fresh and upgraded
platforms always end up with the same schema. Older databases (version 1 or
an unversioned pre-1.0 file) must first be opened once by the last 1.4
release.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from ...core.sqlite import execute_script
from . import v3_takeover, v4_provisioning, v5_account_references

APPLICATION_ID = 0x42574850  # "BWHP", unchanged from 1.4
BASELINE = 2
MIGRATIONS = (
    v3_takeover.upgrade,  # 2 -> 3
    v4_provisioning.upgrade,  # 3 -> 4
    v5_account_references.upgrade,  # 4 -> 5
)
LATEST = BASELINE + len(MIGRATIONS)
SIGNUP_MODES = ("open", "invite", "approval", "closed")

_BASELINE_SQL = Path(__file__).with_name("baseline_v2.sql")


def bootstrap_for(default_signup_mode: str = "open") -> Callable[[sqlite3.Connection], None]:
    """Return the function that creates a brand-new database at the latest version.

    *default_signup_mode* comes from ``HOSTING_DEFAULT_SIGNUP_MODE``, which
    1.4 also read when it created the settings row.
    """
    mode = default_signup_mode if default_signup_mode in SIGNUP_MODES else "open"

    def bootstrap(conn: sqlite3.Connection) -> None:
        execute_script(conn, _BASELINE_SQL.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR IGNORE INTO hosting_settings (id, signup_mode, hosting_activation_required) VALUES (1, ?, ?)",
            (mode, 1 if mode == "approval" else 0),
        )
        for migration in MIGRATIONS:
            migration(conn)

    return bootstrap
