"""Version 16: a saved GPU server address is the read-aloud GPU switch.

The settings page offered "Use a remote GPU server" next to the server's
address. The box is gone: hosted wikis use the GPU server whenever an address
is saved. Where the box was off, the stored address is emptied, so nothing
changes; ``global_tts_gpu_enabled`` follows the address for older readers.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE hosting_settings SET global_tts_gpu_url = '' WHERE global_tts_gpu_enabled = 0")
    conn.execute("UPDATE hosting_settings SET global_tts_gpu_enabled = "
                 "CASE WHEN TRIM(COALESCE(global_tts_gpu_url, '')) = '' THEN 0 ELSE 1 END")
