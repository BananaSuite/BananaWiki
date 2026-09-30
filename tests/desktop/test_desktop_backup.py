"""ZIP backups (online SQLite copy), restores and "delete all data"."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import zipfile

import pytest

from bananawiki.desktop import DesktopError, backup
from bananawiki.desktop.datafolder import RESTORE_JOURNAL, DataFolder
from bananawiki.desktop.server import WikiServer

from .conftest import set_setting


def site_name(folder: DataFolder) -> str:
    conn = sqlite3.connect(folder.database)
    try:
        return conn.execute("SELECT site_name FROM site_settings WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def populated(wiki_folder):
    set_setting(wiki_folder, "UPDATE site_settings SET site_name = ? WHERE id = 1", ("Class 3B",))
    (wiki_folder.root / "uploads" / "photo.png").write_bytes(b"png-bytes")
    (wiki_folder.root / "attachments" / "7").mkdir()
    (wiki_folder.root / "attachments" / "7" / "notes.pdf").write_bytes(b"%PDF")
    (wiki_folder.instance / "translations").mkdir()
    (wiki_folder.instance / "translations" / "fr.json").write_text("{}")
    (wiki_folder.logs / "bananawiki.log").write_text("log line")
    return wiki_folder


def test_backup_contains_database_key_languages_and_files(populated, tmp_path):
    target = backup.create_backup(populated, tmp_path / "out" / "backup.zip")
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"bananawiki.db", "instance/.secret_key", "instance/translations/fr.json",
            "uploads/photo.png", "attachments/7/notes.pdf", "manifest.json"} <= names
    assert not any(name.startswith(("logs/", "previous/")) for name in names)
    assert manifest["product"] == "BananaWiki" and manifest["includes_application_key"] is True
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_backup_while_the_wiki_is_running(populated, tmp_path, free_port):
    wiki = WikiServer(populated, port=free_port, backend="waitress")
    wiki.start()
    try:
        target = backup.create_backup(populated, tmp_path / "live.zip")
    finally:
        wiki.stop()
    with zipfile.ZipFile(target) as archive:
        archive.extract("bananawiki.db", tmp_path / "check")
    backup.check_database(tmp_path / "check" / "bananawiki.db")


def test_backup_refuses_existing_file_and_target_inside_folder(populated, tmp_path):
    (tmp_path / "exists.zip").write_text("keep me")
    with pytest.raises(DesktopError) as caught:
        backup.create_backup(populated, tmp_path / "exists.zip")
    assert caught.value.key == "backup_exists"
    assert (tmp_path / "exists.zip").read_text() == "keep me"
    with pytest.raises(DesktopError) as caught:
        backup.create_backup(populated, populated.root / "uploads" / "b.zip")
    assert caught.value.key == "backup_inside_folder"


def test_backup_of_a_never_started_folder(folder, tmp_path):
    with pytest.raises(DesktopError) as caught:
        backup.create_backup(folder, tmp_path / "b.zip")
    assert caught.value.key == "nothing_to_back_up"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_backup_refuses_symlinked_uploads(populated, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (populated.root / "uploads" / "link").symlink_to(secret)
    with pytest.raises(DesktopError) as caught:
        backup.create_backup(populated, tmp_path / "b.zip")
    assert caught.value.key == "unsafe_file"
    assert not (tmp_path / "b.zip").exists()


def test_restore_round_trip_keeps_previous_data(populated, tmp_path):
    archive = backup.create_backup(populated, tmp_path / "b.zip")
    key = populated.secret_key()
    set_setting(populated, "UPDATE site_settings SET site_name = 'Changed' WHERE id = 1")
    (populated.root / "uploads" / "photo.png").unlink()
    (populated.root / "uploads" / "new.png").write_bytes(b"new")

    previous = backup.restore_backup(populated, archive)

    assert site_name(populated) == "Class 3B"
    assert (populated.root / "uploads" / "photo.png").read_bytes() == b"png-bytes"
    assert not (populated.root / "uploads" / "new.png").exists()
    assert populated.secret_key() == key
    assert previous.parent == populated.previous
    assert (previous / "uploads" / "new.png").exists()
    assert (populated.logs / "bananawiki.log").read_text() == "log line"
    assert not (populated.root / RESTORE_JOURNAL).exists()
    assert not any(p.name.startswith(".restore-") for p in populated.root.iterdir())


def test_restored_wiki_starts(populated, tmp_path, free_port):
    archive = backup.create_backup(populated, tmp_path / "b.zip")
    backup.restore_backup(populated, archive)
    wiki = WikiServer(populated, port=free_port, backend="waitress")
    info = wiki.start()
    wiki.stop()
    assert info.port == free_port


def test_restore_is_refused_while_the_wiki_runs(populated, tmp_path, free_port):
    archive = backup.create_backup(populated, tmp_path / "b.zip")
    wiki = WikiServer(populated, port=free_port, backend="waitress")
    wiki.start()
    try:
        with pytest.raises(DesktopError) as caught:
            backup.restore_backup(populated, archive)
        assert caught.value.key == "folder_in_use"
    finally:
        wiki.stop()


def test_archive_without_key_keeps_the_current_key(populated, tmp_path):
    source = backup.create_backup(populated, tmp_path / "b.zip")
    stripped = tmp_path / "nokey.zip"
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(stripped, "w") as new:
        for info in old.infolist():
            if info.filename != "instance/.secret_key":
                new.writestr(info, old.read(info))
    key = populated.secret_key()
    backup.restore_backup(populated, stripped)
    assert populated.secret_key() == key


def test_a_1x_export_restores(wiki_folder, tmp_path):
    """The 1.4 launcher's "Export All" layout: raw DB at the root plus asset folders."""
    legacy = tmp_path / "bananawiki_export.zip"
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.write(wiki_folder.database, "bananawiki.db")
        archive.writestr("instance/.secret_key", "legacy-key")
        archive.writestr("chat_attachments/a.txt", "hello")
        archive.writestr("site_export.json", "{}")
        archive.writestr("manifest.json", json.dumps({"format_version": 1, "source": "standalone"}))
    backup.restore_backup(wiki_folder, legacy)
    assert wiki_folder.secret_key() == "legacy-key"
    assert (wiki_folder.root / "chat_attachments" / "a.txt").read_text() == "hello"
    assert not (wiki_folder.root / "site_export.json").exists()


