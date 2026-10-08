"""Encrypted platform backups (BWBACKUP1), restore, Google Drive, DNS checks and runtime maintenance."""

from __future__ import annotations

import base64
import errno
import io
import json
import logging
import os
import shutil
import sqlite3
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import dns.resolver
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bananawiki.hosting.db import open_database
from bananawiki.hosting.runtime import DomainCheck, RuntimeFailure, UnavailableRuntime, crypto, gdrive
from bananawiki.hosting.runtime.agent import GDRIVE_RETRY_SECONDS
from bananawiki.hosting.runtime.crypto import MAGIC
from bananawiki.ops.runtime_agent import AgentError

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


# ── Wikis that cannot be backed up ────────────────────────────────────────────


def _two_wikis(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    base = Path(runtime._cfg().instances_dir)
    for port, name in enumerate(("acme", "beta"), 6001):
        provision(runtime, make_spec(name, port=port))
        (base / name / "storage" / "uploads" / "a.png").write_bytes(name.encode())
    return runtime, agent, base


def _backup(runtime, path: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(_decrypt(path, runtime.backup_key())))


def _work(tmp_path, name: str = "work") -> Path:
    work = tmp_path / name
    work.mkdir()
    return work


def test_one_failing_wiki_does_not_stop_the_platform_backup(tmp_path):
    runtime, agent, _base = _two_wikis(tmp_path)
    assert runtime.platform_backup_status() == {}
    agent.failures["tenant.task"] = AgentError("timeout", "the snapshot did not finish")
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert {"hosting.db", "secret_key", "instances/beta/bananawiki.db", "instances/beta/storage/uploads/a.png"} <= names
    assert not any(name.startswith("instances/acme/") for name in names)
    assert manifest["tenants"] == ["beta"]
    assert [(item["tenant"], item["code"]) for item in manifest["skipped"]] == [("acme", "timeout")]
    status = runtime.platform_backup_status()
    assert status["finished_at"] and status["complete_at"] is None
    assert [(item["tenant"], item["code"]) for item in status["skipped"]] == [("acme", "timeout")]

    runtime.export_platform(_work(tmp_path, "again"))
    status = runtime.platform_backup_status()
    assert status["skipped"] == [] and status["complete_at"] == status["finished_at"]
    complete = status["complete_at"]
    agent.failures["tenant.task"] = AgentError("timeout", "the snapshot did not finish")
    runtime.export_platform(_work(tmp_path, "third"))
    assert runtime.platform_backup_status()["complete_at"] == complete, "a partial backup keeps the last complete one"


@pytest.mark.parametrize(("failure", "reported"), [
    (PermissionError(errno.EACCES, "Permission denied"), {"code": "failed", "detail": "Permission denied"}),
    (ValueError("an error nobody foresaw"), {"code": "failed", "detail": "ValueError: an error nobody foresaw"}),
], ids=["unreadable", "unforeseen"])
def test_a_wiki_that_cannot_be_read_is_reported_with_what_was_copied(tmp_path, monkeypatch, failure, reported):
    from bananawiki.hosting.runtime import platform_backup

    runtime, _agent, _base = _two_wikis(tmp_path)
    add_tree = platform_backup.archives.add_tenant_tree

    def unreadable(archive, root, prefix, *args, **kwargs):
        if root.name == "acme":
            raise failure
        return add_tree(archive, root, prefix, *args, **kwargs)

    monkeypatch.setattr(platform_backup.archives, "add_tenant_tree", unreadable)
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert "instances/acme/bananawiki.db" in names, "what was copied before the failure stays in the archive"
    assert "instances/beta/storage/uploads/a.png" in names
    assert manifest["skipped"] == [{"tenant": "acme", **reported}]
    assert not list((Path(runtime._cfg().instances_dir) / "acme" / ".bw-host").iterdir()), "the copy is released"


def test_a_wiki_file_that_shrinks_leaves_a_valid_archive(tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import tenantfs

    runtime, _agent, base = _two_wikis(tmp_path)
    asset = base / "acme" / "storage" / "uploads" / "big.bin"
    asset.write_bytes(os.urandom(300_000))
    iter_files = tenantfs.iter_files

    def shrinking_once_observed(*args, **kwargs):
        for relative, fd, info in iter_files(*args, **kwargs):
            if relative.endswith("/big.bin"):
                os.truncate(asset, 1000)
            yield relative, fd, info

    monkeypatch.setattr(tenantfs, "iter_files", shrinking_once_observed)
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        assert archive.testzip() is None, "the member cut short is closed properly"
        assert archive.read("instances/beta/storage/uploads/a.png") == b"beta"
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert [item["tenant"] for item in manifest["skipped"]] == ["acme"]
    assert "shrank" in manifest["skipped"][0]["detail"]


def test_a_wiki_file_name_that_is_not_utf8_is_reported_and_the_rest_saved(tmp_path):
    """ZIP names are UTF-8; a tenant (its plugins) can create a file name of any bytes."""
    runtime, _agent, base = _two_wikis(tmp_path)
    uploads = base / "acme" / "storage" / "uploads"
    with open(os.path.join(os.fsencode(uploads), b"\xff\xfe.png"), "wb") as handle:
        handle.write(b"hostile")
    (uploads / "b.png").write_bytes(b"b")
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert {"instances/acme/bananawiki.db", "instances/acme/storage/uploads/a.png",
            "instances/acme/storage/uploads/b.png", "instances/beta/storage/uploads/a.png"} <= names
    assert manifest["tenants"] == ["beta"]
    [skipped] = manifest["skipped"]
    assert skipped["tenant"] == "acme" and skipped["code"] == "failed"
    assert "storage/uploads/\\xff\\xfe.png" in skipped["detail"]
    status = runtime.platform_backup_status()
    assert status["complete_at"] is None and [item["tenant"] for item in status["skipped"]] == ["acme"]


def test_a_wiki_whose_database_is_a_link_is_reported_with_its_files(tmp_path):
    """Its files are kept, but a backup without its database is not a complete one."""
    runtime, _agent, base = _two_wikis(tmp_path)
    database = base / "acme" / "bananawiki.db"
    database.unlink()
    database.symlink_to(base / "beta" / "bananawiki.db")
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert "instances/acme/storage/uploads/a.png" in names and "instances/acme/bananawiki.db" not in names
    assert manifest["tenants"] == ["beta"]
    assert [(item["tenant"], item["code"]) for item in manifest["skipped"]] == [("acme", "db_unsafe")]
    assert runtime.platform_backup_status()["complete_at"] is None


def _deep_file(folder: Path) -> str:
    """A file below folders whose path is longer than Linux's 4096-byte limit: created one folder at a time,
    as a tenant can."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    parts = []
    try:
        for level in range(18):
            name = f"{level:02d}" + "d" * 240
            os.mkdir(name, dir_fd=fd)
            inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = inner
            parts.append(name)
        os.close(os.open("deep.png", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd))
    finally:
        os.close(fd)
    return "/".join([*parts, "deep.png"])


def _colon(uploads: Path) -> str:
    (uploads / "x:y.png").write_bytes(b"x")
    return "x:y.png"


def _backslash(uploads: Path) -> str:
    (uploads / "x\\y.png").write_bytes(b"x")
    return "x\\y.png"


def _colon_folder(uploads: Path) -> str:
    (uploads / "a:b").mkdir()
    (uploads / "a:b" / "c.png").write_bytes(b"c")
    return "a:b/c.png"


def _restored(source, backup: Path, tmp_path, monkeypatch) -> Path:
    target, _agent = make_runtime(tmp_path / "restored")
    _hosting_db(target)
    key_path = Path(target._cfg().backup_key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(source.backup_key())
    key_path.chmod(0o600)
    monkeypatch.setenv("HOSTING_SECRET_KEY_PATH", str(tmp_path / "restored" / "secret"))
    target.restore_platform([backup])
    return Path(target._cfg().instances_dir)


@pytest.mark.parametrize("hostile", [_colon, _backslash, _colon_folder, _deep_file],
                         ids=["colon", "backslash", "colon-folder", "too-long"])
def test_tenant_file_names_a_restore_refuses_never_reach_the_backup(tmp_path, monkeypatch, caplog, hostile):
    """One such member made the whole platform restore fail, while the backup counted as complete."""
    runtime, _agent, base = _two_wikis(tmp_path)
    name = "storage/uploads/" + hostile(base / "acme" / "storage" / "uploads")
    backup = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, backup) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert f"instances/acme/{name}" not in names
    assert {"instances/acme/bananawiki.db", "instances/acme/storage/uploads/a.png"} <= names
    assert manifest["tenants"] == ["beta"]
    [skipped] = manifest["skipped"]
    assert (skipped["tenant"], skipped["code"]) == ("acme", "failed") and name[:100] in skipped["detail"]
    status = runtime.platform_backup_status()
    assert status["complete_at"] is None and [item["tenant"] for item in status["skipped"]] == ["acme"]

    with caplog.at_level(logging.WARNING, logger="bananawiki.hosting.runtime.platform"):
        restored = _restored(runtime, backup, tmp_path, monkeypatch)
    assert (restored / "beta" / "storage" / "uploads" / "a.png").read_bytes() == b"beta"
    assert (restored / "acme" / "storage" / "uploads" / "a.png").read_bytes() == b"acme"
    assert (restored / "acme" / "bananawiki.db").is_file()
    assert "did not hold these wikis in full: acme (failed)" in caplog.text, "the restore says what is missing"


@pytest.mark.parametrize("folder", ["bananawiki.db", "bananawiki.db-wal"], ids=["database", "journal"])
def test_a_database_turned_into_a_folder_during_the_backup_stays_out(tmp_path, monkeypatch, folder):
    """A wiki can replace its database with a folder once the copy is taken, before its files are walked: archived,
    that folder made bananawiki.db both a file and a folder (or left a folder where SQLite needs its journal), so
    the restore of every wiki failed while the backup counted as complete."""
    from bananawiki.hosting.runtime import platform_backup

    runtime, _agent, base = _two_wikis(tmp_path)
    add_tree = platform_backup.archives.add_tenant_tree

    def swapped(archive, root, prefix, *args, **kwargs):
        if root.name == "acme":
            (root / folder).unlink(missing_ok=True)
            (root / folder).mkdir()
            (root / folder / "x.png").write_bytes(b"x")
        return add_tree(archive, root, prefix, *args, **kwargs)

    monkeypatch.setattr(platform_backup.archives, "add_tenant_tree", swapped)
    backup = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, backup) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert not [name for name in names if name.startswith(f"instances/acme/{folder}/")]
    assert {"instances/acme/bananawiki.db", "instances/acme/storage/uploads/a.png"} <= names
    assert manifest["tenants"] == ["beta"]
    [skipped] = manifest["skipped"]
    assert (skipped["tenant"], skipped["code"]) == ("acme", "failed") and f"{folder}/x.png" in skipped["detail"]
    assert runtime.platform_backup_status()["complete_at"] is None

    restored = _restored(runtime, backup, tmp_path, monkeypatch)
    assert (restored / "beta" / "storage" / "uploads" / "a.png").read_bytes() == b"beta"
    assert (restored / "acme" / "bananawiki.db").is_file() and not (restored / "acme" / folder).is_dir()


@pytest.mark.parametrize("folder", ["storage", "storage/uploads", "external_plugins"])
def test_a_file_where_a_wiki_needs_a_folder_stays_out_of_the_backup(tmp_path, monkeypatch, folder):
    """A wiki can replace its storage, an asset folder or external_plugins with a file: archived, that file made
    the restore of every wiki fail (the restored layout needs a folder there) while the backup counted as
    complete."""
    runtime, _agent, base = _two_wikis(tmp_path)
    shutil.rmtree(base / "acme" / folder)
    (base / "acme" / folder).write_bytes(b"not a folder")
    backup = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, backup) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert f"instances/acme/{folder}" not in names and "instances/acme/bananawiki.db" in names
    assert manifest["tenants"] == ["beta"]
    [skipped] = manifest["skipped"]
    assert (skipped["tenant"], skipped["code"]) == ("acme", "failed") and skipped["detail"].endswith(f": {folder}")
    assert runtime.platform_backup_status()["complete_at"] is None

    restored = _restored(runtime, backup, tmp_path, monkeypatch)
    assert (restored / "beta" / "storage" / "uploads" / "a.png").read_bytes() == b"beta"
    assert (restored / "acme" / "bananawiki.db").is_file() and (restored / "acme" / folder).is_dir()


@pytest.mark.parametrize("kind", ["link", "fifo"])
@pytest.mark.parametrize("folder", ["storage", "storage/uploads"])
def test_a_storage_folder_replaced_by_a_link_leaves_the_wiki_incomplete(tmp_path, monkeypatch, folder, kind):
    """The walk skips links and special files: a wiki that replaced its storage (or an asset folder) with one
    counted as saved in full, while its restore held empty folders there."""
    runtime, _agent, base = _two_wikis(tmp_path)
    target = base / "acme" / folder
    target.rename(base / "acme" / "moved")
    if kind == "link":
        target.symlink_to(base / "acme" / "moved")
    else:
        os.mkfifo(target)
    backup = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, backup) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert {"instances/acme/bananawiki.db", "instances/beta/storage/uploads/a.png"} <= names
    assert manifest["tenants"] == ["beta"]
    [skipped] = manifest["skipped"]
    assert (skipped["tenant"], skipped["code"]) == ("acme", "db_unsafe") and skipped["detail"].endswith(f": {folder}")
    assert runtime.platform_backup_status()["complete_at"] is None

    restored = _restored(runtime, backup, tmp_path, monkeypatch)
    assert (restored / "beta" / "storage" / "uploads" / "a.png").read_bytes() == b"beta"
    assert (restored / "acme" / "bananawiki.db").is_file() and (restored / "acme" / folder).is_dir()


def _wiki_rows(runtime, *wikis: tuple[str, str, int | None, bool]) -> None:
    """Rows in hosting.db for (subdomain, domain mode, storage limit in MB, owned by an administrator)."""
    conn = sqlite3.connect(runtime._cfg().database_path)
    try:
        with conn:
            for admin in (0, 1):
                conn.execute("INSERT INTO accounts (id, username, password, is_admin, created_at) "
                             "VALUES (?, ?, 'x', ?, 'x')", (f"owner{admin}", f"owner{admin}", admin))
            for slug, mode, limit, admin in wikis:
                conn.execute("INSERT INTO instances (id, account_id, subdomain, domain_mode, storage_limit_mb, "
                             "created_at) VALUES (?, ?, ?, ?, ?, 'x')",
                             (f"id-{slug}-{mode}", f"owner{int(admin)}", slug, mode, limit))
    finally:
        conn.close()


def test_platform_backups_read_each_wikis_storage_limit(tmp_path):
    """The live quota's rules: the wiki's own limit, the default without one, none for administrators' and apex
    wikis or a limit of 0."""
    from bananawiki.hosting.runtime import platform_backup

    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    _wiki_rows(runtime, ("acme", "hosting", 3, False), ("beta", "hosting", None, False),
               ("free", "hosting", 0, False), ("boss", "hosting", 5, True), ("site", "apex", None, False))
    cfg = runtime._cfg()
    assert platform_backup._storage_limits(cfg, Path(cfg.database_path)) == {
        "acme": 3 * 1024 ** 2, "beta": cfg.limits.storage_limit_mb * 1024 ** 2, "free": None, "boss": None,
        "site__apex": None}


def test_a_sparse_file_cannot_make_the_platform_backup_run_out_of_space(tmp_path, monkeypatch):
    """A sparse file costs the wiki's quota nothing, but the backup wrote it at its full length (stored, from
    64 MiB): one wiki could make every platform backup run out of space, hosting.db and the other wikis
    included. Each wiki's files now get its storage limit, and some slack, in the archive."""
    from bananawiki.hosting.runtime import platform_backup

    monkeypatch.setattr(platform_backup, "BUDGET_SLACK_BYTES", 0)
    runtime, _agent, base = _two_wikis(tmp_path)
    _wiki_rows(runtime, ("acme", "hosting", 1, False), ("beta", "hosting", None, False))
    for name in ("acme", "beta"):
        with open(base / name / "storage" / "uploads" / "sparse.bin", "wb") as handle:
            handle.truncate(4 * 1024 ** 2)
        assert (base / name / "storage" / "uploads" / "sparse.bin").stat().st_size == 4 * 1024 ** 2
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert {"hosting.db", "secret_key", "instances/acme/bananawiki.db",
            "instances/beta/storage/uploads/sparse.bin"} <= names
    assert "instances/acme/storage/uploads/sparse.bin" not in names, "beyond the wiki's 1 MB limit"
    assert manifest["tenants"] == ["beta"]
    [skipped] = manifest["skipped"]
    assert (skipped["tenant"], skipped["code"]) == ("acme", "too_large")
    assert "storage limit" in skipped["detail"] and "sparse.bin" in skipped["detail"]
    status = runtime.platform_backup_status()
    assert status["finished_at"] and status["complete_at"] is None


def test_hard_links_to_one_file_go_into_the_platform_backup_once(tmp_path):
    """Each hard link cost the backup a full copy of the file, the wiki's quota only one: a wiki could make every
    platform backup run out of space with links to one upload."""
    runtime, _agent, base = _two_wikis(tmp_path)
    uploads = base / "acme" / "storage" / "uploads"
    data = os.urandom(200 * 1024)
    (uploads / "r.bin").write_bytes(data)
    for index in range(50):
        os.link(uploads / "r.bin", uploads / f"r{index}.bin")
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
        [copy] = [name for name in names if name.startswith("instances/acme/storage/uploads/r")]
        assert archive.read(copy) == data
    assert {"hosting.db", "instances/acme/bananawiki.db", "instances/acme/storage/uploads/a.png",
            "instances/beta/storage/uploads/a.png"} <= names
    assert manifest["tenants"] == ["beta"]
    [skipped] = manifest["skipped"]
    assert (skipped["tenant"], skipped["code"]) == ("acme", "failed")
    assert skipped["detail"].startswith("50 further hard link(s) to files already saved left out: ")
    assert runtime.platform_backup_status()["complete_at"] is None


def test_restore_names_the_wiki_whose_folders_it_cannot_prepare(tmp_path):
    """Backups written before such files were kept out can hold one: the restore says which wiki it is."""
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    part = tmp_path / "layout.zip"
    with zipfile.ZipFile(part, "w") as archive:
        archive.write(runtime._cfg().database_path, "hosting.db")
        archive.writestr("instances/acme/storage", b"not a folder")
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([part])
    assert error.value.code == "db_unsafe" and error.value.detail == "acme: storage is not a directory"
    assert not (Path(runtime._cfg().instances_dir) / "acme").exists(), "nothing was installed"


def test_a_failed_archive_write_still_fails_the_whole_backup(tmp_path, monkeypatch):
    """Whichever wiki is being copied, the encrypted stream is broken after a failed write: never skip and go on."""
    from bananawiki.hosting.runtime import platform_backup

    runtime, _agent, _base = _two_wikis(tmp_path)
    add_tenant = platform_backup._add_tenant
    write = crypto.EncryptedOutput.write
    full = []

    def copying(*args, **kwargs):
        full.append(True)
        return add_tenant(*args, **kwargs)

    def write_once_full(self, data):
        if full:
            full.clear()
            raise OSError(errno.ENOSPC, "No space left on device")
        return write(self, data)

    monkeypatch.setattr(platform_backup, "_add_tenant", copying)
    monkeypatch.setattr(crypto.EncryptedOutput, "write", write_once_full)
    work = _work(tmp_path)
    with pytest.raises(RuntimeFailure) as error:
        runtime.export_platform(work)
    assert error.value.code == "no_space"
    assert not list(work.iterdir()), "no damaged archive is left"
    assert runtime.platform_backup_status() == {}


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("database is locked"), OSError(errno.EIO, "I/O error")],
                         ids=["sqlite", "storage"])
