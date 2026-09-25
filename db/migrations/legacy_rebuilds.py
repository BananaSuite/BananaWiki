"""Table rebuilds for importing legacy Wiki databases in one transaction."""

import re



def _migrate_role_add_protected_admin(conn, cur):
    """Expand the role CHECK constraint to include 'protected_admin'.

    SQLite does not support ALTER TABLE … ALTER COLUMN, so we recreate the
    users table with an updated constraint while preserving all data.
    Also renames any existing 'superadmin' values to 'protected_admin'.

    Column definitions are built dynamically from ``PRAGMA table_info`` so
    that columns added by later migrations are never lost.
    """
    # Read full PRAGMA table_info BEFORE dropping the table.
    col_rows = cur.execute("PRAGMA table_info(users)").fetchall()
    cols = [r[1] for r in col_rows]
    col_list = ", ".join(cols)

    # Preserve data in a temporary table.
    cur.execute(f"""
        CREATE TABLE users_migrate_protected_admin AS
        SELECT {col_list} FROM users
    """)
    cur.execute("DROP TABLE users")

    # Build column definitions dynamically from the original PRAGMA output.
    col_defs = []
    for row in col_rows:
        cid, name, ctype, notnull, default, pk = row
        parts = [f'"{name}"', ctype]
        if notnull:
            parts.append("NOT NULL")
        if default is not None:
            # PRAGMA table_info returns expression defaults (e.g. datetime('now'))
            # without parentheses.  SQLite requires DEFAULT (expr) for function
            # calls, but allows plain values like DEFAULT 'user' or DEFAULT 0.
            if "(" in str(default):
                parts.append(f"DEFAULT ({default})")
            else:
                parts.append(f"DEFAULT {default}")
        if name == "role":
            parts.append("CHECK(role IN ('user','editor','admin','protected_admin'))")
        if name == "username":
            parts.append("UNIQUE COLLATE NOCASE")
        if name == "userbot_mode_lock":
            parts.append("CHECK(userbot_mode_lock IN ('unlocked','force_enabled','force_disabled'))")
        if pk:
            parts.append("PRIMARY KEY")
        col_defs.append(" ".join(parts))

    # Normalize role values in the temp table BEFORE inserting into the new
    # table so the CHECK constraint does not reject them:
    # 'superadmin' and 'owner' both become 'protected_admin' at this stage.
    cur.execute("UPDATE users_migrate_protected_admin SET role='protected_admin' WHERE role IN ('superadmin','owner')")

    cur.execute(f"""
        CREATE TABLE users (
            {', '.join(col_defs)}
        )
    """)
    cur.execute(
        f"INSERT INTO users SELECT {col_list} FROM users_migrate_protected_admin"
    )
    cur.execute("DROP TABLE users_migrate_protected_admin")


def _migrate_role_protected_admin_to_owner(conn, cur):
    """Rename 'protected_admin' role to 'owner' and update the CHECK constraint.

    SQLite does not support ALTER TABLE … ALTER COLUMN, so we recreate the
    users table with an updated constraint while preserving all data.
    """
    col_rows = cur.execute("PRAGMA table_info(users)").fetchall()
    cols = [r[1] for r in col_rows]
    col_list = ", ".join(cols)
    cur.execute(f"""
        CREATE TABLE users_migrate_owner AS
        SELECT {col_list} FROM users
    """)
    cur.execute("DROP TABLE users")

    col_defs = []
    for row in col_rows:
        cid, name, ctype, notnull, default, pk = row
        parts = [f'"{name}"', ctype]
        if notnull:
            parts.append("NOT NULL")
        if default is not None:
            if "(" in str(default):
                parts.append(f"DEFAULT ({default})")
            else:
                parts.append(f"DEFAULT {default}")
        if name == "role":
            parts.append("CHECK(role IN ('user','editor','admin','owner'))")
        if name == "username":
            parts.append("UNIQUE COLLATE NOCASE")
        if name == "userbot_mode_lock":
            parts.append("CHECK(userbot_mode_lock IN ('unlocked','force_enabled','force_disabled'))")
        if pk:
            parts.append("PRIMARY KEY")
        col_defs.append(" ".join(parts))

    # Normalize role values in the temp table BEFORE inserting.
    cur.execute("UPDATE users_migrate_owner SET role='owner' WHERE role='protected_admin'")

    cur.execute(f"""
        CREATE TABLE users (
            {', '.join(col_defs)}
        )
    """)
    cur.execute(
        f"INSERT INTO users SELECT {col_list} FROM users_migrate_owner"
    )
    cur.execute("DROP TABLE users_migrate_owner")


