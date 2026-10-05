# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Schema version 6: account history that permission decisions rely on.

1. ``suspension_audit.imposed_by_top``: whether an owner or superuser imposed
   the suspension, as they ranked at that moment. A suspended administrator
   may lift only a suspension recorded with 0. Earlier rows cannot tell, so
   they count as imposed (the column default) and an owner or superuser lifts
   them.
2. An index on ``username_history.old_username``: former names resolve old
   ``@mentions`` and stay reserved for their account.

Idempotent, like every migration.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns


def upgrade(conn: sqlite3.Connection) -> None:
    add_columns(conn, "suspension_audit", {"imposed_by_top": "INTEGER NOT NULL DEFAULT 1"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_username_history_old "
                 "ON username_history(old_username COLLATE NOCASE)")