def test_platform_backup_errors_are_runtime_failures(tmp_path, monkeypatch, failure):
    """The routes report a RuntimeFailure; any other error was a server error that left the work folder behind."""
    from bananawiki.hosting.runtime import platform_backup

    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)

    def broken(_cfg, _target):
        raise failure

    monkeypatch.setattr(platform_backup, "_snapshot_hosting_db", broken)
    work = _work(tmp_path)
    with pytest.raises(RuntimeFailure) as error:
        runtime.export_platform(work)
    assert error.value.code == "failed"
    assert not list(work.iterdir())


def test_a_backup_that_saves_no_wiki_fails_as_a_whole(tmp_path, monkeypatch):
    """Every wiki failing before any of its data was copied, while the runtime answers, points at the platform
    (the sandbox, storage): a backup holding no wiki at all must not count as one."""
    runtime, agent, _base = _two_wikis(tmp_path)

    def failing(_args):
        raise AgentError("timeout", "the snapshot did not finish")

    monkeypatch.setattr(agent, "tenant_task", failing)
    work = _work(tmp_path)
    with pytest.raises(RuntimeFailure) as error:
        runtime.export_platform(work)
    assert error.value.code == "timeout"
    assert "no wiki could be saved" in error.value.detail
    assert not list(work.iterdir())
    assert runtime.platform_backup_status() == {}
    assert agent.ops("ping"), "the runtime still answered: each wiki was tried"


