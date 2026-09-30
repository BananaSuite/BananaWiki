-- BananaWiki 1.4 schema version 3, as created by a fresh 1.4 installation.
-- Tables of retired features are omitted. New databases start from this file
-- and then run every later migration, so there is one path to the current schema.

CREATE TABLE users (
    id          TEXT    PRIMARY KEY,
    username    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password    TEXT    NOT NULL,
    role        TEXT    NOT NULL DEFAULT 'user'
                        CHECK(role IN ('user','editor','admin','owner')),
    suspended   INTEGER NOT NULL DEFAULT 0,
    userbot_enabled INTEGER NOT NULL DEFAULT 0,
    userbot_mode_lock TEXT NOT NULL DEFAULT 'unlocked'
                        CHECK(userbot_mode_lock IN ('unlocked','force_enabled','force_disabled')),
    userbot_enable_count INTEGER NOT NULL DEFAULT 0,
    userbot_disable_count INTEGER NOT NULL DEFAULT 0,
    reserved_pages_quota INTEGER,
    invite_code TEXT,
    is_superuser    INTEGER NOT NULL DEFAULT 0,
    original_password_backup TEXT,
    onboarding_required INTEGER NOT NULL DEFAULT 0,
    onboarding_completed_at TEXT,
    intro_required INTEGER NOT NULL DEFAULT 0,
    intro_completed_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
, "last_login_at" TEXT, "custom_role_id" INTEGER REFERENCES custom_roles(id) ON DELETE SET NULL, "session_token" TEXT, "accessibility" TEXT, "chat_disabled" INTEGER NOT NULL DEFAULT 0, "suspended_until" TEXT, "suspend_reason" TEXT, "suspend_reason_visible" INTEGER NOT NULL DEFAULT 0, "suspend_time_visible" INTEGER NOT NULL DEFAULT 0, "force_password_change" INTEGER NOT NULL DEFAULT 0, approval_status TEXT NOT NULL DEFAULT 'approved' CHECK(approval_status IN ('approved','pending','denied')), denied_at TEXT, denied_notified INTEGER NOT NULL DEFAULT 0, approved_by TEXT REFERENCES users(id) ON DELETE SET NULL, denied_by TEXT REFERENCES users(id) ON DELETE SET NULL, approved_denied_at TEXT, quota_request_cooldown_until TEXT, "upload_window_start" TEXT, "upload_window_count" INTEGER NOT NULL DEFAULT 0, "upload_window_bytes" INTEGER NOT NULL DEFAULT 0, "contribution_quota" INTEGER, "api_access_enabled" INTEGER NOT NULL DEFAULT 0, "pending_merge_source_id" TEXT REFERENCES users(id) ON DELETE SET NULL, "pending_merge_target_id" TEXT REFERENCES users(id) ON DELETE SET NULL);

CREATE TABLE user_sessions (
    id          TEXT    PRIMARY KEY,
    token_hash  TEXT    NOT NULL,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL,
    last_seen_at TEXT   NOT NULL,
    expires_at  TEXT    NOT NULL,
    remember_me INTEGER NOT NULL DEFAULT 0,
    auth_method TEXT    NOT NULL DEFAULT 'password',
    ip_address  TEXT    NOT NULL DEFAULT '',
    last_ip     TEXT    NOT NULL DEFAULT '',
    user_agent  TEXT    NOT NULL DEFAULT '',
    revoked_at  TEXT
);

CREATE UNIQUE INDEX idx_user_sessions_token_hash
    ON user_sessions(token_hash);

CREATE INDEX idx_user_sessions_user_active
    ON user_sessions(user_id, revoked_at, expires_at);

CREATE TABLE invite_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT,
    max_uses    INTEGER NOT NULL DEFAULT 1,
    use_count   INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_at  TEXT
, "assigned_role" TEXT, "assigned_custom_role_id" INTEGER REFERENCES custom_roles(id) ON DELETE SET NULL);

CREATE TABLE invite_code_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invite_code_id  INTEGER NOT NULL REFERENCES invite_codes(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    used_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    parent_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
, sequential_nav INTEGER NOT NULL DEFAULT 0);

CREATE TABLE pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    slug        TEXT    NOT NULL UNIQUE,
    content     TEXT    NOT NULL DEFAULT '',
    builder_json TEXT   NOT NULL DEFAULT '',
    builder_public INTEGER NOT NULL DEFAULT 0,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    is_home     INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    protected_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    protected_at TEXT,
    protection_unlock_requested_at TEXT,
    protection_unlock_requested_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    last_edited_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    last_edited_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
, "difficulty_tag" TEXT NOT NULL DEFAULT '', "tag_custom_label" TEXT NOT NULL DEFAULT '', "tag_custom_color" TEXT NOT NULL DEFAULT '', "is_deindexed" INTEGER NOT NULL DEFAULT 0, "pending_deletion" INTEGER NOT NULL DEFAULT 0, "pending_deletion_by" TEXT REFERENCES users(id) ON DELETE SET NULL, "pending_deletion_at" TEXT);

