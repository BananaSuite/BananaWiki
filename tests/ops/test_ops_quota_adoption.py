"""Wikis that 1.6 left above their plan still start once 1.7 puts them under hard quotas.

1.6 suspended a wiki only above 110% of its plan, measured apparent sizes
(XFS charges allocated blocks), never limited administrators' and apex wikis,
and let a plan be lowered below what a wiki held. The first 1.7 start adopts
such a wiki with a hard limit just above what it holds (its plan when that is
larger), records it as over its plan, and never gives it more room on later
starts: it keeps serving, and cannot grow past that limit until data is
removed or its plan is raised.
"""

from __future__ import annotations

import os
import sqlite3

import pytest
from test_ops_project_quota import model as model  # noqa: F401 - the fake XFS fixture

from bananawiki.ops import project_quota as quota

MIB = 1024 * 1024
PLAN = 8 * MIB


@pytest.fixture(autouse=True)
def small_headroom(monkeypatch):
    monkeypatch.setattr(quota, "ADOPTION_HEADROOM_BYTES", MIB)
    monkeypatch.setattr(quota, "ADOPTION_HEADROOM_INODES", 16)


def usage(model, witness):
    actual = model.kernel.limits(0, witness.project_id)
    return actual.bcount * 512, actual.icount


def plan_columns(model):
    with sqlite3.connect(model.manager.state_dir / "allocations.sqlite3") as connection:
        return connection.execute("SELECT bytes,inodes,plan_bytes,plan_inodes FROM allocations").fetchall()


def fill(directory, megabytes):
    (directory / "attachments").mkdir(exist_ok=True)
    for number in range(megabytes):
        (directory / "attachments" / f"file-{number}").write_bytes(os.urandom(MIB))


def test_an_existing_wiki_above_its_plan_is_adopted_just_above_what_it_holds(model):
    fill(model.tenant, 9)
    witness = model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128, repair=True)
    used_bytes, used_inodes = usage(model, witness)
    assert used_bytes > PLAN
    assert witness.byte_limit == used_bytes + MIB and witness.inode_limit == 128
    assert model.manager.over_plan(witness, byte_limit=PLAN, inode_limit=128)
    assert plan_columns(model) == [(used_bytes + MIB, 128, PLAN, 128)]
    # The portal keeps asking for the plan: the adopted allocation answers for it everywhere.
    assert model.manager.verify("alpha", byte_limit=PLAN, inode_limit=128) == witness
    assert model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128) == witness  # running
    descriptor = os.open(model.tenant, os.O_RDONLY | os.O_DIRECTORY)
    try:
        model.manager.verify_descriptor(descriptor, witness)
    finally:
        os.close(descriptor)


def test_a_wiki_above_the_host_ceiling_or_the_inode_limit_is_adopted_too(model):
    # The fixture's host ceiling is 64 MiB and 1000 inodes; an unlimited (admin or apex) wiki gets the ceiling.
    fill(model.tenant, 66)
    for number in range(1100):
        (model.tenant / f"note-{number}").write_text("x")
    witness = model.manager.prepare("alpha", byte_limit=64 * MIB, inode_limit=1000, repair=True)
    used_bytes, used_inodes = usage(model, witness)
    assert witness.byte_limit == used_bytes + MIB > 64 * MIB
    assert witness.inode_limit == used_inodes + 16 > 1000
    assert model.manager.verify("alpha", byte_limit=64 * MIB, inode_limit=1000) == witness


def test_restarts_never_grant_more_room_and_freed_space_is_given_back(model):
    fill(model.tenant, 9)
    adopted = model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128, repair=True)
    # The fake kernel does not enforce limits: write up to (and past) the adopted limit.
    (model.tenant / "attachments" / "growth").write_bytes(os.urandom(2 * MIB))
    again = model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128, repair=True)
    assert again.byte_limit == usage(model, again)[0]  # what it holds, never a fresh headroom
    for number in range(3):
        (model.tenant / "attachments" / f"file-{number}").unlink()
    (model.tenant / "attachments" / "growth").unlink()
    shrunk = model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128, repair=True)
    assert shrunk.byte_limit == PLAN  # back within its plan: an ordinary allocation again
    assert not model.manager.over_plan(shrunk, byte_limit=PLAN, inode_limit=128)
    assert plan_columns(model) == [(PLAN, 128, 0, 0)]
    assert adopted.project_id == again.project_id == shrunk.project_id


