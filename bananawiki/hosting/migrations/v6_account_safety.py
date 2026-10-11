# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Version 6: email budgets, expiry warnings, owner renewal, email claims and account safety.

* ``hosting_email_log``: one row per message the portal sent, so the daily
  email budgets (in total, per recipient and per account) hold across
  workers and restarts instead of living in each process.
* ``instances.expiry_warning_for`` / ``expiry_warning_days``: which expiry
  date the owner was last warned about and the smallest number of days
  announced, so each warning goes out once and an extension starts over.
* ``hosting_settings.owner_renewal_enabled``: owners may renew their own
  wikis (off by default, as before).
* ``accounts.email_claimed_at``: when the current address was set. An
  address left unverified for longer than a verification link lives no
  longer blocks its real owner. Existing addresses count from the last
  verification email, or the account's creation.
* ``accounts.email_claims_since`` / ``email_claims``: when the account's
  current 30-day series of address changes began and how many changes it
  holds, so changing an address away and back does not hold it again.
* ``accounts.mfa_epoch``: bumped whenever two-step verification is turned
  on or off; device cookies carry it, so a browser that signed in before
  that gets no code budget of its own.
* ``instances.admin_expiry``: the end date an administrator brought
  forward; owners cannot renew it away while the wiki still ends then.
* ``instance_domain_claims``: claims of a custom domain whose binding to
  another wiki lapsed. The old binding is released only once the claimant
  proves the domain.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, column_names


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hosting_email_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, sent_at TEXT NOT NULL, recipient TEXT NOT NULL, "
        "account_id TEXT, kind TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_hosting_email_log_time ON hosting_email_log(sent_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_hosting_email_log_recipient ON hosting_email_log(recipient, sent_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_hosting_email_log_account ON hosting_email_log(account_id, sent_at)")
    add_columns(conn, "instances", {
        "expiry_warning_for": "TEXT",
        "expiry_warning_days": "INTEGER",
    })
    add_columns(conn, "hosting_settings", {"owner_renewal_enabled": "INTEGER NOT NULL DEFAULT 0"})
    had_claims = "email_claimed_at" in column_names(conn, "accounts")
    add_columns(conn, "accounts", {
        "email_claimed_at": "TEXT",
        "email_claims_since": "TEXT",
        "email_claims": "INTEGER NOT NULL DEFAULT 0",
        "mfa_epoch": "INTEGER NOT NULL DEFAULT 0",
    })
    if not had_claims:
        conn.execute("UPDATE accounts SET email_claimed_at = COALESCE(email_verification_sent_at, created_at) "
                     "WHERE email != ''")
    add_columns(conn, "instances", {"admin_expiry": "TEXT"})
    conn.execute(
        "CREATE TABLE IF NOT EXISTS instance_domain_claims ("
        "instance_id TEXT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE, "
        "domain TEXT NOT NULL COLLATE NOCASE, verification_token TEXT NOT NULL, created_at TEXT NOT NULL, "
        "last_checked_at TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_instance_domain_claims_domain ON instance_domain_claims(domain)")