CREATE TABLE page_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    builder_json TEXT   NOT NULL DEFAULT '',
    builder_public INTEGER NOT NULL DEFAULT 0,
    edited_by   TEXT REFERENCES users(id) ON DELETE SET NULL,
    edit_message TEXT   NOT NULL DEFAULT '',
    is_revert   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(page_id, user_id)
);

CREATE TABLE page_builder_drafts (
    page_id      INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id      TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    builder_json TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (page_id, user_id)
);

CREATE TABLE site_settings (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    site_name   TEXT    NOT NULL DEFAULT 'BananaWiki',
    interface_language TEXT NOT NULL DEFAULT 'en',
    interface_language_fallback TEXT NOT NULL DEFAULT 'en',
    interface_languages_json TEXT NOT NULL DEFAULT '{}',
    primary_color    TEXT NOT NULL DEFAULT '#8fa0d4',
    secondary_color  TEXT NOT NULL DEFAULT '#1e1e2c',
    accent_color     TEXT NOT NULL DEFAULT '#7e9ada',
    text_color       TEXT NOT NULL DEFAULT '#c8ccd8',
    sidebar_color    TEXT NOT NULL DEFAULT '#1a1a24',
    bg_color         TEXT NOT NULL DEFAULT '#16161f',
    light_primary_color   TEXT NOT NULL DEFAULT '#4b63b6',
    light_secondary_color TEXT NOT NULL DEFAULT '#ffffff',
    light_accent_color    TEXT NOT NULL DEFAULT '#3553c7',
    light_text_color      TEXT NOT NULL DEFAULT '#202534',
    light_sidebar_color   TEXT NOT NULL DEFAULT '#e9edf5',
    light_bg_color        TEXT NOT NULL DEFAULT '#f6f7fb',
    default_theme_mode    TEXT NOT NULL DEFAULT 'dark'
                          CHECK(default_theme_mode IN ('dark','light')),
    tts_page_panel_enabled INTEGER NOT NULL DEFAULT 1,
    tts_public_access_enabled INTEGER NOT NULL DEFAULT 1,
    tts_auto_generate_enabled INTEGER NOT NULL DEFAULT 0,
    tts_enabled_languages TEXT NOT NULL DEFAULT 'en,it',
    setup_done  INTEGER NOT NULL DEFAULT 0,
    timezone    TEXT    NOT NULL DEFAULT 'UTC',
    session_limit_enabled INTEGER NOT NULL DEFAULT 0,
    suspended_account_deletion_enabled INTEGER NOT NULL DEFAULT 0,
    page_protection_enabled INTEGER NOT NULL DEFAULT 0,
    page_reservations_enabled INTEGER NOT NULL DEFAULT 0,
    page_reservation_duration_hours INTEGER NOT NULL DEFAULT 48,
    page_reservation_cooldown_hours INTEGER NOT NULL DEFAULT 24,
    default_reserved_pages_quota INTEGER NOT NULL DEFAULT 5,
    reservation_quota_auto_approve_max INTEGER NOT NULL DEFAULT 0,
    contribution_quota_auto_approve_max INTEGER NOT NULL DEFAULT 0,
    profile_contribution_chart_enabled INTEGER NOT NULL DEFAULT 1
    ,new_user_intro_enabled INTEGER NOT NULL DEFAULT 0
    ,onboarding_replay_disabled INTEGER NOT NULL DEFAULT 0
    ,intro_role_switching_enabled INTEGER NOT NULL DEFAULT 1
    ,intro_role_switching_roles TEXT NOT NULL DEFAULT 'user,editor,admin'
    ,assessment_points_badge_enabled INTEGER NOT NULL DEFAULT 0
    ,tts_gpu_enabled INTEGER NOT NULL DEFAULT 0
    ,tts_gpu_url TEXT NOT NULL DEFAULT ''
    ,tts_gpu_auth_token TEXT NOT NULL DEFAULT ''
    ,tts_gpu_timeout INTEGER NOT NULL DEFAULT 120
    ,tts_performance_mode TEXT NOT NULL DEFAULT 'auto'
          CHECK(tts_performance_mode IN ('auto','balanced','fast'))
    ,login_app_selector INTEGER NOT NULL DEFAULT 0
    ,page_builder_enabled INTEGER NOT NULL DEFAULT 0
    ,page_builder_access TEXT NOT NULL DEFAULT 'admin'
          CHECK(page_builder_access IN ('admin','editor','user'))
, "favicon_enabled" INTEGER NOT NULL DEFAULT 0, "favicon_type" TEXT NOT NULL DEFAULT 'yellow', "favicon_custom" TEXT NOT NULL DEFAULT '', "favicon_order" TEXT NOT NULL DEFAULT '[]', maintenance_mode INTEGER NOT NULL DEFAULT 0, maintenance_message TEXT NOT NULL DEFAULT '', "kanban_access" TEXT NOT NULL DEFAULT 'admin', "kanban_write_access" TEXT NOT NULL DEFAULT 'admin', "kanban_public_access_enabled" INTEGER NOT NULL DEFAULT 0, "canvas_access" TEXT NOT NULL DEFAULT 'admin', "canvas_write_access" TEXT NOT NULL DEFAULT 'admin', "canvas_public_access_enabled" INTEGER NOT NULL DEFAULT 0, "canvas_open_access" INTEGER NOT NULL DEFAULT 0, "contributor_leaderboard_enabled" INTEGER NOT NULL DEFAULT 0, "beta_can_preview_drafts" INTEGER NOT NULL DEFAULT 1, "beta_can_use_advanced_editor" INTEGER NOT NULL DEFAULT 1, "beta_can_access_experimental_ui" INTEGER NOT NULL DEFAULT 0, "beta_extended_upload_limit_mb" INTEGER NOT NULL DEFAULT 32, "beta_custom_badge_text" TEXT NOT NULL DEFAULT 'Beta Tester', "last_chat_cleanup_at" TEXT, "last_server_restart_at" TEXT, "chat_attachments_per_day_limit" INTEGER NOT NULL DEFAULT 10, "chat_auto_clear_messages" INTEGER NOT NULL DEFAULT 0, "chat_auto_clear_attachments" INTEGER NOT NULL DEFAULT 0, "chat_message_retention_days" INTEGER NOT NULL DEFAULT 0, "chat_attachment_retention_days" INTEGER NOT NULL DEFAULT 7, "chat_dm_enabled" INTEGER NOT NULL DEFAULT 1, "chat_dm_auto_clear_messages" INTEGER NOT NULL DEFAULT 0, "chat_dm_auto_clear_attachments" INTEGER NOT NULL DEFAULT 0, "chat_dm_message_retention_days" INTEGER NOT NULL DEFAULT 0, "chat_dm_attachment_retention_days" INTEGER NOT NULL DEFAULT 7, "chat_group_enabled" INTEGER NOT NULL DEFAULT 1, "chat_group_auto_clear_messages" INTEGER NOT NULL DEFAULT 0, "chat_group_auto_clear_attachments" INTEGER NOT NULL DEFAULT 0, "chat_group_message_retention_days" INTEGER NOT NULL DEFAULT 0, "chat_group_attachment_retention_days" INTEGER NOT NULL DEFAULT 7, "chat_max_message_length" INTEGER NOT NULL DEFAULT 5000, "chat_attachments_enabled" INTEGER NOT NULL DEFAULT 1, "chat_max_attachment_size_mb" INTEGER NOT NULL DEFAULT 5, "chat_allow_dm_creation" INTEGER NOT NULL DEFAULT 1, "chat_allow_group_creation" INTEGER NOT NULL DEFAULT 1, "chat_cleanup_enabled" INTEGER NOT NULL DEFAULT 0, "chat_cleanup_frequency_days" INTEGER NOT NULL DEFAULT 7, "chat_cleanup_hour" INTEGER NOT NULL DEFAULT 3, "chat_cleanup_split_configured" INTEGER NOT NULL DEFAULT 0, "banana_mode" INTEGER NOT NULL DEFAULT 0, "telegram_sync_enabled" INTEGER NOT NULL DEFAULT 0, "telegram_sync_token" TEXT NOT NULL DEFAULT '', telegram_sync_userids TEXT NOT NULL DEFAULT '', "telegram_sync_split_threshold_mb" INTEGER NOT NULL DEFAULT 45, "telegram_sync_compress_level" INTEGER NOT NULL DEFAULT 9, "telegram_sync_include_chat_attachments" INTEGER NOT NULL DEFAULT 1, "custom_pages_max_video_size_mb" INTEGER NOT NULL DEFAULT 100, "auto_logout_enabled" INTEGER NOT NULL DEFAULT 0, "auto_logout_hour" INTEGER NOT NULL DEFAULT 0, "docs_bypass_deletion_slowdown" INTEGER NOT NULL DEFAULT 1, "docs_category_id" INTEGER, "upload_mode" TEXT NOT NULL DEFAULT 'allow_all', "upload_whitelist" TEXT NOT NULL DEFAULT '', "upload_blacklist" TEXT NOT NULL DEFAULT 'exe,bat,cmd,com,scr,pif,msi,msp,mst,cpl,hta,inf,ins,isp,jse,lnk,reg,rgs,sct,shb,shs,vbe,vbs,wsc,wsf,wsh,ws,ps1,ps2,psc1,psc2,dll,sys', "platform_upload_blacklist" TEXT NOT NULL DEFAULT '', "upload_max_size_mb" INTEGER NOT NULL DEFAULT 100, "profile_group_badges_enabled" INTEGER NOT NULL DEFAULT 0, "last_backup_sent_at" REAL NOT NULL DEFAULT 0, "telegram_sync_trigger_enabled" INTEGER NOT NULL DEFAULT 1, "telegram_sync_trigger_immediate" INTEGER NOT NULL DEFAULT 1, "telegram_sync_trigger_high" INTEGER NOT NULL DEFAULT 1, "telegram_sync_trigger_normal" INTEGER NOT NULL DEFAULT 1, "telegram_sync_trigger_low" INTEGER NOT NULL DEFAULT 1, "telegram_sync_daily_enabled" INTEGER NOT NULL DEFAULT 0, "telegram_sync_daily_time" TEXT NOT NULL DEFAULT '03:00', "telegram_sync_last_update_id" INTEGER NOT NULL DEFAULT 0, "telegram_sync_last_polling_at" REAL NOT NULL DEFAULT 0, "telegram_sync_pending_restores" TEXT NOT NULL DEFAULT '{}', "pdf_export_enabled" INTEGER NOT NULL DEFAULT 1, "markdown_export_enabled" INTEGER NOT NULL DEFAULT 1, "public_mode" INTEGER NOT NULL DEFAULT 0, "public_mode_until" TEXT, "public_mode_message" TEXT NOT NULL DEFAULT '', "public_mode_show_message" INTEGER NOT NULL DEFAULT 0, "open_signup" INTEGER NOT NULL DEFAULT 0, "open_signup_until" TEXT, "bot_protection_enabled" INTEGER NOT NULL DEFAULT 1, "feedback_bot_token" TEXT NOT NULL DEFAULT '', "feedback_telegram_userids" TEXT NOT NULL DEFAULT '', "feedback_show_to_anonymous" INTEGER NOT NULL DEFAULT 0, "feedback_cooldown_minutes" INTEGER NOT NULL DEFAULT 60, sidebar_apps_order TEXT NOT NULL DEFAULT '', approval_required INTEGER NOT NULL DEFAULT 0, approval_denied_timeout_hours INTEGER NOT NULL DEFAULT 24, quota_request_cooldown_hours INTEGER NOT NULL DEFAULT 0, approval_pending_timeout_hours INTEGER NOT NULL DEFAULT 0, devtools_enabled INTEGER NOT NULL DEFAULT 0, "upload_quota_per_day_count" INTEGER NOT NULL DEFAULT 0, "upload_quota_per_day_bytes" INTEGER NOT NULL DEFAULT 0, "contribution_approval_enabled" INTEGER NOT NULL DEFAULT 0, "draft_expiration_hours" INTEGER NOT NULL DEFAULT 0, "default_contribution_quota" INTEGER NOT NULL DEFAULT 5, "feedback_chat_ids" TEXT NOT NULL DEFAULT '', "feedback_show_anonymous" INTEGER NOT NULL DEFAULT 0, "feedback_cooldown_seconds" INTEGER NOT NULL DEFAULT 3600, "api_service_enabled" INTEGER NOT NULL DEFAULT 0, "api_service_rate_limit" INTEGER NOT NULL DEFAULT 60, "api_service_admin_rate_limit" INTEGER NOT NULL DEFAULT 120, "api_service_max_tokens_per_user" INTEGER NOT NULL DEFAULT 5, "kanban_open_access" INTEGER NOT NULL DEFAULT 0, "list_order_version" INTEGER NOT NULL DEFAULT 0);

