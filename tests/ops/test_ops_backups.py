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


def _usage(free: int):
    return type("Usage", (), {"free": free})()


def test_upload_checks_its_staging_space_before_anything_is_pushed(series, monkeypatch):
    """Verification is sized by the snapshot, not the series limit, so a run that pushed also prunes."""
    backups, tmp_path = series
    path = backups.root / "repository.json"
    write_json(path, {**json.loads(path.read_text()), "max_mib": 512})
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"package")
    monkeypatch.setattr(store.shutil, "disk_usage", lambda _: _usage(100 * 1024 * 1024))
    with pytest.raises(ValueError, match="Not enough free space"):
        backups.upload(package)
    assert backups.list() == []
    monkeypatch.setattr(store.shutil, "disk_usage", lambda _: _usage(1024 * 1024 * 1024))
    identifiers = [backups.upload(package)["snapshot"] for _ in range(3)]
    assert [item["snapshot"] for item in backups.list()] == identifiers[:0:-1]


def test_an_upload_whose_staging_space_ran_out_pushes_nothing(series, monkeypatch):
    """Free space is checked again right before the push, with what the upload holds by then."""
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"first")
    first = backups.upload(package)["snapshot"]
    free = [100 * 1024 * 1024, 1024 * 1024 * 1024]
    monkeypatch.setattr(store.shutil, "disk_usage", lambda _: _usage(free.pop() if len(free) > 1 else free[0]))
    package.write_bytes(b"second")
    with pytest.raises(ValueError, match="Not enough free space"):
        backups.upload(package)
    assert [item["snapshot"] for item in backups.list()] == [first]


def test_an_upload_verifies_without_keeping_its_own_copies(series, monkeypatch):
    backups, tmp_path = series
    decrypt, download = crypto.decrypt, store.Store._download

    def verifying(self, git, *args, **kwargs):
        names = {path.name for path in self.root.rglob("*")}
        assert "sealed.age" not in names and not any(name.startswith("part-") for name in names)
        return download(self, git, *args, **kwargs)

    def decrypting(ciphertext, *args):
        assert not (Path(ciphertext).parent / "git").exists()
        return decrypt(ciphertext, *args)

    monkeypatch.setattr(store.Store, "_download", verifying)
    monkeypatch.setattr(crypto, "decrypt", decrypting)
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"package")
    assert backups.upload(package)["verified"]


def test_working_directories_of_a_killed_operation_are_removed(series):
    backups, tmp_path = series
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"not ours")
    for name in ("transfer-abc", "verify-def", "restore-ghi"):
        (backups.root / name).mkdir()
        (backups.root / name / "package.tar.gz").write_bytes(b"decrypted package")
    (backups.root / "transfer-link").symlink_to(outside)
    backups.list()
    left = sorted(path.name for path in backups.root.iterdir()
                  if path.name.startswith(("transfer-", "verify-", "restore-")))
    assert left == ["transfer-link"]
    assert (outside / "keep").read_bytes() == b"not ours"
    assert backups.identity.exists() and (backups.root / "repository.json").exists()


def test_a_restore_holds_the_backup_lock_until_it_is_done_with_the_package(series):
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"genuine")
    identifier = backups.upload(package)["snapshot"]
    parser = argparse.ArgumentParser()
    commands.add_commands(parser.add_subparsers(dest="command"))
    seen = []

    def restore_package(path, options):
        with pytest.raises(RuntimeError, match="Another backup operation"):
            backups.list()
        seen.append(Path(path).read_bytes())
        return {"outcome": "complete", "package": str(path)}

    result = commands.handle(parser.parse_args(["backups", "restore", identifier]), backups, create_package=None,
                             restore_package=restore_package)
    assert seen == [b"genuine"]
    assert not Path(result["package"]).exists()
    assert backups.list()


def test_branches_named_with_any_bytes_break_nothing(series):
    """Whoever can push chooses branch names: not UTF-8, or merely ending like the series identity."""
    backups, tmp_path = series
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"first")
    first = backups.upload(package)
    prefix = backups.prefix(backups.settings())
    remote = Path(backups.settings()["url"])
    for ref in (prefix.encode() + b"x\xff", prefix.encode() + first["snapshot"].encode() + "\u2028x".encode(),
                ("refs/heads/elsewhere/" + prefix.removeprefix("refs/heads/") + "identity").encode()):
        subprocess.run(["git", "--git-dir", str(remote), "update-ref", ref, first["commit"]], check=True)
    package.write_bytes(b"second")
    second = backups.upload(package)
    assert [item["snapshot"] for item in backups.list()] == [second["snapshot"], first["snapshot"]]
    assert backups.verify(first["snapshot"])["authenticated"] is True
    assert backups.prune()["removed"] == []


def test_configure_keeps_the_stored_recovery_key_unless_told_to_replace_it(series):
    backups, tmp_path = series
    original = backups.identity.read_bytes()
    settings = {"repo": "https://github.com/owner/backups.git", "forge": "github", "name": "wiki-production",
                "token_file": backups.token, "keep": 3}
    assert backups.configure(**settings)["repository"]["keep"] == 3
    assert backups.identity.read_bytes() == original
    replacement = crypto.keygen(tmp_path / "new.agekey")["key_file"]
    with pytest.raises(ValueError, match="--replace-key"):
        backups.configure(**settings, key_file=replacement)
    assert backups.identity.read_bytes() == original
    parser = argparse.ArgumentParser()
    commands.add_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["backups", "configure", "--repo", settings["repo"], "--forge", "github", "--name",
                              "other", "--token-file", str(backups.token), "--key-file", replacement,
                              "--replace-key"])
    commands.handle(args, backups, create_package=None, restore_package=None)
    assert backups.identity.read_bytes() == Path(replacement).read_bytes()
    with pytest.raises(ValueError, match="--key-file"):
        store.Store(tmp_path / "new-server", "BananaWiki").configure(**settings)
