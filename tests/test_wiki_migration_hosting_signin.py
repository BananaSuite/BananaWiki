"""Schema version 25: accounts without a password of their own, confirmations and acting sessions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bananawiki.core.sqlite import Database
from bananawiki.wiki import migrations


def _database(path: Path, upto: int | None = None) -> Database:
    chain = migrations.MIGRATIONS if upto is None else migrations.MIGRATIONS[: upto - migrations.BASELINE]
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=chain,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def _at_24(tmp_path: Path) -> Path:
    path = tmp_path / "w.db"
    conn = sqlite3.connect(path)
    conn.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()
    _database(path, upto=24).initialize()
    return path


def _user(conn, user_id: str, created: str, *, linked: str | None = None, method: str | None = None) -> None:
    conn.execute("INSERT INTO users (id, username, password, created_at) VALUES (?, ?, 'x', ?)",
                 (user_id, user_id, created))
    if linked:
        conn.execute("INSERT INTO platform_oauth_links (account_id, user_id, linked_at) VALUES (?, ?, ?)",
                     ("acct-" + user_id, user_id, linked))
    if method:
        conn.execute("INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at, "
                     "auth_method) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("s-" + user_id + method, "h-" + user_id + method, user_id, created, created,
                      "2030-01-01 00:00:00", method))


def test_accounts_made_through_hosting_are_found_once(tmp_path):
    path = _at_24(tmp_path)
    conn = sqlite3.connect(path)
    _user(conn, "made", "2026-03-01 10:00:00", linked="2026-03-01 10:00:01", method="platform_oauth")
    _user(conn, "connected", "2025-01-01 10:00:00", linked="2026-03-01 10:00:00", method="platform_oauth")
    _user(conn, "typed", "2026-03-01 11:00:00", linked="2026-03-01 11:00:02", method="password")
    _user(conn, "local", "2026-03-01 12:00:00", method="password")
    conn.commit()
    conn.close()
    assert _database(path).initialize() == migrations.LATEST - 24
    conn = sqlite3.connect(path)
    flags = dict(conn.execute("SELECT id, password_set FROM users").fetchall())
    assert flags == {"made": 0, "connected": 1, "typed": 1, "local": 1}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(user_sessions)")}
    assert {"reverified_at", "acting_admin_sub", "acting_admin_name", "acting_expires_at"} <= columns
    assert conn.execute("SELECT platform_allow_acting FROM site_settings").fetchone()[0] == 1


def test_a_new_wiki_has_the_columns(tmp_path):
    db = _database(tmp_path / "new.db")
    db.initialize()
    conn = sqlite3.connect(tmp_path / "new.db")
    assert "password_set" in {row[1] for row in conn.execute("PRAGMA table_info(users)")}