def _migrate_user_badges_remove_unique_constraint(conn, cur):
    """Remove the UNIQUE(user_id, badge_type_id) constraint from user_badges.

    SQLite does not support DROP CONSTRAINT, so the table is recreated without
    it.  This allows badge_types.allow_multiple=1 badges to be awarded more
    than once to the same user.  All existing rows are preserved.
    """
    cur.execute("""
        CREATE TABLE user_badges_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            badge_type_id   INTEGER NOT NULL REFERENCES badge_types(id) ON DELETE CASCADE,
            earned_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            awarded_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
            revoked         INTEGER NOT NULL DEFAULT 0,
            revoked_at      TEXT,
            revoked_by      TEXT REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    cur.execute("""
        INSERT INTO user_badges_new
            SELECT id, user_id, badge_type_id, earned_at, awarded_by,
                   revoked, revoked_at, revoked_by
            FROM user_badges
    """)
    cur.execute("DROP TABLE user_badges")
    cur.execute("ALTER TABLE user_badges_new RENAME TO user_badges")


def _migrate_invite_codes_multi_use(conn, cur):
    """Update invite_codes to support multi-use and unlimited expiry."""
    # Check if max_uses already exists
    ic_cols = [r[1] for r in cur.execute("PRAGMA table_info(invite_codes)").fetchall()]
    if "max_uses" in ic_cols:
        return


    # 1. Create usage table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invite_code_usage (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            invite_code_id  INTEGER NOT NULL REFERENCES invite_codes(id) ON DELETE CASCADE,
            user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            used_at         TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # 2. Recreate invite_codes
    cur.execute("""
        CREATE TABLE invite_codes_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    NOT NULL UNIQUE,
            created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at  TEXT,
            max_uses    INTEGER NOT NULL DEFAULT 1,
            use_count   INTEGER NOT NULL DEFAULT 0,
            deleted     INTEGER NOT NULL DEFAULT 0,
            deleted_at  TEXT
        )
    """)

    # 3. Migrate data
    old_cols = {r[1] for r in cur.execute("PRAGMA table_info(invite_codes)").fetchall()}
    has_used_by = "used_by" in old_cols

    if has_used_by:
        cur.execute("""
            INSERT INTO invite_codes_new (
                id, code, created_by, created_at, expires_at, deleted, deleted_at,
                max_uses, use_count
            )
            SELECT id, code, created_by, created_at, expires_at, deleted, deleted_at,
                   1, CASE WHEN used_by IS NOT NULL THEN 1 ELSE 0 END
            FROM invite_codes
        """)
        # 4. Populate usage table
        cur.execute("""
            INSERT INTO invite_code_usage (invite_code_id, user_id, used_at)
            SELECT id, used_by, used_at FROM invite_codes WHERE used_by IS NOT NULL
        """)
    else:
        # Unexpected state, but let's be safe
        cur.execute("""
            INSERT INTO invite_codes_new (
                id, code, created_by, created_at, expires_at, deleted, deleted_at,
                max_uses, use_count
            )
            SELECT id, code, created_by, created_at, expires_at, deleted, deleted_at,
                   1, 0
            FROM invite_codes
        """)

    cur.execute("DROP TABLE invite_codes")
    cur.execute("ALTER TABLE invite_codes_new RENAME TO invite_codes")


def _recreate_table_int_fk_to_text(conn, cur, table, fk_cols):
    """Recreate *table* converting specified *fk_cols* from INTEGER to TEXT.

    Reads the original CREATE TABLE SQL from sqlite_master, replaces the
    column type, creates a new table, copies data with CAST for the FK
    columns, drops the old table, and renames the new one into place.

    Only acts when at least one FK column currently has INTEGER type.
    Preserves all constraints (CHECK, UNIQUE, REFERENCES, etc.)
    """
    existing_types = {
        r[1]: r[2] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if not any(
        col in existing_types and "INT" in existing_types[col].upper()
        for col in fk_cols
    ):
        return

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not row:
        return
    create_sql = row[0]

    new_sql = create_sql
    for col in fk_cols:
        new_sql = re.sub(
            rf'(?<!\w){re.escape(col)}\s+INTEGER\b',
            lambda m: m.group(0).replace("INTEGER", "TEXT", 1),
            new_sql,
        )

    if new_sql == create_sql:
        return

    new_name = f"{table}_fk_text"
    new_ddl = new_sql.replace(f"TABLE {table}", f"TABLE {new_name}", 1)
    new_ddl = new_ddl.replace(
        f"TABLE IF NOT EXISTS {table}", f"TABLE IF NOT EXISTS {new_name}", 1
    )
    cur.execute(new_ddl)

    all_cols = list(existing_types.keys())
    select_exprs = [
        f"CAST({c} AS TEXT)"
        if c in fk_cols and "INT" in existing_types[c].upper()
        else c
        for c in all_cols
    ]

    cur.execute(
        f"INSERT INTO {new_name} ({', '.join(all_cols)}) "
        f"SELECT {', '.join(select_exprs)} FROM {table}"
    )
    cur.execute(f"DROP TABLE {table}")
    cur.execute(f"ALTER TABLE {new_name} RENAME TO {table}")


def _migrate_user_id_to_text(conn, cur):
    """Migrate users.id from INTEGER AUTOINCREMENT to TEXT.

    Existing integer IDs are kept as their string representations (e.g. 1 → '1').
    All foreign-key columns that reference users(id) are likewise changed to TEXT.
    """
    # The migration runner disables foreign keys before its write transaction.

    # users ----
    cur.execute("""
        CREATE TABLE users_new (
            id              TEXT    PRIMARY KEY,
            username        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            password        TEXT    NOT NULL,
            role            TEXT    NOT NULL DEFAULT 'user'
                                    CHECK(role IN ('user','editor','admin','protected_admin')),
            suspended       INTEGER NOT NULL DEFAULT 0,
            userbot_enabled INTEGER NOT NULL DEFAULT 0,
            userbot_mode_lock TEXT NOT NULL DEFAULT 'unlocked'
                                CHECK(userbot_mode_lock IN ('unlocked','force_enabled','force_disabled')),
            userbot_enable_count INTEGER NOT NULL DEFAULT 0,
            userbot_disable_count INTEGER NOT NULL DEFAULT 0,
            reserved_pages_quota INTEGER,
            invite_code     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            last_login_at   TEXT,
            is_superuser    INTEGER NOT NULL DEFAULT 0,
            session_token   TEXT,
            accessibility   TEXT,
            chat_disabled   INTEGER NOT NULL DEFAULT 0,
            suspended_until TEXT,
            suspend_reason  TEXT,
            suspend_reason_visible INTEGER NOT NULL DEFAULT 0,
            suspend_time_visible   INTEGER NOT NULL DEFAULT 0
        )
    """)
    old_user_cols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    cur.execute(f"""
        INSERT INTO users_new
        SELECT CAST(id AS TEXT), username, password, role, suspended,
               {"COALESCE(userbot_enabled, 0)" if "userbot_enabled" in old_user_cols else "0"},
               {"COALESCE(userbot_mode_lock, 'unlocked')" if "userbot_mode_lock" in old_user_cols else "'unlocked'"},
               {"COALESCE(userbot_enable_count, 0)" if "userbot_enable_count" in old_user_cols else "0"},
               {"COALESCE(userbot_disable_count, 0)" if "userbot_disable_count" in old_user_cols else "0"},
               {"reserved_pages_quota" if "reserved_pages_quota" in old_user_cols else "NULL"},
               invite_code, created_at, last_login_at,
               COALESCE(is_superuser, 0),
               {"session_token" if "session_token" in old_user_cols else "NULL"},
               {"accessibility" if "accessibility" in old_user_cols else "NULL"},
               {"COALESCE(chat_disabled, 0)" if "chat_disabled" in old_user_cols else "0"},
               {"suspended_until" if "suspended_until" in old_user_cols else "NULL"},
               {"suspend_reason" if "suspend_reason" in old_user_cols else "NULL"},
               {"COALESCE(suspend_reason_visible, 0)" if "suspend_reason_visible" in old_user_cols else "0"},
               {"COALESCE(suspend_time_visible, 0)" if "suspend_time_visible" in old_user_cols else "0"}
        FROM users
    """)
    cur.execute("DROP TABLE users")
    cur.execute("ALTER TABLE users_new RENAME TO users")

    # invite_codes ----
    cur.execute("""
        CREATE TABLE invite_codes_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    NOT NULL UNIQUE,
            created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at  TEXT    NOT NULL,
            used_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
            used_at     TEXT,
            deleted     INTEGER NOT NULL DEFAULT 0,
            deleted_at  TEXT
        )
    """)
    cur.execute("""
        INSERT INTO invite_codes_new
        SELECT id, code, CAST(created_by AS TEXT), created_at, expires_at,
               CAST(used_by AS TEXT), used_at, deleted, deleted_at
        FROM invite_codes
    """)
    cur.execute("DROP TABLE invite_codes")
    cur.execute("ALTER TABLE invite_codes_new RENAME TO invite_codes")

    # announcements ----
    # Check which columns exist in the old table so we can preserve them
    ann_old_cols = {r[1] for r in cur.execute("PRAGMA table_info(announcements)").fetchall()}
    cur.execute("""
        CREATE TABLE announcements_new (
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
        )
    """)
    nr_expr = "not_removable" if "not_removable" in ann_old_cols else "1"
    sc_expr = "show_countdown" if "show_countdown" in ann_old_cols else "1"
    am_expr = "audience_mode" if "audience_mode" in ann_old_cols else "'all'"
    cb_expr = "custom_background" if "custom_background" in ann_old_cols else "NULL"
    ct_expr = "custom_text_color" if "custom_text_color" in ann_old_cols else "NULL"
    rev_expr = "revision" if "revision" in ann_old_cols else "1"
    cur.execute(f"""
        INSERT INTO announcements_new
        SELECT id, content, color, text_size, visibility, expires_at, is_active,
               {nr_expr}, {sc_expr}, {am_expr}, {cb_expr}, {ct_expr}, {rev_expr},
               CAST(created_by AS TEXT), created_at
        FROM announcements
    """)
    cur.execute("DROP TABLE announcements")
    cur.execute("ALTER TABLE announcements_new RENAME TO announcements")

    # pages (last_edited_by: INT → TEXT) ----
    old_page_cols = {r[1] for r in cur.execute("PRAGMA table_info(pages)").fetchall()}
    cur.execute("""
        CREATE TABLE pages_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT    NOT NULL,
            slug            TEXT    NOT NULL UNIQUE,
            content         TEXT    NOT NULL DEFAULT '',
            category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            is_home         INTEGER NOT NULL DEFAULT 0,
            sort_order      INTEGER NOT NULL DEFAULT 0,
            protected_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
            protected_at    TEXT,
            protection_unlock_requested_at TEXT,
            protection_unlock_requested_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            last_edited_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
            last_edited_at  TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            difficulty_tag      TEXT NOT NULL DEFAULT '',
            tag_custom_label    TEXT NOT NULL DEFAULT '',
            tag_custom_color    TEXT NOT NULL DEFAULT '',
            is_deindexed        INTEGER NOT NULL DEFAULT 0,
            pending_deletion    INTEGER NOT NULL DEFAULT 0,
            pending_deletion_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            pending_deletion_at TEXT
        )
    """)
    cur.execute(f"""
        INSERT INTO pages_new
        SELECT id, title, slug, content, category_id, is_home, sort_order,
               {"CAST(protected_by AS TEXT)" if "protected_by" in old_page_cols else "NULL"},
               {"protected_at" if "protected_at" in old_page_cols else "NULL"},
               {"protection_unlock_requested_at" if "protection_unlock_requested_at" in old_page_cols else "NULL"},
               {"CAST(protection_unlock_requested_by AS TEXT)" if "protection_unlock_requested_by" in old_page_cols else "NULL"},
               CAST(last_edited_by AS TEXT), last_edited_at, created_at,
               {"COALESCE(difficulty_tag, '')" if "difficulty_tag" in old_page_cols else "''"},
               {"COALESCE(tag_custom_label, '')" if "tag_custom_label" in old_page_cols else "''"},
               {"COALESCE(tag_custom_color, '')" if "tag_custom_color" in old_page_cols else "''"},
               {"COALESCE(is_deindexed, 0)" if "is_deindexed" in old_page_cols else "0"},
               {"COALESCE(pending_deletion, 0)" if "pending_deletion" in old_page_cols else "0"},
               {"CAST(pending_deletion_by AS TEXT)" if "pending_deletion_by" in old_page_cols else "NULL"},
               {"pending_deletion_at" if "pending_deletion_at" in old_page_cols else "NULL"}
        FROM pages
    """)

    # page_history (edited_by: INT → TEXT) ----
    old_hist_cols = {r[1] for r in cur.execute("PRAGMA table_info(page_history)").fetchall()}
    cur.execute("""
        CREATE TABLE page_history_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id      INTEGER NOT NULL REFERENCES pages_new(id) ON DELETE CASCADE,
            title        TEXT    NOT NULL,
            content      TEXT    NOT NULL,
            edited_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
            edit_message TEXT    NOT NULL DEFAULT '',
            is_revert    INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cur.execute(f"""
        INSERT INTO page_history_new
        SELECT id, page_id, title, content,
               CAST(edited_by AS TEXT), edit_message,
               {"COALESCE(is_revert, 0)" if "is_revert" in old_hist_cols else "0"},
               created_at
        FROM page_history
    """)

    # drafts (user_id: INT → TEXT) ----
    cur.execute("""
        CREATE TABLE drafts_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id     INTEGER NOT NULL REFERENCES pages_new(id) ON DELETE CASCADE,
            user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title       TEXT    NOT NULL DEFAULT '',
            content     TEXT    NOT NULL DEFAULT '',
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(page_id, user_id)
        )
    """)
    cur.execute("""
        INSERT INTO drafts_new
        SELECT id, page_id, CAST(user_id AS TEXT), title, content, updated_at
        FROM drafts
    """)

    # Drop old tables (FK off so order doesn't matter) and rename
    cur.execute("DROP TABLE drafts")
    cur.execute("DROP TABLE page_history")
    cur.execute("DROP TABLE pages")
    cur.execute("ALTER TABLE pages_new RENAME TO pages")
    cur.execute("ALTER TABLE page_history_new RENAME TO page_history")
    cur.execute("ALTER TABLE drafts_new RENAME TO drafts")


