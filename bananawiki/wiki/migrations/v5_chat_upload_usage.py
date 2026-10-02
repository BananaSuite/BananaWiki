"""Persist chat upload quotas independently of removable message attachments."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    from ..features.chat.schema import upgrade_v5

    upgrade_v5(conn)
