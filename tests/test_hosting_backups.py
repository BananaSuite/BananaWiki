"""Backups preserve committed data and reject unsafe restores before writing."""

from pathlib import Path
import json
import sqlite3
import zipfile

import pytest

from hosting import config, db
from hosting.backups import build_full_backup
from hosting.db._restore import restore_hosting_from_backup_zips


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOSTING_DATABASE_PATH", str(tmp_path / "original/hosting.db"))
    monkeypatch.setattr(config, "INSTANCES_DIR", str(tmp_path / "original/instances"))
    monkeypatch.setattr(config, "_SECRET_KEY_PATH", str(tmp_path / "original/.secret_key"))
    db.init_hosting_db()
    owner = db.create_account("backup-owner", "hash", is_admin=True)
    instance = db.create_instance(owner, "notes", "admin", "initial-password")
    directory = Path(config.INSTANCES_DIR) / "notes"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "bananawiki.db") as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE pages (title TEXT)")
        conn.execute("INSERT INTO pages VALUES ('Committed page')")
        conn.commit()
        (directory / "uploads").mkdir()
        (directory / "uploads/manual.txt").write_text("A retained attachment")
        (directory / "bananawiki.db-wal").exists()
        parts = build_full_backup()
    try:
        yield parts[0], owner, instance
    finally:
        Path(parts[0]).unlink(missing_ok=True)


