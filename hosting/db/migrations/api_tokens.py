"""Version 2: the REST API switch, personal access tokens and signup approval notifications."""

from sqlite_migrations import add_columns, execute_script


def upgrade(connection):
    # The API stays off on every existing platform until an administrator
    # switches it on in Platform Settings.
    add_columns(connection, "hosting_settings", {
        "api_enabled": "INTEGER NOT NULL DEFAULT 0",
    })
    # Only a SHA-256 digest of each token is kept. ``prefix`` is the first few
    # characters, enough for the owner to tell tokens apart on the Account page.
    execute_script(connection, """
        CREATE TABLE IF NOT EXISTS hosting_api_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name         TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 60),
            prefix       TEXT NOT NULL,
            token_hash   TEXT NOT NULL,
            scopes       TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            expires_at   TEXT,
            last_used_at TEXT,
            revoked_at   TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ix_hosting_api_tokens_token_hash
            ON hosting_api_tokens (token_hash);
        CREATE INDEX IF NOT EXISTS ix_hosting_api_tokens_account
            ON hosting_api_tokens (account_id, revoked_at);
    """)
    # Admin notifications for signups awaiting approval: destination address,
    # immediate or digest mode, digest cadence and when the last digest went
    # out (NULL means never). An empty address turns them all off.
    add_columns(connection, "hosting_settings", {
        "approval_notify_email": "TEXT NOT NULL DEFAULT ''",
        "approval_notify_mode": "TEXT NOT NULL DEFAULT 'digest'",
        "approval_notify_digest_hours": "INTEGER NOT NULL DEFAULT 6",
        "approval_notify_last_digest_at": "TEXT",
    })
