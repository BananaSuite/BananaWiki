"""Wiki schema versions 8 to 11 are frozen in their migration modules.

A migration that called a feature's schema function would change meaning
whenever that function changed: databases upgraded before and after the edit
would end up with different schemas. Versions 8-11 therefore carry their own
DDL, and the schema they produce is pinned here (the text SQLite keeps for
each table and index they create, and the definition of each column they add),
all of it: anything else they add fails the test too.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from bananawiki.core.sqlite import Database
from bananawiki.wiki import migrations

FROZEN = ("v8_tokens_and_chat_safety", "v9_two_step", "v10_chat_blocks", "v11_kanban_done_columns")

CREATED = {
    "chat__reports": (
        "CREATE TABLE chat__reports (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
        "parent_id INTEGER NOT NULL, message_id INTEGER NOT NULL, "
        "reporter_id TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "reported_user_id TEXT REFERENCES users(id) ON DELETE SET NULL, content TEXT NOT NULL DEFAULT '', "
        "attachment_names TEXT NOT NULL DEFAULT '', message_created_at TEXT, "
        "status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, "
        "resolved_by TEXT REFERENCES users(id) ON DELETE SET NULL, resolved_at TEXT, "
        "UNIQUE (kind, message_id, reporter_id))"
    ),
    "idx_chat_reports_status": "CREATE INDEX idx_chat_reports_status ON chat__reports(status, id)",
    "idx_api_service_tokens_parent": "CREATE INDEX idx_api_service_tokens_parent ON api_service__tokens(parent_id)",
    "auth__two_step": (
        "CREATE TABLE auth__two_step (user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
        "secret_encrypted TEXT NOT NULL, recovery_hashes TEXT NOT NULL DEFAULT '[]', "
        "last_counter INTEGER NOT NULL DEFAULT -1, enabled_at TEXT NOT NULL)"
    ),
    "chat__blocks": (
        "CREATE TABLE chat__blocks (blocker_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "blocked_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, "
        "PRIMARY KEY (blocker_id, blocked_id), CHECK (blocker_id != blocked_id))"
    ),
    "idx_chat_blocks_blocked": "CREATE INDEX idx_chat_blocks_blocked ON chat__blocks(blocked_id)",
}
# (table, column) -> (type, not null, default)
ADDED = {
    ("api_service__tokens", "parent_id"): ("INTEGER", 0, None),
    ("chats", "cleared_upto_user1"): ("INTEGER", 1, "0"),
    ("chats", "cleared_upto_user2"): ("INTEGER", 1, "0"),
    ("site_settings", "two_step_required_for_admins"): ("INTEGER", 1, "0"),
    ("users", "device_epoch"): ("TEXT", 0, None),
    ("users", "device_epoch_stamp"): ("TEXT", 0, None),
    ("chat_messages", "withheld"): ("INTEGER", 1, "0"),
    ("kanban_columns", "is_done"): ("INTEGER", 1, "0"),
}
FROZEN_UPTO = 11


def _database(path: Path, upto: int | None = None) -> Database:
    chain = migrations.MIGRATIONS if upto is None else migrations.MIGRATIONS[: upto - migrations.BASELINE]
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=chain,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def _version_3(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u1', 'alice', 'x', 'owner')")
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u2', 'bob', 'x', 'user')")
    conn.execute("INSERT INTO chats (user1_id, user2_id) VALUES ('u1', 'u2')")
    conn.execute("INSERT INTO chat_messages (chat_id, sender_id, content) VALUES (1, 'u2', 'hello')")
    conn.execute("INSERT INTO kanban_boards (title, created_by) VALUES ('Plans', 'u1')")
    conn.execute("INSERT INTO kanban_columns (board_id, title) VALUES (1, 'Done')")
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()


def _schema(db: Database) -> dict[str, str | None]:
    conn = db.connect()
    return {row["name"]: row["sql"] for row in conn.execute("SELECT name, sql FROM sqlite_master")}


def _column(db: Database, table: str, column: str) -> tuple[str, int, str | None]:
    rows = {row["name"]: row for row in db.connect().execute(f'PRAGMA table_info("{table}")')}
    row = rows[column]
    return row["type"], row["notnull"], row["dflt_value"]


def _objects_and_columns(db: Database) -> tuple[set[str], set[tuple[str, str]]]:
    """Named tables and indexes (SQLite's own left out), and every (table, column)."""
    conn = db.connect()
    names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite%'")}
    tables = [row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite%'")]
    columns = {(table, row["name"]) for table in tables for row in conn.execute(f'PRAGMA table_info("{table}")')}
    return names, columns


@pytest.mark.parametrize("name", FROZEN)
def test_frozen_migrations_do_not_call_feature_code(name):
    source = Path(migrations.__file__).with_name(f"{name}.py").read_text(encoding="utf-8")
    imported = [node.module or "" for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    imported += [alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Import)
                 for alias in node.names]
    assert not [module for module in imported if "features" in module.split(".")], imported


def test_versions_8_to_11_create_the_pinned_schema(tmp_path):
    fresh = _database(tmp_path / "fresh.db")
    fresh.initialize()
    schema = _schema(fresh)
    for name, sql in CREATED.items():
        assert schema.get(name) == sql, name
    for (table, column), definition in ADDED.items():
        assert _column(fresh, table, column) == definition, (table, column)


def test_versions_8_to_11_add_nothing_but_the_pinned_schema(tmp_path):
    _version_3(tmp_path / "old.db")
    _database(tmp_path / "old.db", upto=7).initialize()
    names, columns = _objects_and_columns(_database(tmp_path / "old.db", upto=7))
    upgraded = _database(tmp_path / "old.db", upto=FROZEN_UPTO)
    assert upgraded.initialize() == FROZEN_UPTO - 7
    later_names, later_columns = _objects_and_columns(upgraded)
    assert later_names - names == set(CREATED) and names <= later_names
    created_tables = {name for name in CREATED if CREATED[name].startswith("CREATE TABLE")}
    assert {(table, column) for table, column in later_columns - columns
            if table not in created_tables} == set(ADDED)
    assert columns <= later_columns
    # Exactly as pinned, whatever later versions do to these tables.
    schema = _schema(upgraded)
    assert {name: schema[name] for name in CREATED} == CREATED
    assert {key: _column(upgraded, *key) for key in ADDED} == ADDED


def test_upgrading_from_version_7_matches_a_fresh_database(tmp_path):
    _version_3(tmp_path / "old.db")
    assert _database(tmp_path / "old.db", upto=7).initialize() == 4
    seven = _schema(_database(tmp_path / "old.db", upto=7))
    assert not set(CREATED) & set(seven)
    old = _database(tmp_path / "old.db")
    assert old.initialize() == migrations.LATEST - 7
    fresh = _database(tmp_path / "fresh.db")
    fresh.initialize()
    assert _schema(old) == _schema(fresh)
    conn = old.connect()
    assert conn.execute("SELECT cleared_upto_user1, cleared_upto_user2 FROM chats").fetchone() == {
        "cleared_upto_user1": 0, "cleared_upto_user2": 0}
    assert conn.execute("SELECT two_step_required_for_admins FROM site_settings").fetchone() == {
        "two_step_required_for_admins": 0}
    assert conn.execute("SELECT content, withheld FROM chat_messages").fetchone() == {"content": "hello", "withheld": 0}
    assert conn.execute("SELECT title, is_done FROM kanban_columns").fetchone() == {"title": "Done", "is_done": 0}
    # Running a version again changes nothing.
    before = _schema(old)
    for upgrade in migrations.MIGRATIONS[4:]:
        upgrade(conn)
    assert _schema(old) == before
