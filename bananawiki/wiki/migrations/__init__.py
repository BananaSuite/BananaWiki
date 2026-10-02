"""Wiki database schema versions.

Version history
---------------
1-3  BananaWiki 1.4 (legacy import, navigation index, federation tables).
4    BananaWiki 1.6 takeover: repairs 1.4 data, normalises timestamps, adds
     indexes and full-text search. See :mod:`.v4_takeover`.
5    Durable chat upload usage, so deleting messages cannot reset daily quotas.

A 1.4 database at version 3 upgrades in place. New databases are created from
``baseline_v3.sql`` followed by every later migration, so fresh and upgraded
installations always end up with the same schema.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from ...core.sqlite import execute_script
from . import v4_takeover, v5_chat_upload_usage

APPLICATION_ID = 0x42574B49  # "BWKI", unchanged from 1.4
BASELINE = 3
MIGRATIONS = (
    v4_takeover.upgrade,  # 3 -> 4
    v5_chat_upload_usage.upgrade,  # 4 -> 5
)
LATEST = BASELINE + len(MIGRATIONS)

_BASELINE_SQL = Path(__file__).with_name("baseline_v3.sql")

DEFAULT_HOME = "# Welcome to your wiki\n\nEdit this page to get started."


def bootstrap(conn: sqlite3.Connection) -> None:
    """Create a brand-new database at the latest version."""
    execute_script(conn, _BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO site_settings (id) VALUES (1)")
    conn.execute("INSERT OR IGNORE INTO cleanup_lease (id) VALUES (1)")
    conn.execute("INSERT OR IGNORE INTO federation_identity (id, wiki_id) VALUES (1, ?)", (str(uuid.uuid4()),))
    conn.execute(
        "INSERT INTO pages (title, slug, content, is_home) VALUES (?, ?, ?, 1)",
        ("Home", "home", DEFAULT_HOME),
    )
    for migration in MIGRATIONS:
        migration(conn)
