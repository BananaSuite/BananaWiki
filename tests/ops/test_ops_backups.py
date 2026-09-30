"""Encrypted repository backups (the 1.4 banana_backup format) against a local bare repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from ops_fakes import git  # type: ignore[import-not-found]

from bananawiki.ops.backups import commands, crypto, store
from bananawiki.ops.backups.files import digest, write_json
from bananawiki.ops.backups.git import Git, location

pytestmark = pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None,
                                reason="needs age")


@pytest.fixture
def series(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "check_private", lambda config, token: True)
    remote = tmp_path / "backups.git"
    git("init", "--quiet", "--bare", str(remote), cwd=tmp_path)
    root = tmp_path / "config/remote-backup"
    root.mkdir(parents=True, mode=0o700)
    key = crypto.keygen(tmp_path / "recovery.agekey")
    token = root / "repo.token"
    token.write_text("unused-for-local\n")
    token.chmod(0o600)
    shutil.copy(tmp_path / "recovery.agekey", root / "recovery.agekey")
    (root / "recovery.agekey").chmod(0o600)
    write_json(root / "repository.json", {"schema": 1, "product": "BananaWiki", "name": "wiki-production",
                                          "url": str(remote), "forge": "github", "username": "git",
                                          "keep": 2, "max_mib": 8, "recipient": key["recipient"]})
    backups = store.Store(root, "BananaWiki")
    backups.allow_local = True
    return backups, tmp_path


def test_upload_list_download_and_retention(series):
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"package-bytes" * 1000)
    identifiers = [backups.upload(package)["snapshot"] for _ in range(3)]
    listed = [item["snapshot"] for item in backups.list()]
    assert listed == identifiers[::-1][:2], "keep=2 prunes the oldest snapshot"
    output = tmp_path / "restored.tar.gz"
    result = backups.download(identifiers[-1], output)
    assert result["verified"] and result["authenticated"] and output.read_bytes() == package.read_bytes()
    assert backups.verify(identifiers[-1])["verified"]
    with pytest.raises(ValueError):
        backups.download(identifiers[-1], output)
    assert backups.status()["last_backup"]["snapshot"] == identifiers[-1]


def test_series_identity_pins_the_recovery_key(series):
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"x")
    backups.upload(package)
    replacement = crypto.keygen(tmp_path / "other.agekey")
    shutil.copy(replacement["key_file"], backups.identity)
    with pytest.raises(ValueError, match="different recovery key"):
        backups.upload(package)


def test_only_bananawiki_series_and_https_forges():
    with pytest.raises(ValueError):
        store.Store(Path("/tmp/x"), "BananaChat")  # noqa: S108
    assert location("https://github.com/o/r.git", "github")["api"] == "https://api.github.com"
    for bad in ("http://github.com/o/r.git", "https://tok@github.com/o/r.git", "https://github.com/o"):
        with pytest.raises(ValueError):
            location(bad, "github")


def forge(backups, tmp_path: Path, *, authentication=None) -> str:
    """What anyone who can push to the backup repository can do, knowing only the public recipient.

    Also what a snapshot uploaded before snapshots were authenticated looks like.
    """
    config = backups.settings()
    identifier = "20260930T000000.000000Z-deadbeef"
    context = backups.context(config, identifier)
    package = b"a package whose source.tar.gz and app.env restore deploys as root"
    header = {**context, "schema": 1, "bytes": len(package), "sha256": hashlib.sha256(package).hexdigest()}
    work = tmp_path / "attacker"
    work.mkdir(mode=0o700)
    part = work / "part-00000.age"
    subprocess.run(["age", "--encrypt", "--recipient", config["recipient"], "--output", str(part)],
                   input=crypto.MAGIC + json.dumps(header, sort_keys=True).encode() + b"\n" + package, check=True)
    index = {**context, "schema": 1,
             "parts": [{"name": part.name, "bytes": part.stat().st_size, "sha256": digest(part)}]}
    if authentication is not None:
        index["authentication"] = authentication
    write_json(work / "index.json", index)
    pusher = Git(work, config, backups.token, allow_local=True)
    pusher.push(pusher.commit([part, work / "index.json"]), backups.prefix(config) + identifier)
    return identifier


def test_a_snapshot_forged_with_the_public_recipient_is_refused(series):
    backups, tmp_path = series
    identifier = forge(backups, tmp_path)
    output = tmp_path / "restored.tar.gz"
    with pytest.raises(ValueError, match="not authenticated"):
        backups.download(identifier, output)
    with pytest.raises(ValueError, match="not authenticated"):
        backups.verify(identifier)
    assert not output.exists()


def test_a_snapshot_with_a_forged_tag_is_refused_even_with_the_opt_in(series):
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"genuine")
    genuine = backups.upload(package)["snapshot"]
    config = backups.settings()
    index = json.loads(git("show", backups.prefix(config) + genuine + ":index.json", cwd=Path(config["url"])))
    # The tag of a genuine snapshot, as anyone with read access sees it, does not cover another index.
    identifier = forge(backups, tmp_path, authentication=index["authentication"])
    assert identifier != genuine
    for allow in (False, True):
        with pytest.raises(ValueError, match="authentication failed"):
            backups.download(identifier, tmp_path / f"restored-{allow}.tar.gz", allow_unauthenticated=allow)


def test_unauthenticated_snapshots_restore_only_with_the_explicit_opt_in(series):
    """Snapshots made before authentication existed look exactly like a forgery; the operator decides."""
    backups, tmp_path = series
    identifier = forge(backups, tmp_path)
    parser = argparse.ArgumentParser()
    commands.add_commands(parser.add_subparsers(dest="command"))
    restored = []

    def restore_package(package, options):
        restored.append(Path(package).read_bytes())
        return {"outcome": "complete"}

    def run(*extra):
        args = parser.parse_args(["backups", "restore", identifier, *extra])
        return commands.handle(args, backups, create_package=None, restore_package=restore_package)

    with pytest.raises(ValueError, match="--allow-unauthenticated"):
        run()
    assert restored == []
    assert run("--allow-unauthenticated") == {"outcome": "complete"}
    assert restored and restored[0].startswith(b"a package")
    verified = backups.verify(identifier, allow_unauthenticated=True)
    assert verified["verified"] and verified["authenticated"] is False


def test_the_authentication_key_comes_from_the_recovery_identity(series):
    """A full-server-loss restore needs only the recovery key the operator already saved offline."""
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"genuine")
    identifier = backups.upload(package)["snapshot"]
    fresh = tmp_path / "new-server/config/remote-backup"
    fresh.mkdir(parents=True, mode=0o700)
    for name in ("repository.json", "repo.token", "recovery.agekey"):
        shutil.copy(backups.root / name, fresh / name)
        (fresh / name).chmod(0o600)
    rebuilt = store.Store(fresh, "BananaWiki")
    rebuilt.allow_local = True
    assert rebuilt.verify(identifier)["authenticated"] is True
    shutil.copy(crypto.keygen(tmp_path / "other.agekey")["key_file"], fresh / "recovery.agekey")
    with pytest.raises(ValueError, match="authentication failed"):
        rebuilt.verify(identifier)
