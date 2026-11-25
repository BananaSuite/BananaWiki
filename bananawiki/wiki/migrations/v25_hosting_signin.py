"""Schema version 25: sign-in methods, confirming with the hosting portal, acting hosting administrators.

* ``users.password_set``: 0 for an account created through the hosting
  portal's sign-in, which never had a password of its own (it was given a
  random one). Such accounts confirm sensitive changes with the portal only,
  set a password later, and cannot disconnect the portal before they have one.
  Earlier accounts are worked out once: linked to a portal account within a
  minute of being created, never signed in with a password, and never given a
  new one (audit log). Anything else counts as having a password.
* ``user_sessions.reverified_at``: when the session last confirmed its
  account with the hosting portal (a fresh portal sign-in), which stands for
  the password for ten minutes.
* ``user_sessions.acting_admin_sub``, ``acting_admin_name`` and
  ``acting_expires_at``: a session a hosting administrator opened while acting
  as the account (the portal's ``act``), and when it ends.
* ``site_settings.platform_allow_acting``: 1 lets hosting administrators sign
  in as members while acting as them (the portal's own setting decides first).

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, column_names, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "users"):
        fresh = "password_set" not in column_names(conn, "users")
        add_columns(conn, "users", {"password_set": "INTEGER NOT NULL DEFAULT 1"})
        if fresh and table_exists(conn, "platform_oauth_links"):
            audited = (" AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.target_id = u.id AND a.action IN "
                       "('user.password_changed', 'user.password_reset'))") if table_exists(conn, "audit_log") else ""
            conn.execute(
                "UPDATE users SET password_set = 0 WHERE id IN (SELECT u.id FROM users u "
                "JOIN platform_oauth_links l ON l.user_id = u.id "
                "WHERE abs(julianday(l.linked_at) - julianday(u.created_at)) * 86400 <= 60 "
                "AND u.original_password_backup IS NULL AND u.force_password_change = 0 "
                "AND NOT EXISTS (SELECT 1 FROM user_sessions s WHERE s.user_id = u.id "
                "AND s.auth_method NOT LIKE 'platform_oauth%')" + audited + ")"
            )
    if table_exists(conn, "user_sessions"):
        add_columns(conn, "user_sessions", {
            "reverified_at": "TEXT",
            "acting_admin_sub": "TEXT",
            "acting_admin_name": "TEXT",
            "acting_expires_at": "TEXT",
        })
    if table_exists(conn, "site_settings"):
        add_columns(conn, "site_settings", {"platform_allow_acting": "INTEGER NOT NULL DEFAULT 1"})
