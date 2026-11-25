"""Version 15: fresh sign-in on request, acting administrators in wikis and apps, account links made by
administrators.

* ``hosting_settings`` gains ``admin_app_signin`` (0: administrators acting
  as an account cannot sign in to wikis and apps as it), ``admin_act_minutes``
  (30: acting ends by itself after this) and ``admin_act_notify`` (0: the
  account is not emailed when an administrator starts acting as it).
* ``hosting_impersonation_logs`` gains the reason the administrator gave
  (``reason``), when acting ends by itself (``expires_at``) and why it ended
  (``end_reason``: ``stopped``, ``expired`` or ``signed_out``).
* Sign-in codes and access tokens remember the portal sign-in they came from
  (``auth_time``: Unix seconds, ``amr``: what it used, space-separated) and,
  when an acting administrator signed in, who acts (``act_account_id``), until
  when (``act_expires_at``) and the acting it belongs to (``act_log_id``).

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns

_SIGN_IN = {
    "auth_time": "INTEGER",
    "amr": "TEXT NOT NULL DEFAULT ''",
    "act_account_id": "TEXT",
    "act_expires_at": "TEXT",
    "act_log_id": "INTEGER",
}


def upgrade(conn: sqlite3.Connection) -> None:
    add_columns(conn, "hosting_settings", {
        "admin_app_signin": "INTEGER NOT NULL DEFAULT 0",
        "admin_act_minutes": "INTEGER NOT NULL DEFAULT 30",
        "admin_act_notify": "INTEGER NOT NULL DEFAULT 0",
    })
    add_columns(conn, "hosting_impersonation_logs", {
        "reason": "TEXT NOT NULL DEFAULT ''",
        "expires_at": "TEXT",
        "end_reason": "TEXT",
    })
    add_columns(conn, "hosting_oauth_authorization_codes", dict(_SIGN_IN))
    add_columns(conn, "hosting_oauth_access_tokens", dict(_SIGN_IN))
    conn.execute("CREATE INDEX IF NOT EXISTS ix_oauth_access_tokens_act ON hosting_oauth_access_tokens (act_log_id) "
                 "WHERE act_log_id IS NOT NULL")
