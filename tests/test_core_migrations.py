import sqlite3

import pytest

from bananawiki.core.sqlite import Database, DatabaseUnavailable
from bananawiki.wiki import migrations


def make_db(path):
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=migrations.MIGRATIONS,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def build_v3(path):
    """A database exactly as 1.4 left it (schema version 3)."""
    conn = sqlite3.connect(path)
    sql = (migrations._BASELINE_SQL).read_text()
    conn.executescript(sql)
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u1', 'alice', 'x', 'owner')")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO pages (title, slug, content, is_home, last_edited_by, last_edited_at) "
                 "VALUES ('Home', 'home', 'hello world', 1, -1, '2026-05-01T10:11:12.5+02:00')")
    conn.execute("INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at) "
                 "VALUES ('s', 'h', 'u1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
                 "'2099-01-01T00:00:00+00:00')")
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()


def test_fresh_database_reaches_latest(tmp_path):
    db = make_db(tmp_path / "wiki.db")
    assert db.initialize() == 1
    assert db.version() == (migrations.APPLICATION_ID, migrations.LATEST)
    assert db.initialize() == 0


def test_v3_database_is_taken_over(tmp_path):
    path = tmp_path / "wiki.db"
    build_v3(path)
    db = make_db(path)
    assert db.initialize() == len(migrations.MIGRATIONS)
    conn = db.connect()
    page = conn.execute("SELECT last_edited_by, last_edited_at, revision FROM pages").fetchone()
    assert page == {"last_edited_by": None, "last_edited_at": "2026-05-01 08:11:12", "revision": 0}
    assert conn.execute("SELECT expires_at FROM user_sessions").fetchone()["expires_at"] == "2099-01-01 00:00:00"
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pages_fts'").fetchone():
        assert conn.execute("SELECT COUNT(*) AS n FROM pages_fts WHERE pages_fts MATCH 'hello'").fetchone()["n"] == 1


def test_fresh_and_upgraded_schemas_match(tmp_path):
    fresh = make_db(tmp_path / "fresh.db")
    fresh.initialize()
    build_v3(tmp_path / "old.db")
    old = make_db(tmp_path / "old.db")
    old.initialize()

    def columns(db):
        conn = db.connect()
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'pages_fts%'")]
        return {t: sorted(r["name"] for r in conn.execute(f'PRAGMA table_info("{t}")')) for t in tables}

    assert columns(fresh) == columns(old)


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "wiki.db"
    db = make_db(path)
    db.initialize()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={migrations.LATEST + 1}")
    conn.close()
    with pytest.raises(DatabaseUnavailable):
        db.initialize()


def test_pre_ledger_database_is_refused(tmp_path):
    path = tmp_path / "wiki.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(DatabaseUnavailable, match="1.4"):
        make_db(path).initialize()


def test_missing_database_is_not_recreated(tmp_path):
    path = tmp_path / "wiki.db"
    db = make_db(path)
    db.initialize()
    path.unlink()
    with pytest.raises(DatabaseUnavailable):
        db.initialize()