def _migrate_plugin_fk_columns_to_text(conn, cur):
    """Convert ALL FK columns referencing ``users(id)`` from INTEGER to TEXT.

    This is the catch-all migration that ensures every table whose FK
    columns were created with INTEGER type (matching the old INTEGER
    ``users.id``) is converted to TEXT after ``_migrate_user_id_to_text``
    has changed ``users.id`` to TEXT.

    The function cannot live inside ``_migrate_user_id_to_text`` because
    that function only runs when ``users.id`` itself is still INTEGER;
    after a first (even partial) run, ``users.id`` becomes TEXT and the
    migration is skipped, leaving all FK columns stuck as INTEGER.

    This function dynamically discovers every table that has FK columns
    referencing ``users(id)`` and converts any that are still INTEGER.
    It is fully idempotent: ``_recreate_table_int_fk_to_text`` checks
    whether any FK column is still INTEGER before acting, so this can
    be safely repeated when importing a legacy database.

    ``PRAGMA foreign_keys`` must be toggled off before the DROP TABLE /
    ALTER TABLE cycle because some of these tables are referenced by FK
    constraints in other tables being recreated in the same batch (e.g.
    ``canvas__permissions → canvas__layouts``).  Without the PRAGMA,
    SQLite may block the DROP or cascade unexpectedly depending on
    connection state.
    """
    # Tables whose FK columns were already handled by the explicit
    # _migrate_user_id_to_text (which recreates them with TEXT columns
    # directly), still safe to pass through _recreate_table_int_fk_to_text
    # because it will see they are already TEXT and skip.
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        table = row["name"]
        if table.startswith("sqlite_"):
            continue
        fk_rows = conn.execute(
            f"PRAGMA foreign_key_list({table})"
        ).fetchall()
        user_fk_cols = [
            r["from"] for r in fk_rows
            if r["table"] == "users"
        ]
        if user_fk_cols:
            _recreate_table_int_fk_to_text(conn, cur, table, user_fk_cols)