def test_a_raised_plan_replaces_the_adopted_limit(model):
    fill(model.tenant, 9)
    model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128, repair=True)
    raised = model.manager.prepare("alpha", byte_limit=32 * MIB, inode_limit=128)
    assert raised.byte_limit == 32 * MIB
    assert plan_columns(model) == [(32 * MIB, 128, 0, 0)]
    with pytest.raises(quota.QuotaError, match="differ"):
        model.manager.verify("alpha", byte_limit=PLAN, inode_limit=128)


def test_a_plan_lowered_below_what_a_stopped_wiki_holds_does_not_keep_it_down(model):
    witness = model.manager.prepare("alpha", byte_limit=32 * MIB, inode_limit=128, repair=True)
    fill(model.tenant, 9)
    # A running wiki is never shrunk below its usage...
    with pytest.raises(quota.QuotaError, match="below current"):
        model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128)
    # ...but once stopped it starts again, held at what it holds plus the headroom, never above its old limit.
    lowered = model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128, repair=True)
    assert lowered.project_id == witness.project_id
    assert lowered.byte_limit == usage(model, lowered)[0] + MIB < 32 * MIB


def test_adopting_existing_data_is_not_refused_by_capacity_admission(model):
    """The data is already on the volume: 1.6 admitted it, so the first 1.7 start must not refuse it."""
    fill(model.tenant, 3)
    model.free.bytes = model.free.total = 2 * MIB
    witness = model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128, repair=True)
    assert witness.byte_limit == PLAN
    # A new, empty wiki is still admitted against capacity.
    beta = model.instances / "beta"
    beta.mkdir()
    if os.geteuid() == 0:
        os.chown(beta, 4242, 4242)
    with pytest.raises(quota.QuotaError, match="capacity"):
        model.manager.prepare("beta", byte_limit=PLAN, inode_limit=128)


def test_wikis_adopted_without_admission_do_not_block_every_later_snapshot(model):
    # 1.6 never admitted plans against the volume, so adopted plans may add up to more
    # than it holds. Their unused room must not count in the staging reserve, or it
    # exceeds the free space and refuses every backup, update and rollback snapshot.
    model.free.bytes = model.free.total = 20 * MIB
    beta = model.instances / "beta"
    beta.mkdir()
    if os.geteuid() == 0:
        os.chown(beta, 4242, 4242)
    fill(model.tenant, 2)
    fill(beta, 2)
    witnesses = [model.manager.prepare(name, byte_limit=32 * MIB, inode_limit=128, repair=True)
                 for name in ("alpha", "beta")]
    assert [witness.byte_limit for witness in witnesses] == [32 * MIB, 32 * MIB]
    _filesystem, reserved = model.manager.reserved_bytes()
    # Snapshots leave each such wiki its adoption headroom, not the whole unused plan.
    room = quota.ADOPTION_HEADROOM_BYTES + quota.ADOPTION_HEADROOM_INODES * 4096
    assert reserved == model.manager.storage_reserve_bytes + 2 * room < model.free.bytes
    # ... and the over-commitment is reported instead of hidden.
    assert model.manager.overcommitted_bytes() > 0
    # Restarting them changes nothing while the volume cannot hold their plans.
    assert model.manager.prepare("alpha", byte_limit=32 * MIB, inode_limit=128, repair=True) == witnesses[0]
    assert model.manager.reserved_bytes()[1] == reserved
    # Once it can (space freed, plans lowered), the next start admits them, and their whole limits count.
    model.free.bytes = model.free.total = 512 * MIB
    for name in ("alpha", "beta"):
        model.manager.prepare(name, byte_limit=32 * MIB, inode_limit=128)
    assert model.manager.overcommitted_bytes() == 0
    assert model.manager.reserved_bytes()[1] > model.manager.storage_reserve_bytes + 2 * 28 * MIB