def _zip_with(tmp_path, wiki_folder, name: str, data: bytes = b"x", *, mode: int | None = None):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(wiki_folder.database, "bananawiki.db")
        info = zipfile.ZipInfo(name)
        if mode is not None:
            info.create_system = 3
            info.external_attr = mode << 16
        archive.writestr(info, data)
    return path


@pytest.mark.parametrize("name", [
    "../evil.txt", "uploads/../../evil.txt", "/etc/passwd",
    pytest.param("uploads\\evil.txt", marks=pytest.mark.skipif(
        os.name == "nt", reason="zipfile itself turns backslashes into / on Windows, a plain uploads/ path")),
    "uploads/C:evil", "uploads/CON.txt", "uploads/trailing.",
])
def test_restore_rejects_unsafe_names(wiki_folder, tmp_path, name):
    archive = _zip_with(tmp_path, wiki_folder, name)
    before = site_name(wiki_folder)
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, archive)
    assert caught.value.key == "archive_unsafe"
    assert site_name(wiki_folder) == before
    assert not (tmp_path / "evil.txt").exists()


def test_restore_rejects_symlink_entries(wiki_folder, tmp_path):
    archive = _zip_with(tmp_path, wiki_folder, "uploads/link", b"/etc/passwd", mode=stat.S_IFLNK | 0o777)
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, archive)
    assert caught.value.key == "archive_unsafe"


def test_restore_rejects_code_outside_the_data_layout(wiki_folder, tmp_path):
    archive = _zip_with(tmp_path, wiki_folder, "app.py", b"import os")
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, archive)
    assert caught.value.key == "archive_unexpected"


def test_restore_rejects_duplicate_names(wiki_folder, tmp_path):
    path = tmp_path / "dup.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(wiki_folder.database, "bananawiki.db")
        archive.writestr("uploads/A.png", "1")
        archive.writestr("uploads/a.png", "2")
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, path)
    assert caught.value.key == "archive_unsafe"


def test_restore_rejects_archives_without_a_wiki_database(wiki_folder, tmp_path):
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("uploads/a.png", "1")
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, empty)
    assert caught.value.key == "archive_no_database"

    foreign_db = tmp_path / "other.db"
    conn = sqlite3.connect(foreign_db)
    conn.execute("CREATE TABLE notes (id INTEGER)")
    conn.commit()
    conn.close()
    foreign = tmp_path / "foreign.zip"
    with zipfile.ZipFile(foreign, "w") as archive:
        archive.write(foreign_db, "bananawiki.db")
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, foreign)
    assert caught.value.key == "archive_foreign"

    not_zip = tmp_path / "text.zip"
    not_zip.write_text("hello")
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, not_zip)
    assert caught.value.key == "archive_invalid"


