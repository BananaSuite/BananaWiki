"""Version 14: administrator approval is one switch, not also a sign-up mode.

Until now "Administrator approval" was both a sign-up mode and the box
``hosting_activation_required``. The mode now only says who may sign up
(``open``, ``invite`` or ``closed``) and the box says whether new accounts
wait for an administrator, with either of the first two. A platform in the
old mode "approval" becomes "open" with the box ticked, which is what that
mode did.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE hosting_settings SET signup_mode = 'open', hosting_activation_required = 1 "
                 "WHERE signup_mode = 'approval'")
