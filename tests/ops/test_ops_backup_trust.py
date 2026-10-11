"""What a backup may trust: the code a restore deploys, the files it treats as databases, the remote series.

* A package's bundled ``source.tar.gz`` runs as the service and, through the
  installed command, as root: a restore takes the code from this server's
  release or update source, or from the package only once the operator
  confirms its revision.
* Only the databases BananaWiki keeps are checked as SQLite databases; a
  user's upload called ``notes.db`` is an opaque file.
* An append-only remote series never deletes snapshots while uploading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest
from ops_fakes import FakeSystem, Upstream, git, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops import cli as ops_cli
from bananawiki.ops import profile
from bananawiki.ops.backups import commands, crypto, store
from bananawiki.ops.backups.files import write_json as write_backup_json
from bananawiki.ops.files import read_json, read_package, write_package
from bananawiki.ops.manager import Manager
from bananawiki.ops.snapshot import Snapshot

FORGED = "f" * 40


@pytest.fixture
def fake_system(tmp_path) -> FakeSystem:
    return FakeSystem(tmp_path / "host")


@pytest.fixture
def upstream(tmp_path) -> Upstream:
    return Upstream(tmp_path)


@pytest.fixture
def installed(tmp_path, fake_system, upstream):
    root = tmp_path / "opt/bananawiki"
    revision = upstream.commit("1.6 release")
    legacy_install(root, fake_system, upstream, revision)
    return Manager(root, system=fake_system), revision


def bundled(package: Path) -> str:
    with tarfile.open(package) as archive:
        return json.load(archive.extractfile("manifest.json"))["revision"]


def repack(package: Path, destination: Path, edit) -> Path:
    """Rewrite a package and its (unsigned) manifest, as anyone who can replace the file could."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with tarfile.open(package) as archive:
            archive.extractall(root, filter="data")
        manifest = json.loads((root / "manifest.json").read_text())
        (root / "manifest.json").unlink()
        edit(root, manifest)
        manifest = {key: value for key, value in manifest.items() if key not in {"files", "schema"}}
        inputs = [(path, path.relative_to(root).as_posix()) for path in sorted(root.rglob("*")) if path.is_file()]
        return write_package(destination, manifest, inputs)


def foreign_source(root: Path) -> None:
    """Code the server never fetched from its update source."""
    with tempfile.TemporaryDirectory() as directory:
        for name, text in (("banana", "# not the reviewed entry point\n"), ("LICENSE", "x\n"),
                           ("README", "attacker\n"), ("requirements.txt", "")):
            Path(directory, name).write_text(text)
        (root / "source.tar.gz").unlink()
        with tarfile.open(root / "source.tar.gz", "w:gz") as output:
            for name in ("banana", "LICENSE", "README", "requirements.txt"):
                output.add(Path(directory, name), arcname=name)


def unknown_revision(root: Path, manifest: dict, value: str = FORGED) -> None:
    foreign_source(root)
    manifest["revision"] = value
    settings = json.loads((root / "config/installation.json").read_text())
    (root / "config/installation.json").write_text(json.dumps({**settings, "revision": value}))


# The source a restore deploys ---------------------------------------------------------------


def test_restore_never_deploys_bundled_source_it_cannot_verify(installed, tmp_path):
    manager, revision = installed
    forged = repack(manager.backup(), tmp_path / "forged.tar.gz", unknown_revision)
    with pytest.raises(ValueError, match=f"revision {FORGED}.*--trust-source {FORGED}"):
        manager.restore(forged)
    assert not (manager.root / "releases" / FORGED).exists()
    assert manager.settings()["revision"] == revision
    assert not list((manager.root / "backups").glob("before-restore-*"))
    with pytest.raises(ValueError, match="--trust-source"):
        manager.restore(forged, trust_source="e" * 40)
    manager.restore(forged, trust_source=FORGED)
    assert (manager.root / "current/README").read_text() == "attacker\n"


def test_a_new_server_shows_the_bundled_revision_and_requires_confirmation(installed, tmp_path):
    manager, revision = installed
    package = manager.backup()
    restored = Manager(tmp_path / "other/bananawiki", system=FakeSystem(tmp_path / "other-host"))
    with pytest.raises(ValueError, match=f"revision {revision}.*--trust-source {revision}"):
        restored.restore(package, new=True)
    assert not (restored.config_dir / "installation.json").exists()
    assert restored.restore(package, new=True, trust_source=revision)["outcome"] == "complete"


def test_a_known_revision_is_taken_from_the_update_source_not_the_package(installed, upstream, tmp_path):
    manager, old = installed
    package = manager.backup()
    upstream.commit("next release")
    manager.update()
    shutil.rmtree(manager.root / "releases" / old)
    shutil.rmtree(manager.root / "repository.git", ignore_errors=True)
    tampered = repack(package, tmp_path / "tampered.tar.gz", lambda root, _: foreign_source(root))
    manager.restore(tampered)
    assert manager.settings()["revision"] == old
    assert (manager.root / "current/README").read_text() == "1.6 release\n"


