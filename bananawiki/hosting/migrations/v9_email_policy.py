"""Version 9: email policy, pausing email, notification switches and easter eggs.

``hosting_settings`` gains:

* ``signup_email`` and ``existing_email``: ``required``, ``optional`` or
  ``off``. ``signup_email`` says whether the sign-up form asks for an address;
  ``existing_email`` whether accounts without one must add one (a page they
  cannot leave), are asked once (a prompt they can dismiss) or are left
  alone. Both are worked out from the 1.4 columns ``ask_email_new_signup``,
  ``ask_email_existing_users`` and ``email_required``, which stay (and are
  still written) for older releases but are no longer read.
* ``email_shared``: 1 lets several accounts use the same address. The unique
  index ``ix_accounts_active_email`` becomes a plain one, so the setting can
  change both ways; while it is 0 the portal refuses an address another
  active account uses (:func:`bananawiki.hosting.accounts.check_email`).
* ``email_paused``, ``email_paused_at`` and ``email_paused_held``: while
  paused the portal sends no email at all; the count of messages held back
  since the pause began is shown to administrators.
* ``email_notices`` and ``attention_email_sources``: JSON objects of
  ``kind -> 0/1`` for the notices account holders get and the waiting
  requests administrators are emailed about. A missing key means on.
* ``easter_shades``, ``easter_konami`` and ``easter_peek``: the portal's
  easter eggs (the mascot's sunglasses, the Konami code's banana shower and
  the mascot peeking into "page not found"), all on.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns


def upgrade(conn: sqlite3.Connection) -> None:
    add_columns(conn, "hosting_settings", {
        "signup_email": "TEXT NOT NULL DEFAULT 'optional'",
        "existing_email": "TEXT NOT NULL DEFAULT 'off'",
        "email_shared": "INTEGER NOT NULL DEFAULT 0",
        "email_paused": "INTEGER NOT NULL DEFAULT 0",
        "email_paused_at": "TEXT",
        "email_paused_held": "INTEGER NOT NULL DEFAULT 0",
        "email_notices": "TEXT NOT NULL DEFAULT '{}'",
        "attention_email_sources": "TEXT NOT NULL DEFAULT '{}'",
        "easter_shades": "INTEGER NOT NULL DEFAULT 1",
        "easter_konami": "INTEGER NOT NULL DEFAULT 1",
        "easter_peek": "INTEGER NOT NULL DEFAULT 1",
    })
    conn.execute(
        "UPDATE hosting_settings SET "
        "signup_email = CASE WHEN ask_email_new_signup = 0 THEN 'off' "
        "WHEN email_required = 1 THEN 'required' ELSE 'optional' END, "
        "existing_email = CASE WHEN ask_email_existing_users = 0 THEN 'off' "
        "WHEN email_required = 1 THEN 'required' ELSE 'optional' END"
    )
    conn.execute("DROP INDEX IF EXISTS ix_accounts_active_email")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_accounts_email ON accounts (email COLLATE NOCASE) "
                 "WHERE email <> '' AND deleted_at IS NULL")