CREATE TABLE login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ip           TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

CREATE TABLE rate_limit_hits (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ip       TEXT NOT NULL,
    bucket   TEXT NOT NULL,
    hit_at   TEXT NOT NULL
);

CREATE TABLE announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT    NOT NULL DEFAULT '',
    color           TEXT    NOT NULL DEFAULT 'orange'
                            CHECK(color IN ('red','orange','yellow','blue','green')),
    text_size       TEXT    NOT NULL DEFAULT 'normal'
                            CHECK(text_size IN ('small','normal','large')),
    visibility      TEXT    NOT NULL DEFAULT 'both'
                            CHECK(visibility IN ('logged_in','logged_out','both')),
    expires_at      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    not_removable   INTEGER NOT NULL DEFAULT 1,
    show_countdown  INTEGER NOT NULL DEFAULT 1,
    audience_mode   TEXT    NOT NULL DEFAULT 'all'
                            CHECK(audience_mode IN ('all','allowlist','denylist')),
    custom_background TEXT,
    custom_text_color TEXT,
    revision        INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE announcement_audience_users (
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (announcement_id, user_id)
);

CREATE INDEX idx_announcement_audience_user
    ON announcement_audience_users(user_id, announcement_id);

CREATE TABLE username_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_username TEXT   NOT NULL,
    new_username TEXT   NOT NULL,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE editor_category_access (
    user_id     TEXT    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    restricted  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE editor_allowed_categories (
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    UNIQUE(user_id, category_id)
);

CREATE TABLE page_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    uploaded_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
, blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL);