def test_a_wiki_that_keeps_itself_out_is_no_platform_fault(tmp_path, monkeypatch):
    """acme's snapshot times out, but beta's task ran and found its database replaced by a link: the platform
    works, so the backup is kept, holding beta's files, and reported incomplete."""
    runtime, agent, base = _two_wikis(tmp_path)
    task = agent.tenant_task

    def failing(args):
        if args["tenant"] == "acme":
            raise AgentError("timeout", "the snapshot did not finish")
        return task(args)

    monkeypatch.setattr(agent, "tenant_task", failing)
    (base / "beta" / "bananawiki.db").unlink()
    (base / "beta" / "bananawiki.db").symlink_to(base / "acme" / "bananawiki.db")
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert {"hosting.db", "instances/beta/storage/uploads/a.png"} <= names
    assert manifest["tenants"] == []
    assert [(item["tenant"], item["code"]) for item in manifest["skipped"]] == [("acme", "timeout"),
                                                                               ("beta", "db_unsafe")]
    assert runtime.platform_backup_status()["complete_at"] is None


def _plugin_cache(root: Path) -> None:
    (root / "external_plugins" / "tool").mkdir(parents=True, exist_ok=True)
    (root / "external_plugins" / "tool" / "cache-12:00.json").write_text("{}")


