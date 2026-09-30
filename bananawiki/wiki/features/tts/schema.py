"""Schema additions for read-aloud audio (1.6).

``tts_generations`` is the 1.4 table. Two nullable columns make the queue
durable across processes:

* ``not_before`` - a retried or rate-limited job waits until this time;
* ``lease_until`` - a worker that claimed a job renews this while it works,
  so a job whose worker died is handed to another worker once it expires.
"""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns, table_exists


def upgrade_v4(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "tts_generations"):
        return
    add_columns(conn, "tts_generations", {"not_before": "TEXT", "lease_until": "TEXT"})
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tts_generations_queue ON tts_generations(status, requested_at, id)"
    )
