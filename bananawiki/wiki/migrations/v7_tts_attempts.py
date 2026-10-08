# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Schema version 7: ``tts_generations.attempts``.

A read-aloud job whose worker died (out of memory, a crash in the speech
engine) went back to the queue every time, so a page that kills the worker
was tried forever. The column counts such claims; see
:func:`bananawiki.wiki.features.tts.service.recover_stale`. Existing rows
start at 0.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    from ..features.tts.schema import upgrade_v7

    upgrade_v7(conn)