CREATE TABLE user_profiles (
    user_id         TEXT    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    real_name       TEXT    NOT NULL DEFAULT '',
    bio             TEXT    NOT NULL DEFAULT '',
    birth_date      TEXT    NOT NULL DEFAULT '',
    avatar_filename TEXT    NOT NULL DEFAULT '',
    page_published  INTEGER NOT NULL DEFAULT 0,
    page_disabled_by_admin INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user1_id    TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user2_id    TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')), "unread_count_user1" INTEGER NOT NULL DEFAULT 0, "unread_count_user2" INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user1_id, user2_id)
);

CREATE TABLE chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    sender_id   TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL DEFAULT '',
    is_deleted  INTEGER NOT NULL DEFAULT 0,
    deleted_at  TEXT,
    ip_address  TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE chat_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
, blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL);

CREATE TABLE group_chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    creator_id  TEXT    REFERENCES users(id) ON DELETE SET NULL,
    invite_code TEXT    NOT NULL UNIQUE,
    is_global   INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
, "description" TEXT NOT NULL DEFAULT '');

CREATE TABLE group_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES group_chats(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT    NOT NULL DEFAULT 'member'
                    CHECK(role IN ('member','moderator','owner')),
    timed_out_until TEXT,
    joined_at       TEXT    NOT NULL DEFAULT (datetime('now')), banned INTEGER NOT NULL DEFAULT 0, unread_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_id, user_id)
);