def _database_link(root: Path) -> None:
    elsewhere = root.parent.parent / "elsewhere.db"
    (root / "bananawiki.db").rename(elsewhere)
    (root / "bananawiki.db").symlink_to(elsewhere)


@pytest.mark.parametrize("hostile", [_plugin_cache, _database_link], ids=["file-name", "database-link"])
def test_a_lone_wiki_that_keeps_itself_out_still_leaves_a_platform_backup(tmp_path, monkeypatch, hostile):
    """On a platform with one wiki, a file name of that wiki's own (a plugin's cache) failed every platform
    backup: hosting.db was never saved, nothing was recorded, and Drive retried every hour, for ever."""
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    provision(runtime, make_spec())
    root = Path(runtime._cfg().instances_dir) / "acme"
    (root / "storage" / "uploads" / "a.png").write_bytes(b"acme")
    hostile(root)
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
    assert {"hosting.db", "secret_key", "instances/acme/storage/uploads/a.png"} <= names
    assert manifest["tenants"] == [] and [item["tenant"] for item in manifest["skipped"]] == ["acme"]
    status = runtime.platform_backup_status()
    assert status["finished_at"] and status["complete_at"] is None
    assert [item["tenant"] for item in status["skipped"]] == ["acme"]

    cfg = runtime._cfg()
    uploads = []
    settings = gdrive.DriveSettings(True, "/creds.json", "folder", 7, (0, 0))
    monkeypatch.setattr(runtime, "_drive_settings", lambda: settings)
    monkeypatch.setattr(gdrive, "service", lambda _settings: FakeDrive())
    monkeypatch.setattr(gdrive, "upload", lambda _client, _settings, path: uploads.append(path.name) or path.name)
    runtime.maintenance_tick()
    assert len(uploads) == 1 and "last_success" in runtime._drive_state(cfg)
    state = runtime._drive_state(cfg)
    earlier = datetime.fromisoformat(state["last_attempt"]) - timedelta(seconds=GDRIVE_RETRY_SECONDS + 60)
    runtime._save_drive_state(cfg, last_attempt=earlier.isoformat())
    runtime.maintenance_tick()
    assert len(uploads) == 1, "the day's backup exists: not retried every hour"


