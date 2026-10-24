"""Wiki database schema versions.

Version history
---------------
1-3  BananaWiki 1.4 (legacy import, navigation index, federation tables).
4    BananaWiki 1.6 takeover: repairs 1.4 data, normalises timestamps, adds
     indexes and full-text search. See :mod:`.v4_takeover`.
5    Durable chat upload usage, so deleting messages cannot reset daily quotas.
6    Who imposed a suspension (``suspension_audit.imposed_by_top``) and an
     index for former user names. See :mod:`.v6_account_history`.
7    Read-aloud claims that ended without an answer from their worker
     (``tts_generations.attempts``). See :mod:`.v7_tts_attempts`.
8    Tokens issued by tokens (``api_service__tokens.parent_id``), direct
     conversations cleared per participant and reported chat messages. See
     :mod:`.v8_tokens_and_chat_safety`.
9    Two-step verification for accounts (``auth__two_step``) and the
     setting that requires it of administrators. See :mod:`.v9_two_step`.
10   Members block other members in chats (``chat__blocks``), and direct
     messages withheld from someone who blocked their sender
     (``chat_messages.withheld``). See :mod:`.v10_chat_blocks`.
11   Kanban columns that count as done (``kanban_columns.is_done``): their
     tickets are never overdue. See :mod:`.v11_kanban_done_columns`.
12   ``api_service__idempotency.token_id`` on databases whose version 4 ran
     without it, an index of page history by date, and the new default
     colours for themes that still have the old ones. See
     :mod:`.v12_idempotency_tokens`.
13   Single sign-out from the hosting portal: the last sign-out fetched
     (``platform_oauth_state``). See :mod:`.v13_platform_signouts`.
14   BM mode, which replaced the easy wiki switch: ``site_settings.bm_mode``,
     ``bm_mode_env`` and ``bm_choices``. See :mod:`.v14_bm_mode`.
15   Easter eggs switched off one by one: ``site_settings.easter_eggs``.
     See :mod:`.v15_easter_eggs`.

A 1.4 database at version 3 upgrades in place. New databases are created from
``baseline_v3.sql`` followed by every later migration, so fresh and upgraded
installations always end up with the same schema.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from ...core.sqlite import execute_script
from . import (
    v4_takeover,
    v5_chat_upload_usage,
    v6_account_history,
    v7_tts_attempts,
    v8_tokens_and_chat_safety,
    v9_two_step,
    v10_chat_blocks,
    v11_kanban_done_columns,
    v12_idempotency_tokens,
    v13_platform_signouts,
    v14_bm_mode,
    v15_easter_eggs,
)

APPLICATION_ID = 0x42574B49  # "BWKI", unchanged from 1.4
BASELINE = 3
MIGRATIONS = (
    v4_takeover.upgrade,  # 3 -> 4
    v5_chat_upload_usage.upgrade,  # 4 -> 5
    v6_account_history.upgrade,  # 5 -> 6
    v7_tts_attempts.upgrade,  # 6 -> 7
    v8_tokens_and_chat_safety.upgrade,  # 7 -> 8
    v9_two_step.upgrade,  # 8 -> 9
    v10_chat_blocks.upgrade,  # 9 -> 10
    v11_kanban_done_columns.upgrade,  # 10 -> 11
    v12_idempotency_tokens.upgrade,  # 11 -> 12
    v13_platform_signouts.upgrade,  # 12 -> 13
    v14_bm_mode.upgrade,  # 13 -> 14
    v15_easter_eggs.upgrade,  # 14 -> 15
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
