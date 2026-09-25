-- Pre-versioned schema baseline; imported atomically by legacy.py.
CREATE TABLE IF NOT EXISTS users (
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
);

CREATE TABLE IF NOT EXISTS user_sessions (
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_sessions_token_hash
    ON user_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_active
    ON user_sessions(user_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS invite_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT,
    max_uses    INTEGER NOT NULL DEFAULT 1,
    use_count   INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_at  TEXT
);

CREATE TABLE IF NOT EXISTS invite_code_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invite_code_id  INTEGER NOT NULL REFERENCES invite_codes(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    used_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    parent_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pages (
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
);

CREATE TABLE IF NOT EXISTS page_history (
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

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(page_id, user_id)
);

CREATE TABLE IF NOT EXISTS page_builder_drafts (
    page_id      INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id      TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    builder_json TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (page_id, user_id)
);

CREATE TABLE IF NOT EXISTS site_settings (
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
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ip           TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limit_hits (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ip       TEXT NOT NULL,
    bucket   TEXT NOT NULL,
    hit_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcements (
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

CREATE TABLE IF NOT EXISTS announcement_audience_users (
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (announcement_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_announcement_audience_user
    ON announcement_audience_users(user_id, announcement_id);

INSERT OR IGNORE INTO site_settings (id) VALUES (1);

CREATE TABLE IF NOT EXISTS username_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_username TEXT   NOT NULL,
    new_username TEXT   NOT NULL,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS editor_category_access (
    user_id     TEXT    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    restricted  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS editor_allowed_categories (
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    UNIQUE(user_id, category_id)
);

CREATE TABLE IF NOT EXISTS page_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    uploaded_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id         TEXT    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    real_name       TEXT    NOT NULL DEFAULT '',
    bio             TEXT    NOT NULL DEFAULT '',
    birth_date      TEXT    NOT NULL DEFAULT '',
    avatar_filename TEXT    NOT NULL DEFAULT '',
    page_published  INTEGER NOT NULL DEFAULT 0,
    page_disabled_by_admin INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user1_id    TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user2_id    TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user1_id, user2_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    sender_id   TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL DEFAULT '',
    is_deleted  INTEGER NOT NULL DEFAULT 0,
    deleted_at  TEXT,
    ip_address  TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    creator_id  TEXT    REFERENCES users(id) ON DELETE SET NULL,
    invite_code TEXT    NOT NULL UNIQUE,
    is_global   INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES group_chats(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT    NOT NULL DEFAULT 'member'
                    CHECK(role IN ('member','moderator','owner')),
    timed_out_until TEXT,
    joined_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS group_messages (
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

CREATE TABLE IF NOT EXISTS group_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES group_messages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile_group_badges (
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id    INTEGER NOT NULL REFERENCES group_chats(id) ON DELETE CASCADE,
    visible     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE IF NOT EXISTS role_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_role    TEXT    NOT NULL,
    new_role    TEXT    NOT NULL,
    changed_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_custom_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT    NOT NULL,
    color       TEXT    NOT NULL DEFAULT '#9b59b6',
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_permissions (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_key  TEXT NOT NULL,
    UNIQUE(user_id, permission_key)
);

CREATE TABLE IF NOT EXISTS user_category_access (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    access_type TEXT NOT NULL CHECK(access_type IN ('read','write')),
    restricted  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, access_type)
);

CREATE TABLE IF NOT EXISTS user_allowed_categories (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    access_type TEXT NOT NULL CHECK(access_type IN ('read','write')),
    UNIQUE(user_id, category_id, access_type)
);

CREATE TABLE IF NOT EXISTS badge_types (
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

CREATE TABLE IF NOT EXISTS user_badges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_type_id   INTEGER NOT NULL REFERENCES badge_types(id) ON DELETE CASCADE,
    earned_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    awarded_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    revoked         INTEGER NOT NULL DEFAULT 0,
    revoked_at      TEXT,
    revoked_by      TEXT REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS badge_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_type_id   INTEGER NOT NULL REFERENCES badge_types(id) ON DELETE CASCADE,
    notified        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS page_reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reserved_at     TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    released_at     TEXT,
    UNIQUE(page_id)
);

CREATE TABLE IF NOT EXISTS user_page_cooldowns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cooldown_until  TEXT    NOT NULL,
    UNIQUE(page_id, user_id)
);

CREATE TABLE IF NOT EXISTS reservation_quota_requests (
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

CREATE TABLE IF NOT EXISTS plugins (
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

CREATE TABLE IF NOT EXISTS api_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS userbot_api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT    NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS impersonation_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id        TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user_id  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT
);

CREATE TABLE IF NOT EXISTS kanban_boards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    created_by  TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kanban_columns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id    INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kanban_tickets (
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

CREATE TABLE IF NOT EXISTS kanban_board_shares (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id     INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
    share_type   TEXT    NOT NULL CHECK(share_type IN ('role','user')),
    target       TEXT    NOT NULL,
    access_level TEXT    NOT NULL DEFAULT 'view'
                         CHECK(access_level IN ('view','write')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(board_id, share_type, target)
);

CREATE TABLE IF NOT EXISTS kanban_ticket_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    uploaded_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kanban_ticket_assignees (
    ticket_id   INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticket_id, user_id)
);

CREATE TABLE IF NOT EXISTS kanban_ticket_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    old_description TEXT    NOT NULL DEFAULT '',
    new_description TEXT    NOT NULL DEFAULT '',
    changed_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kanban_ticket_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES kanban_tickets(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kanban_activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id    INTEGER NOT NULL REFERENCES kanban_boards(id) ON DELETE CASCADE,
    user_id     TEXT    REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT    NOT NULL,
    details     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

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

CREATE TABLE IF NOT EXISTS canvas__layouts (
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

CREATE TABLE IF NOT EXISTS canvas__permissions (
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

CREATE TABLE IF NOT EXISTS assessments (
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

CREATE TABLE IF NOT EXISTS assessment_questions (
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

CREATE TABLE IF NOT EXISTS assessment_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id   INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    total_points    INTEGER NOT NULL DEFAULT 0,
    max_points      INTEGER NOT NULL DEFAULT 0,
    submitted_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(assessment_id, user_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS assessment_answers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id     INTEGER NOT NULL REFERENCES assessment_attempts(id) ON DELETE CASCADE,
    question_id    INTEGER NOT NULL REFERENCES assessment_questions(id) ON DELETE CASCADE,
    answer_json    TEXT    NOT NULL DEFAULT 'null',
    is_correct     INTEGER,
    awarded_points INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS beta_testers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT    NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','expired')),
    added_by    TEXT    REFERENCES users(id) ON DELETE SET NULL,
    joined_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT
);

CREATE TABLE IF NOT EXISTS beta_tester_invites (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invited_by        TEXT    REFERENCES users(id) ON DELETE SET NULL,
    status            TEXT    NOT NULL DEFAULT 'pending'
                              CHECK(status IN ('pending','accepted','declined','expired')),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    invite_expires_at TEXT    NOT NULL,
    beta_expires_at   TEXT
);

CREATE TABLE IF NOT EXISTS temp_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL UNIQUE REFERENCES pages(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    show_countdown  INTEGER NOT NULL DEFAULT 1,
    set_by          TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS temp_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    show_countdown  INTEGER NOT NULL DEFAULT 1,
    set_by          TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS temp_roles (
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

CREATE TABLE IF NOT EXISTS custom_pages (
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

CREATE TABLE IF NOT EXISTS custom_page_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    custom_page_id  INTEGER NOT NULL REFERENCES custom_pages(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    mime_type       TEXT    NOT NULL DEFAULT '',
    file_size       INTEGER NOT NULL DEFAULT 0,
    uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS file_blobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    content     BLOB    NOT NULL,
    mime_type   TEXT    NOT NULL DEFAULT 'application/octet-stream',
    file_size   INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);


CREATE TABLE IF NOT EXISTS temp_page_index_state (
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

CREATE TABLE IF NOT EXISTS custom_roles (
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

CREATE TABLE IF NOT EXISTS custom_role_permissions (
    role_id         INTEGER NOT NULL REFERENCES custom_roles(id) ON DELETE CASCADE,
    permission_key  TEXT    NOT NULL,
    UNIQUE(role_id, permission_key)
);

CREATE TABLE IF NOT EXISTS custom_role_categories (
    role_id         INTEGER NOT NULL REFERENCES custom_roles(id) ON DELETE CASCADE,
    category_id     INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    access_type     TEXT    NOT NULL CHECK(access_type IN ('read','write')),
    UNIQUE(role_id, category_id, access_type)
);

CREATE TABLE IF NOT EXISTS pending_contributions (
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

CREATE TABLE IF NOT EXISTS contribution_quota_requests (
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
);

CREATE TABLE IF NOT EXISTS editing_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username        TEXT    NOT NULL,
    last_heartbeat  TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(page_id, user_id)
);
