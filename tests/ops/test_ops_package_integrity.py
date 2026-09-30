"""Packages and snapshots against tenants that keep writing their directories while root captures them.

Tenant containers run again (and can rewrite, swap or replace any entry in
``data/instances/<tenant>/``) before the package is written from the
snapshot, and while the controller walks and copies the tree.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from ops_fakes import FakeSystem, Upstream, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops import files as files_module
from bananawiki.ops import manager as manager_module
from bananawiki.ops import profile
from bananawiki.ops.files import read_json, read_package, write_package
from bananawiki.ops.manager import Manager
from bananawiki.ops.snapshot import Snapshot

UPLOAD = "data/instances/acme/storage/uploads/big.bin"


@pytest.fixture
def fake_system(tmp_path) -> FakeSystem:
    return FakeSystem(tmp_path / "host")


@pytest.fixture
def hosting(tmp_path, fake_system):
    upstream = Upstream(tmp_path)
    root = tmp_path / "opt/bananawiki"
    settings = legacy_install(root, fake_system, upstream, upstream.commit("1.6"), mode="hosting")
    tenant = root / "data/instances/acme"
    tenant.mkdir(parents=True)
    (tenant / "page.txt").write_text("content")
    profile.prepare_hosting_storage(root / "data")
    return root, settings, tenant


def tenant_snapshot(root: Path) -> Snapshot:
    data = root / "data"
    return Snapshot.create(root, lambda path: profile.inside_tenant(path, data))


def tenant_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "opt/bananawiki"
    for name in ("config", "data", "site", "staging", "backups"):
        (root / name).mkdir(parents=True)
    tenant = root / "data/instances/acme"
    (tenant / "storage/uploads").mkdir(parents=True)
    return root, tenant


def swap_after_walk(monkeypatch, change) -> None:
    """Let *change* run between the walk that lists the tree and the copy of each entry."""
    real = Snapshot._sources

    def listed_then_changed(self):
        sources = real(self)
        change()
        return sources

    monkeypatch.setattr(Snapshot, "_sources", listed_then_changed)


# Finding: a tenant could make every package unrestorable -----------------------------


def test_a_tenant_rewriting_its_file_while_the_package_is_written_cannot_spoil_it(hosting, fake_system,
                                                                                  monkeypatch):
    """The package is written after tenants resume; the snapshot used to hard-link their files."""
    root, _settings, tenant = hosting
    big = tenant / "storage/uploads/big.bin"
    big.write_bytes(b"A" * 1_000_000)  # above the size that used to be hard-linked

    def tenant_writes_meanwhile(real):
        def wrapper(name, *args):
            result = real(name, *args)
            if str(name).endswith(UPLOAD):  # the package writer reads the staged copy of the file
                with open(big, "r+b") as handle:  # the running tenant rewrites its own file in place
                    handle.write(b"B")
            return result
        return wrapper

    # Hashing (1.6.0: a first pass) and opening for the archive: the tenant writes in between.
    monkeypatch.setattr(files_module, "digest_file", tenant_writes_meanwhile(files_module.digest_file))
    monkeypatch.setattr(files_module, "open_regular",
                        tenant_writes_meanwhile(getattr(files_module, "open_regular", None)), raising=False)
    package = Manager(root, system=fake_system).backup()
    monkeypatch.undo()
    with read_package(package, "BananaWiki", root / "staging") as (tree, manifest):
        assert (tree / UPLOAD).read_bytes() == b"A" * 1_000_000
        assert UPLOAD in manifest["files"]
    assert big.read_bytes()[:1] == b"B"


def test_tenant_files_are_copied_into_the_snapshot_never_linked(tmp_path):
    root, tenant = tenant_root(tmp_path)
    (tenant / "storage/uploads/big.bin").write_bytes(b"t" * 200_000)
    (root / "data/uploads").mkdir()
    (root / "data/uploads/platform.bin").write_bytes(b"p" * 200_000)
    snapshot = tenant_snapshot(root)
    index = read_json(snapshot.path / "index.json")["files"]
    assert index[UPLOAD]["method"] == "copy"
    assert (snapshot.path / UPLOAD).stat().st_ino != (tenant / "storage/uploads/big.bin").stat().st_ino
    # Files outside tenant directories are still hard-linked (they are not rewritten in place).
    assert index["data/uploads/platform.bin"]["method"] == "link"
    assert (snapshot.path / "data/uploads/platform.bin").stat().st_ino == (
        root / "data/uploads/platform.bin").stat().st_ino
    snapshot.remove()


def test_the_manifest_checksums_the_bytes_that_were_archived(tmp_path, monkeypatch):
    """A file changed while it is archived still yields a package that agrees with its manifest."""
    source = tmp_path / "live.bin"
    source.write_bytes(b"A" * 300_000)
    real_read = files_module._HashingReader.read

    def changed_mid_stream(self, size=-1):
        chunk = real_read(self, size)
        with open(source, "r+b") as handle:
            handle.seek(200_000)
            handle.write(b"B" * 100_000)
        return chunk

    monkeypatch.setattr(files_module._HashingReader, "read", changed_mid_stream)
    package = write_package(tmp_path / "p.tar.gz", {"product": "BananaWiki"}, [(source, "data/live.bin")])
    monkeypatch.undo()
    with read_package(package, "BananaWiki", tmp_path / "stage") as (tree, manifest):
        assert manifest["files"]["data/live.bin"]["size"] == 300_000
        assert (tree / "data/live.bin").read_bytes().endswith(b"B" * 100_000)


def corrupt_packages(monkeypatch) -> None:
    """What the race produced: an archived file that no longer matches the manifest."""
    real = manager_module.write_package

    def write_then_corrupt(destination, manifest, inputs):
        package = real(destination, manifest, inputs)
        members = []
        with tarfile.open(package) as archive:
            for member in archive.getmembers():
                content = archive.extractfile(member).read()
                if member.name.startswith("data/uploads/"):
                    content = b"B" + content[1:]
                members.append((member, content))
        package.unlink()
        with tarfile.open(package, "w:gz") as archive:
            for member, content in members:
                archive.addfile(member, io.BytesIO(content))
        return package

    monkeypatch.setattr(manager_module, "write_package", write_then_corrupt)


def test_a_package_that_would_not_restore_is_refused_and_removed(hosting, fake_system, monkeypatch):
    root, _settings, _tenant = hosting
    corrupt_packages(monkeypatch)
    # ``backups run`` uploads (and then prunes the remote series) only what backup() returns.
    with pytest.raises(ValueError, match="failed verification"):
        Manager(root, system=fake_system).backup()
    assert not list((root / "backups").glob("*.tar.gz"))
    assert not list((root / "staging").glob("snapshot-*"))


def test_an_update_never_records_an_unrestorable_rollback_package(tmp_path, fake_system, monkeypatch):
    upstream = Upstream(tmp_path)
    root = tmp_path / "opt/bananawiki"
    legacy_install(root, fake_system, upstream, upstream.commit("1.4 release", modern=False))
    new = upstream.commit("1.6 release")
    corrupt_packages(monkeypatch)
    result = Manager(root, system=fake_system).update()
    assert result["outcome"] == "complete" and result["revision"] == new
    assert "failed verification" in result["backup_warning"]
    assert "backup" not in result
    assert not (root / "config/last-update.json").exists()
    assert not list((root / "backups").glob("*.tar.gz"))


# Finding: tenant paths opened by name while tenants run --------------------------------


def test_a_directory_swapped_for_a_link_after_the_walk_is_not_followed(tmp_path, monkeypatch):
    root, tenant = tenant_root(tmp_path)
    (tenant / "storage/uploads/big.bin").write_bytes(b"t" * 100_000)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"root-only secret")

    def swap() -> None:
        (tenant / "storage/uploads").rename(tenant / "storage/moved")
        (tenant / "storage/uploads").symlink_to(outside)

    swap_after_walk(monkeypatch, swap)
    snapshot = tenant_snapshot(root)
    index = read_json(snapshot.path / "index.json")["files"]
    assert UPLOAD not in index
    assert not (snapshot.path / UPLOAD).exists()
    snapshot.remove()


def test_fifos_and_links_swapped_in_after_the_walk_neither_block_nor_leak(tmp_path, monkeypatch):
    """SQLite opened tenant databases by name: a FIFO put there blocked root under the maintenance lock."""
    root, tenant = tenant_root(tmp_path)
    database = sqlite3.connect(tenant / "bananawiki.db")
    database.execute("CREATE TABLE pages (title TEXT)")
    database.execute("INSERT INTO pages VALUES ('Home')")
    database.commit()
    database.close()
    (tenant / "other.db").write_bytes(b"x")
    (tenant / "notes.txt").write_text("notes")
    secret = tmp_path / "secret.db"
    leaked = sqlite3.connect(secret)
    leaked.execute("CREATE TABLE secret (value TEXT)")
    leaked.commit()
    leaked.close()
    outside = tmp_path / "outside.txt"
    outside.write_text("root-only secret")

    def swap() -> None:
        (tenant / "other.db").unlink()
        os.mkfifo(tenant / "other.db")
        (tenant / "notes.txt").unlink()
        (tenant / "notes.txt").symlink_to(outside)
        os.mkfifo(tenant / "bananawiki.db-wal")
        (tenant / "bananawiki.db-journal").symlink_to(secret)

    swap_after_walk(monkeypatch, swap)
    done: list[Snapshot] = []
    worker = threading.Thread(target=lambda: done.append(tenant_snapshot(root)), daemon=True)
    worker.start()
    worker.join(30)
    assert done, "capturing the tenant tree blocked"
    snapshot = done[0]
    index = read_json(snapshot.path / "index.json")["files"]
    assert "data/instances/acme/other.db" not in index
    assert "data/instances/acme/notes.txt" not in index
    assert index["data/instances/acme/bananawiki.db"]["method"] == "sqlite"
    copy = sqlite3.connect(snapshot.path / "data/instances/acme/bananawiki.db")
    try:
        assert copy.execute("SELECT title FROM pages").fetchall() == [("Home",)]
        assert copy.execute("SELECT name FROM sqlite_master WHERE name = 'secret'").fetchall() == []
    finally:
        copy.close()
    # SQLite never opened the live database, so root created nothing in the tenant's directory.
    assert not (tenant / "bananawiki.db-shm").exists()
    assert outside.read_text() == "root-only secret"
    snapshot.remove()


def test_a_database_swapped_for_a_link_after_the_walk_is_not_captured(tmp_path, monkeypatch):
    root, tenant = tenant_root(tmp_path)
    database = sqlite3.connect(tenant / "bananawiki.db")
    database.execute("CREATE TABLE pages (title TEXT)")
    database.commit()
    database.close()
    secret = tmp_path / "platform.db"
    platform = sqlite3.connect(secret)
    platform.execute("CREATE TABLE secret (value TEXT)")
    platform.commit()
    platform.close()

    def swap() -> None:
        (tenant / "bananawiki.db").unlink()
        (tenant / "bananawiki.db").symlink_to(secret)

    swap_after_walk(monkeypatch, swap)
    snapshot = tenant_snapshot(root)
    assert "data/instances/acme/bananawiki.db" not in read_json(snapshot.path / "index.json")["files"]
    snapshot.remove()


def test_tenant_usage_does_not_enter_a_directory_swapped_for_a_link(tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import tenantfs

    tenant = tmp_path / "acme"
    (tenant / "folder").mkdir(parents=True)
    (tenant / "folder/small.bin").write_bytes(b"s" * 10)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"h" * 1_000_000)
    calls: list[int] = []

    def monotonic() -> float:  # consulted once per directory: swap once the walk is under way
        calls.append(1)
        if len(calls) == 3:
            (tenant / "folder").rename(tmp_path / "moved")
            (tenant / "folder").symlink_to(outside)
        return float(len(calls))

    monkeypatch.setattr(tenantfs, "time", SimpleNamespace(monotonic=monotonic))
    assert tenantfs.usage(tenant, deadline_seconds=1000) < 1_000_000
    assert (tenant / "folder").is_symlink()
