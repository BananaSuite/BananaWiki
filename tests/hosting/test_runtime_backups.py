"""Encrypted platform backups (BWBACKUP1), restore, Google Drive, DNS checks and runtime maintenance."""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import dns.resolver
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bananawiki.hosting.db import open_database
from bananawiki.hosting.runtime import DomainCheck, RuntimeFailure, crypto, gdrive
from bananawiki.hosting.runtime.crypto import MAGIC

from .agent_fakes import make_runtime, make_spec, provision


def _hosting_db(runtime) -> None:
    open_database(runtime._cfg()).initialize()


def _decrypt(path: Path, key: bytes) -> bytes:
    """The 1.4 algorithm, written independently: AES-256-GCM over everything between nonce and tag."""
    data = path.read_bytes()
    assert data.startswith(MAGIC)
    nonce = data[len(MAGIC):len(MAGIC) + 12]
    return AESGCM(key).decrypt(nonce, data[len(MAGIC) + 12:], None)


def test_backup_key_from_file_or_environment(tmp_path, monkeypatch):
    runtime, _agent = make_runtime(tmp_path)
    key = runtime.backup_key()
    path = Path(runtime._cfg().backup_key_path)
    assert len(key) == 32 and oct(path.stat().st_mode & 0o777) == "0o600"
    assert runtime.backup_key() == key
    monkeypatch.setenv("HOSTING_BACKUP_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"e" * 32).decode().rstrip("="))
    assert runtime.backup_key() == b"e" * 32
    monkeypatch.setenv("HOSTING_BACKUP_ENCRYPTION_KEY", "c2hvcnQ")
    with pytest.raises(RuntimeFailure) as error:
        runtime.backup_key()
    assert error.value.code == "not_configured"


@pytest.mark.parametrize("kind", ["world_readable", "symlink", "fifo", "large"])
def test_backup_key_refuses_public_or_nonregular_files(tmp_path, kind):
    path = tmp_path / "backup.key"
    if kind == "symlink":
        other = tmp_path / "actual.key"
        other.write_bytes(b"k" * 32)
        other.chmod(0o600)
        path.symlink_to(other)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b"k" * (33 if kind == "large" else 32))
        path.chmod(0o644 if kind == "world_readable" else 0o600)
    with pytest.raises(RuntimeFailure) as error:
        crypto.load_key("", str(path))
    assert error.value.code == "not_configured"


def test_streaming_encryption_matches_the_1x_format(tmp_path):
    key = os.urandom(32)
    target = tmp_path / "out.bwenc"
    output = crypto.EncryptedOutput(target, key)
    output.write(b"hello ")
    output.write(b"world")
    output.finish()
    assert _decrypt(target, key) == b"hello world"
    legacy = tmp_path / "legacy.bwenc"
    nonce = os.urandom(12)
    legacy.write_bytes(MAGIC + nonce + AESGCM(key).encrypt(nonce, b"from 1.4", None))
    assert crypto.decrypt_file(legacy, tmp_path / "plain", key).read_bytes() == b"from 1.4"
    with pytest.raises(RuntimeFailure) as error:
        crypto.decrypt_file(legacy, tmp_path / "wrong", os.urandom(32))
    assert error.value.code == "archive_invalid" and not (tmp_path / "wrong").exists()


def test_encrypted_export_name_collision_preserves_the_completed_backup(tmp_path):
    key = os.urandom(32)
    target = tmp_path / "out.bwenc"
    first = crypto.EncryptedOutput(target, key)
    first.write(b"first backup")
    first.finish()
    previous = target.read_bytes()
    second = crypto.EncryptedOutput(target, key)
    second.write(b"second backup")
    with pytest.raises(FileExistsError):
        second.finish()
    assert target.read_bytes() == previous
    assert not list(tmp_path.glob(".bwenc-*"))