CREATE TABLE group_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES group_chats(id) ON DELETE CASCADE,
    sender_id   TEXT    REFERENCES users(id) ON DELETE SET NULL,
    content     TEXT    NOT NULL DEFAULT '',
    is_system   INTEGER NOT NULL DEFAULT 0,
    is_deleted  INTEGER NOT NULL DEFAULT 0,
    deleted_at  TEXT,
    ip_address  TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE group_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES group_messages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
, blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL);

CREATE TABLE profile_group_badges (
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id    INTEGER NOT NULL REFERENCES group_chats(id) ON DELETE CASCADE,
    visible     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE role_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_role    TEXT    NOT NULL,
    new_role    TEXT    NOT NULL,
    changed_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE user_custom_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT    NOT NULL,
    color       TEXT    NOT NULL DEFAULT '#9b59b6',
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE user_permissions (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_key  TEXT NOT NULL,
    UNIQUE(user_id, permission_key)
);

CREATE TABLE user_category_access (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    access_type TEXT NOT NULL CHECK(access_type IN ('read','write')),
    restricted  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, access_type)
);

CREATE TABLE user_allowed_categories (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    access_type TEXT NOT NULL CHECK(access_type IN ('read','write')),
    UNIQUE(user_id, category_id, access_type)
);

CREATE TABLE badge_types (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT    NOT NULL DEFAULT '',
    icon            TEXT    NOT NULL DEFAULT '🏆',
    color           TEXT    NOT NULL DEFAULT '#ffd700',
    enabled         INTEGER NOT NULL DEFAULT 1,
    auto_trigger    INTEGER NOT NULL DEFAULT 0,
    trigger_type    TEXT    NOT NULL DEFAULT ''
                            CHECK(trigger_type IN ('','category_count','contribution_count','first_edit','member_days','easter_egg','reading_time','article_count')),
    trigger_threshold INTEGER NOT NULL DEFAULT 0,
    allow_multiple  INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE user_badges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_type_id   INTEGER NOT NULL REFERENCES badge_types(id) ON DELETE CASCADE,
    earned_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    awarded_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    revoked         INTEGER NOT NULL DEFAULT 0,
    revoked_at      TEXT,
    revoked_by      TEXT REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE badge_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_type_id   INTEGER NOT NULL REFERENCES badge_types(id) ON DELETE CASCADE,
    notified        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE page_reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reserved_at     TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    released_at     TEXT,
    UNIQUE(page_id)
);

CREATE TABLE user_page_cooldowns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cooldown_until  TEXT    NOT NULL,
    UNIQUE(page_id, user_id)
);

CREATE TABLE reservation_quota_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_quota INTEGER NOT NULL,
    reason          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','approved','denied','cancelled')),
    reviewed_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    review_reason   TEXT    NOT NULL DEFAULT '',
    review_source   TEXT    NOT NULL DEFAULT 'manual'
                            CHECK(review_source IN ('manual','automatic')),
    reviewed_at     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE plugins (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    version      TEXT NOT NULL,
    author       TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    builtin      INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 0,
    installed_at TEXT NOT NULL DEFAULT (datetime('now')),
    enabled_at   TEXT,
    disabled_at  TEXT
);

CREATE TABLE impersonation_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id        TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT
);

CREATE TABLE kanban_boards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    created_by  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
, visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('private','shared','public')));

CREATE TABLE kanban_columns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id    INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE kanban_tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    column_id   INTEGER NOT NULL REFERENCES kanban_columns(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    priority    TEXT    NOT NULL DEFAULT 'medium'
                        CHECK(priority IN ('low','medium','high','critical')),
    assigned_to TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_by  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    due_date    TEXT    DEFAULT NULL,
    color       TEXT    NOT NULL DEFAULT '',
    labels      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE kanban_board_shares (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id     INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
    share_type   TEXT    NOT NULL CHECK(share_type IN ('role','user')),
    target       TEXT    NOT NULL,
    access_level TEXT    NOT NULL DEFAULT 'view'
                         CHECK(access_level IN ('view','write')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(board_id, share_type, target)
);

CREATE TABLE kanban_ticket_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    uploaded_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
, blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL);

CREATE TABLE kanban_ticket_assignees (
    ticket_id   INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticket_id, user_id)
);