def empty_target(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOSTING_DATABASE_PATH", str(tmp_path / "restored/hosting.db"))
    monkeypatch.setattr(config, "INSTANCES_DIR", str(tmp_path / "restored/instances"))
    monkeypatch.setattr(config, "_SECRET_KEY_PATH", str(tmp_path / "restored/.secret_key"))
    monkeypatch.setattr(config, "HOSTING_BACKUP_KEY_PATH", str(tmp_path / "restored/.backup_encryption_key"))
    db.init_hosting_db()


def test_round_trip_restores_account_pages_and_files(platform, tmp_path, monkeypatch):
    backup, owner, instance = platform
    empty_target(tmp_path, monkeypatch)
    with zipfile.ZipFile(backup) as archive:
        assert not any(name.endswith(("-wal", "-shm")) for name in archive.namelist())
        restore_hosting_from_backup_zips([archive])
    assert db.get_account_by_id(owner)["username"] == "backup-owner"
    assert db.get_instance(instance["id"])["subdomain"] == "notes"
    directory = Path(config.INSTANCES_DIR) / "notes"
    with sqlite3.connect(directory / "bananawiki.db") as conn:
        assert conn.execute("SELECT title FROM pages").fetchone()[0] == "Committed page"
    assert (directory / "uploads/manual.txt").read_text() == "A retained attachment"
    assert Path(config._SECRET_KEY_PATH).stat().st_mode & 0o077 == 0


def test_restore_refuses_to_overwrite_an_occupied_installation(platform):
    backup, owner, instance = platform
    with zipfile.ZipFile(backup) as archive, pytest.raises(ValueError, match="fresh hosting"):
        restore_hosting_from_backup_zips([archive])
    assert db.get_instance(instance["id"])["account_id"] == owner


@pytest.mark.parametrize("entry", ["instances/notes/../../escape", "instances/notes/../../../escape", "/tmp/escape", "instances\\notes\\escape"])
def test_restore_rejects_traversal_before_installing_any_files(platform, tmp_path, monkeypatch, entry):
    backup, owner, instance = platform
    empty_target(tmp_path, monkeypatch)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("instances/notes/innocent", "must remain staged")
        archive.writestr(entry, "unsafe")
    with zipfile.ZipFile(backup) as valid, zipfile.ZipFile(bad) as invalid, pytest.raises(ValueError, match="unsafe path"):
        restore_hosting_from_backup_zips([valid, invalid])
    assert not Path(config.INSTANCES_DIR).exists()
    assert db.get_account_by_id(owner) is None


def test_restore_refuses_existing_symlink_to_external_directory(platform, tmp_path, monkeypatch):
    backup, _, _ = platform
    empty_target(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = Path(config.INSTANCES_DIR)
    root.mkdir()
    (root / "notes").symlink_to(outside, target_is_directory=True)
    with zipfile.ZipFile(backup) as archive, pytest.raises(ValueError, match="outside"):
        restore_hosting_from_backup_zips([archive])
    assert list(outside.iterdir()) == []


def test_sqlite_snapshot_accepts_quoted_and_unicode_paths(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE example (value TEXT)")
        conn.execute("INSERT INTO example VALUES ('kept')")
    destination = tmp_path / "Luca's copia è pronta.db"
    db.create_consistent_db_copy(str(source), str(destination))
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT value FROM example").fetchone()[0] == "kept"


def test_restore_reconstructs_omitted_storage_aliases(platform, tmp_path, monkeypatch):
    """Files in managed storage remain reachable through the tenant's upload path."""
    directory = Path(config.INSTANCES_DIR) / "notes"
    (directory / "storage").mkdir()
    (directory / "uploads").rename(directory / "storage/uploads")
    (directory / "uploads").symlink_to("storage/uploads", target_is_directory=True)
    backup = Path(build_full_backup()[0])
    try:
        empty_target(tmp_path, monkeypatch)
        with zipfile.ZipFile(backup) as archive:
            restore_hosting_from_backup_zips([archive])
        restored = Path(config.INSTANCES_DIR) / "notes"
        assert (restored / "uploads").is_symlink()
        assert (restored / "uploads/manual.txt").read_text() == "A retained attachment"
        assert (restored / "uploads").resolve() == restored / "storage/uploads"
    finally:
        backup.unlink(missing_ok=True)


def legacy_bundle(backup, target, *, backup_key=b"k" * 32, include_landing=True):
    """Build the version-three layout emitted by the retired deployment exporter."""
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(target, "w") as output:
        output.writestr("manifest.json", json.dumps({"bundle_version": 3}))
        for entry in source.infolist():
            name = entry.filename
            if name == "hosting.db":
                name = "hosting/hosting.db"
            elif name == "secret_key":
                name = "hosting/.secret_key"
            else:
                name = "hosting/" + name
            output.writestr(name, source.read(entry))
        output.writestr("hosting/.backup_encryption_key", backup_key)
        if include_landing:
            output.writestr("landing/index.html", "Private landing fixture")
        output.writestr("configuration/hosting.env", "OPERATOR_ONLY=fixture")
        # Customer ZIP attachments are ordinary data, not platform envelopes.
        output.writestr("hosting/instances/notes/uploads/customer.zip", b"fixture attachment")


@pytest.mark.parametrize("include_landing", [True, False])
def test_legacy_deploy_bundle_restores_hosting_without_expanding_attachments(platform, tmp_path, monkeypatch, include_landing):
    """Old deploy.sh bundles preserve account IDs, tenant data, and both keys."""
    from hosting.routes.dashboard_archives import _restore_platform_from_uploaded_archives

    backup, owner, instance = platform
    archive = tmp_path / "deploy_bundle.zip"
    legacy_bundle(backup, archive, include_landing=include_landing)
    empty_target(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    _restore_platform_from_uploaded_archives([str(archive)], str(work))
    assert db.get_account_by_id(owner)["username"] == "backup-owner"
    assert db.get_instance(instance["id"])["subdomain"] == "notes"
    restored = Path(config.INSTANCES_DIR) / "notes"
    with sqlite3.connect(restored / "bananawiki.db") as connection:
        assert connection.execute("SELECT title FROM pages").fetchone()[0] == "Committed page"
    assert (restored / "uploads/manual.txt").read_text() == "A retained attachment"
    assert (restored / "uploads/customer.zip").read_bytes() == b"fixture attachment"
    assert Path(config._SECRET_KEY_PATH).read_text() == config.HOSTING_SECRET_KEY
    assert Path(config.HOSTING_BACKUP_KEY_PATH).read_bytes() == b"k" * 32
    assert Path(config.HOSTING_BACKUP_KEY_PATH).stat().st_mode & 0o077 == 0
    assert not (Path(config.HOSTING_DATABASE_PATH).parent / "landing").exists()
    assert not (Path(config.HOSTING_DATABASE_PATH).parent / "hosting.env").exists()


def test_invalid_legacy_backup_key_is_rejected_before_data_changes(platform, tmp_path, monkeypatch):
    """An invalid key cannot partially install an otherwise valid legacy backup."""
    backup, owner, _ = platform
    archive = tmp_path / "invalid-key.zip"
    legacy_bundle(backup, archive, backup_key=b"invalid")
    empty_target(tmp_path, monkeypatch)
    with zipfile.ZipFile(archive) as source, pytest.raises(ValueError, match="backup encryption key"):
        restore_hosting_from_backup_zips([source])
    assert db.get_account_by_id(owner) is None
    assert not Path(config.INSTANCES_DIR).exists()
    assert not Path(config.HOSTING_BACKUP_KEY_PATH).exists()


def test_mixed_legacy_and_current_database_names_are_rejected(platform, tmp_path, monkeypatch):
    """Equivalent archive names cannot overwrite one another after normalization."""
    backup, owner, _ = platform
    archive = tmp_path / "duplicate-db.zip"
    legacy_bundle(backup, archive)
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(archive, "a") as target:
        target.writestr("hosting.db", source.read("hosting.db"))
    empty_target(tmp_path, monkeypatch)
    with zipfile.ZipFile(archive) as source, pytest.raises(ValueError, match="duplicate"):
        restore_hosting_from_backup_zips([source])
    assert db.get_account_by_id(owner) is None
    assert not Path(config.INSTANCES_DIR).exists()
