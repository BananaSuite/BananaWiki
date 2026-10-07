"""Trusted allocation semantics; native XFS behavior has a separate live witness."""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from bananawiki.ops import project_quota as quota


class Kernel:
    """Filesystem-backed fake: independent inode identity, inheritance and usage."""

    def __init__(self, instances):
        self.instances = instances
        self.projects = {}
        self.generations = {}
        self.quotas = {}
        self.accounting = self.enforcement = True
        self.filesystem_id = "xfs:verified-test-uuid"
        self.fail_assignment = False
        self.assignments = []

    def entries(self):
        yield self.instances
        for directory, directories, files in os.walk(self.instances, followlinks=False):
            for name in (*directories, *files):
                yield Path(directory) / name

    def path(self, inode):
        return next(path for path in self.entries() if path.lstat().st_ino == inode)

    def attributes(self, path):
        inode = path.lstat().st_ino
        if inode not in self.projects:
            inherited = self.projects.get(path.parent.lstat().st_ino, (0, 0))
            project = inherited[1] if inherited[0] & quota._PROJINHERIT else 0
            self.projects[inode] = (quota._PROJINHERIT if path.is_dir() and project else 0, project)
        return self.projects[inode]

    def filesystem(self, _fd):
        return self.filesystem_id

    def enforced(self, _fd):
        if not self.accounting or not self.enforcement:
            raise quota.QuotaError("Both accounting and enforcement are required")

    def inode(self, _fd, inode):
        path = self.path(inode)
        info = path.lstat()
        flags, project = self.attributes(path)
        return SimpleNamespace(generation=self.generations.get(inode, 1), project=project,
                               mode=info.st_mode, uid=info.st_uid, xflags=flags)

    def attrs(self, fd):
        return self.attributes(self.path(os.fstat(fd).st_ino))

    def assign(self, fd, project, directory):
        if self.fail_assignment:
            raise OSError(errno.EPERM, "injected assignment refusal")
        inode = os.fstat(fd).st_ino
        self.projects[inode] = (quota._PROJINHERIT if directory else 0, project)
        self.assignments.append((inode, project))

    def occupied(self, _fd, project):
        return project in self.quotas

    def set_limits(self, _fd, project, byte_limit, inode_limit):
        self.quotas[project] = (byte_limit // 512, inode_limit)

    def limits(self, _fd, project):
        bhard, ihard = self.quotas.get(project, (0, 0))
        inodes = {}
        for path in self.entries():
            info = path.lstat()
            if self.attributes(path)[1] == project:
                inodes[info.st_ino] = info
        return SimpleNamespace(bhard=bhard, ihard=ihard, bcount=sum(x.st_blocks for x in inodes.values()),
                               icount=len(inodes), rtbcount=0)


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "_require_root", lambda: None)
    instances = tmp_path / "instances"
    instances.mkdir()
    tenant = instances / "alpha"
    tenant.mkdir()
    if os.geteuid() == 0:
        os.chown(tenant, 4242, 4242)
    kernel = Kernel(instances)
    for path in kernel.entries():
        kernel.attributes(path)
    free = SimpleNamespace(bytes=1024 * 1024 * 1024, total=1024 * 1024 * 1024)
    monkeypatch.setattr(quota.os, "fstatvfs", lambda _fd: SimpleNamespace(
        f_bavail=free.bytes, f_blocks=free.total, f_frsize=1))
    manager = quota.ProjectQuota(instances, tmp_path / "registry", max_bytes=64 * 1024 * 1024,
                                 max_inodes=1000, storage_reserve_bytes=1024 * 1024)
    manager._kernel = kernel
    return SimpleNamespace(manager=manager, kernel=kernel, tenant=tenant, instances=instances, free=free)


def prepare(model, name="alpha", **options):
    return model.manager.prepare(name, byte_limit=8 * 1024 * 1024, inode_limit=128, **options)


