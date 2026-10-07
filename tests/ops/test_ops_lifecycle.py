"""End to end: a 1.4-installed server is updated by the 1.6 controller, then rolled back."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ops_fakes import FakeSystem, Upstream, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops import UNIT_GENERATION
from bananawiki.ops.files import read_environment, read_json, read_package, write_json
from bananawiki.ops.manager import Manager


@pytest.fixture
def fake_system(tmp_path) -> FakeSystem:
    return FakeSystem(tmp_path / "host")


@pytest.fixture
def upstream(tmp_path) -> Upstream:
    return Upstream(tmp_path)


@pytest.fixture
def managed_root(tmp_path) -> Path:
    return tmp_path / "opt" / "bananawiki"


def titles(root: Path) -> list[str]:
    connection = sqlite3.connect(root / "data/bananawiki.db")
    try:
        return [row[0] for row in connection.execute("SELECT title FROM pages ORDER BY id")]
    finally:
        connection.close()


def add_page(root: Path, title: str) -> None:
    connection = sqlite3.connect(root / "data/bananawiki.db")
    connection.execute("INSERT INTO pages (title) VALUES (?)", (title,))
    connection.commit()
    connection.close()


@pytest.fixture
def legacy(managed_root, fake_system, upstream):
    old = upstream.commit("1.4 release", modern=False)
    legacy_install(managed_root, fake_system, upstream, old)
    return old


def test_update_from_legacy_layout_then_rollback(managed_root, fake_system, upstream, legacy):
    new = upstream.commit("1.6 release")
    migrated = []

    def migrate_on_start(names):
        # The new release migrates the database when it starts.
        if (managed_root / "current").resolve().name == new and not migrated:
            add_page(managed_root, "written by 1.6")
            migrated.append(True)

    fake_system.on_start.append(migrate_on_start)
    manager = Manager(managed_root, system=fake_system)
    result = manager.update()

    assert result["outcome"] == "complete"
    assert result["previous_revision"] == legacy and result["revision"] == new
    assert (managed_root / "current").resolve().name == new
    settings = read_json(managed_root / "config/installation.json")
    assert settings["revision"] == new and settings["schema"] == 1
    # Converged, hardened units; the wrapper still runs current/banana.
    unit = (fake_system.unit_dir / "bananawiki.service").read_text()
    assert UNIT_GENERATION in unit and "CapabilityBoundingSet=\n" in unit and "wsgi:app" in unit
    assert (fake_system.bin_dir / "bananawiki").read_text().endswith(
        f'{managed_root}/current/banana --root {managed_root} "$@"\n')
    # Operator settings survive; controller-owned values are refreshed.
    environment = read_environment(managed_root / "config/app.env")
    assert environment["BW_CUSTOM"] == "kept"
    assert not (managed_root / "config/transaction.json").exists()
    assert not (managed_root / "data/.banana-maintenance").exists()
    assert titles(managed_root) == ["Home", "written by 1.6"]
    # The rollback package was written from the pre-update snapshot.
    package = Path(read_json(managed_root / "config/last-update.json")["backup"])
    assert package.name.startswith("before-update-") and package.is_file()
    with read_package(package, "BananaWiki", managed_root / "staging") as (tree, manifest):
        assert manifest["revision"] == legacy
        assert (tree / "data/uploads/picture.png").stat().st_size == 200_004
        assert (tree / "config/installation.json").is_file() and (tree / "source.tar.gz").is_file()
    assert not list((managed_root / "staging").glob("snapshot-*"))

    rolled = manager.restore(package)
    assert rolled["outcome"] == "complete"
    assert (managed_root / "current").resolve().name == legacy
    assert titles(managed_root) == ["Home"]
    # 1.4 releases get the exact 1.4 units back.
    assert UNIT_GENERATION not in (fake_system.unit_dir / "bananawiki.service").read_text()
    assert list((managed_root / "backups").glob("before-restore-*.tar.gz"))


def test_failed_readiness_rolls_back_data_and_release(managed_root, fake_system, upstream, legacy):
    bad = upstream.commit("broken release")
    fake_system.unhealthy_revisions.add(bad)

    def damage(names):
        if (managed_root / "current").resolve().name == bad:
            add_page(managed_root, "half-migrated")
            (managed_root / "data/uploads/picture.png").unlink()

    fake_system.on_start.append(damage)
    manager = Manager(managed_root, system=fake_system)
    with pytest.raises(RuntimeError):
        manager.update()
    assert (managed_root / "current").resolve().name == legacy
    assert titles(managed_root) == ["Home"]
    assert (managed_root / "data/uploads/picture.png").is_file()
    assert read_json(managed_root / "config/failed-revision.json") == {"revision": bad}
    assert read_json(managed_root / "config/installation.json")["revision"] == legacy
    assert not (managed_root / "config/transaction.json").exists()
    assert read_json(managed_root / "config/status.json")["outcome"] == "rolled_back"
    assert not list((managed_root / "staging").glob("snapshot-*"))
    # An automatic run skips the known-bad revision.
    write_json(managed_root / "config/updates.json", {"enabled": True, "interval_minutes": 60, "keep_backups": 3})
    assert manager.update(automatic=True)["outcome"] == "paused"


def test_portable_backup_preserves_update_trust(managed_root, fake_system, upstream, legacy, tmp_path):
    manager = Manager(managed_root, system=fake_system)
    signers = tmp_path / "allowed_signers"
    signers.write_text('fixture@example.invalid ssh-ed25519 fixture-public-key\n')
    manager.configure_source(signers_file=signers)
    package = manager.backup()
    restored = Manager(tmp_path / "restored", system=FakeSystem(tmp_path / "restored-host"))
    restored.restore(package, new=True)
    assert restored.source()["signing"] == "ssh"
    trust = restored.config_dir / "repo.allowed_signers"
    assert trust.read_text() == signers.read_text()
    assert trust.stat().st_mode & 0o777 == 0o600
    assert restored.policy()["enabled"] is False


def test_failed_restore_rolls_back_repository_credentials_and_update_trust(
        managed_root, fake_system, upstream, legacy, tmp_path):
    manager = Manager(managed_root, system=fake_system)
    signers = tmp_path / "allowed_signers"
    signers.write_text('old@example.invalid ssh-ed25519 old-public-key\n')
    manager.configure_source(signers_file=signers)
    package = manager.backup()
    manager.configure_source(clear_signatures=True)
    live = upstream.commit("new release")
    manager.update()
    signers.write_text('live@example.invalid ssh-ed25519 live-public-key\n')
    token = tmp_path / "token"
    token.write_text("fixture-live-repository-token")
    manager.configure_source(url="https://example.invalid/live.git", branch="live",
                             token_file=token, signers_file=signers)
    configuration = manager.source()
    fake_system.unhealthy_revisions.add(legacy)
    with pytest.raises(RuntimeError, match="readiness"):
        manager.restore(package)
    assert manager.settings()["revision"] == live
    assert manager.source() == configuration
    assert (manager.config_dir / "repo.token").read_text().strip() == token.read_text()
    assert (manager.config_dir / "repo.allowed_signers").read_text() == signers.read_text()
    assert not (manager.config_dir / "transaction.json").exists()


def test_divergent_history_pauses(managed_root, fake_system, upstream, legacy):
    upstream.orphan("unrelated history")
    manager = Manager(managed_root, system=fake_system)
    assert manager.update()["outcome"] == "paused"
    assert (managed_root / "current").resolve().name == legacy


def test_current_revision_converges_units_installed_by_old_updater(managed_root, fake_system, upstream):
    new = upstream.commit("1.6 installed by the 1.4 updater")
    legacy_install(managed_root, fake_system, upstream, new)
    manager = Manager(managed_root, system=fake_system)
    assert manager.status()["units_current"] is False
    result = manager.update()
    assert result["outcome"] == "current" and result["units_converged"] is True
    assert manager.status()["units_current"] is True
    assert manager.update()["units_converged"] is False


def test_hosting_convergence_replaces_docker_group_with_agent(managed_root, fake_system, upstream):
    new = upstream.commit("1.6 hosting")
    legacy_install(managed_root, fake_system, upstream, new, mode="hosting")
    fake_system.docker_group = True
    manager = Manager(managed_root, system=fake_system)
    manager.update()
    agent = (fake_system.unit_dir / "bananawiki-agent.service").read_text()
    assert "User=root" in agent and "agent\" \"serve\"" in agent
    portal = (fake_system.unit_dir / "bananawiki.service").read_text()
    assert "SupplementaryGroups=docker" not in portal and "After=network-online.target bananawiki-agent" in portal
    assert fake_system.docker_group is False
    environment = read_environment(managed_root / "config/app.env")
    assert environment["BW_RUNTIME_AGENT_SOCKET"] == "/run/bananawiki-agent/agent.sock"
    assert environment["HOSTING_CONTAINER_IMAGE"] == f"bananawiki-tenant:{new}"


def test_failed_convergence_restores_previous_units(managed_root, fake_system, upstream):
    new = upstream.commit("1.6")
    legacy_install(managed_root, fake_system, upstream, new)
    before = (fake_system.unit_dir / "bananawiki.service").read_text()
    fake_system.unhealthy_revisions.add(new)
    with pytest.raises(RuntimeError):
        Manager(managed_root, system=fake_system).update()
    assert (fake_system.unit_dir / "bananawiki.service").read_text() == before
    assert "bananawiki" in fake_system.running


def test_interrupted_recovery_is_finished_by_the_next_command(managed_root, fake_system, upstream, legacy):
    bad = upstream.commit("broken")
    fake_system.unhealthy_revisions.add(bad)
    manager = Manager(managed_root, system=fake_system)
    original = manager.recover
    calls = []

    def crash_once():
        if not calls and (managed_root / "config/transaction.json").exists():
            calls.append(1)
            raise KeyboardInterrupt  # the operator's session died during rollback
        return original()

    manager.recover = crash_once  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        manager.update()
    journal = read_json(managed_root / "config/transaction.json")
    assert journal["phase"] == "snapshotted" and journal["candidate"] == bad
    assert Manager(managed_root, system=fake_system).status()["recovery_pending"] is True
    fresh = Manager(managed_root, system=fake_system)
    assert fresh.recover() is True
    assert (managed_root / "current").resolve().name == legacy
    assert not (managed_root / "config/transaction.json").exists()
    assert read_json(managed_root / "config/status.json")["data_restored"] is True


def test_recovers_a_journal_written_by_the_1x_updater(managed_root, fake_system, upstream, legacy):
    manager = Manager(managed_root, system=fake_system)
    package = manager.backup()
    settings = read_json(managed_root / "config/installation.json")
    add_page(managed_root, "after the package")
    write_json(managed_root / "config/transaction.json", {
        "settings": settings, "active": ["bananawiki", "bananawiki-tts"], "containers": [],
        "backup": str(package), "candidate": "f" * 40, "phase": "backed_up"})
    assert manager.recover() is True
    assert titles(managed_root) == ["Home"]


def test_backup_is_short_downtime_and_restorable(managed_root, fake_system, upstream, legacy):
    manager = Manager(managed_root, system=fake_system)
    add_page(managed_root, "second")
    package = manager.backup()
    assert package.name.startswith("manual-")
    assert {"bananawiki", "bananawiki-tts"} <= fake_system.running
    with read_package(package, "BananaWiki", managed_root / "staging") as (tree, manifest):
        connection = sqlite3.connect(tree / "data/bananawiki.db")
        assert [row[0] for row in connection.execute("SELECT title FROM pages")] == ["Home", "second"]
        connection.close()
        assert "data/bananawiki.db-wal" not in manifest["files"]
    with pytest.raises(ValueError):
        manager.backup(managed_root / "data" / "inside.tar.gz")


def test_pruning_keeps_rollback_target_and_newest(managed_root, fake_system, upstream, legacy):
    manager = Manager(managed_root, system=fake_system)
    backups = managed_root / "backups"
    names = [f"before-update-2026010{index}T000000Z-aaaaaa.tar.gz" for index in range(1, 7)]
    for name in names:
        (backups / name).write_bytes(b"x")
    write_json(managed_root / "config/last-update.json", {"backup": str(backups / names[0])})
    removed = manager.prune_backups()
    remaining = sorted(path.name for path in backups.iterdir())
    assert remaining == sorted([names[0], *names[-3:]])
    assert set(removed) == set(names[1:3])


def test_update_prunes_tenant_images(managed_root, fake_system, upstream):
    first = upstream.commit("hosting 1")
    legacy_install(managed_root, fake_system, upstream, first, mode="hosting")
    second = upstream.commit("hosting 2")
    images = f"bananawiki-tenant:{'0' * 40}\nbananawiki-tenant:{first}\nbananawiki-tenant:{second}\n"
    original = fake_system._run

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if command[:3] == ["docker", "image", "ls"]:
            result.stdout = images
        return result

    fake_system.runner = run
    result = Manager(managed_root, system=fake_system).update()
    assert result["images_removed"] == [f"bananawiki-tenant:{'0' * 40}"]


def test_update_reports_a_tenant_image_built_on_a_cached_base(managed_root, fake_system, upstream):
    first = upstream.commit("hosting 1")
    legacy_install(managed_root, fake_system, upstream, first, mode="hosting")
    second = upstream.commit("hosting 2")
    fake_system.build_warnings = ["Could not pull python:3.12-slim-trixie: built on the cached copy."]
    manager = Manager(managed_root, system=fake_system)
    result = manager.update()
    assert result["outcome"] == "complete" and result["image_warnings"] == fake_system.build_warnings
    original = fake_system._run

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if command[:3] == ["docker", "image", "inspect"]:
            result.stdout = "2026-10-04T08:00:00Z\n"
        return result

    fake_system.runner = run
    assert manager.status()["tenant_image_built"] == "2026-10-04T08:00:00Z"
    assert ["docker", "image", "inspect", "--format", "{{.Created}}", f"bananawiki-tenant:{second}"] \
        in fake_system.commands


def test_automatic_update_respects_policy_and_stopped_service(managed_root, fake_system, upstream, legacy):
    upstream.commit("1.6")
    manager = Manager(managed_root, system=fake_system)
    assert manager.update(automatic=True) == {"outcome": "disabled"}
    manager.set_updates(True, interval=30, keep=4)
    timer = (fake_system.unit_dir / "bananawiki-update.timer").read_text()
    assert "OnUnitActiveSec=30min" in timer
    fake_system.running.clear()
    assert manager.update(automatic=True)["outcome"] == "skipped"


def test_lock_excludes_concurrent_operations(managed_root, fake_system, upstream, legacy):
    from bananawiki.ops.files import maintenance_lock

    manager = Manager(managed_root, system=fake_system)
    with maintenance_lock(managed_root), pytest.raises(RuntimeError, match="Another maintenance"):
        manager.update()


def test_uninstall_keeps_data_and_removes_units(managed_root, fake_system, upstream, legacy):
    manager = Manager(managed_root, system=fake_system)
    with pytest.raises(ValueError):
        manager.uninstall(purge=True, confirm="wrong")
    assert manager.uninstall()["data_preserved"] is True
    assert not (fake_system.unit_dir / "bananawiki.service").exists()
    assert not (fake_system.bin_dir / "bananawiki").exists()
    assert (managed_root / "data/bananawiki.db").exists()
    assert read_json(managed_root / "config/installation.json")["installed"] is False