def test_platform_export_is_encrypted_while_it_is_written(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    provision(runtime, make_spec())
    root = Path(runtime._cfg().instances_dir) / "acme"
    (root / "storage" / "uploads" / "a.png").write_bytes(b"png")
    (Path(runtime._cfg().instances_dir) / ".import-stale").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    path = runtime.export_platform(work)
    assert [p.name for p in work.iterdir()] == [path.name], "no plaintext archive or database copy is left"
    assert path.name.endswith(".zip.bwenc")
    with zipfile.ZipFile(io.BytesIO(_decrypt(path, runtime.backup_key()))) as archive:
        names = set(archive.namelist())
        assert archive.read("secret_key").decode() == runtime._cfg().secret_key
    assert {"hosting.db", "instances/acme/bananawiki.db", "instances/acme/storage/uploads/a.png",
            "instances/acme/.secret_key"} <= names
    assert not any(".import-" in name or ".bw-host" in name for name in names)
    assert "snapshot" in agent.tasks()


def test_platform_restore_onto_a_fresh_installation(tmp_path, monkeypatch):
    source, _agent = make_runtime(tmp_path / "a")
    _hosting_db(source)
    provision(source, make_spec())
    (Path(source._cfg().instances_dir) / "acme" / "storage" / "uploads" / "a.png").write_bytes(b"png")
    with sqlite3.connect(source._cfg().database_path) as conn:
        conn.execute("UPDATE hosting_settings SET signup_mode = 'closed' WHERE id = 1")
    work = tmp_path / "work"
    work.mkdir()
    backup = source.export_platform(work)

    target, _agent2 = make_runtime(tmp_path / "b")
    _hosting_db(target)
    Path(target._cfg().backup_key_path).parent.mkdir(parents=True, exist_ok=True)
    Path(target._cfg().backup_key_path).write_bytes(source.backup_key())
    Path(target._cfg().backup_key_path).chmod(0o600)
    secret_path = tmp_path / "b" / "restored_secret"
    monkeypatch.setenv("HOSTING_SECRET_KEY_PATH", str(secret_path))
    target.restore_platform([backup])
    restored = Path(target._cfg().instances_dir) / "acme"
    assert (restored / "storage" / "uploads" / "a.png").read_bytes() == b"png"
    assert os.readlink(restored / "uploads") == "storage/uploads", "aliases are recreated"
    assert secret_path.read_text() == source._cfg().secret_key
    with sqlite3.connect(target._cfg().database_path) as conn:
        assert conn.execute("SELECT signup_mode FROM hosting_settings").fetchone()[0] == "closed"
    with pytest.raises(RuntimeFailure):
        target.restore_platform([tmp_path / "a" / "missing.zip"])


def test_platform_restore_refuses_installations_with_wikis_and_unsafe_parts(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("instances/../../etc/cron.d/x", b"x")
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([unsafe])
    assert error.value.code in ("archive_invalid", "invalid")
    with sqlite3.connect(runtime._cfg().database_path) as conn:
        conn.execute("INSERT INTO accounts (id, username, password, created_at) VALUES ('a1', 'u', 'x', 'now')")
        conn.execute("INSERT INTO instances (id, account_id, subdomain, status, created_at) "
                     "VALUES ('i1', 'a1', 'w', 'running', 'now')")
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([unsafe])
    assert error.value.code == "data_exists"


def test_rejected_platform_restore_keeps_live_wikis_running(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    spec = make_spec()
    provision(runtime, spec)
    before = dict(agent.containers)
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([tmp_path / "missing.zip"])
    assert error.value.code == "data_exists"
    assert agent.containers == before and not agent.ops("tenant.stop")


def test_platform_restore_requires_observable_empty_runtime(tmp_path):
    from bananawiki.ops.runtime_agent import AgentError

    runtime, agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    agent.failures["tenant.list"] = AgentError("unavailable", "runtime disconnected")
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([tmp_path / "missing.zip"])
    assert error.value.code == "unavailable"


def test_damaged_backup_member_is_a_reported_restore_error(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    part = tmp_path / "damaged.zip"
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("hosting.db", b"database contents")
    raw = bytearray(part.read_bytes())
    data_offset = 30 + len("hosting.db")
    raw[data_offset] ^= 1
    part.write_bytes(raw)
    before = Path(runtime._cfg().database_path).read_bytes()
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([part])
    assert error.value.code == "archive_invalid"
    assert Path(runtime._cfg().database_path).read_bytes() == before


def test_restore_checks_free_space_before_extracting_platform_members(tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import platform_backup

    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    part = tmp_path / "valid.zip"
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as archive:
        archive.write(runtime._cfg().database_path, "hosting.db")
    monkeypatch.setattr(platform_backup.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([part])
    assert error.value.code == "no_space"


# ── Google Drive ──────────────────────────────────────────────────────────────


def test_drive_schedule():
    settings = gdrive.DriveSettings(True, "/x", "folder", 7, gdrive.parse_time("04:30"))
    now = datetime(2026, 5, 2, 5, 0)
    assert gdrive.due(settings, None, now)
    assert not gdrive.due(settings, datetime(2026, 5, 2, 4, 45), now)
    assert gdrive.due(settings, datetime(2026, 5, 1, 4, 45), now)
    assert not gdrive.due(settings, datetime(2026, 5, 1, 4, 45), datetime(2026, 5, 2, 4, 0))
    assert gdrive.parse_time("99:99") == (3, 0)


def test_drive_credentials_are_validated_and_private(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    with pytest.raises(RuntimeFailure):
        runtime.store_gdrive_credentials(b'{"type": "authorized_user"}')
    path = Path(runtime.store_gdrive_credentials(
        b'{"type": "service_account", "client_email": "a@b.iam", "private_key": "-----BEGIN"}'))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.parent == Path(runtime._cfg().database_path).parent


@pytest.mark.parametrize("extra", [
    {"token_uri": "http://169.254.169.254/latest/meta-data/"},
    {"token_uri": "https://oauth2.googleapis.com.evil.test/token"},
    {"universe_domain": "internal.test"},
])
def test_drive_key_cannot_point_google_auth_at_another_endpoint(tmp_path, extra):
    """google-auth POSTs to the key file's token_uri: an uploaded key must not turn the portal into an SSRF."""
    key = {"type": "service_account", "client_email": "a@b.iam", "private_key": "-----BEGIN"}
    runtime, _agent = make_runtime(tmp_path)
    with pytest.raises(RuntimeFailure):
        runtime.store_gdrive_credentials(json.dumps({**key, **extra}).encode())
    # A key stored before this check (1.4) is refused before google-auth reads it.
    stored = tmp_path / "old-key.json"
    stored.write_text(json.dumps({**key, **extra}))
    with pytest.raises(RuntimeFailure):
        gdrive.service(gdrive.DriveSettings(True, str(stored), "folder", 7, (3, 0)))
    gdrive.validate_credentials(json.dumps({**key, "token_uri": gdrive.TOKEN_URI}).encode())


class FakeDrive:
    def __init__(self):
        self.deleted = []

    def files(self):
        return self

    def list(self, **kwargs):
        return SimpleNamespace(execute=lambda: {"files": [{"id": "1", "name": "bananawiki_backup_old.zip.bwenc"},
                                                          {"id": "2", "name": "someone-else.txt"}]})

    def delete(self, fileId):
        self.deleted.append(fileId)
        return SimpleNamespace(execute=lambda: None)


def test_scheduled_drive_backup_runs_once_per_day(tmp_path, monkeypatch):
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    drive = FakeDrive()
    uploads = []
    settings = gdrive.DriveSettings(True, "/creds.json", "folder", 7, (0, 0))
    monkeypatch.setattr(runtime, "_drive_settings", lambda: settings)
    monkeypatch.setattr(gdrive, "service", lambda _settings: drive)
    monkeypatch.setattr(gdrive, "upload", lambda _client, _settings, path: uploads.append(path.read_bytes()[:9])
                        or path.name)
    runtime.maintenance_tick()
    runtime.maintenance_tick()
    assert uploads == [MAGIC], "only encrypted archives are uploaded, once per scheduled time"
    assert drive.deleted == ["1"], "retention only touches this platform's backups"
    assert not list(Path(runtime._cfg().archives.export_temp_dir).iterdir())


def test_drive_without_configuration(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    settings = gdrive.DriveSettings(False, "", "", 7, (3, 0))
    runtime._drive_settings = lambda: settings  # type: ignore[method-assign]
    with pytest.raises(RuntimeFailure) as error:
        runtime.gdrive_test()
    assert error.value.code == "not_configured"


# ── DNS ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def zone(monkeypatch):
    records: dict[tuple[str, str], list[str]] = {}

    def resolve(name, record_type, **_kwargs):
        if (name, record_type) not in records:
            raise dns.resolver.NoAnswer()
        return [SimpleNamespace(strings=[value.encode()], to_text=lambda value=value: value + ".")
                for value in records[(name, record_type)]]

    monkeypatch.setattr(dns.resolver, "resolve", resolve)
    return records


def test_check_domain_by_cname_or_addresses(tmp_path, zone):
    runtime, _agent = make_runtime(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test",
                                   HOSTING_CUSTOM_DOMAIN_IPS="203.0.113.5")
    zone[("_bananawiki-challenge.docs.example.org", "TXT")] = ["token-1"]
    zone[("docs.example.org", "CNAME")] = ["edge.wiki.test"]
    assert runtime.check_domain("docs.example.org", "token-1") == DomainCheck(True, True, False)
    del zone[("docs.example.org", "CNAME")]
    zone[("docs.example.org", "A")] = ["203.0.113.5"]
    check = runtime.check_domain("docs.example.org", "token-2")
    assert (check.ownership, check.routing) == (False, True)
    zone[("docs.example.org", "A")] = ["198.51.100.1"]
    assert not runtime.check_domain("docs.example.org", "token-1").routing
    assert runtime.check_domain("missing.example.org", "t").dns_error


def test_check_domain_accepts_records_proxied_by_cloudflare(tmp_path, zone):
    """A proxied (orange-cloud) record hides the CNAME and shows only Cloudflare's edge addresses."""
    runtime, _agent = make_runtime(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test",
                                   HOSTING_CUSTOM_DOMAIN_IPS="203.0.113.5")
    zone[("_bananawiki-challenge.docs.example.org", "TXT")] = ["token-1"]
    zone[("docs.example.org", "A")] = ["104.21.2.3", "172.67.4.5"]
    zone[("docs.example.org", "AAAA")] = ["2606:4700:3031::6815:203"]
    check = runtime.check_domain("docs.example.org", "token-1")
    assert (check.ownership, check.routing, check.proxied) == (True, True, True)
    zone[("docs.example.org", "A")] = ["104.21.2.3", "198.51.100.1"]
    assert not runtime.check_domain("docs.example.org", "token-1").routing, "not all addresses are Cloudflare's"
    zone[("docs.example.org", "A")] = ["203.0.113.5"]
    del zone[("docs.example.org", "AAAA")]
    assert runtime.check_domain("docs.example.org", "token-1").proxied is False, "direct records are not proxied"
    strict, _agent = make_runtime(tmp_path / "strict", HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test",
                                  HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED="0")
    zone[("docs.example.org", "A")] = ["104.21.2.3"]
    assert not strict.check_domain("docs.example.org", "token-1").routing


# ── Maintenance ───────────────────────────────────────────────────────────────


def test_maintenance_tick_prunes_leftovers(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    provision(runtime, make_spec())
    base = Path(runtime._cfg().instances_dir)
    old = time.time() - 30 * 86400
    stale = base / ".import-abc"
    stale.mkdir()
    fresh = base / ".import-new"
    fresh.mkdir()
    os.utime(stale, (old, old))
    log = base / "acme" / "access.log"
    log.write_text("1.4 log")
    os.utime(log, (old, old))
    (base / "acme" / ".bw-host").mkdir(exist_ok=True)
    leftover = base / "acme" / ".bw-host" / "snap-x.db"
    leftover.write_bytes(b"x")
    os.utime(leftover, (old, old))
    runtime._drive_settings = lambda: gdrive.DriveSettings(False, "", "", 7, (3, 0))  # type: ignore[method-assign]
    runtime.maintenance_tick()
    assert not stale.exists() and fresh.exists()
    assert not log.exists() and not leftover.exists()
    assert (base / "acme" / "bananawiki.db").exists()
