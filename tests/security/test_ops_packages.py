"""Backup and rollback packages of the lifecycle tool (bananawiki.ops.files)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile

from bananawiki.ops.files import read_package, regular_files, write_package


def _data(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.bin").write_bytes(b"x" * 100)
    os.link(data / "a.bin", data / "b.bin")
    return data


def test_w6_1_a_hard_link_in_data_does_not_make_packages_unrestorable(tmp_path):
    data = _data(tmp_path)
    inputs = [(path, "data/" + name) for path, name in regular_files(data)]
    package = write_package(tmp_path / "out" / "pkg.tar.gz", {"product": "BananaWiki"}, inputs)

    with tarfile.open(package) as archive:
        assert all(member.isfile() for member in archive.getmembers())
    with read_package(package, "BananaWiki", tmp_path / "staging") as (root, manifest):
        assert (root / "data/b.bin").read_bytes() == b"x" * 100
        assert set(manifest["files"]) == {"data/a.bin", "data/b.bin"}


def test_w6_1_packages_written_with_link_members_still_restore(tmp_path):
    """A rollback package written by an earlier release (one name as a hard-link member)."""
    content = b"y" * 64
    digest = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    package = tmp_path / "old.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        info = tarfile.TarInfo("data/a.bin")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo("data/b.bin")
        link.type, link.linkname = tarfile.LNKTYPE, "data/a.bin"
        archive.addfile(link)
        raw = json.dumps({"schema": 1, "product": "BananaWiki",
                          "files": {"data/a.bin": digest, "data/b.bin": digest}}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))

    with read_package(package, "BananaWiki", tmp_path / "staging") as (root, _manifest):
        assert (root / "data/b.bin").read_bytes() == content


def test_v1_1_a_refused_snapshot_names_the_wikis_holding_the_most_files(tmp_path):
    import pytest

    from bananawiki.ops import profile
    from bananawiki.ops.files import Refused
    from bananawiki.ops.snapshot import Snapshot

    root = tmp_path / "inst"
    for folder in ("config", "data/instances/evil/storage", "data/instances/good", "site", "staging"):
        (root / folder).mkdir(parents=True)
    (root / "data/hosting.db").write_bytes(b"")
    for n in range(20):
        (root / f"data/instances/evil/storage/{n}").write_bytes(b"")

    with pytest.raises(Refused) as refused:
        Snapshot.check(root, lambda path: profile.inside_tenant(path, root / "data"), max_files=10)
    assert "evil (20 files)" in str(refused.value)
