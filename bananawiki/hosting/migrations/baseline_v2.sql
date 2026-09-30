-- BananaWiki Hosting 1.4 schema (application_id 0x42574850, user_version 2).
-- Never edit this file: later versions live in migrations/v3_*.py.

CREATE TABLE accounts (
            id          TEXT PRIMARY KEY,
            username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password    TEXT NOT NULL,
            is_admin    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            decision_reason TEXT NOT NULL DEFAULT ''
        , "email" TEXT NOT NULL DEFAULT '', "terms_accepted_at" TEXT, "terms_version" TEXT NOT NULL DEFAULT '', "email_prompt_dismissed" INTEGER NOT NULL DEFAULT 0, "deleted_at" TEXT, "email_verified_at" TEXT, "email_verification_token_hash" TEXT NOT NULL DEFAULT '', "email_verification_sent_at" TEXT, "email_verification_expires_at" TEXT, "password_reset_token_hash" TEXT NOT NULL DEFAULT '', "password_reset_sent_at" TEXT, "password_reset_expires_at" TEXT, "session_version" INTEGER NOT NULL DEFAULT 0, "suspended" INTEGER NOT NULL DEFAULT 0, "suspended_at" TEXT, "suspend_reason" TEXT NOT NULL DEFAULT '', "suspended_until" TEXT, "suspend_reason_visible" INTEGER NOT NULL DEFAULT 0, "suspend_time_visible" INTEGER NOT NULL DEFAULT 0, "approval_status" TEXT NOT NULL DEFAULT 'approved' CHECK(approval_status IN ('approved','pending','denied')), "denied_at" TEXT, "denied_notified" INTEGER NOT NULL DEFAULT 0, "approved_by" TEXT REFERENCES accounts(id) ON DELETE SET NULL, "denied_by" TEXT REFERENCES accounts(id) ON DELETE SET NULL, "approved_denied_at" TEXT, "theme_mode" TEXT NOT NULL DEFAULT 'default', "totp_secret_encrypted" TEXT NOT NULL DEFAULT '', "totp_enabled" INTEGER NOT NULL DEFAULT 0, "totp_recovery_hashes" TEXT NOT NULL DEFAULT '[]', "totp_enabled_at" TEXT, "totp_last_counter" INTEGER NOT NULL DEFAULT -1, "pending_merge_source_id" TEXT REFERENCES accounts(id) ON DELETE SET NULL, "pending_merge_target_id" TEXT REFERENCES accounts(id) ON DELETE SET NULL, "email_flagged_invalid" INTEGER NOT NULL DEFAULT 0, "email_flag_reason" TEXT NOT NULL DEFAULT '', "email_flagged_previous" TEXT NOT NULL DEFAULT '', "email_flag_reason_visible" INTEGER NOT NULL DEFAULT 0, "pending_deletion" INTEGER NOT NULL DEFAULT 0, "pending_deletion_at" TEXT, "pending_deletion_seconds" INTEGER NOT NULL DEFAULT 86400, "pending_deletion_reason" TEXT NOT NULL DEFAULT '', signup_use_case TEXT NOT NULL DEFAULT '');
CREATE TABLE hosting_account_sessions (
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
        );
CREATE UNIQUE INDEX ix_hosting_account_sessions_token_hash ON hosting_account_sessions (token_hash);
CREATE INDEX ix_hosting_account_sessions_account_active ON hosting_account_sessions (account_id, revoked_at, expires_at);
CREATE TABLE hosting_banners (
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
        );
CREATE TABLE hosting_banner_audience (
            banner_id  INTEGER NOT NULL REFERENCES hosting_banners(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            PRIMARY KEY (banner_id, account_id)
        );
CREATE INDEX idx_hosting_banner_audience_account ON hosting_banner_audience(account_id, banner_id);
CREATE TABLE hosting_impersonation_logs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            started_at        TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at          TEXT
        );
CREATE TABLE hosting_login_attempts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ip           TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );
CREATE INDEX ix_hosting_login_attempts_ip_at
        ON hosting_login_attempts (ip, attempted_at)
    ;
