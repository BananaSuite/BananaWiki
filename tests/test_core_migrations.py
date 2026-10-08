import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock, Timeout

from bananawiki.core import sqlite as sqlite_module
from bananawiki.core.sqlite import Database, DatabaseUnavailable
from bananawiki.wiki import migrations, takeover


def make_db(path):
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=migrations.MIGRATIONS,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def build_v3(path):
    """A database exactly as 1.4 left it (schema version 3)."""
    conn = sqlite3.connect(path)
    sql = (migrations._BASELINE_SQL).read_text(encoding="utf-8")
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


def test_earlier_suspensions_count_as_imposed_by_an_owner(tmp_path):
    path = tmp_path / "wiki.db"
    build_v3(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO suspension_audit (user_id, action, performed_by, created_at) "
                 "VALUES ('u1', 'suspend', 'u1', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()
    db = make_db(path)
    db.initialize()
    conn = db.connect()
    assert conn.execute("SELECT imposed_by_top FROM suspension_audit").fetchone()["imposed_by_top"] == 1
    migrations.v6_account_history.upgrade(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_username_history_old'").fetchone()


def test_tts_jobs_start_without_lost_attempts(tmp_path):
    path = tmp_path / "wiki.db"
    build_v3(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO tts_generations (page_id, language, status, content_hash) "
                 "VALUES (1, 'en', 'processing', 'x')")
    conn.commit()
    conn.close()
    db = make_db(path)
    db.initialize()
    conn = db.connect()
    assert conn.execute("SELECT attempts FROM tts_generations").fetchone()["attempts"] == 0
    migrations.v7_tts_attempts.upgrade(conn)  # idempotent


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


# R-26: upgrades that outlast a worker, and the copies taken before them ---------------------------


def test_the_boot_lock_waits_for_an_upgrade_running_in_another_process(tmp_path, monkeypatch, caplog):
    """Workers gave up after 120 s, failed to boot and stopped Gunicorn, killing the worker that migrated."""
    attempts = []

    class HeldElsewhere(sqlite_module.FileLock):
        def acquire(self, timeout=None, *args, **kwargs):
            attempts.append(timeout)
            if len(attempts) <= 2:  # longer than any timeout this process would allow itself
                raise Timeout(self.lock_file)
            return super().acquire(timeout, *args, **kwargs)

    monkeypatch.setattr(sqlite_module, "FileLock", HeldElsewhere)
    with caplog.at_level(logging.WARNING, logger="bananawiki.database"):
        assert make_db(tmp_path / "wiki.db").initialize() == 1
    assert len(attempts) == 3 and "still running in another process" in caplog.text


def test_the_boot_lock_is_taken_once_the_other_process_releases_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_module, "_BOOT_LOCK_NOTICE", 0.1)
    db = make_db(tmp_path / "wiki.db")
    holder = FileLock(str(tmp_path / "wiki.db") + ".schema.lock", thread_local=False)  # released by the timer
    holder.acquire()
    timer = threading.Timer(0.5, holder.release)
    timer.start()
    started = time.monotonic()
    try:
        assert db.initialize() == 1
    finally:
        timer.join()
    assert time.monotonic() - started >= 0.4


def instance(tmp_path):
    """A 1.4 database (schema 3, WAL) last written a minute ago, in an instance directory."""
    path = tmp_path / "instance" / "wiki.db"
    path.parent.mkdir()
    build_v3(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    past = time.time() - 60
    os.utime(path, (past, past))
    return SimpleNamespace(instance_dir=str(path.parent)), path


def copies(cfg) -> list[Path]:
    return sorted((Path(cfg.instance_dir) / "backups").glob("pre-upgrade-v3-*.db"))


def test_an_interrupted_pre_upgrade_copy_never_looks_like_a_backup(tmp_path, monkeypatch):
    """The copy used to be written under its final name: a process killed meanwhile left half a database."""
    cfg, path = instance(tmp_path)
    db = make_db(path)

    class KilledWhileCopying:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def backup(self, target, **_options):
            def killed(_status, _remaining, _total):
                raise KeyboardInterrupt

            self.conn.backup(target, pages=1, progress=killed)

    with pytest.raises(KeyboardInterrupt):
        db.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, db, KilledWhileCopying(conn)))
    assert copies(cfg) == [] and db.version()[1] == 3
    assert list((tmp_path / "instance" / "backups").iterdir()) == []
    synced = []
    monkeypatch.setattr(takeover, "_fsync_directory", synced.append)
    db.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, db, conn))
    assert len(copies(cfg)) == 1 and db.version()[1] == migrations.LATEST
    assert synced == [tmp_path / "instance" / "backups"]  # the new name survives a power cut
    assert copies(cfg)[0].stat().st_mode & 0o777 == 0o600 or os.name != "posix"