CREATE TABLE kanban_ticket_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    old_description TEXT    NOT NULL DEFAULT '',
    new_description TEXT    NOT NULL DEFAULT '',
    changed_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE kanban_ticket_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE kanban_activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id    INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
    user_id     TEXT    REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT    NOT NULL,
    details     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE kanban_board_history (
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

CREATE TABLE canvas__layouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    creator_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    is_published INTEGER NOT NULL DEFAULT 1,
    is_archived  INTEGER NOT NULL DEFAULT 0,
    visibility  TEXT    NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','shared','public')),
    data        TEXT    NOT NULL DEFAULT '{"nodes":[],"edges":[],"viewport":{"x":0,"y":0,"zoom":1}}',
    version     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE canvas__permissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    layout_id   INTEGER NOT NULL REFERENCES canvas__layouts(id) ON DELETE CASCADE,
    user_id     TEXT    REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT,
    permission  TEXT    NOT NULL DEFAULT 'view'
                        CHECK(permission IN ('view','edit','none')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(layout_id, user_id),
    UNIQUE(layout_id, role)
);

CREATE TABLE canvas__history (
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

CREATE TABLE assessments (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id                INTEGER NOT NULL UNIQUE REFERENCES pages(id) ON DELETE CASCADE,
    title                  TEXT    NOT NULL DEFAULT '',
    description            TEXT    NOT NULL DEFAULT '',
    allow_multiple_attempts INTEGER NOT NULL DEFAULT 0,
    banned_roles_json      TEXT    NOT NULL DEFAULT '[]',
    banned_users_json      TEXT    NOT NULL DEFAULT '[]',
    created_by             TEXT    REFERENCES users(id) ON DELETE SET NULL,
    updated_by             TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE assessment_questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id     INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    prompt            TEXT    NOT NULL,
    question_type     TEXT    NOT NULL
                           CHECK(question_type IN ('single_choice','multiple_choice','free_text')),
    options_json      TEXT    NOT NULL DEFAULT '[]',
    correct_answers_json TEXT NOT NULL DEFAULT '[]',
    points_correct    INTEGER NOT NULL DEFAULT 1,
    points_incorrect  INTEGER NOT NULL DEFAULT 0,
    is_required       INTEGER NOT NULL DEFAULT 1,
    sort_order        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE assessment_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id   INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    total_points    INTEGER NOT NULL DEFAULT 0,
    max_points      INTEGER NOT NULL DEFAULT 0,
    submitted_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(assessment_id, user_id, attempt_number)
);

CREATE TABLE assessment_answers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id     INTEGER NOT NULL REFERENCES assessment_attempts(id) ON DELETE CASCADE,
    question_id    INTEGER NOT NULL REFERENCES assessment_questions(id) ON DELETE CASCADE,
    answer_json    TEXT    NOT NULL DEFAULT 'null',
    is_correct     INTEGER,
    awarded_points INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE temp_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL UNIQUE REFERENCES pages(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    show_countdown  INTEGER NOT NULL DEFAULT 1,
    set_by          TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE temp_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    show_countdown  INTEGER NOT NULL DEFAULT 1,
    set_by          TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE temp_roles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    original_role   TEXT    NOT NULL
                            CHECK(original_role IN ('user','editor','admin','owner')),
    expires_at      TEXT    NOT NULL,
    show_countdown  INTEGER NOT NULL DEFAULT 1,
    set_by          TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE custom_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL DEFAULT '',
    content_type    TEXT    NOT NULL DEFAULT 'html'
                            CHECK(content_type IN (
                                'redirect','html','html_styled','html_full',
                                'markdown','wiki_page','plain_text',
                                'json_content','xml_content',
                                'image','image_page',
                                'youtube_video','video_hosted',
                                'file_download','file_listing',
                                'iframe_embed','page_embed',
                                'link_list','code_snippet'
                            )),
    content         TEXT    NOT NULL DEFAULT '',
    css             TEXT    NOT NULL DEFAULT '',
    js              TEXT    NOT NULL DEFAULT '',
    redirect_url    TEXT    NOT NULL DEFAULT '',
    redirect_code   INTEGER NOT NULL DEFAULT 302,
    links_json      TEXT    NOT NULL DEFAULT '[]',
    code_language   TEXT    NOT NULL DEFAULT 'text',
    iframe_url      TEXT    NOT NULL DEFAULT '',
    iframe_height   TEXT    NOT NULL DEFAULT '600px',
    iframe_sandbox  TEXT    NOT NULL DEFAULT '',
    iframe_allow    TEXT    NOT NULL DEFAULT '',
    video_url       TEXT    NOT NULL DEFAULT '',
    video_autoplay  INTEGER NOT NULL DEFAULT 0,
    video_controls  INTEGER NOT NULL DEFAULT 1,
    video_loop      INTEGER NOT NULL DEFAULT 0,
    video_muted     INTEGER NOT NULL DEFAULT 0,
    meta_description TEXT   NOT NULL DEFAULT '',
    is_published    INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE custom_page_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    custom_page_id  INTEGER NOT NULL REFERENCES custom_pages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    mime_type       TEXT    NOT NULL DEFAULT '',
    file_size       INTEGER NOT NULL DEFAULT 0,
    uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
, blob_id INTEGER REFERENCES file_blobs(id) ON DELETE SET NULL);

CREATE TABLE file_blobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    content     BLOB    NOT NULL,
    mime_type   TEXT    NOT NULL DEFAULT 'application/octet-stream',
    file_size   INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE temp_page_index_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL UNIQUE REFERENCES pages(id) ON DELETE CASCADE,
    target_state    INTEGER NOT NULL,
    restore_state   INTEGER NOT NULL,
    expires_at      TEXT    NOT NULL,
    show_countdown  INTEGER NOT NULL DEFAULT 1,
    set_by          TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE custom_roles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description     TEXT    NOT NULL DEFAULT '',
    base_role       TEXT    NOT NULL DEFAULT 'user'
                            CHECK(base_role IN ('user','editor')),
    read_restricted  INTEGER NOT NULL DEFAULT 0,
    write_restricted INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE custom_role_permissions (
    role_id         INTEGER NOT NULL REFERENCES custom_roles(id) ON DELETE CASCADE,
    permission_key  TEXT    NOT NULL,
    UNIQUE(role_id, permission_key)
);

CREATE TABLE custom_role_categories (
    role_id         INTEGER NOT NULL REFERENCES custom_roles(id) ON DELETE CASCADE,
    category_id     INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    access_type     TEXT    NOT NULL CHECK(access_type IN ('read','write')),
    UNIQUE(role_id, category_id, access_type)
);

CREATE TABLE pending_contributions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT    NOT NULL DEFAULT '',
    content         TEXT    NOT NULL DEFAULT '',
    reason          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','approved','denied','expired','withdrawn')),
    reviewed_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    review_reason   TEXT    NOT NULL DEFAULT '',
    review_source   TEXT    NOT NULL DEFAULT 'manual'
                            CHECK(review_source IN ('manual','automatic')),
    reviewed_at     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(page_id, user_id)
);

CREATE TABLE contribution_quota_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_quota INTEGER NOT NULL,
    reason          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','approved','denied','cancelled')),
    reviewed_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    review_reason   TEXT    NOT NULL DEFAULT '',
    reviewed_at     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
, "review_source" TEXT NOT NULL DEFAULT 'manual' CHECK(review_source IN ('manual','automatic')));

