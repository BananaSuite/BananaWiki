"""
Tests for DB schema migration correctness.

Verifies that _migrate_user_id_to_text() preserves all columns added by
ALTER TABLE migrations when converting users.id from INTEGER to TEXT.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate limit store before and after each test."""
    try:
        import app as app_mod
        with app_mod._RL_LOCK:
            app_mod._RL_STORE.clear()
    except (ImportError, AttributeError):
        pass
    yield
    try:
        import app as app_mod
        with app_mod._RL_LOCK:
            app_mod._RL_STORE.clear()
    except (ImportError, AttributeError):
        pass


def _create_legacy_db_with_integer_user_ids(db_path):
    """Create a legacy database with INTEGER user IDs and extra columns.

    Simulates a database that was created with the old schema and then had
    ALTER TABLE migrations applied to add the additional columns.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")

    # Create users table with INTEGER id (old schema) plus all extra columns
    conn.executescript("""
    CREATE TABLE users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        password        TEXT    NOT NULL,
        role            TEXT    NOT NULL DEFAULT 'user'
                                CHECK(role IN ('user','editor','admin')),
        suspended       INTEGER NOT NULL DEFAULT 0,
        reserved_pages_quota INTEGER,
        invite_code     TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        last_login_at   TEXT,
        easter_egg_found INTEGER NOT NULL DEFAULT 0,
        is_superuser    INTEGER NOT NULL DEFAULT 0,
        session_token   TEXT,
        accessibility   TEXT,
        chat_disabled   INTEGER NOT NULL DEFAULT 0,
        suspended_until TEXT,
        suspend_reason  TEXT,
        suspend_reason_visible INTEGER NOT NULL DEFAULT 0,
        suspend_time_visible   INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE categories (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        parent_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        sequential_nav INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE pages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT    NOT NULL,
        slug            TEXT    NOT NULL UNIQUE,
        content         TEXT    NOT NULL DEFAULT '',
        category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
        is_home         INTEGER NOT NULL DEFAULT 0,
        sort_order      INTEGER NOT NULL DEFAULT 0,
        last_edited_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        last_edited_at  TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        difficulty_tag      TEXT NOT NULL DEFAULT '',
        tag_custom_label    TEXT NOT NULL DEFAULT '',
        tag_custom_color    TEXT NOT NULL DEFAULT '',
        is_deindexed        INTEGER NOT NULL DEFAULT 0,
        pending_deletion    INTEGER NOT NULL DEFAULT 0,
        pending_deletion_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        pending_deletion_at TEXT
    );

    CREATE TABLE page_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        title       TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        edited_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
        edit_message TEXT   NOT NULL DEFAULT '',
        is_revert   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE invite_codes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT    NOT NULL UNIQUE,
        created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        expires_at  TEXT    NOT NULL,
        used_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
        used_at     TEXT,
        deleted     INTEGER NOT NULL DEFAULT 0,
        deleted_at  TEXT
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
        created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE drafts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title       TEXT    NOT NULL DEFAULT '',
        content     TEXT    NOT NULL DEFAULT '',
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(page_id, user_id)
    );

    CREATE TABLE site_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        site_name TEXT NOT NULL DEFAULT 'BananaWiki',
        primary_color TEXT NOT NULL DEFAULT '#8fa0d4',
        secondary_color TEXT NOT NULL DEFAULT '#1e1e2c',
        accent_color TEXT NOT NULL DEFAULT '#7e9ada',
        text_color TEXT NOT NULL DEFAULT '#c8ccd8',
        bg_color TEXT NOT NULL DEFAULT '#16161f',
        sidebar_color TEXT NOT NULL DEFAULT '#1a1a24',
        setup_done INTEGER NOT NULL DEFAULT 0,
        banana_mode INTEGER NOT NULL DEFAULT 0,
        timezone TEXT NOT NULL DEFAULT 'UTC',
        favicon_enabled INTEGER NOT NULL DEFAULT 0,
        favicon_type TEXT NOT NULL DEFAULT 'yellow',
        favicon_custom TEXT NOT NULL DEFAULT '',
        maintenance_mode INTEGER NOT NULL DEFAULT 0,
        maintenance_message TEXT NOT NULL DEFAULT '',
        session_limit_enabled INTEGER NOT NULL DEFAULT 1,
        page_reservations_enabled INTEGER NOT NULL DEFAULT 0,
        page_reservation_duration_hours INTEGER NOT NULL DEFAULT 48,
        page_reservation_cooldown_hours INTEGER NOT NULL DEFAULT 24,
        default_reserved_pages_quota INTEGER NOT NULL DEFAULT 5,
        light_primary_color TEXT NOT NULL DEFAULT '#4b63b6',
        light_secondary_color TEXT NOT NULL DEFAULT '#ffffff',
        light_accent_color TEXT NOT NULL DEFAULT '#3553c7',
        light_text_color TEXT NOT NULL DEFAULT '#202534',
        light_sidebar_color TEXT NOT NULL DEFAULT '#e9edf5',
        light_bg_color TEXT NOT NULL DEFAULT '#f6f7fb',
        default_theme_mode TEXT NOT NULL DEFAULT 'dark',
        kanban_access TEXT NOT NULL DEFAULT 'admin',
        kanban_write_access TEXT NOT NULL DEFAULT 'admin'
    );

    INSERT INTO site_settings (id) VALUES (1);
    """)

    # Insert test data with values in the extra columns
    conn.execute("""
        INSERT INTO users (id, username, password, role, session_token,
                           accessibility, chat_disabled, suspended_until,
                           suspend_reason, suspend_reason_visible,
                           suspend_time_visible, reserved_pages_quota)
        VALUES (1, 'testadmin', 'hashed_pw', 'admin', 'tok123',
                '{"font_size": 16}', 0, NULL,
                NULL, 0, 0, 10)
    """)
    conn.execute("""
        INSERT INTO users (id, username, password, role, session_token,
                           chat_disabled, suspend_reason, suspend_reason_visible)
        VALUES (2, 'testuser', 'hashed_pw2', 'user', 'tok456',
                1, 'spamming', 1)
    """)

    conn.execute("""
        INSERT INTO pages (id, title, slug, content, is_home, last_edited_by,
                           difficulty_tag, tag_custom_label, tag_custom_color,
                           is_deindexed, pending_deletion, pending_deletion_by)
        VALUES (1, 'Home', 'home', '# Welcome', 1, 1,
                'intermediate', 'Custom Tag', '#ff0000',
                0, 1, 2)
    """)

    conn.execute("""
        INSERT INTO page_history (id, page_id, title, content, edited_by,
                                  edit_message, is_revert)
        VALUES (1, 1, 'Home', '# Old content', 1, 'initial version', 0)
    """)
    conn.execute("""
        INSERT INTO page_history (id, page_id, title, content, edited_by,
                                  edit_message, is_revert)
        VALUES (2, 1, 'Home', '# Reverted', 1, 'reverted to v1', 1)
    """)

    conn.commit()
    conn.close()


class TestUserIdMigration:
    """Tests for the INTEGER→TEXT user ID migration."""

    def test_migration_preserves_user_columns(self, isolated_db):
        """Verify that all user columns survive the INT→TEXT migration."""
        _create_legacy_db_with_integer_user_ids(isolated_db)

        import db as db_mod
        db_mod.init_db()

        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row

        # Check user ID is now TEXT
        id_type = next(
            (r[2] for r in conn.execute("PRAGMA table_info(users)").fetchall()
             if r[1] == "id"),
            None,
        )
        assert id_type == "TEXT", f"Expected TEXT, got {id_type}"

        # Check all columns exist
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        expected = {
            "id", "username", "password", "role", "suspended",
            "reserved_pages_quota", "invite_code", "created_at",
            "last_login_at", "is_superuser",
            "session_token", "accessibility", "chat_disabled",
            "suspended_until", "suspend_reason",
            "suspend_reason_visible", "suspend_time_visible",
        }
        for col in expected:
            assert col in user_cols, f"Column {col!r} missing from users table after migration"

        # Verify data preserved
        admin = conn.execute("SELECT * FROM users WHERE username='testadmin'").fetchone()
        assert admin["id"] == "1"
        assert admin["session_token"] == "tok123"
        assert admin["accessibility"] == '{"font_size": 16}'
        assert admin["reserved_pages_quota"] == 10

        user = conn.execute("SELECT * FROM users WHERE username='testuser'").fetchone()
        assert user["id"] == "2"
        assert user["session_token"] == "tok456"
        assert user["chat_disabled"] == 1
        assert user["suspend_reason"] == "spamming"
        assert user["suspend_reason_visible"] == 1

        conn.close()

    def test_migration_preserves_page_columns(self, isolated_db):
        """Verify that all pages columns survive the INT→TEXT migration."""
        _create_legacy_db_with_integer_user_ids(isolated_db)

        import db as db_mod
        db_mod.init_db()

        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row

        page_cols = {r[1] for r in conn.execute("PRAGMA table_info(pages)").fetchall()}
        expected = {
            "difficulty_tag", "tag_custom_label", "tag_custom_color",
            "is_deindexed", "pending_deletion", "pending_deletion_by",
            "pending_deletion_at",
        }
        for col in expected:
            assert col in page_cols, f"Column {col!r} missing from pages table after migration"

        page = conn.execute("SELECT * FROM pages WHERE slug='home'").fetchone()
        assert page["difficulty_tag"] == "intermediate"
        assert page["tag_custom_label"] == "Custom Tag"
        assert page["tag_custom_color"] == "#ff0000"
        # The home page is normalized during migration so stale deletion state
        # from legacy databases cannot remove the wiki landing page.
        assert page["pending_deletion"] == 0
        assert page["pending_deletion_by"] is None
        assert page["last_edited_by"] == "1"  # converted to TEXT

        conn.close()

    def test_migration_preserves_page_history_is_revert(self, isolated_db):
        """Verify that is_revert column survives the INT→TEXT migration."""
        _create_legacy_db_with_integer_user_ids(isolated_db)

        import db as db_mod
        db_mod.init_db()

        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row

        hist_cols = {r[1] for r in conn.execute("PRAGMA table_info(page_history)").fetchall()}
        assert "is_revert" in hist_cols, "is_revert column missing from page_history after migration"

        rows = conn.execute(
            "SELECT * FROM page_history ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["is_revert"] == 0
        assert rows[0]["edited_by"] == "1"  # converted to TEXT
        assert rows[1]["is_revert"] == 1
        assert rows[1]["edit_message"] == "reverted to v1"

        conn.close()