def test_failed_upgrades_tried_again_keep_one_copy_until_the_database_changes(tmp_path, monkeypatch):
    """A restart loop used to write a full copy of the database at every attempt, until the disk was full."""
    cfg, path = instance(tmp_path)
    stamps = iter(f"20261008-1200{second:02d}" for second in range(60))
    monkeypatch.setattr(takeover, "time", SimpleNamespace(strftime=lambda _format: next(stamps)))

    def failing(conn):
        conn.execute("UPDATE pages SET content = content || ' upgraded'")
        raise RuntimeError("the upgrade failed")

    broken = Database(path, application_id=migrations.APPLICATION_ID, migrations=(failing, *migrations.MIGRATIONS[1:]),
                      baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)
    for _attempt in range(3):
        with pytest.raises(RuntimeError, match="upgrade failed"):
            broken.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, broken, conn))
        assert len(copies(cfg)) == 1
    # Written meanwhile (by 1.4, started again): the next attempt saves a new copy, with it.
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u2', 'bob', 'x', 'user')")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="upgrade failed"):
        broken.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, broken, conn))
    assert len(copies(cfg)) == 2
    newest = sqlite3.connect(copies(cfg)[-1])
    assert newest.execute("SELECT username FROM users ORDER BY id").fetchall() == [("alice",), ("bob",)]
    newest.close()
    past = copies(cfg)[-1].stat().st_mtime - 5  # the coarse clock of a file system: written well before
    os.utime(path, (past, past))
    db = make_db(path)
    db.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, db, conn))
    assert len(copies(cfg)) == 2 and db.version()[1] == migrations.LATEST


def test_copies_left_incomplete_by_earlier_releases_are_not_kept_as_the_copy(tmp_path):
    """Written under their final name, a copy interrupted at full size has a journal beside it, or no header."""
    for case in ("journal", "header"):
        (tmp_path / case).mkdir()
        cfg, path = instance(tmp_path / case)
        backups = Path(cfg.instance_dir) / "backups"
        backups.mkdir()
        stale = backups / "pre-upgrade-v3-20261008-115900.db"  # as large as the database, and newer
        if case == "journal":
            shutil.copyfile(path, stale)
            Path(f"{stale}-journal").write_bytes(b"\0" * 512)
        else:
            stale.write_bytes(b"\0" * path.stat().st_size)
        db = make_db(path)
        db.initialize(before=lambda conn, cfg=cfg, db=db: takeover.backup_before_upgrade(cfg, db, conn))
        assert len(copies(cfg)) == 2 and db.version()[1] == migrations.LATEST


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 0)() == 0, reason="root reads any file")
def test_a_copy_this_process_cannot_read_does_not_stop_the_upgrade(tmp_path):
    """Written by root (``bananawiki migrate``), it is not one the service can reuse, and not a reason to fail."""
    cfg, path = instance(tmp_path)
    backups = Path(cfg.instance_dir) / "backups"
    backups.mkdir()
    unreadable = backups / "pre-upgrade-v3-20261008-115900.db"
    shutil.copyfile(path, unreadable)
    unreadable.chmod(0)
    db = make_db(path)
    try:
        db.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, db, conn))
    finally:
        unreadable.chmod(0o600)
    assert len(copies(cfg)) == 2 and db.version()[1] == migrations.LATEST
