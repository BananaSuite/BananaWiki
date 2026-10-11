# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Schema version 9: two-step verification for wiki accounts.

1. ``auth__two_step``: one row per account that turned two-step verification
   on. A row means it is on. The authenticator secret is encrypted with a key
   derived from the wiki's secret key; recovery codes are kept as keyed
   digests (a JSON list), and ``last_counter`` is the last 30-second step
   accepted, so each code works once. Deleting an account deletes its row.
2. ``site_settings.two_step_required_for_admins``: owners and superusers may
   require it of administrators.
3. ``users.device_epoch`` and ``users.device_epoch_stamp``: what the device
   cookies of an account are signed with, so that turning two-step
   verification on, off or resetting it retires them, and a new password
   (not a re-hash of the same one) does too.

Nothing changes for existing accounts: two-step verification starts off for
everyone and is not required (0), and device cookies start with the first
sign-in after the upgrade (NULL until then). The DDL is frozen here: later schema
changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS auth__two_step ("
        "user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
        "secret_encrypted TEXT NOT NULL, "
        "recovery_hashes TEXT NOT NULL DEFAULT '[]', "
        "last_counter INTEGER NOT NULL DEFAULT -1, "
        "enabled_at TEXT NOT NULL)"
    )
    add_columns(conn, "site_settings", {"two_step_required_for_admins": "INTEGER NOT NULL DEFAULT 0"})
    add_columns(conn, "users", {"device_epoch": "TEXT", "device_epoch_stamp": "TEXT"})