CREATE TABLE hosting_rate_limit_hits (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ip       TEXT NOT NULL,
            bucket   TEXT NOT NULL,
            hit_at   TEXT NOT NULL
        );
CREATE INDEX ix_hosting_rate_limit_hits_ip_bucket
        ON hosting_rate_limit_hits (ip, bucket, hit_at)
    ;
CREATE TABLE hosting_settings (
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
        , "bot_protection_enabled" INTEGER NOT NULL DEFAULT 1, "hosting_activation_required" INTEGER NOT NULL DEFAULT 0, "hosting_activation_denied_timeout_hours" INTEGER NOT NULL DEFAULT 24, hosting_activation_denied_timeout_seconds INTEGER NOT NULL DEFAULT 0, "devtools_enabled" INTEGER NOT NULL DEFAULT 1, "platform_oauth_enabled" INTEGER NOT NULL DEFAULT 0, email_flag_block_reentry INTEGER NOT NULL DEFAULT 0, "arabic_mirror_enabled" INTEGER NOT NULL DEFAULT 0, "instance_url_suffix" TEXT, "instance_suffix_disabled" INTEGER NOT NULL DEFAULT 0, signup_use_case_required INTEGER NOT NULL DEFAULT 0, "api_enabled" INTEGER NOT NULL DEFAULT 0, "approval_notify_email" TEXT NOT NULL DEFAULT '', "approval_notify_mode" TEXT NOT NULL DEFAULT 'digest', "approval_notify_digest_hours" INTEGER NOT NULL DEFAULT 6, "approval_notify_last_digest_at" TEXT);
CREATE TABLE hosting_invite_codes (
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
        );
CREATE INDEX ix_hosting_invite_codes_code ON hosting_invite_codes (code);
CREATE UNIQUE INDEX ix_accounts_active_email ON accounts(email COLLATE NOCASE) WHERE email <> '' AND deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS "instances" (
        id          TEXT PRIMARY KEY,
        account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        subdomain   TEXT NOT NULL COLLATE NOCASE,
        status      TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'stopped', 'suspended', 'terminated')),
        port        INTEGER,
        admin_username TEXT,
        admin_password_plain TEXT,
        storage_limit_mb INTEGER,
        upload_max_size_mb INTEGER,
        upload_blocked_extensions TEXT,
        created_at  TEXT NOT NULL,
        expires_at  TEXT,
        suspended_at TEXT,
        suspended_accumulated_seconds INTEGER NOT NULL DEFAULT 0,
        suspended_until TEXT,
        suspend_reason TEXT NOT NULL DEFAULT '',
        suspend_reason_visible INTEGER NOT NULL DEFAULT 0,
        suspend_time_visible INTEGER NOT NULL DEFAULT 0,
        stopped_at  TEXT,
        terminated_at TEXT,
        data_retained_until TEXT,
        terminated_reason TEXT,
        domain_mode TEXT NOT NULL DEFAULT 'hosting',
        declared_use_case TEXT NOT NULL DEFAULT '',
        tos_compliance_declared_at TEXT,
        grace_period_suspended INTEGER NOT NULL DEFAULT 0,
        custom_credentials INTEGER NOT NULL DEFAULT 0,
        oauth_client_id TEXT,
        oauth_client_secret_hash TEXT,
        easy_wiki INTEGER NOT NULL DEFAULT 0,
        public_wiki_allowed INTEGER NOT NULL DEFAULT 0,
        page_builder_allowed INTEGER NOT NULL DEFAULT 0,
        custom_domain_allowed INTEGER NOT NULL DEFAULT 0,
        UNIQUE (subdomain, domain_mode) ON CONFLICT ABORT
    );
CREATE TABLE account_suspension_audit (
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
        );
CREATE TABLE instance_suspension_audit (
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
        );
CREATE TABLE hosting_oauth_authorization_codes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            code            TEXT NOT NULL UNIQUE,
            client_id       TEXT NOT NULL,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            redirect_uri    TEXT NOT NULL DEFAULT '',
            used            INTEGER NOT NULL DEFAULT 0,
            expires_at      TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