CREATE TABLE editing_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username        TEXT    NOT NULL,
    last_heartbeat  TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(page_id, user_id)
);

CREATE TABLE suspension_audit (
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

CREATE INDEX idx_pages_protected_by ON pages(protected_by);

CREATE INDEX idx_pages_unlock_requested_at ON pages(protection_unlock_requested_at);

CREATE UNIQUE INDEX idx_reservation_quota_requests_pending ON reservation_quota_requests(user_id) WHERE status='pending';

CREATE INDEX idx_login_attempts_ip ON login_attempts(ip);

CREATE INDEX idx_login_attempts_ip_at ON login_attempts (ip, attempted_at);

CREATE INDEX idx_rate_limit_hits_ip_bucket ON rate_limit_hits(ip, bucket, hit_at);

CREATE INDEX idx_page_history_page_id ON page_history(page_id);

CREATE INDEX idx_chat_messages_chat_id ON chat_messages(chat_id);

CREATE INDEX idx_group_messages_group_id ON group_messages(group_id);

CREATE INDEX idx_badge_notifications_user_id ON badge_notifications(user_id, notified);

CREATE INDEX idx_pages_title ON pages(title COLLATE NOCASE);

CREATE INDEX idx_pages_category_id ON pages(category_id);

CREATE INDEX idx_pages_last_edited_by ON pages(last_edited_by);

CREATE INDEX idx_page_history_edited_by ON page_history(edited_by);

CREATE INDEX idx_drafts_user_id ON drafts(user_id);

CREATE INDEX idx_user_profiles_user_id ON user_profiles(user_id);

CREATE INDEX idx_kanban_tickets_column ON kanban_tickets(column_id);

CREATE INDEX idx_kanban_columns_board ON kanban_columns(board_id);

CREATE INDEX idx_kanban_ticket_comments_ticket ON kanban_ticket_comments(ticket_id);

CREATE INDEX idx_kanban_ticket_history_ticket ON kanban_ticket_history(ticket_id);

CREATE INDEX idx_kanban_ticket_attach_ticket ON kanban_ticket_attachments(ticket_id);

CREATE INDEX idx_kanban_ticket_assignees_user ON kanban_ticket_assignees(user_id);

CREATE INDEX idx_kanban_activity_log_board ON kanban_activity_log(board_id, created_at DESC);

CREATE INDEX idx_user_badges_user ON user_badges(user_id);

CREATE INDEX idx_user_permissions_user ON user_permissions(user_id);

CREATE INDEX idx_canvas_perms_layout ON canvas__permissions(layout_id);

CREATE INDEX idx_assessment_questions_assessment ON assessment_questions(assessment_id);

CREATE INDEX idx_assessment_attempts_assessment_user ON assessment_attempts(assessment_id, user_id);

CREATE INDEX idx_assessment_answers_attempt ON assessment_answers(attempt_id);

CREATE INDEX idx_rate_limit_hits_hit_at ON rate_limit_hits(hit_at);

CREATE UNIQUE INDEX idx_pages_single_home ON pages(is_home) WHERE is_home=1;

CREATE TABLE cleanup_lease (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        held_by         TEXT,
        acquired_at     TEXT,
        expires_at      TEXT
    );

CREATE INDEX idx_pending_contributions_page_user ON pending_contributions(page_id, user_id);

CREATE INDEX idx_pending_contributions_status ON pending_contributions(status);

CREATE UNIQUE INDEX idx_contribution_quota_requests_pending ON contribution_quota_requests(user_id) WHERE status='pending';

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

CREATE INDEX idx_tts_generations_status
        ON tts_generations(status);

CREATE TABLE canvas__events (
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

CREATE INDEX idx_canvas_events_layout_seq
        ON canvas__events(layout_id, seq);

CREATE TABLE kanban_events (
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

CREATE INDEX idx_kanban_events_board_seq
        ON kanban_events(board_id, seq);

CREATE INDEX idx_kanban_board_history_board
        ON kanban_board_history(board_id, created_at);

CREATE INDEX idx_canvas_history_layout
        ON canvas__history(layout_id, created_at);

CREATE TABLE analytics_daily (
        day              TEXT    NOT NULL,
        kind             TEXT    NOT NULL,
        count            INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, kind)
    );

CREATE INDEX idx_analytics_daily_day
        ON analytics_daily(day);

CREATE TABLE api_service__tokens (
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

CREATE INDEX idx_api_service_tokens_user
        ON api_service__tokens(user_id);

CREATE TABLE api_service__audit_log (
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

CREATE INDEX idx_api_service_audit_user
        ON api_service__audit_log(user_id);

CREATE INDEX idx_api_service_audit_created
        ON api_service__audit_log(created_at);

CREATE TABLE account_merge_requests (
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

CREATE INDEX idx_merge_requests_source
        ON account_merge_requests(source_user_id);

CREATE INDEX idx_merge_requests_target
        ON account_merge_requests(target_user_id);

CREATE INDEX idx_merge_requests_status
        ON account_merge_requests(status, created_at DESC);

CREATE TABLE account_merge_logs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        target_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        source_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        merged_by       TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        data_transferred TEXT   NOT NULL DEFAULT '{}',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );

CREATE INDEX idx_merge_logs_target
        ON account_merge_logs(target_user_id);

CREATE INDEX idx_merge_logs_source
        ON account_merge_logs(source_user_id);

CREATE TABLE kanban_user_board_order (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT,
        board_id    INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        UNIQUE(user_id, board_id)
    );

CREATE INDEX idx_kanban_user_board_order_user ON kanban_user_board_order(user_id, sort_order);

CREATE TABLE canvas_user_layout_order (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT,
        layout_id   INTEGER NOT NULL REFERENCES canvas__layouts(id) ON DELETE CASCADE,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        UNIQUE(user_id, layout_id)
    );

CREATE INDEX idx_canvas_user_layout_order_user ON canvas_user_layout_order(user_id, sort_order);

CREATE INDEX idx_pages_navigation ON pages(is_home, category_id, sort_order, title, id);

CREATE TABLE federation_identity (
            id INTEGER PRIMARY KEY CHECK (id=1),
            wiki_id TEXT NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 0
        );

CREATE TABLE federation_peers (
            wiki_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            secret TEXT NOT NULL,
            audience_category INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            generation TEXT NOT NULL,
            last_sequence INTEGER NOT NULL DEFAULT 0,
            last_success REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            next_attempt REAL NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            lease TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,
            last_served REAL NOT NULL DEFAULT 0
        );

CREATE TABLE federation_page_ids (
            page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
            public_id TEXT NOT NULL UNIQUE
        );

CREATE TABLE federation_shares (
            peer_id TEXT NOT NULL REFERENCES federation_peers(wiki_id) ON DELETE CASCADE,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            granted_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER,
            PRIMARY KEY (peer_id, page_id)
        );

CREATE TABLE federation_copies (
            peer_id TEXT NOT NULL REFERENCES federation_peers(wiki_id) ON DELETE CASCADE,
            page_id TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_path TEXT NOT NULL,
            revision TEXT NOT NULL,
            PRIMARY KEY (peer_id, page_id)
        );

CREATE TABLE federation_nonces (
            peer_id TEXT NOT NULL REFERENCES federation_peers(wiki_id) ON DELETE CASCADE,
            nonce TEXT NOT NULL,
            expires REAL NOT NULL,
            PRIMARY KEY (peer_id, nonce)
        );