def test_registries_written_before_plans_were_recorded_are_upgraded(model):
    first = model.manager.prepare("alpha", byte_limit=PLAN, inode_limit=128)
    with sqlite3.connect(model.manager.state_dir / "allocations.sqlite3") as connection:
        connection.execute("ALTER TABLE allocations DROP COLUMN plan_bytes")
        connection.execute("ALTER TABLE allocations DROP COLUMN plan_inodes")
    assert model.manager.verify("alpha", byte_limit=PLAN, inode_limit=128) == first
    assert plan_columns(model) == [(PLAN, 128, 0, 0)]


# The runtime agent

from dataclasses import replace  # noqa: E402

from test_ops_agent import runtime as runtime  # noqa: E402,F401 - the agent with a fake quota backend

from bananawiki.ops.runtime_agent import DEFAULT_STORAGE_BYTES, DEFAULT_STORAGE_INODES  # noqa: E402


def test_a_plan_above_the_host_ceiling_is_held_to_it_instead_of_refused(runtime):
    """1.6 let administrators set plans above what the 1.7 agent allows: the wiki still starts."""
    response = runtime.call("tenant.quota", {"tenant": "acme", "prepare": True,
                                             "limits": {"storage_bytes": 4 * DEFAULT_STORAGE_BYTES}})
    assert response["storage_quota"]["byte_limit"] == DEFAULT_STORAGE_BYTES
    assert response["over_plan"] is False
    runtime.call("tenant.start", {"tenant": "acme", "limits": {"storage_bytes": 4 * DEFAULT_STORAGE_BYTES}})
    assert any(call[1] == "run" for call in runtime.docker_calls.calls)


def test_the_agent_reports_a_wiki_adopted_above_its_plan(runtime):
    fake = runtime._injected_quota
    original = fake.prepare

    def adopted(tenant, *, byte_limit, inode_limit, **options):
        witness = original(tenant, byte_limit=byte_limit, inode_limit=inode_limit, **options)
        fake.entries[tenant] = replace(witness, byte_limit=byte_limit + MIB)
        return fake.entries[tenant]

    fake.prepare = adopted
    response = runtime.call("tenant.quota", {"tenant": "acme", "prepare": True, "limits": {"storage_bytes": PLAN}})
    assert response["over_plan"] is True
    assert response["plan"] == {"storage_bytes": PLAN, "inodes": DEFAULT_STORAGE_INODES}
    assert response["storage_quota"]["byte_limit"] == PLAN + MIB


def test_the_lifecycle_controller_warns_about_wikis_adopted_beyond_the_volume(model, monkeypatch, capsys, tmp_path):
    from bananawiki.ops import system as system_module

    model.free.bytes = model.free.total = 20 * MIB
    fill(model.tenant, 2)
    model.manager.prepare("alpha", byte_limit=32 * MIB, inode_limit=128, repair=True)
    monkeypatch.setattr(system_module, "load_config", lambda _root: {
        "instances_dir": str(model.instances),
        "quota": {"state_dir": str(model.manager.state_dir), "max_bytes": model.manager.max_bytes,
                  "max_inodes": model.manager.max_inodes, "project_start": model.manager.project_start,
                  "storage_reserve_bytes": model.manager.storage_reserve_bytes}})
    original = quota.ProjectQuota

    def with_fake_kernel(*args, **kwargs):
        manager = original(*args, **kwargs)
        manager._kernel = model.kernel
        return manager

    monkeypatch.setattr(system_module.project_quota, "ProjectQuota", with_fake_kernel)
    reserve = system_module.System().quota_reserve(tmp_path)
    assert reserve == model.manager.reserved_bytes()[1] < model.free.bytes
    assert "adopted from before 1.7 exceed the free space" in capsys.readouterr().err