CREATE INDEX ix_oauth_auth_codes_code ON hosting_oauth_authorization_codes (code);
CREATE TABLE hosting_oauth_access_tokens (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash      TEXT NOT NULL UNIQUE,
            client_id       TEXT NOT NULL,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            scope           TEXT NOT NULL DEFAULT 'openid profile',
            expires_at      TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
CREATE INDEX ix_oauth_access_tokens_hash ON hosting_oauth_access_tokens (token_hash);
CREATE INDEX ix_oauth_access_tokens_account ON hosting_oauth_access_tokens (account_id);
CREATE TABLE hosting_oauth_account_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            wiki_user_id    TEXT NOT NULL,
            wiki_username   TEXT NOT NULL,
            linked_at       TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(instance_id, account_id)
        );
CREATE INDEX ix_oauth_links_instance ON hosting_oauth_account_links (instance_id);
CREATE INDEX ix_oauth_links_account ON hosting_oauth_account_links (account_id);
CREATE TABLE hosting_account_merge_requests (
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
CREATE INDEX ix_merge_requests_source
        ON hosting_account_merge_requests(source_account_id);
CREATE INDEX ix_merge_requests_target
        ON hosting_account_merge_requests(target_account_id);
CREATE INDEX ix_merge_requests_status
        ON hosting_account_merge_requests(status, created_at DESC);
CREATE TABLE hosting_account_merge_logs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        source_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        merged_by       TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        instances_transferred INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE INDEX ix_merge_logs_target
        ON hosting_account_merge_logs(target_account_id);
CREATE INDEX ix_merge_logs_source
        ON hosting_account_merge_logs(source_account_id);
CREATE TABLE instance_collaborators (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            role            TEXT NOT NULL DEFAULT 'custom'
                                CHECK(role IN ('full_access', 'custom')),
            permissions     TEXT NOT NULL DEFAULT '[]',
            invited_by      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(instance_id, account_id)
        );
CREATE INDEX ix_instance_collaborators_instance ON instance_collaborators (instance_id);
CREATE INDEX ix_instance_collaborators_account ON instance_collaborators (account_id);
CREATE TABLE instance_ownership_transfers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id         TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            from_account_id     TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            to_account_id       TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            status              TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending', 'accepted', 'declined', 'cancelled')),
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at         TEXT,
            CHECK(from_account_id != to_account_id)
        );
CREATE INDEX ix_ownership_transfers_instance ON instance_ownership_transfers (instance_id);
CREATE INDEX ix_ownership_transfers_to ON instance_ownership_transfers (to_account_id, status);
CREATE TABLE instance_feature_requests (
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
        );
CREATE UNIQUE INDEX ux_instance_feature_requests_pending ON instance_feature_requests(instance_id, feature) WHERE status='pending';
CREATE INDEX ix_instance_feature_requests_instance ON instance_feature_requests(instance_id, requested_at DESC);
CREATE INDEX ix_instance_feature_requests_status ON instance_feature_requests(status, requested_at);
CREATE TABLE instance_custom_domains (
            domain TEXT PRIMARY KEY COLLATE NOCASE,
            instance_id TEXT NOT NULL UNIQUE REFERENCES instances(id) ON DELETE CASCADE,
            verification_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            verified_at TEXT,
            verified_until TEXT,
            last_checked_at TEXT
        );
CREATE TABLE hosting_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL CHECK(subject_type IN ('account', 'instance')),
            subject_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor_id TEXT,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
CREATE INDEX ix_hosting_events_subject ON hosting_events(subject_type, subject_id, id);
CREATE TABLE hosting_api_tokens (
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
CREATE UNIQUE INDEX ix_hosting_api_tokens_token_hash
            ON hosting_api_tokens (token_hash);
CREATE INDEX ix_hosting_api_tokens_account
            ON hosting_api_tokens (account_id, revoked_at);
