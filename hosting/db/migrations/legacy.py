"""One-time import of pre-versioned hosting schemas and settings."""

import os

from sqlite_migrations import add_columns, execute_script
from .legacy_rebuilds import (
    _instances_table_has_legacy_subdomain_unique,
    _instances_table_has_not_null_expires_at,
    _rebuild_instances_with_composite_unique,
)


def _upgrade_legacy(conn):
    """Import supported pre-versioned schemas without intermediate commits."""
    _init_hosting_tables(conn)
    _init_domain_and_event_tables(conn)


def _init_domain_and_event_tables(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(instances)")}
    if "custom_domain_allowed" not in columns:
        conn.execute("ALTER TABLE instances ADD COLUMN custom_domain_allowed INTEGER NOT NULL DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instance_custom_domains (
            domain TEXT PRIMARY KEY COLLATE NOCASE,
            instance_id TEXT NOT NULL UNIQUE REFERENCES instances(id) ON DELETE CASCADE,
            verification_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            verified_at TEXT,
            verified_until TEXT,
            last_checked_at TEXT
        )
    """)
    domain_columns = {row[1] for row in conn.execute("PRAGMA table_info(instance_custom_domains)")}
    if "last_checked_at" not in domain_columns:
        conn.execute("ALTER TABLE instance_custom_domains ADD COLUMN last_checked_at TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hosting_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL CHECK(subject_type IN ('account', 'instance')),
            subject_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor_id TEXT,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_hosting_events_subject ON hosting_events(subject_type, subject_id, id)")


def _init_hosting_tables(conn):
    """Create tables, indexes, and run migrations inside *conn*."""
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id          TEXT PRIMARY KEY,
            username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password    TEXT NOT NULL,
            is_admin    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            decision_reason TEXT NOT NULL DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_account_sessions (
            id              TEXT PRIMARY KEY,
            token_hash      TEXT NOT NULL,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            session_version INTEGER NOT NULL,
            created_at      TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            expires_at      TEXT NOT NULL,
            auth_method     TEXT NOT NULL DEFAULT 'password',
            ip_address      TEXT NOT NULL DEFAULT '',
            last_ip         TEXT NOT NULL DEFAULT '',
            user_agent      TEXT NOT NULL DEFAULT '',
            revoked_at      TEXT
        )
    """)
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_hosting_account_sessions_token_hash "
        "ON hosting_account_sessions (token_hash)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_hosting_account_sessions_account_active "
        "ON hosting_account_sessions (account_id, revoked_at, expires_at)"
    )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_banners (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            content           TEXT NOT NULL DEFAULT '',
            color             TEXT NOT NULL DEFAULT 'orange'
                                      CHECK(color IN ('red','orange','yellow','blue','green')),
            visibility        TEXT NOT NULL DEFAULT 'both'
                                      CHECK(visibility IN ('logged_in','logged_out','both')),
            audience_mode     TEXT NOT NULL DEFAULT 'all'
                                      CHECK(audience_mode IN ('all','allowlist','denylist')),
            expires_at        TEXT,
            is_active         INTEGER NOT NULL DEFAULT 1,
            not_removable     INTEGER NOT NULL DEFAULT 1,
            show_countdown    INTEGER NOT NULL DEFAULT 0,
            custom_background TEXT,
            custom_text_color TEXT,
            revision          INTEGER NOT NULL DEFAULT 1,
            created_by        TEXT REFERENCES accounts(id) ON DELETE SET NULL,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_banner_audience (
            banner_id  INTEGER NOT NULL REFERENCES hosting_banners(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            PRIMARY KEY (banner_id, account_id)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosting_banner_audience_account "
        "ON hosting_banner_audience(account_id, banner_id)"
    )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_impersonation_logs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            started_at        TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at          TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS instances (
            id          TEXT PRIMARY KEY,
            account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            subdomain   TEXT NOT NULL UNIQUE COLLATE NOCASE,
            status      TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'stopped', 'suspended', 'terminated')),
            port        INTEGER,
            admin_username TEXT,
            admin_password_plain TEXT,
            storage_limit_mb INTEGER,
            upload_max_size_mb INTEGER,
            upload_blocked_extensions TEXT,
            created_at  TEXT NOT NULL,
            -- ``expires_at`` is nullable: apex / vanity-domain instances
            -- are created with ``NULL`` to mean "never auto-expires".
            expires_at  TEXT,
            suspended_at TEXT,
            -- Total whole seconds an instance has spent in the
            -- ``suspended`` state across all suspensions.  When an
            -- instance is unsuspended ``expires_at`` is shifted by the
            -- delta (current ``suspended_at`` to now) so the user gets
            -- back the time they were locked out.
            suspended_accumulated_seconds INTEGER NOT NULL DEFAULT 0,
            -- Timed suspension: NULL means permanent; ISO datetime = auto-unsuspend when reached
            suspended_until TEXT,
            -- Suspension reason shown on the suspended page
            suspend_reason TEXT NOT NULL DEFAULT '',
            -- Whether the reason is visible to the instance owner
            suspend_reason_visible INTEGER NOT NULL DEFAULT 0,
            -- Whether remaining suspension time is shown to the owner
            suspend_time_visible INTEGER NOT NULL DEFAULT 0,
            stopped_at  TEXT,
            terminated_at TEXT,
            -- EasyWiki mode: minimal-feature flag (0=full, 1=easy)
            easy_wiki INTEGER NOT NULL DEFAULT 0,
            public_wiki_allowed INTEGER NOT NULL DEFAULT 0,
            page_builder_allowed INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_login_attempts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ip           TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_hosting_login_attempts_ip_at
        ON hosting_login_attempts (ip, attempted_at)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_rate_limit_hits (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ip       TEXT NOT NULL,
            bucket   TEXT NOT NULL,
            hit_at   TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_hosting_rate_limit_hits_ip_bucket
        ON hosting_rate_limit_hits (ip, bucket, hit_at)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_settings (
            id                              INTEGER PRIMARY KEY DEFAULT 1,
            gdrive_backup_enabled           INTEGER NOT NULL DEFAULT 0,
            gdrive_credentials_path         TEXT NOT NULL DEFAULT '',
            gdrive_folder_id                TEXT NOT NULL DEFAULT '',
            gdrive_retention_days           INTEGER NOT NULL DEFAULT 7,
            gdrive_backup_time              TEXT NOT NULL DEFAULT '03:00',
            last_gdrive_backup_at           REAL NOT NULL DEFAULT 0,
            global_limit_enabled           INTEGER NOT NULL DEFAULT 1,
            global_limit_max_instances     INTEGER NOT NULL DEFAULT 50,
            global_limit_max_storage_mb    INTEGER NOT NULL DEFAULT 5000,
            global_wiki_upload_max_size_mb INTEGER NOT NULL DEFAULT 100,
            global_wiki_blocked_extensions TEXT NOT NULL DEFAULT '',
            grace_period_days              INTEGER NOT NULL DEFAULT 30,
            signup_mode                    TEXT NOT NULL DEFAULT 'open',
            ask_email_new_signup           INTEGER NOT NULL DEFAULT 0,
            ask_email_existing_users       INTEGER NOT NULL DEFAULT 0,
            email_required                 INTEGER NOT NULL DEFAULT 0,
            email_verification_required    INTEGER NOT NULL DEFAULT 0,
            email_verification_cooldown_seconds INTEGER NOT NULL DEFAULT 60,
            contact_email_policy_version   INTEGER NOT NULL DEFAULT 1,
            forbid_non_admin_public_wikis  INTEGER NOT NULL DEFAULT 1,
            forbid_non_admin_page_builder  INTEGER NOT NULL DEFAULT 1,
            auto_approve_public_access_requests INTEGER NOT NULL DEFAULT 0,
            auto_approve_page_builder_requests INTEGER NOT NULL DEFAULT 0,
            global_tts_gpu_enabled         INTEGER NOT NULL DEFAULT 0,
            global_tts_gpu_url             TEXT NOT NULL DEFAULT '',
            global_tts_gpu_auth_token      TEXT NOT NULL DEFAULT '',
            global_tts_gpu_timeout         INTEGER NOT NULL DEFAULT 120,
            global_tts_enabled             INTEGER NOT NULL DEFAULT 1,
            global_tts_mode                TEXT NOT NULL DEFAULT 'all',
            global_tts_list                TEXT NOT NULL DEFAULT '',
            global_tour_enabled           INTEGER NOT NULL DEFAULT 0,
            allow_owner_delete_expired    INTEGER NOT NULL DEFAULT 0,
            allow_owner_download_expired  INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Ensure the single settings row always exists
    default_signup_mode = os.environ.get(
        "HOSTING_DEFAULT_SIGNUP_MODE", "open"
    ).strip().lower()
    if default_signup_mode not in {"open", "approval", "invite", "closed"}:
        default_signup_mode = "open"
    cur.execute(
        "INSERT OR IGNORE INTO hosting_settings (id, signup_mode) VALUES (1, ?)",
        (default_signup_mode,),
    )

    # Invite codes table.  ``signup_mode = 'invite'`` requires
    # one of these codes during signup; admins manage them from the
    # admin dashboard.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_invite_codes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            code         TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_by   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            note         TEXT NOT NULL DEFAULT '',
            max_uses     INTEGER NOT NULL DEFAULT 1,
            current_uses INTEGER NOT NULL DEFAULT 0,
            expires_at   TEXT,
            last_used_by TEXT,
            last_used_at TEXT
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_hosting_invite_codes_code "
        "ON hosting_invite_codes (code)"
    )

    # Migrations
    add_columns(conn, 'accounts', {
        'is_admin': 'INTEGER NOT NULL DEFAULT 0',
        'email': "TEXT NOT NULL DEFAULT ''",
        'terms_accepted_at': 'TEXT',
        'terms_version': "TEXT NOT NULL DEFAULT ''",
        'email_prompt_dismissed': 'INTEGER NOT NULL DEFAULT 0',
        'deleted_at': 'TEXT',
        'email_verified_at': 'TEXT',
        'email_verification_token_hash': "TEXT NOT NULL DEFAULT ''",
        'email_verification_sent_at': 'TEXT',
        'email_verification_expires_at': 'TEXT',
        'password_reset_token_hash': "TEXT NOT NULL DEFAULT ''",
        'password_reset_sent_at': 'TEXT',
        'password_reset_expires_at': 'TEXT',
        'session_version': 'INTEGER NOT NULL DEFAULT 0',
    })

    # A verified contact address identifies one active account. Older builds
    # allowed duplicates; keep the first record and require the others to add
    # a different address on their next login before creating the unique
    # partial index. The database constraint closes concurrent-signup races
    # that application-level pre-checks cannot prevent.
    cur.execute(
        "UPDATE accounts SET email='', email_verified_at=NULL, "
        "email_verification_token_hash='', email_verification_sent_at=NULL, "
        "email_verification_expires_at=NULL "
        "WHERE email <> '' AND deleted_at IS NULL AND id NOT IN ("
        "SELECT MIN(id) FROM accounts WHERE email <> '' AND deleted_at IS NULL "
        "GROUP BY lower(email))"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_active_email "
        "ON accounts(email COLLATE NOCASE) "
        "WHERE email <> '' AND deleted_at IS NULL"
    )

    inst_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(instances)").fetchall()
    }
    add_columns(conn, 'instances', {
        'admin_username': 'TEXT',
        'admin_password_plain': 'TEXT',
        # Grace period: keep instance data on disk for a configurable window
        # after termination/expiry so admins can download or restore it.
        'data_retained_until': 'TEXT',
        'terminated_reason': 'TEXT',
        # ``domain_mode`` controls the URL format for the instance:
        #   * ``"hosting"`` (default): ``{slug}-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``
        #     (e.g. ``team-hosting.example.com``): always allowed.
        #   * ``"apex"``: ``{slug}.{BASE_DOMAIN}`` (e.g. ``wiki.example.com``):
        #     admin-only.  When an account loses admin, its apex instances
        #     are converted back to ``"hosting"`` automatically.
        'domain_mode': "TEXT NOT NULL DEFAULT 'hosting'",
    })

    # ``(subdomain, domain_mode)`` uniqueness migration.
    #
    # The original ``instances`` schema declared
    # ``subdomain TEXT NOT NULL UNIQUE COLLATE NOCASE`` which puts a
    # column-level UNIQUE constraint on ``subdomain`` alone.  That
    # prevents the same slug from being claimed in both apex mode
    # (``slug.{BASE_DOMAIN}``) and hosting mode
    # (``slug-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``) even though those
    # are distinct public URLs.
    #
    # SQLite has no ``ALTER TABLE DROP CONSTRAINT``, so we detect the
    # legacy single-column UNIQUE index and rebuild the table with a
    # composite ``UNIQUE (subdomain, domain_mode)`` instead.  The
    # migration runs at most once per database: subsequent calls find
    # no single-column UNIQUE index on ``subdomain`` and skip it.
    if _instances_table_has_legacy_subdomain_unique(conn):
        _rebuild_instances_with_composite_unique(conn)
        inst_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(instances)").fetchall()
        }
    add_columns(conn, 'instances', {
        'storage_limit_mb': 'INTEGER',
        'upload_max_size_mb': 'INTEGER',
        'upload_blocked_extensions': 'TEXT',
        # Suspension bookkeeping for the frozen expiry timer.
        'suspended_at': 'TEXT',
        'suspended_accumulated_seconds': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Legacy databases declared ``expires_at TEXT NOT NULL``.  Apex
    # instances must be allowed to carry a NULL expiry now, so detect
    # the legacy constraint and rebuild the table to relax it.  The
    # rebuild also folds in the newer ``suspended_at`` /
    # ``suspended_accumulated_seconds`` columns from above.
    if _instances_table_has_not_null_expires_at(conn):
        _rebuild_instances_with_composite_unique(conn)

    # A table rebuild can add columns that were not present in the original
    # PRAGMA snapshot. Refresh before the remaining additive migrations so
    # they never attempt to add a column twice.
    inst_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(instances)").fetchall()
    }

    # hosting_settings migrations
    hs_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(hosting_settings)").fetchall()
    }
    add_columns(conn, 'hosting_settings', {
        'gdrive_backup_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'gdrive_credentials_path': "TEXT NOT NULL DEFAULT ''",
        'gdrive_folder_id': "TEXT NOT NULL DEFAULT ''",
        'gdrive_retention_days': 'INTEGER NOT NULL DEFAULT 7',
        'gdrive_backup_time': "TEXT NOT NULL DEFAULT '03:00'",
        'last_gdrive_backup_at': 'REAL NOT NULL DEFAULT 0',
        'global_limit_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'global_limit_max_instances': 'INTEGER NOT NULL DEFAULT 50',
        'global_limit_max_storage_mb': 'INTEGER NOT NULL DEFAULT 5000',
        'global_wiki_upload_max_size_mb': 'INTEGER NOT NULL DEFAULT 100',
        'global_wiki_blocked_extensions': "TEXT NOT NULL DEFAULT ''",
        # Bot protection on auth forms (enabled by default)
        'bot_protection_enabled': 'INTEGER NOT NULL DEFAULT 1',
        # Grace period (days) for terminated/expired instance data retention.
        'grace_period_days': 'INTEGER NOT NULL DEFAULT 30',
        # Signup gating mode: 'open', 'invite', 'closed', 'approval'.
        'signup_mode': "TEXT NOT NULL DEFAULT 'open'",
        'ask_email_new_signup': 'INTEGER NOT NULL DEFAULT 0',
        'ask_email_existing_users': 'INTEGER NOT NULL DEFAULT 1',
        'email_required': 'INTEGER NOT NULL DEFAULT 1',
        'email_verification_required': 'INTEGER NOT NULL DEFAULT 0',
        'email_verification_cooldown_seconds': 'INTEGER NOT NULL DEFAULT 60',
    })
    # 1.4.1 changes contact email from an identity gate into a mandatory
    # operator-contact field. Existing installations inherited verification=1
    # from the old default, so migrate that default exactly once. Operators can
    # still opt back into verification afterwards without a later restart
    # silently changing their choice.
    if "contact_email_policy_version" not in hs_cols:
        cur.execute(
            "ALTER TABLE hosting_settings ADD COLUMN contact_email_policy_version "
            "INTEGER NOT NULL DEFAULT 0"
        )
        cur.execute(
            "UPDATE hosting_settings SET ask_email_new_signup=0, "
            "ask_email_existing_users=1, email_required=1, "
            "email_verification_required=0, contact_email_policy_version=1 "
            "WHERE id=1"
        )
    cur.execute("UPDATE hosting_settings SET global_limit_enabled=1")
    add_columns(conn, 'hosting_settings', {
        'forbid_non_admin_public_wikis': 'INTEGER NOT NULL DEFAULT 1',
        'forbid_non_admin_page_builder': 'INTEGER NOT NULL DEFAULT 1',
        'auto_approve_public_access_requests': 'INTEGER NOT NULL DEFAULT 0',
        'auto_approve_page_builder_requests': 'INTEGER NOT NULL DEFAULT 0',
    })

    add_columns(conn, 'instances', {
        'declared_use_case': "TEXT NOT NULL DEFAULT ''",
        'tos_compliance_declared_at': 'TEXT',
    })
    # Global TTS GPU settings: platform admins can enable a remote GPU
    # server for all hosted wikis.  Individual wikis can override or disable.
    add_columns(conn, 'hosting_settings', {
        'global_tts_gpu_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'global_tts_gpu_url': "TEXT NOT NULL DEFAULT ''",
        'global_tts_gpu_auth_token': "TEXT NOT NULL DEFAULT ''",
        'global_tts_gpu_timeout': 'INTEGER NOT NULL DEFAULT 120',
        # Global TTS enable/disable policy: lets admins restrict TTS generation
        # platform-wide, per a whitelist, or per a blacklist of subdomains.
        'global_tts_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'global_tts_mode': "TEXT NOT NULL DEFAULT 'all'",
        'global_tts_list': "TEXT NOT NULL DEFAULT ''",
    })

    # Account suspension columns
    add_columns(conn, 'accounts', {
        'suspended': 'INTEGER NOT NULL DEFAULT 0',
        'suspended_at': 'TEXT',
        'suspend_reason': "TEXT NOT NULL DEFAULT ''",
        # Timed suspension + visibility (mirrors instance suspension UX)
        'suspended_until': 'TEXT',
        'suspend_reason_visible': 'INTEGER NOT NULL DEFAULT 0',
        'suspend_time_visible': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Account suspension audit log table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS account_suspension_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            action          TEXT NOT NULL CHECK(action IN ('suspend','unsuspend')),
            reason          TEXT,
            reason_visible  INTEGER NOT NULL DEFAULT 0,
            time_visible    INTEGER NOT NULL DEFAULT 0,
            duration        TEXT,
            suspended_until TEXT,
            performed_by    TEXT REFERENCES accounts(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Instance timed-suspension columns
    add_columns(conn, 'instances', {
        'suspended_until': 'TEXT',
        'suspend_reason': "TEXT NOT NULL DEFAULT ''",
        'suspend_reason_visible': 'INTEGER NOT NULL DEFAULT 0',
        'suspend_time_visible': 'INTEGER NOT NULL DEFAULT 0',
        # Grace-period suspension: when a terminated instance is in its grace
        # window, admins can suspend it to disable the user-facing DB export.
        'grace_period_suspended': 'INTEGER NOT NULL DEFAULT 0',
        # Track whether the instance was provisioned with user-supplied custom
        # credentials (True) or auto-generated temporary ones (False).  Used to
        # suppress the one-time password display on the detail page. Users who
        # set their own password already know it.
        'custom_credentials': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Instance suspension audit log table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS instance_suspension_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            action          TEXT NOT NULL CHECK(action IN ('suspend','unsuspend')),
            reason          TEXT,
            reason_visible  INTEGER NOT NULL DEFAULT 0,
            time_visible    INTEGER NOT NULL DEFAULT 0,
            duration        TEXT,
            suspended_until TEXT,
            performed_by    TEXT REFERENCES accounts(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Re-fetch column sets after all the ALTER TABLE above, in case
    # we just added some columns.
    hs_cols = {r[1] for r in cur.execute("PRAGMA table_info(hosting_settings)").fetchall()}

    # Account activation approval columns
    add_columns(conn, 'accounts', {
        'approval_status': "TEXT NOT NULL DEFAULT 'approved' CHECK(approval_status IN ('approved','pending','denied'))",
        'denied_at': 'TEXT',
        'denied_notified': 'INTEGER NOT NULL DEFAULT 0',
        'approved_by': 'TEXT REFERENCES accounts(id) ON DELETE SET NULL',
        'denied_by': 'TEXT REFERENCES accounts(id) ON DELETE SET NULL',
        'approved_denied_at': 'TEXT',
        'decision_reason': "TEXT NOT NULL DEFAULT ''",
        # Account theme mode preference (dark/light/default)
        'theme_mode': "TEXT NOT NULL DEFAULT 'default'",
        'totp_secret_encrypted': "TEXT NOT NULL DEFAULT ''",
        'totp_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'totp_recovery_hashes': "TEXT NOT NULL DEFAULT '[]'",
        'totp_enabled_at': 'TEXT',
        'totp_last_counter': 'INTEGER NOT NULL DEFAULT -1',
    })

    # Hosting activation approval settings
    add_columns(conn, 'hosting_settings', {
        'hosting_activation_required': 'INTEGER NOT NULL DEFAULT 0',
        'hosting_activation_denied_timeout_hours': 'INTEGER NOT NULL DEFAULT 24',
    })
    # Migrate from hours-only to seconds-based timeout.
    # hosting_activation_denied_timeout_seconds: 0 = immediate, -1 = no deletion, >0 = seconds.
    if "hosting_activation_denied_timeout_seconds" not in hs_cols:
        cur.execute(
            "ALTER TABLE hosting_settings ADD COLUMN hosting_activation_denied_timeout_seconds "
            "INTEGER NOT NULL DEFAULT 0"
        )
        # Migrate existing hours value to seconds (if the hours column has data)
        cur.execute(
            "UPDATE hosting_settings SET hosting_activation_denied_timeout_seconds = "
            "CASE WHEN hosting_activation_denied_timeout_hours > 0 THEN hosting_activation_denied_timeout_hours * 3600 ELSE 86400 END"
        )

    add_columns(conn, 'hosting_settings', {
        'global_tour_enabled': 'INTEGER NOT NULL DEFAULT 0',
        # Devtools: hosting platform admin tools visibility (always on for admins)
        'devtools_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'platform_oauth_enabled': 'INTEGER NOT NULL DEFAULT 0',
    })

    # OAuth client columns on instances
    if "oauth_client_id" not in inst_cols:
        cur.execute("ALTER TABLE instances ADD COLUMN oauth_client_id TEXT")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_instances_oauth_client_id "
            "ON instances (oauth_client_id) WHERE oauth_client_id IS NOT NULL"
        )
    if "oauth_client_secret_hash" not in inst_cols:
        cur.execute("ALTER TABLE instances ADD COLUMN oauth_client_secret_hash TEXT")

    # OAuth authorization codes table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_oauth_authorization_codes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            code            TEXT NOT NULL UNIQUE,
            client_id       TEXT NOT NULL,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            redirect_uri    TEXT NOT NULL DEFAULT '',
            used            INTEGER NOT NULL DEFAULT 0,
            expires_at      TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_auth_codes_code "
        "ON hosting_oauth_authorization_codes (code)"
    )

    # OAuth access tokens table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_oauth_access_tokens (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash      TEXT NOT NULL UNIQUE,
            client_id       TEXT NOT NULL,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            scope           TEXT NOT NULL DEFAULT 'openid profile',
            expires_at      TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_access_tokens_hash "
        "ON hosting_oauth_access_tokens (token_hash)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_access_tokens_account "
        "ON hosting_oauth_access_tokens (account_id)"
    )

    # Hosting-account ↔ wiki-user linking table.
    # Records that a hosting account has been linked to a specific wiki user
    # on a specific wiki instance.  This is the single source of truth for
    # merge decisions during platform OAuth SSO.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hosting_oauth_account_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            wiki_user_id    TEXT NOT NULL,
            wiki_username   TEXT NOT NULL,
            linked_at       TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(instance_id, account_id)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_links_instance "
        "ON hosting_oauth_account_links (instance_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_links_account "
        "ON hosting_oauth_account_links (account_id)"
    )

    # Re-fetch inst_cols after the ALTER TABLE additions above so that
    # subsequent migrations see the freshly-added columns.  This prevents
    # re-running the oauth_client_id / oauth_client_secret_hash migration.
    inst_cols = {
        row[1]
        for row in cur.execute("PRAGMA table_info(instances)").fetchall()
    }

    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS hosting_account_merge_requests (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        requested_by    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','approved','merged','cancelled','denied')),
        source_approved INTEGER NOT NULL DEFAULT 0,
        target_approved INTEGER NOT NULL DEFAULT 0,
        admin_approved  INTEGER NOT NULL DEFAULT 0,
        request_reason  TEXT NOT NULL DEFAULT '',
        completed_at    TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK(source_account_id != target_account_id)
    );
    CREATE INDEX IF NOT EXISTS ix_merge_requests_source
        ON hosting_account_merge_requests(source_account_id);
    CREATE INDEX IF NOT EXISTS ix_merge_requests_target
        ON hosting_account_merge_requests(target_account_id);
    CREATE INDEX IF NOT EXISTS ix_merge_requests_status
        ON hosting_account_merge_requests(status, created_at DESC);

    CREATE TABLE IF NOT EXISTS hosting_account_merge_logs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        source_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        merged_by       TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        instances_transferred INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS ix_merge_logs_target
        ON hosting_account_merge_logs(target_account_id);
    CREATE INDEX IF NOT EXISTS ix_merge_logs_source
        ON hosting_account_merge_logs(source_account_id);
    """)

    # Track pending merges on accounts table
    acct_cols = {row[1] for row in cur.execute("PRAGMA table_info(accounts)").fetchall()}
    add_columns(conn, 'accounts', {
        'pending_merge_source_id': 'TEXT REFERENCES accounts(id) ON DELETE SET NULL',
        'pending_merge_target_id': 'TEXT REFERENCES accounts(id) ON DELETE SET NULL',
        # An admin raises email_flagged_invalid to force the account to enter
        # a new address before it can carry on.
        'email_flagged_invalid': 'INTEGER NOT NULL DEFAULT 0',
        'email_flag_reason': "TEXT NOT NULL DEFAULT ''",
        'email_flagged_previous': "TEXT NOT NULL DEFAULT ''",
        'email_flag_reason_visible': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Re-fetch hosting_settings columns for the email-flag setting
    hs_cols = {r[1] for r in cur.execute("PRAGMA table_info(hosting_settings)").fetchall()}
    if "email_flag_block_reentry" not in hs_cols:
        cur.execute(
            "ALTER TABLE hosting_settings ADD COLUMN email_flag_block_reentry "
            "INTEGER NOT NULL DEFAULT 0"
        )

    # easy_wiki is the per-instance minimal-feature mode.
    if "easy_wiki" not in inst_cols:
        cur.execute(
            "ALTER TABLE instances ADD COLUMN easy_wiki "
            "INTEGER NOT NULL DEFAULT 0"
        )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS instance_collaborators (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            role            TEXT NOT NULL DEFAULT 'custom'
                                CHECK(role IN ('full_access', 'custom')),
            permissions     TEXT NOT NULL DEFAULT '[]',
            invited_by      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(instance_id, account_id)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_instance_collaborators_instance "
        "ON instance_collaborators (instance_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_instance_collaborators_account "
        "ON instance_collaborators (account_id)"
    )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS instance_ownership_transfers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id         TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            from_account_id     TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            to_account_id       TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            status              TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending', 'accepted', 'declined', 'cancelled')),
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at         TEXT,
            CHECK(from_account_id != to_account_id)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_ownership_transfers_instance "
        "ON instance_ownership_transfers (instance_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_ownership_transfers_to "
        "ON instance_ownership_transfers (to_account_id, status)"
    )

    add_columns(conn, 'hosting_settings', {
        'allow_owner_delete_expired': 'INTEGER NOT NULL DEFAULT 0',
        'allow_owner_download_expired': 'INTEGER NOT NULL DEFAULT 0',
        # arabic_mirror_enabled mirrors the whole layout to RTL, portal-wide.
        'arabic_mirror_enabled': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Account deletion runs on a countdown rather than taking effect at once.
    acct_cols = {r[1] for r in cur.execute("PRAGMA table_info(accounts)").fetchall()}
    add_columns(conn, 'accounts', {
        'pending_deletion': 'INTEGER NOT NULL DEFAULT 0',
        'pending_deletion_at': 'TEXT',
        'pending_deletion_seconds': 'INTEGER NOT NULL DEFAULT 86400',
        'pending_deletion_reason': "TEXT NOT NULL DEFAULT ''",
    })

    hs_cols = {r[1] for r in cur.execute("PRAGMA table_info(hosting_settings)").fetchall()}
    add_columns(conn, 'hosting_settings', {
        'instance_url_suffix': 'TEXT',
        'instance_suffix_disabled': 'INTEGER NOT NULL DEFAULT 0',
    })

    inst_cols = {r[1] for r in cur.execute("PRAGMA table_info(instances)").fetchall()}
    add_columns(conn, 'instances', {
        'public_wiki_allowed': 'INTEGER NOT NULL DEFAULT 0',
        'page_builder_allowed': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Feature requests double as the decision history for an instance: rows
    # are reviewed or cancelled in place, never deleted.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS instance_feature_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            feature         TEXT NOT NULL CHECK(feature IN ('public_access', 'page_builder')),
            requested_by    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            reason          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'approved', 'denied', 'cancelled')),
            reviewed_by     TEXT REFERENCES accounts(id) ON DELETE SET NULL,
            review_note     TEXT NOT NULL DEFAULT '',
            review_source   TEXT NOT NULL DEFAULT 'manual'
                                CHECK(review_source IN ('manual', 'automatic')),
            requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
            reviewed_at     TEXT,
            cancelled_at    TEXT
        )
    """)
    feature_request_cols = {
        row[1] for row in cur.execute("PRAGMA table_info(instance_feature_requests)").fetchall()
    }
    if "review_source" not in feature_request_cols:
        cur.execute(
            "ALTER TABLE instance_feature_requests ADD COLUMN review_source "
            "TEXT NOT NULL DEFAULT 'manual' "
            "CHECK(review_source IN ('manual', 'automatic'))"
        )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_instance_feature_requests_pending "
        "ON instance_feature_requests(instance_id, feature) WHERE status='pending'"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_instance_feature_requests_instance "
        "ON instance_feature_requests(instance_id, requested_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_instance_feature_requests_status "
        "ON instance_feature_requests(status, requested_at)"
    )

    # signup_use_case holds the applicant's own answer to "why do you want to
    # use this?", and hosting_settings decides whether it is mandatory.
    acct_cols = {r[1] for r in cur.execute("PRAGMA table_info(accounts)").fetchall()}
    if "signup_use_case" not in acct_cols:
        cur.execute(
            "ALTER TABLE accounts ADD COLUMN signup_use_case "
            "TEXT NOT NULL DEFAULT ''"
        )
    hs_cols = {r[1] for r in cur.execute("PRAGMA table_info(hosting_settings)").fetchall()}
    if "signup_use_case_required" not in hs_cols:
        cur.execute(
            "ALTER TABLE hosting_settings ADD COLUMN signup_use_case_required "
            "INTEGER NOT NULL DEFAULT 0"
        )

    retired_columns = {row[1] for row in cur.execute("PRAGMA table_info(instances)")}
    for column in ("custom_license_name", "custom_license_content", "custom_license_assigned_at"):
        if column in retired_columns:
            cur.execute(f'ALTER TABLE instances DROP COLUMN "{column}"')