def _migrate_quota_request_add_cancelled(conn, cur):
    """Expand the status CHECK constraint on reservation_quota_requests to
    include 'cancelled'.

    SQLite does not support ALTER TABLE … ALTER COLUMN, so the table is
    recreated with the updated constraint while preserving all data.
    """
    cur.execute("""
        CREATE TABLE reservation_quota_requests_new (
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
        )
    """)
    cur.execute("""
        INSERT INTO reservation_quota_requests_new
        SELECT id, user_id, requested_quota, reason, status,
               reviewed_by, '', reviewed_at, created_at
        FROM reservation_quota_requests
    """)
    cur.execute("DROP TABLE reservation_quota_requests")
    cur.execute(
        "ALTER TABLE reservation_quota_requests_new "
        "RENAME TO reservation_quota_requests"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reservation_quota_requests_pending "
        "ON reservation_quota_requests(user_id) WHERE status='pending'"
    )


def _migrate_custom_pages_add_video_and_embed(conn, cur):
    """Add video and enhanced iframe support to the custom_pages table.

    This migration:
    1. Adds new columns (iframe_height, iframe_sandbox, iframe_allow,
       video_url, video_autoplay, video_controls, video_loop, video_muted)
       to existing databases that predate these columns.
    2. Recreates the custom_pages table with an updated CHECK constraint that
       includes the new content types (youtube_video, video_hosted, page_embed)
       if not already present.

    SQLite does not support ALTER TABLE … ALTER COLUMN, so step 2 uses the
    standard rename-copy-drop-rename migration approach.
    """
    # Step 1: add missing columns via ALTER TABLE (safe, idempotent)
    cp_cols = {r[1] for r in cur.execute("PRAGMA table_info(custom_pages)").fetchall()}
    if "iframe_height" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN iframe_height TEXT NOT NULL DEFAULT '600px'")
    if "iframe_sandbox" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN iframe_sandbox TEXT NOT NULL DEFAULT ''")
    if "iframe_allow" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN iframe_allow TEXT NOT NULL DEFAULT ''")
    if "video_url" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN video_url TEXT NOT NULL DEFAULT ''")
    if "video_autoplay" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN video_autoplay INTEGER NOT NULL DEFAULT 0")
    if "video_controls" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN video_controls INTEGER NOT NULL DEFAULT 1")
    if "video_loop" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN video_loop INTEGER NOT NULL DEFAULT 0")
    if "video_muted" not in cp_cols:
        cur.execute("ALTER TABLE custom_pages ADD COLUMN video_muted INTEGER NOT NULL DEFAULT 0")

    # Step 2: expand CHECK constraint to include new types (only if needed)
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='custom_pages'"
    ).fetchone()
    if schema_row and "youtube_video" in schema_row[0]:
        # Already migrated
        return

    cur.execute("""
        CREATE TABLE custom_pages_new (
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
        )
    """)
    cur.execute("""
        INSERT INTO custom_pages_new (
            id, path, title, content_type, content, css, js,
            redirect_url, redirect_code, links_json, code_language,
            iframe_url, iframe_height, iframe_sandbox, iframe_allow,
            video_url, video_autoplay, video_controls, video_loop, video_muted,
            meta_description, is_published, created_by, created_at, updated_at
        )
        SELECT
            id, path, title, content_type, content, css, js,
            redirect_url, redirect_code, links_json, code_language,
            iframe_url,
            COALESCE(iframe_height, '600px'),
            COALESCE(iframe_sandbox, ''),
            COALESCE(iframe_allow, ''),
            COALESCE(video_url, ''),
            COALESCE(video_autoplay, 0),
            COALESCE(video_controls, 1),
            COALESCE(video_loop, 0),
            COALESCE(video_muted, 0),
            meta_description, is_published, created_by, created_at, updated_at
        FROM custom_pages
    """)
    cur.execute("DROP TABLE custom_pages")
    cur.execute("ALTER TABLE custom_pages_new RENAME TO custom_pages")