def test_required_signatures_also_apply_to_restored_source(installed, upstream, tmp_path):
    manager, old = installed
    package = manager.backup()
    private = tmp_path / "maintainer"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "maintainer", "-f", str(private)],
                   check=True, capture_output=True)
    signers = tmp_path / "allowed_signers"
    signers.write_text(f'fixture@example.invalid namespaces="git" {private.with_suffix(".pub").read_text().strip()}\n')
    manager.configure_source(signers_file=signers)
    forged = repack(package, tmp_path / "forged.tar.gz", unknown_revision)
    with pytest.raises(ValueError, match="requires signed revisions"):
        manager.restore(forged, trust_source=FORGED)
    unsigned = upstream.commit("unsigned release")
    known = repack(package, tmp_path / "unsigned.tar.gz",
                   lambda root, manifest: unknown_revision(root, manifest, unsigned))
    with pytest.raises(ValueError, match="neither signed itself.*requires signed revisions"):
        manager.restore(known, trust_source=unsigned)
    assert manager.settings()["revision"] == old
    assert not (manager.root / "releases" / unsigned).exists()


def test_the_commands_pass_the_confirmation_through(installed, monkeypatch):
    manager, _ = installed
    seen = []
    monkeypatch.setattr(Manager, "restore", lambda self, package, **options: seen.append(options) or {})
    parser = ops_cli.parser()
    for argv in (["restore", "package.tar.gz", "--trust-source", FORGED],
                 ["install", "--restore", "package.tar.gz", "--trust-source", FORGED],
                 ["rollback", "--package", "package.tar.gz", "--trust-source", FORGED]):
        ops_cli.dispatch(manager, parser.parse_args(["--root", str(manager.root), *argv]))
    assert [options["trust_source"] for options in seen] == [FORGED] * 3
    with pytest.raises(ValueError, match="--restore"):
        ops_cli.dispatch(manager, parser.parse_args(["--root", str(manager.root), "install", "--mode", "wiki",
                                                     "--trust-source", FORGED]))


# Only BananaWiki's own databases are databases ------------------------------------------------


def uploads(data: Path) -> dict[str, bytes]:
    files = {"attachments/0123456789abcdef0123456789abcdef.db": os.urandom(100_000),
             "attachments/thumbs.sqlite3": b"OLE, not SQLite", "uploads/empty.sqlite": b"",
             "chat_attachments/notes.db-wal": b"not a sidecar of anything", "backups/manual-1.db": b"old copy"}
    for name, content in files.items():
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_bytes(content)
    return files


def test_uploads_named_like_databases_do_not_break_backups(installed):
    """Before: 'A packaged database failed its integrity check' for every backup, update package and remote backup."""
    manager, _ = installed
    data = manager.root / "data"
    files = uploads(data)
    package = manager.backup()
    with read_package(package, "BananaWiki", manager.root / "staging") as (tree, _):
        for name, content in files.items():
            assert (tree / "data" / name).read_bytes() == content
        with sqlite3.connect(tree / "data/bananawiki.db") as connection:
            assert connection.execute("SELECT title FROM pages").fetchone()[0] == "Home"
    manager.restore(package)
    assert (data / "chat_attachments/notes.db-wal").read_bytes() == files["chat_attachments/notes.db-wal"]


def test_the_wiki_database_itself_is_still_checked(installed):
    manager, _ = installed
    (manager.root / "data/bananawiki.db").write_bytes(b"damaged" * 1000)
    for suffix in ("-wal", "-shm"):
        Path(str(manager.root / "data/bananawiki.db") + suffix).unlink(missing_ok=True)
    with pytest.raises(ValueError, match="integrity check"):
        manager.backup()
    assert not list((manager.root / "backups").glob("manual-*"))


def test_hosting_checks_the_portal_database_and_treats_tenant_files_as_files(tmp_path, fake_system, upstream):
    root = tmp_path / "opt/bananawiki"
    legacy_install(root, fake_system, upstream, upstream.commit("1.6 release"), mode="hosting")
    with sqlite3.connect(root / "data/hosting.db") as connection:
        connection.execute("CREATE TABLE accounts (id INTEGER)")
    tenant = root / "data/instances/acme"
    tenant.mkdir(parents=True)
    with sqlite3.connect(tenant / "bananawiki.db") as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE pages (title TEXT)")
        connection.execute("INSERT INTO pages VALUES ('tenant home')")
    profile.prepare_hosting_storage(root / "data")
    files = uploads(tenant / "storage")
    snapshot = Snapshot.create(root, lambda path: profile.inside_tenant(path, root / "data"))
    try:
        index = snapshot.index()
        assert index["data/hosting.db"]["method"] == "sqlite"
        assert index["data/instances/acme/bananawiki.db"]["method"] == "copy"  # with its WAL, never opened
        assert all(index["data/instances/acme/storage/" + name]["method"] != "sqlite" for name in files)
        assert "data/instances/acme/storage/chat_attachments/notes.db-wal" in index
    finally:
        snapshot.remove()
    manager = Manager(root, system=fake_system)
    package = manager.backup()
    with read_package(package, "BananaWiki", root / "staging") as (tree, _):
        for name, content in files.items():
            assert (tree / "data/instances/acme/storage" / name).read_bytes() == content
        with sqlite3.connect(tree / "data/instances/acme/bananawiki.db") as connection:
            assert connection.execute("SELECT title FROM pages").fetchone()[0] == "tenant home"
    # A damaged tenant database is the tenant's own: it is packaged, it does not fail every tenant's backup.
    (tenant / "bananawiki.db").write_bytes(b"damaged" * 1000)
    for suffix in ("-wal", "-shm"):
        Path(str(tenant / "bananawiki.db") + suffix).unlink(missing_ok=True)
    assert manager.backup().is_file()
    (root / "data/hosting.db").write_bytes(b"damaged" * 1000)
    with pytest.raises(ValueError, match="integrity check"):
        manager.backup()


