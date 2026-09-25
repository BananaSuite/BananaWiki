"""One-time import of supported Wiki schemas predating the version ledger."""

from pathlib import Path

from sqlite_migrations import add_columns, execute_script
from .legacy_rebuilds import (
    _migrate_role_add_protected_admin,
    _migrate_role_protected_admin_to_owner,
    _migrate_user_badges_remove_unique_constraint,
    _migrate_invite_codes_multi_use,
    _migrate_user_id_to_text,
    _migrate_plugin_fk_columns_to_text,
    _migrate_quota_request_add_cancelled,
    _migrate_custom_pages_add_video_and_embed,
    _migrate_banana_chat_to_banana_ai,
)


def _upgrade_legacy(conn):
    """Import supported pre-versioned schemas without intermediate commits."""
    cur = conn.cursor()

    execute_script(conn, Path(__file__).with_name("legacy_schema.sql").read_text(encoding="utf-8"))

    # -- Migrations: add columns to existing tables --
    # Add last_login_at to users if missing
    add_columns(conn, 'users', {
        'last_login_at': 'TEXT',
        'is_superuser': 'INTEGER NOT NULL DEFAULT 0',
        'reserved_pages_quota': 'INTEGER',
        'custom_role_id': 'INTEGER REFERENCES custom_roles(id) ON DELETE SET NULL',
        'original_password_backup': 'TEXT',
    })

    profile_cols = [r[1] for r in cur.execute("PRAGMA table_info(user_profiles)").fetchall()]
    if "birth_date" not in profile_cols:
        cur.execute("ALTER TABLE user_profiles ADD COLUMN birth_date TEXT NOT NULL DEFAULT ''")

    # Add timezone / favicon columns to site_settings if missing
    ss_cols = [r[1] for r in cur.execute("PRAGMA table_info(site_settings)").fetchall()]
    add_columns(conn, 'site_settings', {
        'interface_language': "TEXT NOT NULL DEFAULT 'en'",
        'interface_language_fallback': "TEXT NOT NULL DEFAULT 'en'",
        'interface_languages_json': "TEXT NOT NULL DEFAULT '{}'",
        'timezone': "TEXT NOT NULL DEFAULT 'UTC'",
        'favicon_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'favicon_type': "TEXT NOT NULL DEFAULT 'yellow'",
        'favicon_custom': "TEXT NOT NULL DEFAULT ''",
        'favicon_order': "TEXT NOT NULL DEFAULT '[]'",
    })
    # Maintenance mode (formerly lockdown mode).  On databases where the
    # legacy columns still exist they are renamed in-place so any saved
    # value is preserved; new installs simply get the new columns added.
    if "lockdown_mode" in ss_cols and "maintenance_mode" not in ss_cols:
        cur.execute("ALTER TABLE site_settings RENAME COLUMN lockdown_mode TO maintenance_mode")
    elif "maintenance_mode" not in ss_cols:
        cur.execute("ALTER TABLE site_settings ADD COLUMN maintenance_mode INTEGER NOT NULL DEFAULT 0")
    if "lockdown_message" in ss_cols and "maintenance_message" not in ss_cols:
        cur.execute("ALTER TABLE site_settings RENAME COLUMN lockdown_message TO maintenance_message")
    elif "maintenance_message" not in ss_cols:
        cur.execute("ALTER TABLE site_settings ADD COLUMN maintenance_message TEXT NOT NULL DEFAULT ''")
    add_columns(conn, 'site_settings', {
        'session_limit_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'suspended_account_deletion_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'page_protection_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'page_reservations_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'page_reservation_duration_hours': 'INTEGER NOT NULL DEFAULT 48',
        'page_reservation_cooldown_hours': 'INTEGER NOT NULL DEFAULT 24',
        'default_reserved_pages_quota': 'INTEGER NOT NULL DEFAULT 5',
        'light_primary_color': "TEXT NOT NULL DEFAULT '#4b63b6'",
        'light_secondary_color': "TEXT NOT NULL DEFAULT '#ffffff'",
        'light_accent_color': "TEXT NOT NULL DEFAULT '#3553c7'",
        'light_text_color': "TEXT NOT NULL DEFAULT '#202534'",
        'light_sidebar_color': "TEXT NOT NULL DEFAULT '#e9edf5'",
        'light_bg_color': "TEXT NOT NULL DEFAULT '#f6f7fb'",
        'default_theme_mode': "TEXT NOT NULL DEFAULT 'dark'",
        'kanban_access': "TEXT NOT NULL DEFAULT 'admin'",
        'kanban_write_access': "TEXT NOT NULL DEFAULT 'admin'",
        'kanban_public_access_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'canvas_access': "TEXT NOT NULL DEFAULT 'admin'",
        'canvas_write_access': "TEXT NOT NULL DEFAULT 'admin'",
        'canvas_public_access_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'canvas_open_access': 'INTEGER NOT NULL DEFAULT 0',
        'assessment_points_badge_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'contributor_leaderboard_enabled': 'INTEGER NOT NULL DEFAULT 0',
    })
    # kanban_board_shares table is created above; add visibility column to kanban_boards
    kb_cols = {r[1] for r in cur.execute("PRAGMA table_info(kanban_boards)").fetchall()}
    if "visibility" not in kb_cols:
        cur.execute("ALTER TABLE kanban_boards ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('private','shared','public'))")
    cl_cols = {r[1] for r in cur.execute("PRAGMA table_info(canvas__layouts)").fetchall()}
    if "visibility" not in cl_cols:
        cur.execute("ALTER TABLE canvas__layouts ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','shared','public'))")
    # Kanban ticket extra fields: due_date, color label
    add_columns(conn, 'kanban_tickets', {
        'due_date': 'TEXT DEFAULT NULL',
        'color': "TEXT NOT NULL DEFAULT ''",
        'labels': "TEXT NOT NULL DEFAULT ''",
    })
    # Beta tester advantage flags
    add_columns(conn, 'site_settings', {
        'beta_can_preview_drafts': 'INTEGER NOT NULL DEFAULT 1',
        'beta_can_use_advanced_editor': 'INTEGER NOT NULL DEFAULT 1',
        'beta_can_access_experimental_ui': 'INTEGER NOT NULL DEFAULT 0',
        'beta_extended_upload_limit_mb': 'INTEGER NOT NULL DEFAULT 32',
        'beta_custom_badge_text': "TEXT NOT NULL DEFAULT 'Beta Tester'",
    })

    # Add session_token column to users if missing
    add_columns(conn, 'users', {
        'session_token': 'TEXT',
        # Add accessibility column to users if missing
        'accessibility': 'TEXT',
    })

    # Add last_chat_cleanup_at to site_settings if missing
    add_columns(conn, 'site_settings', {
        'last_chat_cleanup_at': 'TEXT',
        # Add last_server_restart_at to site_settings if missing.
        # Used to enforce the per-wiki server-restart cooldown
        # (see ``config.SERVER_RESTART_COOLDOWN_SECONDS``).
        'last_server_restart_at': 'TEXT',
    })

    # Add chat_disabled column to users if missing
    add_columns(conn, 'users', {
        'chat_disabled': 'INTEGER NOT NULL DEFAULT 0',
        # Add suspended_until column to users if missing (timed suspension)
        'suspended_until': 'TEXT',
        # Add suspension metadata columns to users if missing
        'suspend_reason': 'TEXT',
        'suspend_reason_visible': 'INTEGER NOT NULL DEFAULT 0',
        'suspend_time_visible': 'INTEGER NOT NULL DEFAULT 0',
        'force_password_change': 'INTEGER NOT NULL DEFAULT 0',
        'onboarding_required': 'INTEGER NOT NULL DEFAULT 0',
        'onboarding_completed_at': 'TEXT',
        'intro_required': 'INTEGER NOT NULL DEFAULT 0',
        'intro_completed_at': 'TEXT',
    })

    # Suspension audit log table
    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS suspension_audit (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        action          TEXT    NOT NULL CHECK(action IN ('suspend','unsuspend')),
        reason          TEXT,
        reason_visible  INTEGER NOT NULL DEFAULT 0,
        time_visible    INTEGER NOT NULL DEFAULT 0,
        duration        TEXT,
        suspended_until TEXT,
        performed_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    """)

    # Add banned column to group_members if missing
    gm_cols = [r[1] for r in cur.execute("PRAGMA table_info(group_members)").fetchall()]
    if "banned" not in gm_cols:
        cur.execute("ALTER TABLE group_members ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")

    # Add is_active column to group_chats if missing
    add_columns(conn, 'group_chats', {
        'is_active': 'INTEGER NOT NULL DEFAULT 1',
        # Add description column to group_chats if missing
        'description': "TEXT NOT NULL DEFAULT ''",
    })

    # Add chat configuration columns to site_settings if missing
    add_columns(conn, 'site_settings', {
        'chat_attachments_per_day_limit': 'INTEGER NOT NULL DEFAULT 10',
        'chat_auto_clear_messages': 'INTEGER NOT NULL DEFAULT 0',
        'chat_auto_clear_attachments': 'INTEGER NOT NULL DEFAULT 0',
        'chat_message_retention_days': 'INTEGER NOT NULL DEFAULT 0',
        'chat_attachment_retention_days': 'INTEGER NOT NULL DEFAULT 7',
        # Add separate DM and Group chat settings
        'chat_dm_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'chat_dm_auto_clear_messages': 'INTEGER NOT NULL DEFAULT 0',
        'chat_dm_auto_clear_attachments': 'INTEGER NOT NULL DEFAULT 0',
        'chat_dm_message_retention_days': 'INTEGER NOT NULL DEFAULT 0',
        'chat_dm_attachment_retention_days': 'INTEGER NOT NULL DEFAULT 7',
        'chat_group_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'chat_group_auto_clear_messages': 'INTEGER NOT NULL DEFAULT 0',
        'chat_group_auto_clear_attachments': 'INTEGER NOT NULL DEFAULT 0',
        'chat_group_message_retention_days': 'INTEGER NOT NULL DEFAULT 0',
        'chat_group_attachment_retention_days': 'INTEGER NOT NULL DEFAULT 7',
        # Add additional chat customization options
        'chat_max_message_length': 'INTEGER NOT NULL DEFAULT 5000',
        'chat_attachments_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'chat_max_attachment_size_mb': 'INTEGER NOT NULL DEFAULT 5',
        'chat_allow_dm_creation': 'INTEGER NOT NULL DEFAULT 1',
        'chat_allow_group_creation': 'INTEGER NOT NULL DEFAULT 1',
        # Add chat cleanup schedule settings (moved from config.py to allow admin configuration).
        # Auto-deletion must always be opt-in: defaults to 0 so a fresh install never
        # silently deletes messages or attachments. Admins explicitly enable cleanup
        # by both turning on the ``chat_cleanup`` plugin and ticking this setting.
        'chat_cleanup_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'chat_cleanup_frequency_days': 'INTEGER NOT NULL DEFAULT 7',
        'chat_cleanup_hour': 'INTEGER NOT NULL DEFAULT 3',
        'chat_cleanup_split_configured': 'INTEGER NOT NULL DEFAULT 0',
        'banana_mode': 'INTEGER NOT NULL DEFAULT 0',
        # Telegram Sync settings (moved from config.py to database)
        'telegram_sync_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'telegram_sync_token': "TEXT NOT NULL DEFAULT ''",
    })
    if "telegram_sync_userid" not in ss_cols and "telegram_sync_userids" not in ss_cols:
        cur.execute("ALTER TABLE site_settings ADD COLUMN telegram_sync_userids TEXT NOT NULL DEFAULT ''")
    elif "telegram_sync_userid" in ss_cols and "telegram_sync_userids" not in ss_cols:
        cur.execute("ALTER TABLE site_settings RENAME COLUMN telegram_sync_userid TO telegram_sync_userids")

    # Telegram Sync advanced options (moved from config.py to database)
    add_columns(conn, 'site_settings', {
        'telegram_sync_split_threshold_mb': 'INTEGER NOT NULL DEFAULT 45',
        'telegram_sync_compress_level': 'INTEGER NOT NULL DEFAULT 9',
        'telegram_sync_include_chat_attachments': 'INTEGER NOT NULL DEFAULT 1',
    })

    # Retire registration metadata when upgrading an existing wiki.
    cur.execute("DROP TABLE IF EXISTS licenses")
    if "license_info_enabled" in ss_cols:
        cur.execute("ALTER TABLE site_settings DROP COLUMN license_info_enabled")

    # Custom Pages plugin: video upload size limit (MB)
    add_columns(conn, 'site_settings', {
        'custom_pages_max_video_size_mb': 'INTEGER NOT NULL DEFAULT 100',
        # Automatic mass-logout scheduler settings
        'auto_logout_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'auto_logout_hour': 'INTEGER NOT NULL DEFAULT 0',
        # Wiki documentation category: bypass deletion slowdown for docs pages
        'docs_bypass_deletion_slowdown': 'INTEGER NOT NULL DEFAULT 1',
        # Track the category ID of the spawned BananaWiki docs category
        'docs_category_id': 'INTEGER',
        # Upload flexibility settings
        # upload_mode: 'allow_all' (default), 'whitelist' (only allowed extensions), 'blacklist' (block specific)
        'upload_mode': "TEXT NOT NULL DEFAULT 'allow_all'",
        # Comma-separated custom whitelist extensions (used when upload_mode='whitelist')
        'upload_whitelist': "TEXT NOT NULL DEFAULT ''",
        # Comma-separated custom blacklist extensions (used when upload_mode='blacklist')
        'upload_blacklist': "TEXT NOT NULL DEFAULT 'exe,bat,cmd,com,scr,pif,msi,msp,mst,cpl,hta,inf,ins,isp,jse,lnk,reg,rgs,sct,shb,shs,vbe,vbs,wsc,wsf,wsh,ws,ps1,ps2,psc1,psc2,dll,sys'",
        # Host-controlled upload blocklist.  Hidden from the wiki UI so hosted
        # wiki admins cannot bypass platform policy.
        'platform_upload_blacklist': "TEXT NOT NULL DEFAULT ''",
        # Max upload size in MB (applies to page attachments, kanban, etc.)
        'upload_max_size_mb': 'INTEGER NOT NULL DEFAULT 100',
    })
    cur.execute(
        """UPDATE site_settings
           SET upload_mode='allow_all'
           WHERE id=1
             AND upload_mode='whitelist'
             AND COALESCE(upload_whitelist, '')=''"""
    )
    # Profile group badges global toggle (disabled by default)
    add_columns(conn, 'site_settings', {
        'profile_group_badges_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'profile_contribution_chart_enabled': 'INTEGER NOT NULL DEFAULT 1',
        # Sync: cross-worker backup timestamp (Unix epoch, coordinated via SQLite)
        'last_backup_sent_at': 'REAL NOT NULL DEFAULT 0',
        'telegram_sync_trigger_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'telegram_sync_trigger_immediate': 'INTEGER NOT NULL DEFAULT 1',
        'telegram_sync_trigger_high': 'INTEGER NOT NULL DEFAULT 1',
        'telegram_sync_trigger_normal': 'INTEGER NOT NULL DEFAULT 1',
        'telegram_sync_trigger_low': 'INTEGER NOT NULL DEFAULT 1',
        'telegram_sync_daily_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'telegram_sync_daily_time': "TEXT NOT NULL DEFAULT '03:00'",
        'telegram_sync_last_update_id': 'INTEGER NOT NULL DEFAULT 0',
        'telegram_sync_last_polling_at': 'REAL NOT NULL DEFAULT 0',
        'telegram_sync_pending_restores': "TEXT NOT NULL DEFAULT '{}'",
        # PDF export toggle (core feature, not plugin-gated)
        'pdf_export_enabled': 'INTEGER NOT NULL DEFAULT 1',
        # Markdown export toggle (core feature, not plugin-gated)
        'markdown_export_enabled': 'INTEGER NOT NULL DEFAULT 1',
        # Public mode: allow unauthenticated read access to the wiki
        'public_mode': 'INTEGER NOT NULL DEFAULT 0',
        'public_mode_until': 'TEXT',
        'public_mode_message': "TEXT NOT NULL DEFAULT ''",
        'public_mode_show_message': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Core visual page builder (not a plugin). It is opt-in and can also be
    # prohibited by the managed hosting environment.
    add_columns(conn, 'site_settings', {
        'page_builder_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'page_builder_access': "TEXT NOT NULL DEFAULT 'admin'",
    })

    add_columns(conn, 'pages', {
        'builder_json': "TEXT NOT NULL DEFAULT ''",
        'builder_public': 'INTEGER NOT NULL DEFAULT 0',
    })
    add_columns(conn, 'page_history', {
        'builder_json': "TEXT NOT NULL DEFAULT ''",
        'builder_public': 'INTEGER NOT NULL DEFAULT 0',
    })
    cur.execute("""
        CREATE TABLE IF NOT EXISTS page_builder_drafts (
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            builder_json TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (page_id, user_id)
        )
    """)

    # Open signup: allow sign-up without an invite code
    add_columns(conn, 'site_settings', {
        'open_signup': 'INTEGER NOT NULL DEFAULT 0',
        'open_signup_until': 'TEXT',
        # Bot protection: honeypot + timing checks on public auth forms (enabled by default)
        'bot_protection_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'new_user_intro_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'onboarding_replay_disabled': 'INTEGER NOT NULL DEFAULT 0',
        'intro_role_switching_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'intro_role_switching_roles': "TEXT NOT NULL DEFAULT 'user,editor,admin'",
        # TTS in-page panel: enabled by default when the column is introduced.
        # Admins can still turn it off in Site Settings if they want auto-gen
        # without the per-page player UI.
        'tts_page_panel_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'tts_public_access_enabled': 'INTEGER NOT NULL DEFAULT 1',
        # TTS automatic generation: opt-in.  Creating or updating a page can
        # kick off background MP3 synthesis with automatic language detection,
        # but bulk imports and large restored wikis should not start with this
        # enabled by accident.
        'tts_auto_generate_enabled': 'INTEGER NOT NULL DEFAULT 0',
        # TTS enabled languages: comma-separated list of TTS language codes
        # that admins want available on this wiki.  Existing installs default
        # to ``"en,it"`` to preserve the historical EN/IT-only behaviour;
        # fresh installs (and any admin who clears the list to the empty
        # string) likewise fall back to EN/IT via the parser in
        # ``helpers._tts.parse_enabled_languages``.  Admins can opt in to any
        # subset of the full TTS language catalogue via Site Settings
        # \u2192 Features \u2192 TTS.  The column is plain text so existing tools that
        # inspect ``site_settings`` rows keep working without a JSON parser.
        'tts_enabled_languages': "TEXT NOT NULL DEFAULT 'en,it'",
        # Remote GPU TTS: off by default.  Optional accelerator for
        # self-hosted or hosting-platform instances with a GPU server.
        'tts_gpu_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'tts_gpu_url': "TEXT NOT NULL DEFAULT ''",
        'tts_gpu_auth_token': "TEXT NOT NULL DEFAULT ''",
        'tts_gpu_timeout': 'INTEGER NOT NULL DEFAULT 120',
        'tts_performance_mode': "TEXT NOT NULL DEFAULT 'auto'",
    })

    # Drop the legacy ``CHECK(language IN ('en','it'))`` constraint on
    # ``tts_generations`` so the column accepts the full TTS catalogue.
    # SQLite has no ``ALTER TABLE \u2026 DROP CONSTRAINT`` so we recreate
    # the table when (and only when) the old constraint is present.
    # Validation is enforced in Python via ``helpers._tts`` and
    # ``db/_tts._VALID_LANGUAGES``, so removing this DB-side allowlist
    # does not weaken the input checks.
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tts_generations'"
    ).fetchone()
    old_sql = row[0] if row else ""
    if old_sql and "language IN ('en','it')" in old_sql:
        execute_script(cur, """
            ALTER TABLE tts_generations RENAME TO tts_generations__old;
            CREATE TABLE tts_generations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id         INTEGER NOT NULL UNIQUE
                                        REFERENCES pages(id) ON DELETE CASCADE,
                language        TEXT    NOT NULL
                                        CHECK(length(language) > 0),
                status          TEXT    NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending','processing',
                                                         'completed','failed')),
                filename        TEXT,
                content_hash    TEXT    NOT NULL,
                file_size       INTEGER NOT NULL DEFAULT 0,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                requested_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
                requested_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                started_at      TEXT,
                completed_at    TEXT,
                error_message   TEXT
            );
            INSERT INTO tts_generations (
                id, page_id, language, status, filename, content_hash,
                file_size, retry_count, requested_by, requested_at, started_at,
                completed_at, error_message
            )
            SELECT id, page_id, language, status, filename, content_hash,
                   file_size, 0, requested_by, requested_at, started_at,
                   completed_at, error_message
            FROM tts_generations__old;
            DROP TABLE tts_generations__old;
            CREATE INDEX IF NOT EXISTS idx_tts_generations_status
                ON tts_generations(status);
        """)
    tts_cols = [
        r[1] for r in cur.execute(
            "PRAGMA table_info(tts_generations)"
        ).fetchall()
    ]
    if tts_cols and "retry_count" not in tts_cols:
        cur.execute(
            "ALTER TABLE tts_generations ADD COLUMN retry_count "
            "INTEGER NOT NULL DEFAULT 0"
        )

    # Feedback plugin settings
    add_columns(conn, 'site_settings', {
        'feedback_bot_token': "TEXT NOT NULL DEFAULT ''",
        'feedback_telegram_userids': "TEXT NOT NULL DEFAULT ''",
        'feedback_show_to_anonymous': 'INTEGER NOT NULL DEFAULT 0',
        'feedback_cooldown_minutes': 'INTEGER NOT NULL DEFAULT 60',
    })

    # Add unread_count column to chats if missing
    add_columns(conn, 'chats', {
        'unread_count_user1': 'INTEGER NOT NULL DEFAULT 0',
        'unread_count_user2': 'INTEGER NOT NULL DEFAULT 0',
    })

    add_columns(conn, 'chat_messages', {
        'is_deleted': 'INTEGER NOT NULL DEFAULT 0',
        'deleted_at': 'TEXT',
    })

    # Add unread_count to group_members if missing
    if "unread_count" not in gm_cols:
        cur.execute("ALTER TABLE group_members ADD COLUMN unread_count INTEGER NOT NULL DEFAULT 0")

    add_columns(conn, 'group_messages', {
        'is_deleted': 'INTEGER NOT NULL DEFAULT 0',
        'deleted_at': 'TEXT',
    })

    # Add sequential_nav to categories if missing
    cat_cols = [r[1] for r in cur.execute("PRAGMA table_info(categories)").fetchall()]
    if "sequential_nav" not in cat_cols:
        cur.execute("ALTER TABLE categories ADD COLUMN sequential_nav INTEGER NOT NULL DEFAULT 0")

    # Add difficulty_tag / custom-tag columns to pages if missing
    add_columns(conn, 'pages', {
        'difficulty_tag': "TEXT NOT NULL DEFAULT ''",
        'tag_custom_label': "TEXT NOT NULL DEFAULT ''",
        'tag_custom_color': "TEXT NOT NULL DEFAULT ''",
        'is_deindexed': 'INTEGER NOT NULL DEFAULT 0',
        'pending_deletion': 'INTEGER NOT NULL DEFAULT 0',
        'pending_deletion_by': 'TEXT REFERENCES users(id) ON DELETE SET NULL',
        'pending_deletion_at': 'TEXT',
        'protected_by': 'TEXT REFERENCES users(id) ON DELETE SET NULL',
        'protected_at': 'TEXT',
        'protection_unlock_requested_at': 'TEXT',
        'protection_unlock_requested_by': 'TEXT REFERENCES users(id) ON DELETE SET NULL',
    })

    # Create indexes on page-protection columns now that they are guaranteed
    # to exist (either they were in the original schema or just added above).
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_protected_by ON pages(protected_by)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_unlock_requested_at "
        "ON pages(protection_unlock_requested_at)"
    )

    # Add announcement behavior and targeting columns when upgrading.
    add_columns(conn, 'announcements', {
        'not_removable': 'INTEGER NOT NULL DEFAULT 1',
        'show_countdown': 'INTEGER NOT NULL DEFAULT 1',
        'audience_mode': "TEXT NOT NULL DEFAULT 'all'",
        'custom_background': 'TEXT',
        'custom_text_color': 'TEXT',
        'revision': 'INTEGER NOT NULL DEFAULT 1',
    })
    cur.execute(
        "CREATE TABLE IF NOT EXISTS announcement_audience_users ("
        "announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,"
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
        "PRIMARY KEY (announcement_id, user_id))"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_announcement_audience_user "
        "ON announcement_audience_users(user_id, announcement_id)"
    )

    # Add is_revert column to page_history if missing
    hist_cols = [r[1] for r in cur.execute("PRAGMA table_info(page_history)").fetchall()]
    if "is_revert" not in hist_cols:
        cur.execute("ALTER TABLE page_history ADD COLUMN is_revert INTEGER NOT NULL DEFAULT 0")

    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reservation_quota_requests_pending "
        "ON reservation_quota_requests(user_id) WHERE status='pending'"
    )

    # Indexes for the hot paths that would otherwise full-scan: login
    # rate-limiting, chat polling, history views and badge counts.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip "
        "ON login_attempts(ip)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_at "
        "ON login_attempts (ip, attempted_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_ip_bucket "
        "ON rate_limit_hits(ip, bucket, hit_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_history_page_id "
        "ON page_history(page_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_id "
        "ON chat_messages(chat_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_messages_group_id "
        "ON group_messages(group_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_badge_notifications_user_id "
        "ON badge_notifications(user_id, notified)"
    )

    # Indexes on frequently queried foreign keys (ISSUE-006, ISSUE-017)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_title "
        "ON pages(title COLLATE NOCASE)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_category_id "
        "ON pages(category_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_last_edited_by "
        "ON pages(last_edited_by)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_history_edited_by "
        "ON page_history(edited_by)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_drafts_user_id "
        "ON drafts(user_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_profiles_user_id "
        "ON user_profiles(user_id)"
    )

    # Indexes for Kanban, badge, permissions, and canvas hot paths (M-3)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_tickets_column "
        "ON kanban_tickets(column_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_columns_board "
        "ON kanban_columns(board_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_ticket_comments_ticket "
        "ON kanban_ticket_comments(ticket_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_ticket_history_ticket "
        "ON kanban_ticket_history(ticket_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_ticket_attach_ticket "
        "ON kanban_ticket_attachments(ticket_id)"
    )
    # Multi-assignee table: idempotent create + backfill from existing
    # ``kanban_tickets.assigned_to`` rows so previously assigned tickets
    # show up in the new assignees list immediately on first upgrade.
    cur.execute(
        "CREATE TABLE IF NOT EXISTS kanban_ticket_assignees ("
        "ticket_id INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,"
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
        "assigned_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "PRIMARY KEY (ticket_id, user_id))"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_ticket_assignees_user "
        "ON kanban_ticket_assignees(user_id)"
    )
    cur.execute(
        "INSERT OR IGNORE INTO kanban_ticket_assignees (ticket_id, user_id) "
        "SELECT id, assigned_to FROM kanban_tickets "
        "WHERE assigned_to IS NOT NULL AND assigned_to != ''"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS kanban_activity_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "board_id INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,"
        "user_id TEXT REFERENCES users(id) ON DELETE SET NULL,"
        "action TEXT NOT NULL,"
        "details TEXT NOT NULL DEFAULT '',"
        "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_activity_log_board "
        "ON kanban_activity_log(board_id, created_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_badges_user "
        "ON user_badges(user_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_permissions_user "
        "ON user_permissions(user_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_canvas_perms_layout "
        "ON canvas__permissions(layout_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_assessment_questions_assessment "
        "ON assessment_questions(assessment_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_assessment_attempts_assessment_user "
        "ON assessment_attempts(assessment_id, user_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_assessment_answers_attempt "
        "ON assessment_answers(attempt_id)"
    )
    # Separate index on hit_at alone for the cleanup DELETE scan (DB-2)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_hit_at "
        "ON rate_limit_hits(hit_at)"
    )

    # Migrate users.id from INTEGER to TEXT if needed
    user_id_type = next(
        (r[2] for r in cur.execute("PRAGMA table_info(users)").fetchall() if r[1] == 'id'),
        None,
    )
    if user_id_type and 'INT' in user_id_type.upper():
        _migrate_user_id_to_text(conn, cur)

    # Migrate canvas, kanban & assessment FK columns from INTEGER to TEXT
    # (runs unconditionally so it catches tables that the old version of
    # _migrate_user_id_to_text skipped: idempotent when already TEXT)
    _migrate_plugin_fk_columns_to_text(conn, cur)

    # Migrate role CHECK constraint to include 'protected_admin' if not already present
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if schema_row and "protected_admin" not in schema_row[0] and "owner" not in schema_row[0]:
        _migrate_role_add_protected_admin(conn, cur)

    # Migrate 'protected_admin' role to 'owner'
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if schema_row and "protected_admin" in schema_row[0]:
        _migrate_role_protected_admin_to_owner(conn, cur)

    # Migrate reservation_quota_requests CHECK constraint to include 'cancelled'
    rqr_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reservation_quota_requests'"
    ).fetchone()
    if rqr_schema and "cancelled" not in rqr_schema[0]:
        _migrate_quota_request_add_cancelled(conn, cur)

    add_columns(conn, 'reservation_quota_requests', {
        'review_reason': "TEXT NOT NULL DEFAULT ''",
        'review_source': "TEXT NOT NULL DEFAULT 'manual' CHECK(review_source IN ('manual','automatic'))",
    })
    add_columns(conn, 'contribution_quota_requests', {
        'review_reason': "TEXT NOT NULL DEFAULT ''",
        'review_source': "TEXT NOT NULL DEFAULT 'manual' CHECK(review_source IN ('manual','automatic'))",
    })

    # Migrate custom_pages: add new content types and video/iframe columns
    _migrate_custom_pages_add_video_and_embed(conn, cur)

    # Move the experimental AI chat plugin to its current id. It shipped under
    # ``banana_chat`` with the table prefix ``banana_chat__``; installs that
    # imported the .bwplugin must keep their data across the change.
    _migrate_banana_chat_to_banana_ai(conn, cur)

    # Migrate user_badges: remove UNIQUE(user_id, badge_type_id) so that
    # badge_types.allow_multiple can permit awarding the same badge multiple times.
    ub_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_badges'"
    ).fetchone()
    if ub_schema and "UNIQUE(user_id, badge_type_id)" in ub_schema[0]:
        _migrate_user_badges_remove_unique_constraint(conn, cur)

    # Deduplicate home pages: keep only the one with the lowest id.
    home_rows = cur.execute("SELECT id FROM pages WHERE is_home=1 ORDER BY id").fetchall()
    if len(home_rows) > 1:
        keep_id = home_rows[0]["id"]
        cur.execute(
            "UPDATE pages SET is_home=0 WHERE is_home=1 AND id != ?",
            (keep_id,),
        )

    # Ensure home page exists
    home = cur.execute("SELECT id FROM pages WHERE is_home=1").fetchone()
    if not home:
        cur.execute(
            "INSERT INTO pages (title, slug, content, is_home) VALUES (?, ?, ?, 1)",
            ("Home", "home", "# Welcome to your Wiki\n\nEdit this page to get started."),
        )
    cur.execute(
        "UPDATE pages SET is_deindexed=0, pending_deletion=0, "
        "pending_deletion_by=NULL, pending_deletion_at=NULL, "
        "protected_by=NULL, protected_at=NULL, "
        "protection_unlock_requested_at=NULL, protection_unlock_requested_by=NULL "
        "WHERE is_home=1"
    )
    cur.execute("DELETE FROM temp_pages WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)")
    cur.execute(
        "DELETE FROM temp_page_index_state WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)"
    )
    cur.execute("DELETE FROM page_reservations WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)")
    cur.execute("DELETE FROM user_page_cooldowns WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pages_single_home "
        "ON pages(is_home) WHERE is_home=1"
    )

    # Phase 1: add blob_id columns to all attachment tables (nullable FK to file_blobs)
    pa_cols = {r[1] for r in cur.execute("PRAGMA table_info(page_attachments)").fetchall()}
    if "blob_id" not in pa_cols:
        cur.execute("ALTER TABLE page_attachments ADD COLUMN blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL")

    ca_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat_attachments)").fetchall()}
    if "blob_id" not in ca_cols:
        cur.execute("ALTER TABLE chat_attachments ADD COLUMN blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL")

    ga_cols = {r[1] for r in cur.execute("PRAGMA table_info(group_attachments)").fetchall()}
    if "blob_id" not in ga_cols:
        cur.execute("ALTER TABLE group_attachments ADD COLUMN blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL")

    kta_cols = {r[1] for r in cur.execute("PRAGMA table_info(kanban_ticket_attachments)").fetchall()}
    if "blob_id" not in kta_cols:
        cur.execute("ALTER TABLE kanban_ticket_attachments ADD COLUMN blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL")

    cpf_cols = {r[1] for r in cur.execute("PRAGMA table_info(custom_page_files)").fetchall()}
    if "blob_id" not in cpf_cols:
        cur.execute("ALTER TABLE custom_page_files ADD COLUMN blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL")

    # Migrate invite codes to support multi-use
    _migrate_invite_codes_multi_use(conn, cur)

    # Cleanup leader-election lease (post-audit hardening)
    # ``_run_periodic_cleanup_once`` is called from a daemon thread in
    # every Gunicorn worker.  Without coordination, every worker runs
    # the same cleanup queries simultaneously every 5 minutes: wasted
    # DB pressure plus the risk of double-deletes if a query is not
    # perfectly idempotent.  This singleton table is used as a
    # leader-election lock: the worker that wins
    # ``UPDATE … SET held_by=?, expires_at=? WHERE expires_at IS NULL
    # OR expires_at < ?`` runs the cleanup; the others skip.  The
    # ``id=1`` CHECK enforces the singleton.
    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS cleanup_lease (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        held_by         TEXT,
        acquired_at     TEXT,
        expires_at      TEXT
    );
    INSERT OR IGNORE INTO cleanup_lease (id) VALUES (1);
    """)

    # Per-user upload tracking (post-audit hardening)
    # Aggregate counters supporting per-user daily upload quotas.  The
    # "current_window" columns hold the count and total bytes since
    # ``window_start``; helper code resets the window when ``now -
    # window_start`` exceeds 24 hours.  All columns default to 0 / NULL
    # so existing users start with a clean slate after upgrade.
    add_columns(conn, 'users', {
        'upload_window_start': 'TEXT',
        'upload_window_count': 'INTEGER NOT NULL DEFAULT 0',
        'upload_window_bytes': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Site-wide quota knobs (admins can raise/lower without code change).
    # 0 means "unlimited" so existing deployments are not silently
    # restricted on upgrade.
    ss_cols = {r[1] for r in cur.execute("PRAGMA table_info(site_settings)").fetchall()}
    add_columns(conn, 'site_settings', {
        'upload_quota_per_day_count': 'INTEGER NOT NULL DEFAULT 0',
        'upload_quota_per_day_bytes': 'INTEGER NOT NULL DEFAULT 0',
        # Contribution approval system (disabled by default)
        'contribution_approval_enabled': 'INTEGER NOT NULL DEFAULT 0',
        # Draft expiration (0 = disabled, hours before drafts auto-expire)
        'draft_expiration_hours': 'INTEGER NOT NULL DEFAULT 0',
        # Default contribution quota (how many pending contributions a user can have)
        'default_contribution_quota': 'INTEGER NOT NULL DEFAULT 5',
        'reservation_quota_auto_approve_max': 'INTEGER NOT NULL DEFAULT 0',
        'contribution_quota_auto_approve_max': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Per-user contribution quota override
    add_columns(conn, 'users', {
        'contribution_quota': 'INTEGER',
        # API Service: per-user API access flag (admins always have access)
        'api_access_enabled': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Index for pending contributions lookups
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_contributions_page_user "
        "ON pending_contributions(page_id, user_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_contributions_status "
        "ON pending_contributions(status)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_contribution_quota_requests_pending "
        "ON contribution_quota_requests(user_id) WHERE status='pending'"
    )

    # Invite code role assignment: allow codes to assign a base role
    # and/or a custom role on signup.
    add_columns(conn, 'invite_codes', {
        'assigned_role': 'TEXT',
        'assigned_custom_role_id': 'INTEGER REFERENCES custom_roles(id) ON DELETE SET NULL',
    })

    # Text-to-speech generations (built-in `tts` plugin)
    # One row per page describing the latest (or in-flight) MP3 cache.
    # ``page_id`` is UNIQUE so a new generation request always supersedes
    # the previous one: this is what makes "page updated -> invalidate
    # TTS" trivially correct and keeps the cache to one file per page.
    #
    # ``status`` transitions:
    #   pending    -- row inserted, worker not yet started
    #   processing -- worker has begun synthesizing audio
    #   completed  -- MP3 written to disk, ``filename`` populated
    #   failed     -- worker errored out (incl. cancelled by page update)
    # ``language`` is validated in Python (helpers._tts +
    # db/_tts._VALID_LANGUAGES) against the full TTS catalogue, so the
    # DB-side CHECK only enforces that the column is non-empty.  An
    # earlier version of the schema hard-coded ``CHECK(language IN
    # ('en','it'))`` here. The migration block below rebuilds the
    # table for installs that still carry that constraint.
    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS tts_generations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id         INTEGER NOT NULL UNIQUE
                                REFERENCES pages(id) ON DELETE CASCADE,
        language        TEXT    NOT NULL
                                CHECK(length(language) > 0),
        status          TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','processing',
                                                 'completed','failed')),
        filename        TEXT,
        content_hash    TEXT    NOT NULL,
        file_size       INTEGER NOT NULL DEFAULT 0,
        retry_count     INTEGER NOT NULL DEFAULT 0,
        requested_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
        requested_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        started_at      TEXT,
        completed_at    TEXT,
        error_message   TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_tts_generations_status
        ON tts_generations(status);

    -- ----------------------------------------------------------------
    --  Canvas + Kanban real-time collaboration event logs
    -- ----------------------------------------------------------------
    -- These tables back the cooperative-editing sync polled by the
    -- canvas/kanban clients.  Every mutation appends one row; clients
    -- request rows with ``seq > <since>`` to learn what other users
    -- have changed since their last poll.  Rows are append-only and
    -- pruned periodically (see ``canvas_prune_events`` /
    -- ``kanban_prune_events``).
    CREATE TABLE IF NOT EXISTS canvas__events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        layout_id    INTEGER NOT NULL REFERENCES canvas__layouts(id)
                             ON DELETE CASCADE,
        seq          INTEGER NOT NULL,
        op_type      TEXT    NOT NULL,
        payload      TEXT    NOT NULL DEFAULT '{}',
        by_user_id   TEXT    REFERENCES users(id) ON DELETE SET NULL,
        by_session   TEXT    NOT NULL DEFAULT '',
        created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(layout_id, seq)
    );
    CREATE INDEX IF NOT EXISTS idx_canvas_events_layout_seq
        ON canvas__events(layout_id, seq);

    CREATE TABLE IF NOT EXISTS kanban_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        board_id     INTEGER NOT NULL REFERENCES kanban_boards(id)
                             ON DELETE CASCADE,
        seq          INTEGER NOT NULL,
        op_type      TEXT    NOT NULL,
        payload      TEXT    NOT NULL DEFAULT '{}',
        by_user_id   TEXT    REFERENCES users(id) ON DELETE SET NULL,
        by_session   TEXT    NOT NULL DEFAULT '',
        created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(board_id, seq)
    );
    CREATE INDEX IF NOT EXISTS idx_kanban_events_board_seq
        ON kanban_events(board_id, seq);

    CREATE TABLE IF NOT EXISTS kanban_board_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        board_id        INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
        title           TEXT    NOT NULL,
        description     TEXT    NOT NULL DEFAULT '',
        snapshot        TEXT    NOT NULL DEFAULT '{}',
        edited_by       TEXT REFERENCES users(id) ON DELETE SET NULL,
        edit_message    TEXT    NOT NULL DEFAULT '',
        is_revert       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_kanban_board_history_board
        ON kanban_board_history(board_id, created_at);

    CREATE TABLE IF NOT EXISTS canvas__history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        layout_id       INTEGER NOT NULL REFERENCES canvas__layouts(id) ON DELETE CASCADE,
        title           TEXT    NOT NULL,
        description     TEXT    NOT NULL DEFAULT '',
        data            TEXT    NOT NULL DEFAULT '{}',
        edited_by       TEXT REFERENCES users(id) ON DELETE SET NULL,
        edit_message    TEXT    NOT NULL DEFAULT '',
        is_revert       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_canvas_history_layout
        ON canvas__history(layout_id, created_at);

    CREATE TABLE IF NOT EXISTS analytics_daily (
        day              TEXT    NOT NULL,
        kind             TEXT    NOT NULL,
        count            INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, kind)
    );
    CREATE INDEX IF NOT EXISTS idx_analytics_daily_day
        ON analytics_daily(day);
    """)

    # Feedback plugin tables
    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS feedback_reports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT    REFERENCES users(id) ON DELETE SET NULL,
        username        TEXT    NOT NULL,
        ip_address      TEXT    NOT NULL DEFAULT '',
        contact_info    TEXT    NOT NULL DEFAULT '',
        can_contact     INTEGER NOT NULL DEFAULT 0,
        telemetry       INTEGER NOT NULL DEFAULT 0,
        what_happened   TEXT    NOT NULL,
        page_url        TEXT    NOT NULL DEFAULT '',
        last_actions    TEXT    NOT NULL DEFAULT '',
        visited_pages   TEXT    NOT NULL DEFAULT '',
        archived        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS feedback_banned_users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT    NOT NULL,
        user_id         TEXT    REFERENCES users(id) ON DELETE SET NULL,
        reason          TEXT    NOT NULL DEFAULT '',
        banned_by       TEXT    REFERENCES users(id) ON DELETE SET NULL,
        ban_type        TEXT    NOT NULL DEFAULT 'temporary'
                        CHECK(ban_type IN ('temporary','permanent')),
        expires_at      TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_reports_username "
        "ON feedback_reports(username)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_reports_created "
        "ON feedback_reports(created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_reports_archived "
        "ON feedback_reports(archived)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_banned_users_username "
        "ON feedback_banned_users(username)"
    )

    # Feedback plugin site_settings columns
    add_columns(conn, 'site_settings', {
        'feedback_bot_token': "TEXT NOT NULL DEFAULT ''",
        'feedback_chat_ids': "TEXT NOT NULL DEFAULT ''",
        'feedback_show_anonymous': 'INTEGER NOT NULL DEFAULT 0',
        'feedback_cooldown_seconds': 'INTEGER NOT NULL DEFAULT 3600',
    })

    # Tables owned by the built-in `api_service` plugin.
    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS api_service__tokens (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name        TEXT    NOT NULL DEFAULT '',
        token_hash  TEXT    NOT NULL UNIQUE,
        permissions TEXT    NOT NULL DEFAULT '{"read":true,"write":false,"scopes":["pages"]}',
        last_used_at TEXT,
        expires_at  TEXT,
        active      INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_api_service_tokens_user
        ON api_service__tokens(user_id);

    CREATE TABLE IF NOT EXISTS api_service__audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        token_id    INTEGER REFERENCES api_service__tokens(id) ON DELETE SET NULL,
        user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        username    TEXT    NOT NULL DEFAULT '',
        endpoint    TEXT    NOT NULL,
        method      TEXT    NOT NULL,
        status_code INTEGER NOT NULL DEFAULT 200,
        ip_address  TEXT    NOT NULL DEFAULT '',
        request_body TEXT   NOT NULL DEFAULT '',
        duration_ms INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_api_service_audit_user
        ON api_service__audit_log(user_id);
    CREATE INDEX IF NOT EXISTS idx_api_service_audit_created
        ON api_service__audit_log(created_at);
    """)

    # API Service site settings
    add_columns(conn, 'site_settings', {
        'api_service_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'api_service_rate_limit': 'INTEGER NOT NULL DEFAULT 60',
        'api_service_admin_rate_limit': 'INTEGER NOT NULL DEFAULT 120',
        'api_service_max_tokens_per_user': 'INTEGER NOT NULL DEFAULT 5',
    })

    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS account_merge_requests (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        target_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_by      TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status          TEXT    NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','approved_by_both','merged','cancelled','denied')),
        source_approved INTEGER NOT NULL DEFAULT 0,
        target_approved INTEGER NOT NULL DEFAULT 0,
        admin_approved  TEXT REFERENCES users(id) ON DELETE SET NULL,
        request_reason  TEXT    NOT NULL DEFAULT '',
        completed_at    TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        CHECK(source_user_id != target_user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_merge_requests_source
        ON account_merge_requests(source_user_id);
    CREATE INDEX IF NOT EXISTS idx_merge_requests_target
        ON account_merge_requests(target_user_id);
    CREATE INDEX IF NOT EXISTS idx_merge_requests_status
        ON account_merge_requests(status, created_at DESC);
    """)

    # Users hold their own pending-merge pointers, so an in-flight request can be
    # found from either account without joining account_merge_requests.
    add_columns(conn, 'users', {
        'pending_merge_source_id': 'TEXT REFERENCES users(id) ON DELETE SET NULL',
        'pending_merge_target_id': 'TEXT REFERENCES users(id) ON DELETE SET NULL',
    })

    execute_script(cur, """
    CREATE TABLE IF NOT EXISTS account_merge_logs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        target_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        source_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        merged_by       TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        data_transferred TEXT   NOT NULL DEFAULT '{}',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_merge_logs_target
        ON account_merge_logs(target_user_id);
    CREATE INDEX IF NOT EXISTS idx_merge_logs_source
        ON account_merge_logs(source_user_id);
    """)

    # Kanban open-access (mirror of canvas_open_access)
    add_columns(conn, 'site_settings', {
        'kanban_open_access': 'INTEGER NOT NULL DEFAULT 0',
        # List-order version counter bumped on every global reorder so
        # other clients can detect and reload the new order in real time.
        'list_order_version': 'INTEGER NOT NULL DEFAULT 0',
    })

    # Per-user (or global when open-access is active) board order for
    # kanban list pages.  user_id is NULL for the global/shared order.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS kanban_user_board_order (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT,
        board_id    INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        UNIQUE(user_id, board_id)
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_user_board_order_user "
        "ON kanban_user_board_order(user_id, sort_order)"
    )

    # Per-user (or global when open-access is active) layout order for
    # canvas list pages.  user_id is NULL for the global/shared order.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS canvas_user_layout_order (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT,
        layout_id   INTEGER NOT NULL REFERENCES canvas__layouts(id) ON DELETE CASCADE,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        UNIQUE(user_id, layout_id)
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_canvas_user_layout_order_user "
        "ON canvas_user_layout_order(user_id, sort_order)"
    )
