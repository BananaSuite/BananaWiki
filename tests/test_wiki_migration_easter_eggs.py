"""Schema version 21: easter eggs administrators design, who found them, and the 1.4 eggs imported."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from bananawiki.core import eggs
from bananawiki.core.sqlite import Database
from bananawiki.wiki import migrations

FIXTURE = Path(__file__).parent / "fixtures" / "v1_instance" / "bananawiki-v3.sqlite3"
FINDS = ("CREATE TABLE easter_egg_finds (user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
         "egg_id TEXT NOT NULL, found_at TEXT NOT NULL, PRIMARY KEY (user_id, egg_id))")
# The columns BananaWiki 1.4 added to site_settings and users for its easter egg (db/_schema.py there).
LEGACY_DDL = """
    ALTER TABLE users ADD COLUMN easter_egg_found INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE site_settings ADD COLUMN easter_egg_emoji TEXT NOT NULL DEFAULT '🍌';
    ALTER TABLE site_settings ADD COLUMN easter_egg_count INTEGER NOT NULL DEFAULT 30;
    ALTER TABLE site_settings ADD COLUMN easter_egg_speed_min REAL NOT NULL DEFAULT 1.5;
    ALTER TABLE site_settings ADD COLUMN easter_egg_speed_max REAL NOT NULL DEFAULT 3.5;
    ALTER TABLE site_settings ADD COLUMN easter_egg_delay INTEGER NOT NULL DEFAULT 80;
    ALTER TABLE site_settings ADD COLUMN easter_egg_unlock_delay INTEGER NOT NULL DEFAULT 2500;
    ALTER TABLE site_settings ADD COLUMN easter_egg_keys TEXT NOT NULL DEFAULT '38,38,40,40,37,39,37,39,66,65';
    ALTER TABLE site_settings ADD COLUMN easter_eggs TEXT;
"""
PIZZA = [{"keys": "80,73,90,90,65", "emoji": "🍕", "count": 40, "speed_min": 1, "speed_max": 2, "delay": 50,
          "unlock_delay": 3000}, {"keys": "38,38", "emoji": "🎉"}]


def _database(path: Path) -> Database:
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=migrations.MIGRATIONS,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def _version_3(path: Path, *, legacy: bool = True, **settings) -> None:
    """A 1.4 database (version 3) with its easter egg columns, two accounts, alice having found the egg."""
    conn = sqlite3.connect(path)
    conn.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    if legacy:
        conn.executescript(LEGACY_DDL)
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    for column, value in settings.items():
        conn.execute(f"UPDATE site_settings SET {column} = ?", (value,))
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u1', 'alice', 'x', 'owner')")
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u2', 'bob', 'x', 'user')")
    if legacy:
        conn.execute("UPDATE users SET easter_egg_found = 1 WHERE id = 'u1'")
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()


def _upgrade(path: Path) -> sqlite3.Connection:
    database = _database(path)
    assert database.initialize() == migrations.LATEST - 3
    return sqlite3.connect(path)


def _stored(conn) -> dict | None:
    raw = conn.execute("SELECT easter_egg_configs FROM site_settings").fetchone()[0]
    return None if raw is None else json.loads(raw)


def _finds(conn) -> list[tuple[str, str]]:
    return conn.execute("SELECT user_id, egg_id FROM easter_egg_finds ORDER BY user_id").fetchall()


def test_a_new_wiki_has_the_table_and_no_list_yet(tmp_path):
    database = _database(tmp_path / "fresh.db")
    database.initialize()
    conn = sqlite3.connect(tmp_path / "fresh.db")
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name = 'easter_egg_finds'").fetchone()[0] == FINDS
    column = [row for row in conn.execute("PRAGMA table_info(site_settings)") if row[1] == "easter_egg_configs"]
    assert [(row[2], row[3], row[4]) for row in column] == [("TEXT", 0, None)]
    assert _stored(conn) is None and _finds(conn) == []


def test_a_1_4_list_of_eggs_is_imported_and_kept(tmp_path):
    path = tmp_path / "old.db"
    _version_3(path, easter_eggs=json.dumps(PIZZA))
    conn = _upgrade(path)
    stored = _stored(conn)
    assert stored == eggs.from_legacy(json.dumps(PIZZA))
    assert [egg["id"] for egg in stored["eggs"]] == ["legacy-1", "legacy-2"]
    assert stored["eggs"][0]["keys"] == ["p", "i", "z", "z", "a"] and stored["eggs"][0]["unlock"]["action"] == "page"
    assert _finds(conn) == [("u1", "legacy-1")]
    # The 1.4 value stays where it was.
    assert json.loads(conn.execute("SELECT easter_eggs FROM site_settings").fetchone()[0]) == PIZZA


def test_the_older_1_4_columns_are_imported(tmp_path):
    path = tmp_path / "old.db"
    _version_3(path, easter_egg_emoji="🍎", easter_egg_count=12, easter_egg_keys="65,66")
    conn = _upgrade(path)
    [egg] = _stored(conn)["eggs"]
    assert egg["id"] == "legacy-1" and egg["keys"] == ["a", "b"] and egg["count"] == 12
    assert egg["sprite"] == {"type": "emoji", "value": "🍎"}
    assert _finds(conn) == [("u1", "legacy-1")]


def test_an_empty_1_4_list_meant_no_eggs(tmp_path):
    path = tmp_path / "old.db"
    _version_3(path, easter_eggs="[]")
    conn = _upgrade(path)
    assert _stored(conn) == {"version": 1, "eggs": []}
    assert _finds(conn) == [("u1", "konami")]


def test_eggs_of_a_switched_off_1_4_plugin_stay_off(tmp_path):
    path = tmp_path / "old.db"
    _version_3(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO plugins (id, name, version, builtin, enabled) VALUES ('easter_egg', 'Easter egg', '1', 1, 0)")
    conn.commit()
    conn.close()
    conn = _upgrade(path)
    assert [egg["enabled"] for egg in _stored(conn)["eggs"]] == [False]


def test_without_1_4_eggs_nothing_is_imported(tmp_path):
    path = tmp_path / "old.db"
    _version_3(path, legacy=False)
    conn = _upgrade(path)
    assert _stored(conn) is None and _finds(conn) == []


def test_the_import_runs_once_and_running_the_version_again_changes_nothing(tmp_path):
    path = tmp_path / "old.db"
    _version_3(path, easter_eggs=json.dumps(PIZZA))
    conn = _upgrade(path)
    conn.execute("UPDATE site_settings SET easter_egg_configs = ?", (eggs.dumps(eggs.default_config()),))
    conn.execute("DELETE FROM easter_egg_finds")
    conn.commit()
    schema = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    migrations.v21_easter_egg_configs.upgrade(conn)
    assert _stored(conn) == eggs.default_config(), "an administrator's list is never replaced"
    assert conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema


@pytest.mark.parametrize("stored", ["[]", json.dumps(PIZZA)])
def test_the_real_1_4_fixture_upgrades_with_its_eggs(tmp_path, stored):
    path = tmp_path / "v1.sqlite3"
    shutil.copyfile(FIXTURE, path)  # never the tracked file itself
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_DDL)
    conn.execute("UPDATE site_settings SET easter_eggs = ?", (stored,))
    conn.execute("UPDATE users SET easter_egg_found = 1 WHERE username = 'owner'")
    conn.commit()
    conn.close()
    conn = _upgrade(path)
    expected = eggs.from_legacy(stored) or {"version": 1, "eggs": []}
    assert _stored(conn) == expected
    owner = conn.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0]
    assert _finds(conn) == [(owner, "legacy-1" if expected["eggs"] else "konami")]