def _damaged_database(_runtime, root: Path, _monkeypatch) -> None:
    """Every page after the first overwritten: SQLite opens the file, the copy then fails inside the sandbox."""
    database = root / "bananawiki.db"
    size = database.stat().st_size
    assert size > 4096
    with database.open("r+b") as handle:
        handle.seek(4096)
        handle.write(b"\xa5" * (size - 4096))


def _removed_copy(runtime, root: Path, monkeypatch) -> None:
    """The wiki deletes the database copy its sandbox made, before the host opens it."""
    backup_copy = runtime._backup_copy

    def removed(tenant):
        copy = backup_copy(tenant)
        if tenant == root.name:
            (root / copy).unlink()
        return copy

    monkeypatch.setattr(runtime, "_backup_copy", removed)


@pytest.mark.parametrize("wikis", [("acme",), ("acme", "beta")], ids=["one-wiki", "two-wikis"])
@pytest.mark.parametrize("hostile", [_damaged_database, _removed_copy], ids=["damaged-database", "removed-copy"])
def test_wikis_whose_own_database_cannot_be_copied_still_leave_a_platform_backup(tmp_path, monkeypatch, hostile,
                                                                                 wikis):
    """The tenant task ran and found the database damaged, or the wiki removed the copy: the wikis' doing, not the
    platform's. When no other wiki was saved, hosting.db and the session key were never backed up, nothing was
    recorded and Drive retried every hour, for ever. Their uploads, which nothing else could replace, were left
    out as well."""
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    base = Path(runtime._cfg().instances_dir)
    for port, name in enumerate(wikis, 6001):
        provision(runtime, make_spec(name, port=port))
        (base / name / "storage" / "uploads" / "a.png").write_bytes(name.encode())
        hostile(runtime, base / name, monkeypatch)
    path = runtime.export_platform(_work(tmp_path))
    with _backup(runtime, path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup_manifest.json"))
        assert archive.read("secret_key").decode() == runtime._cfg().secret_key
        for name in wikis:
            assert archive.read(f"instances/{name}/storage/uploads/a.png") == name.encode()
            assert f"instances/{name}/bananawiki.db" not in names
    assert "hosting.db" in names
    assert manifest["tenants"] == []
    assert [(item["tenant"], item["code"]) for item in manifest["skipped"]] == [(name, "failed") for name in wikis]
    status = runtime.platform_backup_status()
    assert status["finished_at"] and status["complete_at"] is None
    assert [item["tenant"] for item in status["skipped"]] == list(wikis)


def test_an_unreachable_runtime_fails_the_backup_and_drive_retries_within_the_hour(tmp_path, monkeypatch):
    """Every snapshot fails when the agent (or Docker) is down: not the wikis' fault, so nothing is skipped."""
    runtime, agent, _base = _two_wikis(tmp_path)
    cfg = runtime._cfg()
    uploads = []
    settings = gdrive.DriveSettings(True, "/creds.json", "folder", 7, (0, 0))
    monkeypatch.setattr(runtime, "_drive_settings", lambda: settings)
    monkeypatch.setattr(gdrive, "service", lambda _settings: FakeDrive())
    monkeypatch.setattr(gdrive, "upload", lambda _client, _settings, path: uploads.append(path.name) or path.name)
    call = agent.call
    down = [True]

    def unreachable(op, args=None):
        if down[0]:
            agent.calls.append((op, args or {}))
            raise AgentError("unavailable", "The runtime agent is not reachable: ConnectionRefusedError")
        return call(op, args)

    monkeypatch.setattr(agent, "call", unreachable)
    agent.calls.clear()
    work = _work(tmp_path)
    with pytest.raises(RuntimeFailure) as error:
        runtime.export_platform(work)
    assert error.value.code == "unavailable" and "stopped at acme" in error.value.detail
    assert not list(work.iterdir())
    assert not [args for op, args in agent.calls if op == "tenant.task" and args["tenant"] == "beta"], \
        "no further wiki is tried"
    with pytest.raises(RuntimeFailure) as error:
        runtime.gdrive_backup_now()
    assert error.value.code == "unavailable" and uploads == []
    assert runtime.platform_backup_status() == {}
    state = runtime._drive_state(cfg)
    assert "last_attempt" in state and "last_success" not in state

    down[0] = False
    runtime.maintenance_tick()
    assert uploads == [], "not again within the hour"
    earlier = datetime.fromisoformat(state["last_attempt"]) - timedelta(seconds=GDRIVE_RETRY_SECONDS + 60)
    runtime._save_drive_state(cfg, last_attempt=earlier.isoformat())
    runtime.maintenance_tick()
    assert len(uploads) == 1, "retried after an hour, not the next day"
    assert "last_success" in runtime._drive_state(cfg)
    assert runtime.platform_backup_status()["skipped"] == []


@pytest.mark.parametrize("skipped", [3, True, "acme", {"tenant": "acme"}], ids=["int", "bool", "str", "dict"])
def test_a_malformed_backup_status_file_is_ignored(tmp_path, skipped):
    runtime, _agent = make_runtime(tmp_path)
    folder = Path(runtime._cfg().platform_state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "platform-backup.json").write_text(json.dumps({"finished_at": "2026-10-08T03:00:00+00:00",
                                                             "complete_at": 7, "skipped": skipped}))
    assert runtime.platform_backup_status() == {"finished_at": "2026-10-08T03:00:00+00:00", "complete_at": None,
                                                "skipped": []}


def test_settings_page_shows_wikis_missing_from_the_latest_backup(web, runtime, make_account, login):
    login(web, make_account(admin=True))
    page = web.get("/admin/settings").get_data(as_text=True)
    assert "No complete platform backup has been recorded yet." in page
    runtime.backup_status = {"finished_at": "2026-10-08T03:00:00+00:00", "complete_at": "2026-10-01T03:00:00+00:00",
                             "skipped": [{"tenant": "acme", "code": "timeout", "detail": "the snapshot did not finish"}]}
    page = web.get("/admin/settings").get_data(as_text=True)
    assert "Latest complete platform backup: 2026-10-01 03:00 UTC." in page
    assert "(2026-10-08 03:00 UTC) did not save these wikis in full" in page
    assert "<code>acme</code>: The operation took too long." in page
    assert UnavailableRuntime().platform_backup_status() == {}, "the page also renders without a runtime backend"


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


@pytest.mark.parametrize("order", ["file-first", "folder-first"])
def test_restore_names_a_path_used_as_both_a_file_and_a_folder(tmp_path, order):
    """Backups written before the copy's name was reserved can hold one: the restore says which path it is,
    instead of reporting a part that cannot be read."""
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    part = tmp_path / "conflict.zip"
    members = [("instances/acme/bananawiki.db", b"database"), ("instances/acme/bananawiki.db/x.png", b"x")]
    with zipfile.ZipFile(part, "w") as archive:
        archive.write(runtime._cfg().database_path, "hosting.db")
        for name, data in members if order == "file-first" else reversed(members):
            archive.writestr(name, data)
    with pytest.raises(RuntimeFailure) as error:
        runtime.restore_platform([part])
    assert error.value.code == "archive_invalid"
    assert "instances/acme/bananawiki.db" in error.value.detail and "both a file and a folder" in error.value.detail
    assert not list(Path(runtime._cfg().instances_dir).glob("acme")), "nothing was installed"


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA], ids=["bzip2", "lzma"])
def test_restore_refuses_members_whose_decompression_cannot_be_bounded(tmp_path, method):
    """The same filter as tenant imports: bzip2 and LZMA members never reach a decompressor."""
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    part = tmp_path / "packed.zip"
    with zipfile.ZipFile(part, "w") as archive:
        archive.write(runtime._cfg().database_path, "hosting.db", compress_type=method)
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