def _migrate_banana_chat_to_banana_ai(conn, cur):
    """Rename the experimental ``banana_chat`` plugin to ``banana_ai``.

    The plugin shipped under the id ``banana_chat``, which read as the
    unrelated *Chat* plugin (private one-on-one direct messaging), so it was
    given its own id.

    On disk the plugin moved from ``plugins/builtin/banana_chat/`` to
    ``plugins/builtin/banana_ai/`` and its SQLite table prefix changed from
    ``banana_chat__`` to ``banana_ai__``.  This migration brings existing
    installs in line:

    * If a ``plugins`` row exists for ``banana_chat`` (without a conflicting
      ``banana_ai`` row), rename it in place and update the display name.
    * If both rows somehow exist, drop the legacy ``banana_chat`` row so the
      newer ``banana_ai`` registration wins.
    * Rename any ``banana_chat__*`` tables to ``banana_ai__*`` so the
      conversations / credentials / messages a user already saved are
      preserved.
    """
    cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    ss_cols = [r[1] for r in cur.execute("PRAGMA table_info(site_settings)").fetchall()]
    # plugins registry row
    plugins_table = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plugins'"
    ).fetchone()
    if plugins_table:
        has_old = cur.execute(
            "SELECT 1 FROM plugins WHERE id='banana_chat'"
        ).fetchone()
        has_new = cur.execute(
            "SELECT 1 FROM plugins WHERE id='banana_ai'"
        ).fetchone()
        if has_old and not has_new:
            cur.execute(
                "UPDATE plugins SET id='banana_ai', name='AI Chat' "
                "WHERE id='banana_chat'"
            )
        elif has_old and has_new:
            cur.execute("DELETE FROM plugins WHERE id='banana_chat'")

    # banana_chat__* → banana_ai__* table renames
    legacy_tables = [
        row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'banana_chat__%'"
        ).fetchall()
    ]
    for legacy_name in legacy_tables:
        new_name = "banana_ai__" + legacy_name[len("banana_chat__"):]
        existing_new = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (new_name,),
        ).fetchone()
        if existing_new:
            cur.execute(f"DROP TABLE {legacy_name}")
        else:
            cur.execute(f"ALTER TABLE {legacy_name} RENAME TO {new_name}")

    # Feedback plugin tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback__reports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT REFERENCES users(id) ON DELETE SET NULL,
            username        TEXT,
            what_happened   TEXT NOT NULL,
            can_contact     INTEGER NOT NULL DEFAULT 1,
            contact_info    TEXT,
            telemetry       TEXT,
            page_slug       TEXT,
            page_title      TEXT,
            ip_address      TEXT,
            submitted_at    TEXT NOT NULL DEFAULT (datetime('now')),
            telegram_msg_id INTEGER,
            telegram_sent   INTEGER NOT NULL DEFAULT 0,
            telegram_error  TEXT,
            archived        INTEGER NOT NULL DEFAULT 0,
            archived_at     TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback__bans (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT REFERENCES users(id) ON DELETE CASCADE,
            username        TEXT NOT NULL,
            reason          TEXT,
            banned_by       TEXT REFERENCES users(id) ON DELETE SET NULL,
            ban_type        TEXT NOT NULL DEFAULT 'temporary' CHECK(ban_type IN ('temporary', 'permanent')),
            expires_at      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            lifted_at       TEXT,
            lifted_by       TEXT REFERENCES users(id) ON DELETE SET NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback__telemetry_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT,
            action          TEXT NOT NULL,
            page_slug       TEXT,
            ip_address      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Sidebar apps ordering
    if "sidebar_apps_order" not in ss_cols:
        cur.execute(
            "ALTER TABLE site_settings ADD COLUMN sidebar_apps_order "
            "TEXT NOT NULL DEFAULT ''"
        )

    # Account approval system (admin must approve new signups)
    if "approval_status" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN approval_status "
            "TEXT NOT NULL DEFAULT 'approved' "
            "CHECK(approval_status IN ('approved','pending','denied'))"
        )
    if "denied_at" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN denied_at TEXT"
        )
    if "denied_notified" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN denied_notified "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "approved_by" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN approved_by TEXT "
            "REFERENCES users(id) ON DELETE SET NULL"
        )
    if "denied_by" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN denied_by TEXT "
            "REFERENCES users(id) ON DELETE SET NULL"
        )
    if "approved_denied_at" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN approved_denied_at TEXT"
        )

    # Account approval site settings
    if "approval_required" not in ss_cols:
        cur.execute(
            "ALTER TABLE site_settings ADD COLUMN approval_required "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "approval_denied_timeout_hours" not in ss_cols:
        cur.execute(
            "ALTER TABLE site_settings ADD COLUMN approval_denied_timeout_hours "
            "INTEGER NOT NULL DEFAULT 24"
        )

    # Quota request cooldown rate-limit column on users
    if "quota_request_cooldown_until" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN quota_request_cooldown_until TEXT"
        )

    # Quota request cooldown setting (hours, 0 = disabled)
    if "quota_request_cooldown_hours" not in ss_cols:
        cur.execute(
            "ALTER TABLE site_settings ADD COLUMN quota_request_cooldown_hours "
            "INTEGER NOT NULL DEFAULT 0"
        )

    # Pending activation auto-deletion timeout (hours, 0 = disabled)
    if "approval_pending_timeout_hours" not in ss_cols:
        cur.execute(
            "ALTER TABLE site_settings ADD COLUMN approval_pending_timeout_hours "
            "INTEGER NOT NULL DEFAULT 0"
        )

    # Devtools: per-wiki toggle (only enabled by hosting platform admin)
    if "devtools_enabled" not in ss_cols:
        cur.execute(
            "ALTER TABLE site_settings ADD COLUMN devtools_enabled "
            "INTEGER NOT NULL DEFAULT 0"
        )

    # Login app selector: post-login page asking users which app to enter
    if "login_app_selector" not in ss_cols:
        cur.execute(
            "ALTER TABLE site_settings ADD COLUMN login_app_selector "
            "INTEGER NOT NULL DEFAULT 0"
        )