# Remote series: append-only, separate pruning, loud opt-in ------------------------------------


@pytest.fixture
def series(tmp_path, monkeypatch):
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        pytest.skip("needs age")
    monkeypatch.setattr(store, "check_private", lambda config, token: True)
    remote = tmp_path / "backups.git"
    git("init", "--quiet", "--bare", str(remote), cwd=tmp_path)
    root = tmp_path / "config/remote-backup"
    root.mkdir(parents=True, mode=0o700)
    key = crypto.keygen(tmp_path / "recovery.agekey")
    token = root / "repo.token"
    token.write_text("upload-only-token\n")
    token.chmod(0o600)
    shutil.copy(tmp_path / "recovery.agekey", root / "recovery.agekey")
    (root / "recovery.agekey").chmod(0o600)
    write_backup_json(root / "repository.json", {
        "schema": 1, "product": "BananaWiki", "name": "wiki-production", "url": str(remote), "forge": "github",
        "username": "git", "keep": 2, "max_mib": 8, "recipient": key["recipient"], "append_only": True})
    backups = store.Store(root, "BananaWiki")
    backups.allow_local = True
    return backups, tmp_path


def run(backups, *argv, restore_package=None):
    parser = argparse.ArgumentParser()
    commands.add_commands(parser.add_subparsers(dest="command"))
    return commands.handle(parser.parse_args(["backups", *argv]), backups, create_package=None,
                           restore_package=restore_package)


def test_an_append_only_series_never_deletes_snapshots_while_uploading(series, monkeypatch):
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"package")
    monkeypatch.setattr(store.Git, "remove", lambda *_: pytest.fail("an append-only upload deleted a branch"))
    identifiers = [backups.upload(package)["snapshot"] for _ in range(3)]
    assert sorted(item["snapshot"] for item in backups.list()) == sorted(identifiers)
    monkeypatch.undo()
    monkeypatch.setattr(store, "check_private", lambda config, token: True)
    privileged = tmp_path / "delete.token"
    privileged.write_text("delete-token\n")
    privileged.chmod(0o600)
    result = run(backups, "prune", "--token-file", str(privileged))
    assert result["removed"] == identifiers[:1] and result["warnings"] == []
    assert [item["snapshot"] for item in backups.list()] == identifiers[:0:-1]
    assert (backups.root / "repo.token").read_text() == "upload-only-token\n"
    assert read_json(backups.root / "repository.json")["append_only"] is True


def test_configure_records_the_append_only_choice(series):
    backups, tmp_path = series
    result = backups.configure(repo="https://github.com/owner/backups.git", forge="github", name="wiki-production",
                               token_file=backups.token, key_file=backups.identity, append_only=True)
    assert result["repository"]["append_only"] is True
    assert backups.configure(repo="https://github.com/owner/backups.git", forge="github", name="wiki-production",
                             token_file=backups.token, key_file=backups.identity)["repository"]["append_only"] is False


def test_accepting_an_unauthenticated_snapshot_warns_loudly(series, capsys):
    backups, tmp_path = series
    config = backups.settings()
    identifier = "20260930T000000.000000Z-deadbeef"
    context = backups.context(config, identifier)
    payload = b"a package from before snapshots were authenticated"
    header = {**context, "schema": 1, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    work = tmp_path / "old"
    work.mkdir(mode=0o700)
    part = work / "part-00000.age"
    subprocess.run(["age", "--encrypt", "--recipient", config["recipient"], "--output", str(part)],
                   input=crypto.MAGIC + json.dumps(header, sort_keys=True).encode() + b"\n" + payload, check=True)
    write_backup_json(work / "index.json", {**context, "schema": 1, "parts": [
        {"name": part.name, "bytes": part.stat().st_size, "sha256": hashlib.sha256(part.read_bytes()).hexdigest()}]})
    pusher = store.Git(work, config, backups.token, allow_local=True)
    pusher.push(pusher.commit([part, work / "index.json"]), backups.prefix(config) + identifier)
    assert run(backups, "verify", identifier, "--allow-unauthenticated")["authenticated"] is False
    assert "WARNING" in capsys.readouterr().err
    assert run(backups, "restore", identifier, "--allow-unauthenticated", "--trust-source", FORGED,
               restore_package=lambda package, options: {"trust_source": options.trust_source}) == {
        "trust_source": FORGED}
    assert "WARNING" in capsys.readouterr().err
