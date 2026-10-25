"""A failed update leaves nothing behind, and one whose snapshot could not be taken prepares nothing.

Every attempt prepares a release (``releases/<commit>``, a virtual
environment of a few hundred MB) and, on a hosting server, a tenant image
(close to a GB). A hosting server whose automatic updates followed a branch
filled its disk that way: each new commit was prepared, then refused because
the pre-update snapshot no longer fitted, and only a successful update pruned
releases and images. Once the disk was full, SQLite failed and every wiki
answered 500. These tests run the real controller against the simulated host
of ``ops_fakes`` (hosting: the platform of ``test_ops_recovery``), with
Docker's tenant images kept in memory.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from ops_fakes import FakeSystem  # type: ignore[import-not-found]
from test_ops_lifecycle import fake_system as fake_system
from test_ops_lifecycle import legacy as legacy
from test_ops_lifecycle import managed_root as managed_root
from test_ops_lifecycle import upstream as upstream
from test_ops_recovery import assert_in_service, history, stops, with_project_quotas
from test_ops_recovery import hosting as hosting
from test_ops_tenant_quota_modes import OK, PREFLIGHT, candidate_preflight, enable_automatic_updates

from bananawiki.ops.files import Refused, read_json
from bananawiki.ops.manager import Manager
from bananawiki.ops.system import CandidateRefused, System

NO_ROOM = "Not enough free space for a pre-update snapshot"


class Images:
    """Docker's tenant images: preparing a hosting release builds one, ``docker image rm`` removes one no container
    uses (``in_use``), ``docker info`` keeps them under ``root_dir`` and each one is ``size`` bytes."""

    def __init__(self, system: FakeSystem, *tags: str):
        self.tags = set(tags)
        self.in_use: set[str] = set()
        self.root_dir = ""
        self.size = 800 * 2**20
        run, prepare = system.runner, system.prepare_release

        def runner(command, **options):
            result = run(command, **options)
            if command[:3] == ["docker", "image", "inspect"]:
                present = command[-1] in self.tags
                result.returncode = 0 if present else 1
                result.stdout = (str(self.size) if "{{.Size}}" in command else "sha256:0") + "\n" if present else ""
            elif command[:3] == ["docker", "image", "rm"]:
                if command[-1] in self.in_use:
                    result.returncode, result.stderr = 1, "Error: image is being used by a running container\n"
                else:
                    self.tags.discard(command[-1])
            elif command[:3] == ["docker", "image", "ls"]:
                result.stdout = "".join(tag + "\n" for tag in sorted(self.tags))
            elif command[:2] == ["docker", "info"] and "{{.DockerRootDir}}" in command:
                result.stdout = self.root_dir + "\n"
            return result

        def prepare_release(settings, release, features):
            warnings = prepare(settings, release, features)
            if settings["mode"] == "hosting":
                self.tags.add(tag(settings["revision"]))
            return warnings

        system.runner = runner
        system.prepare_release = prepare_release  # type: ignore[method-assign]


def tag(revision: str) -> str:
    return f"bananawiki-tenant:{revision}"


def free_space(volume: str, free: int):
    """``shutil.disk_usage``, answering *free* bytes for paths whose name starts with *volume*."""
    real = shutil.disk_usage

    def usage(path):
        result = real(path)
        return result._replace(free=free) if Path(path).name.startswith(volume) else result

    return usage


def removals(system: FakeSystem) -> list[str]:
    return [command[-1] for command in system.commands if command[:3] == ["docker", "image", "rm"]]


# The snapshot is checked before the release is prepared


def test_an_update_whose_snapshot_would_not_fit_prepares_nothing(managed_root, fake_system, upstream, legacy,
                                                                 monkeypatch):
    manager = Manager(managed_root, system=fake_system)
    new = upstream.commit("1.6.1")
    monkeypatch.setattr(shutil, "disk_usage", free_space("staging", 0))
    with pytest.raises(Refused, match=NO_ROOM):
        manager.update()
    assert fake_system.prepared == [] and not (managed_root / "releases" / new).exists()
    assert not [command for command in fake_system.commands if command[:2] == ["systemctl", "stop"]]
    status = read_json(managed_root / "config/status.json")
    assert status["outcome"] == "failed" and NO_ROOM in status["reason"]
    # Not the revision's failure, and every automatic run costs nothing until there is room.
    assert not (managed_root / "config/failed-revision.json").exists()
    enable_automatic_updates(managed_root)
    with pytest.raises(Refused, match=NO_ROOM):
        manager.update(automatic=True)
    assert fake_system.prepared == []
    monkeypatch.undo()
    assert manager.update(automatic=True)["outcome"] == "complete"
    assert fake_system.prepared == [new] and (managed_root / "current").resolve().name == new


def test_the_check_counts_the_release_the_update_would_prepare(managed_root, fake_system, upstream, legacy,
                                                               monkeypatch):
    """Preparing it takes about what the running release takes: that room must be left for the snapshot too."""
    manager = Manager(managed_root, system=fake_system)
    settings = manager.settings()
    running = managed_root / "releases" / legacy
    assert manager.preparation_bytes(settings) >= sum(path.stat().st_size for path in running.rglob("*")
                                                      if path.is_file())
    new = upstream.commit("1.6.1")
    monkeypatch.setattr(Manager, "preparation_bytes",
                        lambda self, settings: shutil.disk_usage(self.root / "staging").free)
    with pytest.raises(Refused, match=NO_ROOM):
        manager.update()
    assert fake_system.prepared == [] and not (managed_root / "releases" / new).exists()
    # A release already prepared takes nothing more.
    manager.stage({**settings, "revision": new})
    assert manager.update()["outcome"] == "complete" and fake_system.prepared == [new]


def test_the_tenant_image_counts_where_docker_keeps_it(tmp_path):
    volume = tmp_path / "staging"
    volume.mkdir()
    answers = {"info": (0, f"{tmp_path}\n"), "inspect": (0, "781000000\n")}

    def runner(command, **options):
        code, output = answers["info" if command[:2] == ["docker", "info"] else "inspect"]
        return subprocess.CompletedProcess(command, code, output, "")

    system = System(runner=runner, log_dir=None)
    assert system.image_bytes(tag("a" * 40), volume) == 781000000
    answers["info"] = (0, "/proc\n")  # another filesystem
    assert system.image_bytes(tag("a" * 40), volume) == 0
    answers["info"] = (1, "")  # Docker did not answer
    assert system.image_bytes(tag("a" * 40), volume) == 0
    answers.update(info=(0, f"{tmp_path}\n"), inspect=(1, ""))  # no such image
    assert system.image_bytes(tag("a" * 40), volume) == 0


def test_a_hosting_update_counts_the_tenant_image_it_would_build(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    images = Images(system, tag(hosting.old))
    new = hosting.upstream.commit("1.6.1")
    images.root_dir = str(root)
    images.size = shutil.disk_usage(root / "staging").free  # one more image would leave no room for the snapshot
    stopped = stops(system)
    with pytest.raises(Refused, match=NO_ROOM):
        manager.update()
    assert system.prepared == [] and images.tags == {tag(hosting.old)} and stops(system) == stopped
    assert_in_service(root, hosting.wikis)
    images.root_dir = "/proc"  # Docker keeps its images on another filesystem
    assert manager.update()["outcome"] == "complete" and (root / "current").resolve().name == new


# A failed update removes what it prepared


def test_a_snapshot_refused_after_preparing_removes_the_release(managed_root, fake_system, upstream, legacy,
                                                                monkeypatch, capsys):
    """Preparing the release can itself take the room the snapshot needed."""
    manager = Manager(managed_root, system=fake_system)
    new = upstream.commit("1.6.1")
    monkeypatch.setattr(shutil, "disk_usage", free_space("snapshot-", 0))
    with pytest.raises(Refused, match=NO_ROOM):
        manager.update()
    assert fake_system.prepared == [new] and not (managed_root / "releases" / new).exists()
    assert (managed_root / "releases" / legacy).is_dir() and (managed_root / "current").resolve().name == legacy
    event = history(managed_root)[-1]
    assert event["outcome"] == "failed" and event["discarded"] == [f"releases/{new}"]
    assert f"Removed what the failed update prepared: releases/{new}." in capsys.readouterr().err
    # Still not the revision's failure: the next automatic run tries it again.
    assert not (managed_root / "config/failed-revision.json").exists()
    monkeypatch.undo()
    enable_automatic_updates(managed_root)
    assert manager.update(automatic=True)["outcome"] == "complete" and fake_system.prepared == [new, new]


def test_a_rolled_back_update_removes_its_release_and_image_but_not_the_running_or_rollback_ones(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    images = Images(system, tag(hosting.old))
    images.in_use.add(tag(hosting.old))
    good = hosting.upstream.commit("1.6.1")
    assert manager.update()["outcome"] == "complete"
    assert read_json(root / "config/last-update.json")["previous_revision"] == hosting.old
    bad = hosting.upstream.commit("1.6.2")
    system.unhealthy_revisions.add(bad)
    with pytest.raises(RuntimeError):
        manager.update()
    assert (root / "current").resolve().name == good
    assert sorted(path.name for path in (root / "releases").iterdir()) == sorted([hosting.old, good])
    assert images.tags == {tag(hosting.old), tag(good)}
    event = history(root)[-1]
    assert event["outcome"] == "rolled_back" and event["discarded"] == [f"releases/{bad}", tag(bad)]
    assert_in_service(root, hosting.wikis)
    # Never the running release or the one 'rollback' returns to.
    kept = manager.discard(good), manager.discard(hosting.old)
    assert kept == ({}, {}) and removals(system) == [tag(bad)]
    assert (root / "releases" / good).is_dir() and (root / "releases" / hosting.old).is_dir()


def test_a_release_its_preflight_refused_is_removed_and_not_prepared_again_every_hour(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    images = Images(system, tag(hosting.old))
    new = hosting.upstream.commit("1.7.1", extra=PREFLIGHT)
    with_project_quotas(hosting, enforced=True)
    asked = candidate_preflight(system, (3, {"ok": False, "protocol": 1, "reason": "Needs a newer Docker."}), OK)
    with pytest.raises(CandidateRefused):
        manager.update()
    assert not (root / "releases" / new).exists() and images.tags == {tag(hosting.old)}
    assert read_json(root / "config/failed-revision.json")["stage"] == "preflight"
    assert history(root)[-1]["discarded"] == [f"releases/{new}", tag(new)]
    enable_automatic_updates(root)
    for _hour in range(3):
        assert manager.update(automatic=True)["outcome"] == "paused"
    assert system.prepared == [new] and len(asked) == 1
    # The operator fixed the cause: a manual update prepares it again.
    assert manager.update()["outcome"] == "complete" and system.prepared == [new, new]
    assert (root / "current").resolve().name == new and tag(new) in images.tags


def test_removing_what_a_failed_update_prepared_is_best_effort(hosting, capsys):
    """The update's own error is what the operator gets; what could not be removed is reported with it."""
    root, system, manager = hosting.root, hosting.system, hosting.manager
    images = Images(system, tag(hosting.old))
    new = hosting.upstream.commit("1.6.1")
    images.in_use.add(tag(new))
    prepare = system.prepare_release

    def prepare_then_fail(settings, release, features):
        prepare(settings, release, features)  # the tenant image is built
        raise RuntimeError("Sealing the release failed.")

    system.prepare_release = prepare_then_fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Sealing the release failed"):
        manager.update()
    assert not (root / "releases" / new).exists() and tag(new) in images.tags
    event = history(root)[-1]
    assert event["outcome"] == "failed" and "discarded" not in event
    assert tag(new) in event["cleanup_warning"] and "Docker kept it" in event["cleanup_warning"]
    assert "Warning: What the failed update prepared could not all be removed" in capsys.readouterr().err
    assert read_json(root / "config/failed-revision.json")["stage"] == "preparation"
    assert (root / "current").resolve().name == hosting.old
    assert_in_service(root, hosting.wikis)


def test_nothing_is_removed_while_a_transaction_is_recorded(hosting):
    """Its recovery may still need the release; the next command finishes it first."""
    root, system, manager = hosting.root, hosting.system, hosting.manager
    new = "b" * 40
    images = Images(system, tag(hosting.old), tag(new))
    (root / "releases" / new).mkdir()
    (root / "config/transaction.json").write_text("{}")
    result = manager.discard(new)
    assert result == {}
    assert (root / "releases" / new).is_dir() and tag(new) in images.tags and removals(system) == []
    (root / "config/transaction.json").unlink()
    result = manager.discard(new)
    assert result == {"discarded": [f"releases/{new}", tag(new)]}
    assert not (root / "releases" / new).exists() and images.tags == {tag(hosting.old)}
