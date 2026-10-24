"""Hosting database schema versions (``hosting.db``).

Version history
---------------
1-2  BananaWiki Hosting 1.4 (legacy import, REST API tokens and approval
     notifications). ``baseline_v2.sql`` is that schema, verbatim.
3    BananaWiki 1.6 takeover. See :mod:`.v3_takeover`.
4    Pending tenant initialization. See :mod:`.v4_provisioning`.
5    Purging an account keeps the collaborators it invited and the merges
     it carried out. See :mod:`.v5_account_references`.
6    Email budgets shared by every worker, expiry warnings, owner renewal,
     the age of each email claim, device cookies tied to two-step changes,
     end dates set by administrators and proven take-overs of lapsed
     custom domains. See :mod:`.v6_account_safety`.
7    A denied account waits one day before it is deleted (the old column
     default deleted it at once). See :mod:`.v7_denied_default`.
8    One portal account across the platform's wikis: remembered sign-in
     consent per wiki and the sign-outs wikis fetch. See
     :mod:`.v8_cross_wiki_accounts`.
9    Email policy (address at sign-up and for existing accounts, one or
     several accounts per address), pausing every email, a switch per
     notice and per waiting-request queue, and the portal's easter eggs.
     See :mod:`.v9_email_policy`.
10   Main wikis: kept for the platform, never expired, terminated or reset
     until an administrator takes the mark away. See :mod:`.v10_main_wikis`.
11   Who may use custom domains (administrators only, accounts an
     administrator allows, or everyone), and owners asking for one. See
     :mod:`.v11_custom_domain_policy`.

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
from . import (
    v3_takeover,
    v4_provisioning,
    v5_account_references,
    v6_account_safety,
    v7_denied_default,
    v8_cross_wiki_accounts,
    v9_email_policy,
    v10_main_wikis,
    v11_custom_domain_policy,
)

APPLICATION_ID = 0x42574850  # "BWHP", unchanged from 1.4
BASELINE = 2
MIGRATIONS = (
    v3_takeover.upgrade,  # 2 -> 3
    v4_provisioning.upgrade,  # 3 -> 4
    v5_account_references.upgrade,  # 4 -> 5
    v6_account_safety.upgrade,  # 5 -> 6
    v7_denied_default.upgrade,  # 6 -> 7
    v8_cross_wiki_accounts.upgrade,  # 7 -> 8
    v9_email_policy.upgrade,  # 8 -> 9
    v10_main_wikis.upgrade,  # 9 -> 10
    v11_custom_domain_policy.upgrade,  # 10 -> 11
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