def test_restore_rejects_a_database_from_a_newer_version(wiki_folder, tmp_path):
    newer = tmp_path / "newer.db"
    backup.snapshot_database(wiki_folder.database, newer)
    conn = sqlite3.connect(newer)
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    path = tmp_path / "newer.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(newer, "bananawiki.db")
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, path)
    assert caught.value.key == "archive_too_new"


def test_restore_rejects_a_lying_size(wiki_folder, tmp_path, monkeypatch):
    archive = _zip_with(tmp_path, wiki_folder, "uploads/big.bin", b"y" * 5000)
    real = backup.archive_entries

    def understated(zf):
        entries, total = real(zf)
        for info, _target in entries:
            if info.filename == "uploads/big.bin":
                info.file_size = 10
        return entries, total

    monkeypatch.setattr(backup, "archive_entries", understated)
    with pytest.raises(DesktopError) as caught:
        backup.restore_backup(wiki_folder, archive)
    assert caught.value.key == "archive_damaged"
    assert not (wiki_folder.root / "uploads" / "big.bin").exists()


def test_interrupted_restore_is_rolled_back(populated, tmp_path, monkeypatch):
    archive = backup.create_backup(populated, tmp_path / "b.zip")
    set_setting(populated, "UPDATE site_settings SET site_name = 'Current' WHERE id = 1")
    moves = []
    real_replace = os.replace

    def crash_midway(src, dst):
        moves.append(src)
        if len(moves) == 4:  # journal written, instance swapped, then it dies moving uploads aside
            raise KeyboardInterrupt
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", crash_midway)
    with pytest.raises(KeyboardInterrupt):
        backup.restore_backup(populated, archive)
    monkeypatch.setattr(backup.os, "replace", real_replace)
    assert site_name(populated) == "Current"
    assert (populated.root / "uploads" / "photo.png").exists()
    assert not (populated.root / RESTORE_JOURNAL).exists()


def test_recovery_on_next_start_after_a_crash(populated, tmp_path):
    """Simulate a crash that left the journal behind (the process died mid-swap)."""
    staged = populated.root / ".restore-abc123"
    (staged / "instance").mkdir(parents=True)
    previous = populated.previous / "20260101-000000"
    previous.mkdir(parents=True)
    os.replace(populated.instance, previous / "instance")
    os.replace(staged / "instance", populated.instance)  # the incoming (empty) instance
    (populated.root / RESTORE_JOURNAL).write_text(json.dumps(
        {"staged": staged.name, "previous": previous.name, "phase": "prepared"}))

    backup.recover(populated)

    assert site_name(populated) == "Class 3B"
    assert not staged.exists() and not previous.exists()
    assert not (populated.root / RESTORE_JOURNAL).exists()


@pytest.mark.parametrize("record", [
    {"staged": "../x", "previous": "20260101-000000", "phase": "prepared"},
    {"staged": ".restore-a", "previous": "../../home", "phase": "prepared"},
    {"staged": ".restore-a", "previous": "20260101-000000", "phase": "unknown"},
    ["not", "an", "object"],
])
def test_a_tampered_journal_is_not_followed(wiki_folder, record):
    (wiki_folder.root / RESTORE_JOURNAL).write_text(json.dumps(record))
    with pytest.raises(DesktopError) as caught:
        backup.recover(wiki_folder)
    assert caught.value.key == "restore_journal_invalid"
    assert wiki_folder.database.is_file()


def test_reset_deletes_everything_but_the_key(populated):
    key = populated.secret_key()
    backup.reset_data(populated)
    assert not populated.database.exists()
    assert not (populated.root / "uploads" / "photo.png").exists()
    assert populated.secret_key() == key
    assert not populated.previous.exists()
    assert (populated.logs / "bananawiki.log").exists()


def test_cli_backup_and_restore(populated, tmp_path, capsys):
    from bananawiki.desktop.__main__ import main

    target = tmp_path / "cli.zip"
    assert main(["backup", "--data-dir", str(populated.root), "--output", str(target)]) == 0
    assert target.is_file()
    assert main(["restore", "--data-dir", str(populated.root), str(target)]) == 0
    assert "Previous data kept in" in capsys.readouterr().out
    assert main(["backup", "--data-dir", str(populated.root), "--output", str(target)]) == 1
    assert "already exists" in capsys.readouterr().err
