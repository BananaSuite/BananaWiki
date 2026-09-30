"""The root controller and files tenants control: never follow their links, never let them block updates.

Tenant containers write ``data/instances/<tenant>/`` (and keep running while
units are converged) while the controller walks the same tree as root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from ops_fakes import FakeSystem, Upstream, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops import profile
from bananawiki.ops import system as system_module
from bananawiki.ops.files import read_package
from bananawiki.ops.manager import Manager

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="ownership checks need root")
STRANGER = 43210


@pytest.fixture
def fake_system(tmp_path) -> FakeSystem:
    return FakeSystem(tmp_path / "host")


@pytest.fixture
def hosting(tmp_path, fake_system):
    """A hosting installation with one tenant, and a root-owned file outside it that tenants must not reach."""
    upstream = Upstream(tmp_path)
    root = tmp_path / "opt/bananawiki"
    settings = legacy_install(root, fake_system, upstream, upstream.commit("1.6"), mode="hosting")
    tenant = root / "data/instances/acme"
    tenant.mkdir(parents=True)
    (tenant / "page.txt").write_text("content")
    profile.prepare_hosting_storage(root / "data")
    outside = tmp_path / "shadow"
    outside.write_text("root:secret\n")
    os.chown(outside, STRANGER, STRANGER)
    outside.chmod(0o644)
    return root, settings, tenant, outside


def untouched(path: Path) -> bool:
    info = path.stat()
    return (info.st_uid, info.st_gid, info.st_mode & 0o777) == (STRANGER, STRANGER, 0o644)


def test_tenant_links_and_fifos_are_skipped_not_followed(hosting, fake_system):
    root, settings, tenant, outside = hosting
    (tenant / "evil").symlink_to(outside)
    (tenant / "evil-dir").symlink_to(outside.parent)
    os.mkfifo(tenant / "pipe")
    fake_system.data_permissions(settings)  # 1.4 and the first 1.6 controller refused: every update failed
    assert untouched(outside)
    assert (tenant / "page.txt").stat().st_mode & 0o777 == 0o600
    assert (tenant / "uploads").is_symlink() and os.readlink(tenant / "uploads") == "storage/uploads"


def test_a_file_swapped_for_a_link_during_the_walk_is_not_followed(hosting, fake_system, monkeypatch):
    """The race: the file is checked as a regular file, then replaced by a link to a root-owned file."""
    root, settings, tenant, outside = hosting
    page = tenant / "page.txt"

    def swap() -> None:
        if not page.is_symlink():
            page.unlink()
            page.symlink_to(outside)

    real_stat, real_is_symlink = os.stat, Path.is_symlink

    def checked_stat(path, *args, **kwargs):  # the check this controller makes
        result = real_stat(path, *args, **kwargs)
        if path == "page.txt":
            swap()
        return result

    def checked_is_symlink(path: Path) -> bool:  # the check 1.4 and the first 1.6 controller made
        result = real_is_symlink(path)
        if path == page and not result:
            monkeypatch.setattr(Path, "is_symlink", real_is_symlink)
            swap()
        return result

    monkeypatch.setattr(system_module.os, "stat", checked_stat)
    monkeypatch.setattr(Path, "is_symlink", checked_is_symlink)
    fake_system.data_permissions(settings)
    assert untouched(outside)


def test_hard_links_to_files_of_another_owner_are_refused(hosting, fake_system):
    root, settings, tenant, outside = hosting
    os.link(outside, tenant / "linked")
    with pytest.raises(ValueError, match="hard-link"):
        fake_system.data_permissions(settings)
    assert untouched(outside)


def test_links_outside_tenant_directories_are_still_refused(hosting, fake_system):
    root, settings, _tenant, outside = hosting
    (root / "data/stray").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        fake_system.data_permissions(settings)
    assert untouched(outside)


def test_backups_and_updates_are_not_blocked_by_a_tenant_link(hosting, fake_system):
    root, _settings, tenant, outside = hosting
    (tenant / "evil").symlink_to(outside)
    os.mkfifo(tenant / "pipe")
    package = Manager(root, system=fake_system).backup()
    with read_package(package, "BananaWiki", root / "staging") as (tree, manifest):
        assert "data/instances/acme/page.txt" in manifest["files"]
        assert not any(name.endswith(("/evil", "/pipe")) for name in manifest["files"])
        assert not (tree / "data/instances/acme/evil").exists()


def test_storage_preparation_does_not_follow_a_planted_storage_link(hosting, tmp_path):
    root, _settings, tenant, _outside = hosting
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in profile.HOSTING_STORAGE_NAMES:
        (tenant / name).unlink()
    (tenant / "storage").rename(tenant / "old-storage")
    (tenant / "storage").symlink_to(elsewhere)
    profile.prepare_hosting_storage(root / "data")  # no error: the portal repairs the layout at start
    assert list(elsewhere.iterdir()) == []
