"""Schema additions of the REST API (run by migration 4).

* ``api_service__tokens.credential_stamp``: which password a token was issued
  under (see :func:`tokens.credential_stamp`); tokens of 1.4 are stamped with
  the current one.
* ``api_service__idempotency``: answers to ``POST`` requests sent with an
  ``Idempotency-Key``, kept for a day so a retried request is answered
  instead of being run twice.
* ``api_service__webhooks`` and ``api_service__webhook_deliveries``: outgoing
  webhooks configured by administrators and their delivery queue and log;
  ``failing_since``, ``disabled_reason`` and ``disabled_at`` record a run of
  failures and the automatic switch-off it led to.
* ``site_settings.api_service_webhook_max_failures``: failed attempts in a
  row after which a webhook is switched off (0: never).
"""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns, table_exists

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS api_service__idempotency (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idempotency_key TEXT    NOT NULL,
        request_hash    TEXT    NOT NULL,
        status_code     INTEGER,
        response_body   TEXT,
        created_at      TEXT    NOT NULL,
        UNIQUE (user_id, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_service__webhooks (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        url                   TEXT    NOT NULL,
        description           TEXT    NOT NULL DEFAULT '',
        secret                TEXT    NOT NULL,
        events                TEXT    NOT NULL DEFAULT '[]',
        active                INTEGER NOT NULL DEFAULT 1,
        allow_private_network INTEGER NOT NULL DEFAULT 0,
        consecutive_failures  INTEGER NOT NULL DEFAULT 0,
        last_delivery_at      TEXT,
        last_status           TEXT,
        created_by            TEXT REFERENCES users(id) ON DELETE SET NULL,
        created_at            TEXT    NOT NULL,
        updated_at            TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_service__webhook_deliveries (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        webhook_id       INTEGER NOT NULL REFERENCES api_service__webhooks(id) ON DELETE CASCADE,
        delivery_uuid    TEXT    NOT NULL,
        event            TEXT    NOT NULL,
        payload          TEXT    NOT NULL,
        state            TEXT    NOT NULL DEFAULT 'pending',
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT,
        response_status  INTEGER,
        error            TEXT,
        duration_ms      INTEGER,
        created_at       TEXT    NOT NULL,
        finished_at      TEXT
    )
    """,
)

_INDEXES = {
    "idx_api_service_idempotency_created": "api_service__idempotency(created_at)",
    "idx_api_service_deliveries_due": "api_service__webhook_deliveries(state, next_attempt_at)",
    "idx_api_service_deliveries_webhook": "api_service__webhook_deliveries(webhook_id, id)",
    "idx_api_service_deliveries_created": "api_service__webhook_deliveries(created_at)",
}


def upgrade_v4(conn: sqlite3.Connection) -> None:
    for statement in _TABLES:
        conn.execute(statement)
    add_columns(conn, "api_service__webhooks", {
        "failing_since": "TEXT",
        "disabled_reason": "TEXT",
        "disabled_at": "TEXT",
    })
    if table_exists(conn, "site_settings"):
        add_columns(conn, "site_settings", {"api_service_webhook_max_failures": "INTEGER NOT NULL DEFAULT 20"})
    for name, target in _INDEXES.items():
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    if not table_exists(conn, "api_service__tokens"):
        return
    add_columns(conn, "api_service__tokens", {"credential_stamp": "TEXT"})
    from .tokens import credential_stamp

    conn.create_function("bw_credential_stamp", 1, credential_stamp, deterministic=True)
    conn.execute("UPDATE api_service__tokens SET credential_stamp = (SELECT bw_credential_stamp(password) FROM users "
                 "WHERE users.id = api_service__tokens.user_id) WHERE credential_stamp IS NULL")