class TimedDrive(FakeDrive):
    """Drive's side of retention: files dated by Drive's own clock (*skew* from the portal's), listed by the
    query's createdTime cutoff."""

    def __init__(self, skew: timedelta):
        super().__init__()
        self.skew = skew
        self.held: dict[str, tuple[str, datetime]] = {}
        self.uploads = 0

    def add(self, name: str, created: datetime) -> None:
        self.held[f"id-{len(self.held) + len(self.deleted)}"] = (name, created)

    def upload(self, _client, _settings, _path: Path) -> str:
        self.uploads += 1
        name = f"bananawiki_hosting_backup_{self.uploads}.zip.bwenc"
        self.add(name, datetime.now(UTC) + self.skew)
        return name

    def list(self, q, **_kwargs):
        cutoff = datetime.strptime(q.split("createdTime < '")[1][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        hits = [{"id": key, "name": name} for key, (name, created) in self.held.items() if created < cutoff]
        return SimpleNamespace(execute=lambda: {"files": hits})

    def delete(self, fileId):
        del self.held[fileId]
        return super().delete(fileId)

    def names(self) -> set[str]:
        return {name for name, _created in self.held.values()}


@pytest.mark.parametrize("skew", [timedelta(0), timedelta(seconds=-5)], ids=["clocks-agree", "drive-clock-behind"])
def test_drive_retention_keeps_the_latest_complete_backup(tmp_path, monkeypatch, skew):
    """An incomplete upload must not let retention delete the backups that still hold the missing wiki, least of
    all the complete backup itself, which Drive dates by its own clock."""
    runtime, agent, _base = _two_wikis(tmp_path)
    cfg = runtime._cfg()
    drive = TimedDrive(skew)
    settings = gdrive.DriveSettings(True, "/creds.json", "folder", 7, (0, 0))
    monkeypatch.setattr(runtime, "_drive_settings", lambda: settings)
    monkeypatch.setattr(gdrive, "service", lambda _settings: drive)
    monkeypatch.setattr(gdrive, "upload", drive.upload)
    drive.add("bananawiki_hosting_backup_ancient.zip.bwenc", datetime.now(UTC) - timedelta(days=40))

    def a_day_later():
        drive.held = {key: (name, created - timedelta(days=1)) for key, (name, created) in drive.held.items()}
        complete = runtime._drive_state(cfg).get("last_complete")
        if complete:
            runtime._save_drive_state(cfg, last_complete=(datetime.fromisoformat(complete)
                                                          - timedelta(days=1)).isoformat())

    agent.failures["tenant.task"] = AgentError("timeout", "the snapshot did not finish")
    assert "without acme" in runtime.gdrive_backup_now()
    assert drive.names() == {"bananawiki_hosting_backup_ancient.zip.bwenc", "bananawiki_hosting_backup_1.zip.bwenc"}, \
        "with no complete backup known, nothing is removed"
    a_day_later()
    runtime.gdrive_backup_now()
    assert drive.names() == {"bananawiki_hosting_backup_1.zip.bwenc", "bananawiki_hosting_backup_2.zip.bwenc"}, \
        "a complete backup applies the usual retention"
    # The wiki breaks for ten days: one incomplete backup a day.
    for _day in range(10):
        a_day_later()
        agent.failures["tenant.task"] = AgentError("timeout", "the snapshot did not finish")
        assert "without acme" in runtime.gdrive_backup_now()
    held = drive.names()
    assert "bananawiki_hosting_backup_2.zip.bwenc" in held, "the latest complete backup is never pruned"
    assert "bananawiki_hosting_backup_1.zip.bwenc" not in held, "what is older than it still expires"
    assert {f"bananawiki_hosting_backup_{n}.zip.bwenc" for n in range(3, 13)} <= held, "everything newer is kept"


@pytest.mark.parametrize("earlier", [False, True], ids=["first", "after-a-complete-one"])
def test_a_failed_drive_upload_is_not_recorded_as_a_backup(tmp_path, monkeypatch, earlier):
    """The archive is deleted when the upload fails: no backup exists anywhere, complete or not."""
    runtime, _agent = make_runtime(tmp_path)
    _hosting_db(runtime)
    provision(runtime, make_spec())
    if earlier:
        state = {"finished_at": "2026-10-01T03:00:00+00:00", "complete_at": "2026-10-01T03:00:00+00:00",
                 "skipped": []}
        folder = Path(runtime._cfg().platform_state_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "platform-backup.json").write_text(json.dumps(state))
    before = runtime.platform_backup_status()
    settings = gdrive.DriveSettings(True, "/creds.json", "folder", 7, (0, 0))
    monkeypatch.setattr(runtime, "_drive_settings", lambda: settings)
    monkeypatch.setattr(gdrive, "service", lambda _settings: FakeDrive())

    def failing(_client, _settings, _path):
        raise RuntimeFailure("failed", "the upload failed: HttpError")

    monkeypatch.setattr(gdrive, "upload", failing)
    with pytest.raises(RuntimeFailure):
        runtime.gdrive_backup_now()
    assert runtime.platform_backup_status() == before
    assert "last_complete" not in runtime._drive_state(runtime._cfg())


def test_drive_message_names_at_most_ten_missing_wikis(tmp_path, monkeypatch):
    """The message is flashed into a cookie session: hundreds of names would not fit."""
    from bananawiki.hosting.runtime import platform_backup

    runtime, _agent = make_runtime(tmp_path)
    settings = gdrive.DriveSettings(True, "/creds.json", "folder", 7, (0, 0))
    monkeypatch.setattr(runtime, "_drive_settings", lambda: settings)
    monkeypatch.setattr(gdrive, "service", lambda _settings: FakeDrive())
    monkeypatch.setattr(gdrive, "upload", lambda _client, _settings, path: path.name)
    skipped = tuple({"tenant": f"wiki{n:03d}", "code": "timeout", "detail": ""} for n in range(250))
    monkeypatch.setattr(runtime, "_export_platform",
                        lambda _cfg, work: platform_backup.Exported(work / "backup.bwenc", datetime.now(UTC), skipped))
    message = runtime.gdrive_backup_now()
    assert "wiki009 and 240 more" in message and "wiki010" not in message


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
