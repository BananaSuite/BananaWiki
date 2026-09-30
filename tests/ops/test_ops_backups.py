"""Encrypted repository backups (the 1.4 banana_backup format) against a local bare repository."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from ops_fakes import git  # type: ignore[import-not-found]

from bananawiki.ops.backups import crypto, store
from bananawiki.ops.backups.files import write_json
from bananawiki.ops.backups.git import location

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
    assert result["verified"] and output.read_bytes() == package.read_bytes()
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