def rows(model):
    with sqlite3.connect(model.manager.state_dir / "allocations.sqlite3") as connection:
        return connection.execute("SELECT project,ready,bytes,inodes FROM allocations ORDER BY project").fetchall()


def test_empty_root_is_prepared_before_first_write_and_limits_survive_restart(model):
    witness = prepare(model)
    assert witness.byte_limit == 8 * 1024 * 1024 and witness.inode_limit == 128
    assert witness.to_dict()["inheritance"] is True
    (model.tenant / "first-write").write_bytes(b"tenant data")
    project = model.kernel.inode(0, (model.tenant / "first-write").stat().st_ino).project
    assert project == witness.project_id
    restarted = quota.ProjectQuota(model.instances, model.manager.state_dir, max_bytes=64 * 1024 * 1024,
                                   max_inodes=1000, storage_reserve_bytes=1024 * 1024)
    restarted._kernel = model.kernel
    assert restarted.verify("alpha") == witness
    assert prepare(model) == witness


def test_prepare_accepts_the_trusted_stopped_inventory_descriptor(model):
    descriptor = os.open(model.tenant, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        witness = prepare(model, expected_descriptor=descriptor)
        assert witness == model.manager.verify("alpha")
    finally:
        os.close(descriptor)


def test_replaced_path_is_refused_before_allocation_or_kernel_mutation(model):
    descriptor = os.open(model.tenant, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        model.tenant.rename(model.instances / "original-after-inventory")
        model.tenant.mkdir()
        if os.geteuid() == 0:
            os.chown(model.tenant, 4242, 4242)
        with pytest.raises(quota.QuotaError, match="trusted pre-scan descriptor"):
            prepare(model, expected_descriptor=descriptor, repair=True)
        assert model.kernel.assignments == [] and model.kernel.quotas == {}
        assert not model.manager.state_dir.exists()
        assert model.kernel.attributes(model.tenant)[1] == 0
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("descriptor", [-1, True, "3"])
def test_invalid_trusted_descriptor_is_refused_before_allocating(model, descriptor):
    with pytest.raises(quota.QuotaError, match="open directory FD"):
        prepare(model, expected_descriptor=descriptor)
    assert not model.manager.state_dir.exists() and model.kernel.assignments == []


def test_non_directory_or_closed_descriptor_cannot_authorize_repair(model):
    file = model.instances / "ordinary-file"
    file.write_bytes(b"unrelated")
    descriptor = os.open(file, os.O_RDONLY)
    try:
        with pytest.raises(quota.QuotaError, match="trusted pre-scan descriptor"):
            prepare(model, expected_descriptor=descriptor, repair=True)
        assert not model.manager.state_dir.exists() and model.kernel.assignments == []
    finally:
        os.close(descriptor)
    with pytest.raises(quota.QuotaError, match="Bad file descriptor"):
        prepare(model, expected_descriptor=descriptor, repair=True)
    assert not model.manager.state_dir.exists() and model.kernel.assignments == []


@pytest.mark.parametrize(("field", "value"), [("byte_limit", 0), ("byte_limit", -512),
    ("byte_limit", True), ("byte_limit", 513), ("byte_limit", 65 * 1024 * 1024),
    ("inode_limit", 0), ("inode_limit", 1001), ("inode_limit", 1.5), ("inode_limit", False)])
def test_unlimited_malformed_or_above_operator_ceiling_is_refused_without_mutations(model, field, value):
    options = {"byte_limit": 8 * 1024 * 1024, "inode_limit": 128, field: value}
    with pytest.raises(quota.QuotaError):
        model.manager.prepare("alpha", **options)
    assert model.kernel.assignments == [] and not model.manager.state_dir.exists()


@pytest.mark.parametrize("field", ["max_bytes", "max_inodes", "project_start", "storage_reserve_bytes"])
def test_zero_operator_configuration_is_refused(model, field):
    options = {"max_bytes": 8 * 1024 * 1024, "max_inodes": 128, "project_start": 1_000_000,
               "storage_reserve_bytes": 1024 * 1024, field: 0}
    with pytest.raises(quota.QuotaError):
        quota.ProjectQuota(model.instances, model.manager.state_dir, **options)


@pytest.mark.parametrize("tenant", ["../alpha", "Alpha", "alpha/child", "", "a" * 80, None])
def test_tenant_cannot_supply_paths_or_invalid_names(model, tenant):
    with pytest.raises(quota.QuotaError):
        prepare(model, tenant)


@pytest.mark.parametrize("setting", ["accounting", "enforcement"])
def test_accounting_without_enforcement_and_enforcement_without_accounting_fail(model, setting):
    setattr(model.kernel, setting, False)
    with pytest.raises(quota.QuotaError):
        prepare(model)
    assert not model.manager.state_dir.exists()


def test_live_verify_never_adopts_unknown_projects_or_implicit_limit_changes(model):
    with pytest.raises(quota.QuotaError, match="not been prepared"):
        model.manager.verify("alpha")
    witness = prepare(model)
    assert model.manager.verify("alpha", byte_limit=witness.byte_limit) == witness
    with pytest.raises(quota.QuotaError, match="differ"):
        model.manager.verify("alpha", byte_limit=4 * 1024 * 1024)
    assert model.manager.verify("alpha") == witness
    assignments = list(model.kernel.assignments)
    updated = model.manager.prepare("alpha", byte_limit=4 * 1024 * 1024, inode_limit=128)
    assert updated.project_id == witness.project_id and updated.byte_limit == 4 * 1024 * 1024
    assert model.manager.verify("alpha") == updated and model.kernel.assignments == assignments


def test_live_update_cannot_lower_below_usage_or_mask_kernel_drift(model):
    original = prepare(model)
    (model.tenant / "payload").write_bytes(b"x" * 8192)
    with pytest.raises(quota.QuotaError, match="below current"):
        model.manager.prepare("alpha", byte_limit=512, inode_limit=128)
    assert model.manager.verify("alpha") == original
    model.kernel.quotas[original.project_id] = (0, 128)
    with pytest.raises(quota.QuotaError, match="hard byte"):
        model.manager.prepare("alpha", byte_limit=16 * 1024 * 1024, inode_limit=128)
    assert rows(model)[0][2] == original.byte_limit


def test_live_update_failure_remains_pending_and_requires_stopped_repair(model, monkeypatch):
    original = prepare(model)
    setter = model.kernel.set_limits
    def fail(*_args):
        raise OSError(errno.EPERM, "injected quota limit refusal")
    monkeypatch.setattr(model.kernel, "set_limits", fail)
    with pytest.raises(quota.QuotaError, match="limit refusal"):
        model.manager.prepare("alpha", byte_limit=16 * 1024 * 1024, inode_limit=128)
    assert rows(model)[0][1:3] == (0, 16 * 1024 * 1024)
    with pytest.raises(quota.QuotaError, match="not been prepared"):
        model.manager.verify("alpha")
    monkeypatch.setattr(model.kernel, "set_limits", setter)
    repaired = model.manager.prepare("alpha", byte_limit=16 * 1024 * 1024, inode_limit=128, repair=True)
    assert repaired.project_id == original.project_id


@pytest.mark.parametrize("tamper", ["project", "inheritance", "bytes", "inodes", "enforcement"])
def test_tampered_kernel_state_fails_closed_without_repair(model, tamper):
    witness = prepare(model)
    inode = model.tenant.stat().st_ino
    if tamper == "project":
        model.kernel.projects[inode] = (quota._PROJINHERIT, witness.project_id + 1)
    elif tamper == "inheritance":
        model.kernel.projects[inode] = (0, witness.project_id)
    elif tamper == "enforcement":
        model.kernel.enforcement = False
    else:
        old = model.kernel.quotas[witness.project_id]
        model.kernel.quotas[witness.project_id] = (0, old[1]) if tamper == "bytes" else (old[0], 0)
    previous = list(model.kernel.assignments)
    with pytest.raises(quota.QuotaError):
        model.manager.verify("alpha")
    with pytest.raises(quota.QuotaError):
        prepare(model)
    assert model.kernel.assignments == previous


def test_rename_keeps_identity_but_replacement_and_inode_generation_change_do_not(model):
    witness = prepare(model)
    model.tenant.rename(model.instances / "renamed")
    assert model.manager.verify("renamed") == witness
    replacement = model.instances / "alpha"
    replacement.mkdir()
    if os.geteuid() == 0:
        os.chown(replacement, 4242, 4242)
    with pytest.raises(quota.QuotaError, match="not been prepared"):
        model.manager.verify("alpha")
    new = prepare(model)
    assert new.project_id > witness.project_id
    model.kernel.generations[new.root_inode] = new.root_generation + 1
    with pytest.raises(quota.QuotaError, match="not been prepared"):
        model.manager.verify("alpha")


def test_kernel_and_registry_collisions_are_skipped_and_ids_are_not_reused(model):
    model.kernel.quotas[1_000_000] = (1, 1)
    first = prepare(model)
    assert first.project_id == 1_000_001
    model.tenant.rmdir()
    model.tenant.mkdir()
    if os.geteuid() == 0:
        os.chown(model.tenant, 4242, 4242)
    # Explicit generation simulates inode reuse even on filesystems recycling immediately.
    model.kernel.generations[model.tenant.stat().st_ino] = 2
    model.kernel.projects[model.tenant.stat().st_ino] = (0, 0)
    second = prepare(model)
    assert second.project_id == 1_000_002
    assert len(rows(model)) == 2


def test_failure_reserves_id_durably_and_retry_repairs_pending_allocation(model):
    model.kernel.fail_assignment = True
    with pytest.raises(quota.QuotaError, match="assignment refusal"):
        prepare(model)
    pending = rows(model)
    assert pending == [(1_000_000, 0, 8 * 1024 * 1024, 128)]
    with pytest.raises(quota.QuotaError, match="not been prepared"):
        model.manager.verify("alpha")
    model.kernel.fail_assignment = False
    assert prepare(model, repair=True).project_id == pending[0][0]
    assert rows(model)[0][1] == 1


def test_stopped_repair_covers_legacy_files_directories_and_unknown_project(model):
    child = model.tenant / "content"
    child.mkdir()
    data = child / "database"
    data.write_bytes(b"existing data")
    for path in (model.tenant, child, data):
        model.kernel.projects[path.stat().st_ino] = (0, 101)
    with pytest.raises(quota.QuotaError, match="stopped-only"):
        prepare(model)
    witness = prepare(model, repair=True)
    assert data.read_bytes() == b"existing data"
    for path in (model.tenant, child, data):
        inode = model.kernel.inode(0, path.stat().st_ino)
        assert inode.project == witness.project_id
        if path.is_dir():
            assert inode.xflags & quota._PROJINHERIT


def test_legacy_symlink_is_charged_without_opening_or_modifying_its_target(model, monkeypatch):
    outside = model.instances.parent / "outside-secret"
    outside.write_bytes(b"untouched")
    link = model.tenant / "alias"
    link.symlink_to(outside)
    original = link.lstat()
    model.kernel.attributes(link)
    # Fake inherits on newly recreated inode, just as actual XFS does.
    original_symlink = os.symlink
    def symlink(target, name, *, dir_fd):
        original_symlink(target, name, dir_fd=dir_fd)
        number = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_ino
        model.kernel.projects[number] = (0, model.kernel.projects[os.fstat(dir_fd).st_ino][1])
    monkeypatch.setattr(quota.os, "symlink", symlink)
    witness = prepare(model, repair=True)
    assert os.readlink(link) == str(outside) and outside.read_bytes() == b"untouched"
    assert link.lstat().st_uid == original.st_uid and link.lstat().st_gid == original.st_gid
    assert model.kernel.inode(0, link.lstat().st_ino).project == witness.project_id
    assert not any(number == outside.stat().st_ino for number, _ in model.kernel.assignments)


def test_failed_atomic_symlink_repair_preserves_original_target(model, monkeypatch):
    link = model.tenant / "alias"
    link.symlink_to("../a-target-that-is-never-followed")
    model.kernel.attributes(link)
    def fail(*_args, **_kwargs):
        raise OSError(errno.EPERM, "injected nofollow owner preservation refusal")
    monkeypatch.setattr(quota.os, "chown", fail)
    with pytest.raises(quota.QuotaError, match="owner preservation"):
        prepare(model, repair=True)
    assert os.readlink(link) == "../a-target-that-is-never-followed"
    assert not list(model.tenant.glob(".quota-link-*"))
    assert rows(model)[0][1] == 0


def test_external_hardlinks_special_files_and_root_symlinks_are_refused(model):
    outside = model.instances.parent / "outside"
    outside.write_bytes(b"host data")
    os.link(outside, model.tenant / "hardlink")
    with pytest.raises(quota.QuotaError, match="hardlink outside"):
        prepare(model, repair=True)
    (model.tenant / "hardlink").unlink()
    os.mkfifo(model.tenant / "fifo")
    with pytest.raises(quota.QuotaError, match="special"):
        prepare(model, repair=True)
    (model.tenant / "fifo").unlink()
    model.tenant.rmdir()
    model.tenant.symlink_to(model.instances.parent, target_is_directory=True)
    with pytest.raises(quota.QuotaError):
        prepare(model, repair=True)
    assert model.kernel.assignments == []


def test_internal_hardlinks_are_charged_once_and_preserved(model):
    data = model.tenant / "data"
    data.write_bytes(b"shared data")
    os.link(data, model.tenant / "alias")
    witness = prepare(model, repair=True)
    assert data.stat().st_nlink == 2
    assert model.kernel.limits(0, witness.project_id).icount == 2  # root + shared inode


@pytest.mark.parametrize("unsafe", ["public-state", "symlink-state", "symlink-registry", "hardlink-registry"])
def test_tenant_cannot_substitute_or_read_trusted_allocation_state(model, unsafe):
    state = model.manager.state_dir
    if unsafe == "public-state":
        state.mkdir(mode=0o700)
        state.chmod(0o777)
    elif unsafe == "symlink-state":
        state.symlink_to(model.tenant, target_is_directory=True)
    else:
        state.mkdir(mode=0o700)
        target = model.tenant / "registry"
        target.write_bytes(b"untrusted")
        target.chmod(0o600)
        if unsafe == "symlink-registry":
            (state / "allocations.sqlite3").symlink_to(target)
        else:
            os.link(target, state / "allocations.sqlite3")
    with pytest.raises(quota.QuotaError):
        prepare(model)
    assert model.kernel.assignments == []


def test_systemd_root_owned_state_directory_is_normalized_without_exposing_registry(model):
    model.manager.state_dir.mkdir(mode=0o755)
    prepare(model)
    assert stat.S_IMODE(model.manager.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((model.manager.state_dir / "allocations.sqlite3").stat().st_mode) == 0o600


def test_state_parent_symlinks_and_attacker_writable_parents_are_refused(model):
    parent = model.instances.parent / "unsafe-parent"
    parent.mkdir(mode=0o700)
    alias = model.instances.parent / "parent-alias"
    alias.symlink_to(parent, target_is_directory=True)
    model.manager.state_dir = alias / "registry"
    with pytest.raises(quota.QuotaError):
        prepare(model)
    assert not (parent / "registry").exists()
    parent.chmod(0o777)
    model.manager.state_dir = parent / "registry"
    with pytest.raises(quota.QuotaError, match="parents must be trusted"):
        prepare(model)
    assert not (parent / "registry").exists()


def test_existing_ordinary_directory_is_not_chmodded_as_quota_state(model):
    model.manager.state_dir.mkdir(mode=0o755)
    (model.manager.state_dir / "ordinary-host-file").write_bytes(b"host data")
    with pytest.raises(quota.QuotaError, match="dedicated allocation"):
        prepare(model)
    assert stat.S_IMODE(model.manager.state_dir.stat().st_mode) == 0o755
    assert (model.manager.state_dir / "ordinary-host-file").read_bytes() == b"host data"


def test_capacity_reserves_all_unused_limits_and_host_headroom(model):
    model.free.bytes = 12 * 1024 * 1024
    first = prepare(model)
    second = model.instances / "beta"
    second.mkdir()
    if os.geteuid() == 0:
        os.chown(second, 4242, 4242)
    with pytest.raises(quota.QuotaError, match="capacity"):
        prepare(model, "beta")
    fd = os.open(second, os.O_RDONLY)
    try:
        assert model.kernel.attrs(fd)[1] == 0
    finally:
        os.close(fd)
    assert model.manager.verify("alpha") == first
    # External writes depleting physical headroom also refuse subsequent guards.
    model.free.bytes = 2 * 1024 * 1024
    with pytest.raises(quota.QuotaError, match="capacity"):
        model.manager.verify("alpha")


def test_deleted_zero_usage_project_releases_budget_but_not_its_id(model):
    model.free.bytes = 12 * 1024 * 1024
    first = prepare(model)
    model.tenant.rmdir()
    model.tenant.mkdir()
    if os.geteuid() == 0:
        os.chown(model.tenant, 4242, 4242)
    model.kernel.generations[model.tenant.stat().st_ino] = 2
    model.kernel.projects[model.tenant.stat().st_ino] = (0, 0)
    assert prepare(model).project_id > first.project_id


def test_concurrent_deletion_cannot_admit_overcommitted_hard_limits(model, monkeypatch):
    model.free.bytes = model.free.total = 14 * 1024 * 1024
    first = prepare(model)
    second = model.instances / "beta"
    second.mkdir()
    if os.geteuid() == 0:
        os.chown(second, 4242, 4242)
    before = rows(model)
    native_limits = model.kernel.limits
    events = []

    def limits(fd, project):
        actual = native_limits(fd, project)
        if project == first.project_id:
            # Sample a full tenant just before its concurrent deletion. The
            # later statvfs sees free space that the tenant can reclaim again.
            actual.bcount = first.byte_limit // 512
            events.append("usage-full")
        return actual

    def capacity(_fd):
        assert "usage-full" in events
        events.append("deleted-before-free-read")
        return SimpleNamespace(f_bavail=model.free.total, f_blocks=model.free.total, f_frsize=1)

    monkeypatch.setattr(model.kernel, "limits", limits)
    monkeypatch.setattr(quota.os, "fstatvfs", capacity)
    with pytest.raises(quota.QuotaError, match="total capacity"):
        prepare(model, "beta")
    assert events == ["usage-full", "deleted-before-free-read"]
    assert rows(model) == before  # Refusal does not poison an existing reservation.
    assert model.kernel.attributes(second)[1] == 0


def test_project_scoped_parent_is_refused_for_physical_capacity(model):
    model.kernel.projects[model.instances.stat().st_ino] = (quota._PROJINHERIT, 55)
    with pytest.raises(quota.QuotaError, match="Instances parent"):
        prepare(model)


def test_repair_detects_a_tree_race_before_assignment(model, monkeypatch):
    child = model.tenant / "content"
    child.write_bytes(b"original")
    scanner = model.manager._scan
    def scan(fd, info):
        result = scanner(fd, info)
        child.unlink()
        child.mkdir()
        return result
    monkeypatch.setattr(model.manager, "_scan", scan)
    with pytest.raises(quota.QuotaError, match="inode changed"):
        prepare(model, repair=True)
    assert rows(model)[0][1] == 0


def test_repair_refuses_submount_even_when_device_number_matches(model, monkeypatch):
    child = model.tenant / "nested"
    child.mkdir()
    identifier = quota._mount_id
    def mount(fd):
        if os.fstat(fd).st_ino == child.stat().st_ino:
            return identifier(fd) + 1
        return identifier(fd)
    monkeypatch.setattr(quota, "_mount_id", mount)
    with pytest.raises(quota.QuotaError, match="submount"):
        prepare(model, repair=True)
    assert not model.kernel.assignments


def test_concurrent_allocations_serialize_and_never_share_an_id(model):
    for number in range(4):
        path = model.instances / f"parallel-{number}"
        path.mkdir()
        if os.geteuid() == 0:
            os.chown(path, 4242, 4242)
    with ThreadPoolExecutor(max_workers=4) as workers:
        witnesses = list(workers.map(lambda number: prepare(model, f"parallel-{number}"), range(4)))
    assert len({value.project_id for value in witnesses}) == 4
    for number, witness in enumerate(witnesses):
        assert model.manager.verify(f"parallel-{number}") == witness


def test_real_backend_refuses_unsupported_host_filesystem_before_creating_registry(model):
    model.manager._kernel = quota._Kernel()
    with pytest.raises(quota.QuotaError, match="XFS filesystem"):
        prepare(model)
    assert not model.manager.state_dir.exists()


def test_state_inside_tenant_tree_is_not_a_trusted_registry(model):
    with pytest.raises(quota.QuotaError, match="outside tenant"):
        quota.ProjectQuota(model.instances, model.tenant / "registry", max_bytes=1024, max_inodes=10)


def test_descriptor_keeps_verified_inode_across_rename_and_refuses_replacement(model):
    witness = prepare(model)
    fd = os.open(model.tenant, quota._OPEN | os.O_DIRECTORY)
    try:
        model.manager.verify_descriptor(fd, witness.to_dict())
        model.tenant.rename(model.instances / "renamed")
        model.tenant.mkdir()
        if os.geteuid() == 0:
            os.chown(model.tenant, 4242, 4242)
        model.manager.verify_descriptor(fd, witness)
        replacement = os.open(model.tenant, quota._OPEN | os.O_DIRECTORY)
        try:
            with pytest.raises(quota.QuotaError, match="does not match"):
                model.manager.verify_descriptor(replacement, witness)
        finally:
            os.close(replacement)
    finally:
        os.close(fd)


@pytest.mark.parametrize("tamper", ["project", "generation", "bytes", "inheritance", "accounting"])
def test_descriptor_refuses_mutated_kernel_state_after_path_verification(model, tamper):
    witness = prepare(model)
    fd = os.open(model.tenant, quota._OPEN | os.O_DIRECTORY)
    try:
        if tamper == "project":
            model.kernel.projects[witness.root_inode] = (quota._PROJINHERIT, witness.project_id + 1)
        elif tamper == "generation":
            model.kernel.generations[witness.root_inode] = witness.root_generation + 1
        elif tamper == "inheritance":
            model.kernel.projects[witness.root_inode] = (0, witness.project_id)
        elif tamper == "accounting":
            model.kernel.accounting = False
        else:
            model.kernel.quotas[witness.project_id] = (0, witness.inode_limit)
        with pytest.raises(quota.QuotaError):
            model.manager.verify_descriptor(fd, witness)
    finally:
        os.close(fd)


@pytest.mark.parametrize("witness", [{}, {"project_id": True}, None])
def test_descriptor_does_not_accept_untyped_or_incomplete_witness(model, witness):
    fd = os.open(model.tenant, quota._OPEN | os.O_DIRECTORY)
    try:
        with pytest.raises(quota.QuotaError):
            model.manager.verify_descriptor(fd, witness)
    finally:
        os.close(fd)
