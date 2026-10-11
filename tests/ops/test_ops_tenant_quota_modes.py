"""The lifecycle controller and hosted wikis' storage: quota modes, the new release's own preflight, BuildKit.

An update must never stop wikis for a refusal it could have predicted (the
production incident in AUDIT.md: an update stopped the wikis of a server on
ext4 storage, waited out the readiness timeout on storage the new release
refused, rolled back, and did it again every hour). These tests run the real
controller against a simulated host (see ``ops_fakes``) with the hosting
platform of ``test_ops_recovery``.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
from ops_fakes import FakeSystem, Upstream, legacy_install  # type: ignore[import-not-found]
from test_ops_recovery import QUOTAS, assert_in_service, history, stops, with_project_quotas
from test_ops_recovery import hosting as hosting

from bananawiki.ops import cli, tenant_quotas
from bananawiki.ops.files import Refused, read_environment, read_json, write_environment, write_json
from bananawiki.ops.manager import Manager
from bananawiki.ops.system import BuildKitUnavailable, CandidateRefused, System, TenantStorageUnsupported

MODES = {**QUOTAS, "bananawiki/ops/tenant_quotas.py": "# BW_HOSTING_TENANT_QUOTAS\n"}  # 1.7.0 and later
PREFLIGHT = {**MODES, "bananawiki/ops/preflight.py": "# banana preflight\n"}


def configure(root: Path, value: str) -> None:
    environment = root / "config/app.env"
    write_environment(environment, {**read_environment(environment), "BW_HOSTING_TENANT_QUOTAS": value})


def quota_registry(system: FakeSystem) -> Path:
    """What the runtime agent leaves once it has put a wiki under quota."""
    directory = system.state_dir / "bananawiki-quotas"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "allocations.sqlite3").write_bytes(b"")
    return directory


@pytest.fixture
def kernel_with_ipv6(monkeypatch, tmp_path):
    """The preflight's IPv6 check, independent of the machine running the tests."""
    from bananawiki.ops import preflight

    (tmp_path / "proc-sys-net-ipv6").mkdir()
    monkeypatch.setattr(preflight, "IPV6_SYSCTLS", tmp_path / "proc-sys-net-ipv6")


def enable_automatic_updates(root: Path) -> None:
    write_json(root / "config/updates.json", {"enabled": True, "interval_minutes": 60, "keep_backups": 3})


# BW_HOSTING_TENANT_QUOTAS ----------------------------------------------------------------------------------------

LOST = "Hard quotas were enforced on this platform before and are no longer available: "
EXT4 = "Tenant storage must be an XFS filesystem with enforced project quotas"


@pytest.mark.parametrize(("configured", "problem", "before", "expected"), [
    # (enforced, refused, lost): XFS is never required, unless the operator asks for it.
    ("auto", None, False, (True, False, False)),
    ("auto", None, True, (True, False, False)),
    ("auto", EXT4, False, (False, False, False)),
    ("auto", EXT4, True, (False, False, True)),  # lost: the wikis still start, with a loud warning
    ("required", None, True, (True, False, False)),
    ("required", EXT4, False, (True, True, False)),
    ("required", EXT4, True, (True, True, False)),
    ("off", None, True, (False, False, False)),
    ("off", EXT4, True, (False, False, False)),
])
def test_the_three_settings_decide_as_documented(configured, problem, before, expected):
    policy = tenant_quotas.decide(configured, problem=lambda: problem, enforced_before=lambda: before)
    assert (policy.enforced, policy.refused, policy.lost) == expected
    assert policy.to_dict()["lost"] is policy.lost
    assert policy.severity == ("error" if policy.refused else "warning" if policy.lost
                               else None if policy.hard_quotas else "notice")
    if policy.lost:
        assert policy.warning().startswith(LOST + EXT4)
        assert "config set tenant-quotas required" in policy.warning()
        # Neutral about the cause: the storage, or an agent still under units an older controller wrote.
        assert "prjquota" in policy.warning() and "older controller" in policy.warning()
        assert "'bananawiki restart'" in policy.warning()
    elif not policy.hard_quotas and not policy.refused:
        assert "no longer available" not in policy.warning() and "prjquota" in policy.warning()


def test_new_platforms_default_to_auto():
    """The owner's decision: no installation or update ever requires XFS; required is an explicit opt-in."""
    assert tenant_quotas.INSTALL_DEFAULT == tenant_quotas.DEFAULT == "auto"


def test_auto_updates_a_platform_whose_storage_has_no_quotas_and_says_so(hosting):
    root, manager = hosting.root, hosting.manager
    new = hosting.upstream.commit("1.7.0", extra=MODES)
    with_project_quotas(hosting, enforced=False)
    result = manager.update()
    assert result["outcome"] == "complete" and (root / "current").resolve().name == new
    # A supported configuration: a calm note, not a warning.
    assert "Hard per-wiki storage quotas are not enforced" in result["storage_note"]
    assert "storage_warning" not in result
    status = manager.status()
    assert status["tenant_storage"] == {"setting": "auto", "hard_quotas": False, "refused": False, "lost": False,
                                        "reason": EXT4}
    assert any("prjquota" in note for note in status["notes"])
    assert not any("prjquota" in warning for warning in status.get("warnings", []))
    assert not any("older controller wrote" in warning for warning in status.get("warnings", []))
    # What an earlier release's updater leaves: its own units, under which the agent cannot use quotas.
    unit = hosting.system.unit_dir / "bananawiki-agent.service"
    unit.write_text(unit.read_text().replace("# bananawiki-ops: 3", "# bananawiki-ops: 2"))
    assert any("older controller wrote" in warning for warning in manager.status()["warnings"])
    assert manager.update()["units_converged"] is True
    assert_in_service(root, hosting.wikis)


def test_status_reports_what_an_agent_under_older_units_can_do(hosting):
    """After an older updater installed 1.7 on XFS: its units give the agent no quota system calls."""
    system, manager = hosting.system, hosting.manager
    hosting.upstream.commit("1.7.0", extra=MODES)
    with_project_quotas(hosting, enforced=True)
    manager.update()
    assert manager.status()["tenant_storage"]["hard_quotas"] is True
    older_units(system)
    storage = manager.status()["tenant_storage"]
    assert storage["hard_quotas"] is False and storage["refused"] is False
    assert "units an older controller wrote" in storage["reason"]
    assert cli.config_command(manager, ["get", "tenant-quotas"])["tenant_quotas"] == storage
    quota_registry(system)  # quotas were enforced here: the wikis still start, and status says loudly what was lost
    status = manager.status()
    assert status["tenant_storage"]["refused"] is False and status["tenant_storage"]["lost"] is True
    assert any(warning.startswith(LOST) and "older controller wrote" in warning for warning in status["warnings"])


def test_status_warns_when_tenant_plugins_run_without_hard_quotas(hosting):
    """Plugins run their owners' code with write access to the wiki's files: at disk speed, without a kernel limit."""
    root, manager = hosting.root, hosting.manager
    hosting.upstream.commit("1.7.0", extra=MODES)
    with_project_quotas(hosting, enforced=False)
    manager.update()
    environment = root / "config/app.env"
    write_environment(environment, {**read_environment(environment), "HOSTING_ALLOW_TENANT_PLUGINS": "yes"})
    assert any("HOSTING_ALLOW_TENANT_PLUGINS" in warning for warning in manager.status()["warnings"])
    with_project_quotas(hosting, enforced=True)
    assert not any("HOSTING_ALLOW_TENANT_PLUGINS" in warning for warning in manager.status().get("warnings", []))
    with_project_quotas(hosting, enforced=False)
    write_environment(environment, {**read_environment(environment), "HOSTING_ALLOW_TENANT_PLUGINS": "0"})
    assert not any("HOSTING_ALLOW_TENANT_PLUGINS" in warning for warning in manager.status().get("warnings", []))


def test_turning_quotas_off_on_xfs_says_that_the_limits_already_set_stay(hosting):
    system, manager = hosting.system, hosting.manager
    hosting.upstream.commit("1.7.0", extra=MODES)
    with_project_quotas(hosting, enforced=True)
    manager.update()
    assert "stay" not in cli.config_command(manager, ["set", "tenant-quotas", "off"])["note"]
    quota_registry(system)  # wikis were put under quota here
    note = cli.config_command(manager, ["set", "tenant-quotas", "off"])["note"]
    assert "limits already set on each wiki stay" in note and "xfs_quota" in note


def test_required_refuses_an_update_before_anything_stops(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    hosting.upstream.commit("1.7.0", extra=MODES)
    configure(root, "required")
    with_project_quotas(hosting, enforced=False)
    stopped = stops(system)
    with pytest.raises(TenantStorageUnsupported, match="BW_HOSTING_TENANT_QUOTAS=required"):
        manager.update()
    assert stops(system) == stopped and (root / "current").resolve().name == hosting.old
    assert_in_service(root, hosting.wikis)


def test_auto_keeps_the_wikis_running_once_quotas_are_lost_and_says_so_loudly(hosting, capsys):
    """prjquota lost (a remount, a replaced volume): the wikis start without hard quotas, never silently."""
    root, system, manager = hosting.root, hosting.system, hosting.manager
    new = hosting.upstream.commit("1.7.0", extra=MODES)
    quota_registry(system)
    with_project_quotas(hosting, enforced=False)
    result = manager.update()
    assert result["outcome"] == "complete" and (root / "current").resolve().name == new
    assert result["storage_warning"].startswith(LOST + EXT4) and "storage_note" not in result
    assert "Warning: " + LOST in capsys.readouterr().err
    assert_in_service(root, hosting.wikis)
    status = manager.status()
    assert status["tenant_storage"] == {"setting": "auto", "hard_quotas": False, "refused": False, "lost": True,
                                        "reason": EXT4}
    assert any(warning.startswith(LOST) for warning in status["warnings"])
    assert not any("no longer available" in note for note in status.get("notes", []))
    # Setting auto again (or getting it) says the same; required, the explicit opt-in, cannot be set on it.
    assert LOST in cli.config_command(manager, ["set", "tenant-quotas", "auto"])["note"]
    assert cli.config_command(manager, ["get", "tenant-quotas"])["tenant_quotas"]["lost"] is True
    with pytest.raises(TenantStorageUnsupported, match="Nothing was changed"):
        cli.config_command(manager, ["set", "tenant-quotas", "required"])
    # Turned off on purpose: no regression to warn about.
    configure(root, "off")
    status = manager.status()
    assert status["tenant_storage"]["setting"] == "off" and status["tenant_storage"]["lost"] is False
    assert not any("no longer available" in warning for warning in status.get("warnings", []))


def test_required_refuses_an_update_on_storage_whose_quotas_were_lost_before_anything_stops(hosting):
    """The explicit opt-in keeps 1.7.0's behaviour: hard quotas or nothing, refused before anything stops."""
    root, system, manager = hosting.root, hosting.system, hosting.manager
    hosting.upstream.commit("1.7.0", extra=MODES)
    quota_registry(system)
    configure(root, "required")
    with_project_quotas(hosting, enforced=False)
    stopped = stops(system)
    with pytest.raises(TenantStorageUnsupported, match="BW_HOSTING_TENANT_QUOTAS=required"):
        manager.update()
    assert stops(system) == stopped and (root / "current").resolve().name == hosting.old
    assert system.tenant_storage(manager.settings()).refused is True
    assert_in_service(root, hosting.wikis)


def test_a_quota_registry_the_controller_cannot_read_counts_as_used(hosting):
    """Never a quiet notice when it cannot be told (Python 3.14's Path.exists() answers False for such a path)."""
    hosting.upstream.commit("1.7.0", extra=MODES)
    registry = quota_registry(hosting.system)
    with_project_quotas(hosting, enforced=False)
    registry.chmod(0)
    try:
        policy = hosting.system.tenant_storage(hosting.manager.settings())
    finally:
        registry.chmod(0o700)
    assert policy.lost and not policy.refused and policy.warning().startswith(LOST)


def test_a_release_from_before_quota_modes_always_needs_quotas(hosting):
    """Such a release (9591b29, say) has no auto mode: whatever the setting, its agent refuses the storage."""
    hosting.upstream.commit("1.7.0-rc", extra=QUOTAS)
    with_project_quotas(hosting, enforced=False)
    with pytest.raises(TenantStorageUnsupported, match="confirm that the wiki storage .* enforces XFS"):
        hosting.manager.update()


def test_an_invalid_setting_is_refused_before_the_release_is_built(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    hosting.upstream.commit("1.7.0", extra=MODES)
    configure(root, "sometimes")
    with pytest.raises(Refused, match="auto, required or off"):
        manager.update()
    assert system.prepared == [] and not (root / "config/failed-revision.json").exists()
    assert_in_service(root, hosting.wikis)


# The new release's own preflight -----------------------------------------------------------------------------


def candidate_preflight(system: FakeSystem, *answers):
    """Answer ``banana preflight`` of the new release with *answers* in turn (exit code, JSON or text, or an error)."""
    asked: list[list[str]] = []
    original = system.runner
    queue = list(answers)

    def runner(command, **options):
        if command[-1] == "preflight":
            asked.append(command)
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, BaseException):
                raise answer
            code, output = answer
            text = output if isinstance(output, str) else json.dumps(output)
            return subprocess.CompletedProcess(command, code, text + "\n" if code in (0, 3) else "",
                                               "" if code in (0, 3) else text)
        return original(command, **options)

    system.runner = runner
    return asked


OK = (0, {"ok": True, "protocol": 1, "warnings": []})


def test_this_releases_preflight_answers_what_it_needs_from_the_server(hosting, capsys, kernel_with_ipv6):
    """The command an update of an earlier 1.7 controller runs in this release (the protocol above)."""
    from bananawiki.ops import preflight

    root, manager = hosting.root, hosting.manager
    with_project_quotas(hosting, enforced=False)
    assert preflight.run(manager) == 0
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert answer["ok"] is True and answer["protocol"] == 1
    # Storage that never had hard quotas is supported: a note, not a warning (an older controller ignores notes).
    assert any("Hard per-wiki storage quotas are not enforced" in note for note in answer["notes"])
    assert answer["warnings"] == []
    registry = quota_registry(hosting.system)  # quotas were enforced here before: lost, said loudly, not refused
    assert preflight.run(manager) == 0
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert answer["ok"] is True and any(warning.startswith(LOST) for warning in answer["warnings"])
    assert answer["notes"] == []
    (registry / "allocations.sqlite3").unlink()
    configure(root, "required")
    assert preflight.run(manager) == preflight.REFUSED
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert answer["ok"] is False and "BW_HOSTING_TENANT_QUOTAS=required" in answer["reason"]
    configure(root, "bogus")
    assert preflight.run(manager) == preflight.REFUSED
    assert "auto, required or off" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["reason"]
    assert_in_service(root, hosting.wikis)


def test_this_releases_preflight_refuses_what_would_keep_its_portal_or_wikis_down(hosting, capsys, monkeypatch,
                                                                                    kernel_with_ipv6):
    """Two causes of a rollback after the readiness timeout that the release can see before anything stops."""
    from bananawiki.ops import preflight

    root, manager = hosting.root, hosting.manager
    environment = root / "config/app.env"
    assert read_environment(environment)["HOSTING_MODE"] == "port"
    write_environment(environment, {**read_environment(environment), "HOSTING_ALLOW_TENANT_PLUGINS": "1"})
    assert preflight.run(manager) == preflight.REFUSED
    reason = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["reason"]
    assert "HOSTING_ALLOW_TENANT_PLUGINS" in reason and "port" in reason
    write_environment(environment, {**read_environment(environment), "HOSTING_MODE": "subdomain"})
    assert preflight.run(manager) == 0
    capsys.readouterr()
    # A kernel booted with ipv6.disable=1 has no IPv6 sysctls, which every tenant container sets.
    monkeypatch.setattr(preflight, "IPV6_SYSCTLS", root / "no-such-directory")
    assert preflight.run(manager) == preflight.REFUSED
    assert "ipv6.disable=1" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["reason"]


@pytest.mark.parametrize("environment", [
    {}, {"BASE_DOMAIN": "example.org"}, {"BASE_DOMAIN": " Hosting.Example.ORG "}, {"BASE_DOMAIN": "localhost"},
    {"BASE_DOMAIN": "0.0.0.0"}, {"BASE_DOMAIN": "192.0.2.10"}, {"BASE_DOMAIN": "[2001:db8::1]"},
    {"BASE_DOMAIN": "intranet"}, {"BASE_DOMAIN": "cafe"}, {"HOSTING_MODE": "onion", "BASE_DOMAIN": "example.org"},
    {"HOSTING_MODE": " Subdomain ", "BASE_DOMAIN": ""}, {"HOSTING_MODE": "port", "BASE_DOMAIN": "example.org"},
    {"BASE_DOMAIN": "a.b.example.org", "HOSTING_AUTO_FLATTEN_DOMAIN_LAYOUT": "0"},
])
def test_the_preflight_decides_the_hosting_mode_as_the_portal_does(environment):
    """The preflight runs with the system Python and cannot import the portal's configuration: same answer."""
    from bananawiki.core.env import Env
    from bananawiki.hosting import config
    from bananawiki.ops import preflight

    env = Env(environment)
    base = config.normalize_domain_layout(env.str("BASE_DOMAIN"), env.str("PORTAL_DOMAIN"), "",
                                          auto_flatten=True)[0]
    assert preflight.hosting_mode(environment) == config._detect_hosting_mode(env, base)


def test_the_new_release_checks_this_server_before_anything_stops(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    new = hosting.upstream.commit("1.7.1", extra=PREFLIGHT)
    with_project_quotas(hosting, enforced=True)
    refusal = {"ok": False, "protocol": 1, "reason": "This release needs a newer Docker."}
    asked = candidate_preflight(system, (3, refusal), OK)
    stopped = stops(system)
    with pytest.raises(CandidateRefused, match="This release needs a newer Docker"):
        manager.update()
    release = root / "releases" / new
    assert asked == [["/usr/bin/python3", "-I", str(release / "banana"), "--root", str(root), "preflight"]]
    assert stops(system) == stopped and (root / "current").resolve().name == hosting.old
    assert_in_service(root, hosting.wikis)
    # Recorded: automatic updates do not repeat it (nor rebuild it) every hour.
    failed = read_json(root / "config/failed-revision.json")
    assert failed["revision"] == new and failed["stage"] == "preflight"
    assert "This release needs a newer Docker" in failed["reason"]
    assert manager.status()["failed_revision"]["revision"] == new
    enable_automatic_updates(root)
    paused = manager.update(automatic=True)
    assert paused["outcome"] == "paused" and "This release needs a newer Docker" in paused["reason"]
    assert len(asked) == 1 and stops(system) == stopped
    # A manual update tries it again (the operator fixed the cause).
    assert manager.update()["outcome"] == "complete" and len(asked) == 2
    assert (root / "current").resolve().name == new
    assert not (root / "config/failed-revision.json").exists()
    assert "failed_revision" not in manager.status()


@pytest.mark.parametrize("answer", [(1, "Traceback (most recent call last):\nImportError: no module"),
                                    (0, "not json")])
def test_a_preflight_that_fails_or_does_not_answer_is_a_refusal(hosting, answer):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    hosting.upstream.commit("1.7.1", extra=PREFLIGHT)
    candidate_preflight(system, answer)
    stopped = stops(system)
    with pytest.raises(CandidateRefused):
        manager.update()
    assert stops(system) == stopped and (root / "current").resolve().name == hosting.old
    assert read_json(root / "config/failed-revision.json")["stage"] == "preflight"
    assert_in_service(root, hosting.wikis)


@pytest.mark.parametrize("answer", [
    (3, {"ok": False, "protocol": 1, "retryable": True, "reason": "Docker did not answer, so nothing was changed."}),
    subprocess.TimeoutExpired(["banana"], 180), OSError(11, "Resource temporarily unavailable")])
def test_a_preflight_held_up_by_the_host_is_not_the_revisions_failure(hosting, answer):
    """Docker restarting, or a slow host: not a reason to skip the revision (a security release, say) for good."""
    root, system, manager = hosting.root, hosting.system, hosting.manager
    new = hosting.upstream.commit("1.7.1", extra=PREFLIGHT)
    asked = candidate_preflight(system, answer, OK)
    stopped = stops(system)
    with pytest.raises(Refused) as error:
        manager.update(automatic=False)
    assert not isinstance(error.value, CandidateRefused)
    assert stops(system) == stopped and (root / "current").resolve().name == hosting.old
    assert not (root / "config/failed-revision.json").exists()
    assert read_json(root / "config/status.json")["outcome"] == "failed"
    assert_in_service(root, hosting.wikis)
    enable_automatic_updates(root)
    assert manager.update(automatic=True)["outcome"] == "complete" and len(asked) == 2
    assert (root / "current").resolve().name == new


def test_this_releases_preflight_says_when_docker_held_it_up(hosting, capsys, kernel_with_ipv6):
    from bananawiki.ops import preflight

    original = hosting.system.runner

    def docker_down(command, **options):
        if command[:1] == ["docker"]:
            return subprocess.CompletedProcess(command, 1, "", "Cannot connect to the Docker daemon.\n")
        return original(command, **options)

    hosting.system.runner = docker_down
    assert preflight.run(hosting.manager) == preflight.REFUSED
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert answer["ok"] is False and answer["retryable"] is True and "Docker did not answer" in answer["reason"]
    hosting.system.runner = original
    configure(hosting.root, "required")
    with_project_quotas(hosting, enforced=False)
    assert preflight.run(hosting.manager) == preflight.REFUSED
    assert "retryable" not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_release_without_a_preflight_of_its_own_is_not_asked(hosting):
    hosting.upstream.commit("1.6.1")
    asked = candidate_preflight(hosting.system, OK)
    assert hosting.manager.update()["outcome"] == "complete"
    assert asked == []


def test_the_preflight_warnings_of_the_new_release_are_reported(hosting):
    hosting.upstream.commit("1.7.1", extra=PREFLIGHT)
    candidate_preflight(hosting.system, (0, {"ok": True, "protocol": 1, "warnings": ["Something to look at."]}))
    assert hosting.manager.update()["preflight_warnings"] == ["Something to look at."]


def test_the_preflight_notes_of_the_new_release_are_reported_as_notes(hosting, capsys):
    hosting.upstream.commit("1.7.1", extra=PREFLIGHT)
    candidate_preflight(hosting.system, (0, {"ok": True, "protocol": 1, "warnings": [], "notes": ["Good to know."]}))
    result = hosting.manager.update()
    assert result["preflight_notes"] == ["Good to know."] and "preflight_warnings" not in result
    err = capsys.readouterr().err
    assert "Note: Good to know." in err and "Warning: Good to know." not in err


@pytest.mark.parametrize("lost", [False, True], ids=["never-had", "lost"])
def test_an_update_says_once_what_the_wikis_storage_gets(hosting, capsys, kernel_with_ipv6, lost):
    """The new release's preflight and the agent it then starts say the same about hard quotas: printed once, as a
    note for storage that never had them (a supported configuration), as a warning when they were lost."""
    from bananawiki.ops import preflight

    with_project_quotas(hosting, enforced=False)
    if lost:
        quota_registry(hosting.system)
    message = hosting.system.tenant_storage(hosting.manager.settings()).warning()
    answer = preflight.check(hosting.manager)  # what this release's own preflight answers on this server
    hosting.upstream.commit("1.7.1", extra=PREFLIGHT)
    candidate_preflight(hosting.system, (0, answer))
    result = hosting.manager.update()
    assert result["outcome"] == "complete"
    assert result["storage_warning" if lost else "storage_note"] == message
    assert result["preflight_warnings" if lost else "preflight_notes"] == [message]
    assert "preflight_notes" not in result if lost else "preflight_warnings" not in result
    err = capsys.readouterr().err
    assert err.count(message) == 1 and ("Warning: " if lost else "Note: ") + message in err


# The failed-revision record ------------------------------------------------------------------------------------


def test_a_rolled_back_update_records_why_and_automatic_updates_skip_it_until_a_newer_one(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    bad = hosting.upstream.commit("1.7.1")
    system.unhealthy_revisions.add(bad)
    with pytest.raises(RuntimeError):
        manager.update()
    failed = read_json(root / "config/failed-revision.json")
    assert failed["revision"] == bad and failed["stage"] == "rollback"
    assert "readiness" in failed["reason"] and failed["time"]
    status = manager.status()
    assert status["failed_revision"]["revision"] == bad
    assert any(bad[:12] in warning and "update --retry-failed" in warning for warning in status["warnings"])
    enable_automatic_updates(root)
    stopped = stops(system)
    paused = manager.update(automatic=True)
    assert paused["outcome"] == "paused" and "readiness" in paused["reason"] and stops(system) == stopped
    good = hosting.upstream.commit("1.7.2")
    assert manager.update(automatic=True)["outcome"] == "complete"
    assert (root / "current").resolve().name == good
    assert not (root / "config/failed-revision.json").exists()


def test_a_record_written_by_an_older_controller_is_still_honoured(hosting):
    root, manager = hosting.root, hosting.manager
    bad = hosting.upstream.commit("1.7.1")
    write_json(root / "config/failed-revision.json", {"revision": bad})  # b8bc352 wrote only this
    enable_automatic_updates(root)
    assert manager.update(automatic=True)["outcome"] == "paused"
    assert manager.status()["failed_revision"] == {"revision": bad}


def test_a_failed_revision_the_platform_has_moved_past_is_forgotten(hosting):
    """An older updater (b8bc352) recorded 9591b29, then installed 1.7, which contains it, and kept the record."""
    root, manager = hosting.root, hosting.manager
    bad = hosting.upstream.commit("9591b29: the release that needed XFS")
    new = hosting.upstream.commit("1.7.0", extra=MODES)
    assert manager.update()["outcome"] == "complete" and (root / "current").resolve().name == new
    write_json(root / "config/failed-revision.json", {"revision": bad})  # what the old updater left behind
    status = manager.status()
    assert "failed_revision" not in status
    assert not any(bad[:12] in warning for warning in status.get("warnings", []))
    enable_automatic_updates(root)
    assert manager.update(automatic=True)["outcome"] == "current"
    assert not (root / "config/failed-revision.json").exists()
    # A record of a revision the platform does not contain is kept (it may still be offered).
    hosting.upstream.orphan("an unrelated history")
    other = hosting.upstream.commit("a rewrite")
    write_json(root / "config/failed-revision.json", {"revision": other, "stage": "rollback", "reason": "x"})
    assert manager.status()["failed_revision"]["revision"] == other


# A failed unit convergence (the units an older updater wrote) ------------------------------------------------


def older_units(system: FakeSystem) -> None:
    """What an updater from before 1.7 writes when it installs this release: its own (generation 2) units."""
    for unit in system.unit_dir.glob("bananawiki*.service"):
        unit.write_text(unit.read_text().replace("# bananawiki-ops: 3", "# bananawiki-ops: 2"))


def test_a_failed_unit_convergence_is_not_repeated_by_every_automatic_run(hosting):
    """The incident's pattern must not come back through the convergence every pre-1.7 server goes through.

    Once an older updater installed 1.7, the next ``update`` installs 1.7's
    own units: it stops the services, starts them under the new units and
    waits for readiness. When the new units fail on a host, that wait was
    repeated (with the portal, and so every wiki another reverse proxy sends
    through it, down) at every automatic run.
    """
    root, system, manager = hosting.root, hosting.system, hosting.manager
    enable_automatic_updates(root)
    older_units(system)
    system.unhealthy_revisions.add(hosting.old)  # the services fail under the converged units, on this host only
    stopped = stops(system)
    with pytest.raises(RuntimeError, match="converged configuration"):
        manager.update(automatic=True)
    assert stops(system) > stopped
    assert "# bananawiki-ops: 2" in (system.unit_dir / "bananawiki-agent.service").read_text(), "put back"
    failed = read_json(root / "config/failed-convergence.json")
    assert failed["revision"] == hosting.old and "readiness" in failed["reason"] and failed["time"]
    assert [event for event in history(root) if event["operation"] == "converge"][-1]["outcome"] == "failed"
    status = manager.status()
    assert status["failed_convergence"]["revision"] == hosting.old
    assert any("units failed" in warning and "'update'" in warning for warning in status["warnings"])
    # The next automatic runs leave the services alone.
    stopped = stops(system)
    for _run in range(2):
        paused = manager.update(automatic=True)
        assert paused["outcome"] == "paused" and paused["units_converged"] is False
        assert "readiness" in paused["reason"]
    assert stops(system) == stopped
    assert_in_service(root, hosting.wikis)
    # An operator's update tries again; once it succeeds the record is gone.
    with pytest.raises(RuntimeError):
        manager.update()
    assert stops(system) > stopped
    system.unhealthy_revisions.clear()
    assert manager.update()["units_converged"] is True
    assert not (root / "config/failed-convergence.json").exists()
    assert "failed_convergence" not in manager.status()


def test_a_paused_convergence_does_not_hold_back_a_new_revision(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    enable_automatic_updates(root)
    older_units(system)
    system.unhealthy_revisions.add(hosting.old)
    with pytest.raises(RuntimeError):
        manager.update(automatic=True)
    new = hosting.upstream.commit("1.7.1")
    assert manager.update(automatic=True)["outcome"] == "complete"
    assert (root / "current").resolve().name == new
    assert "# bananawiki-ops: 3" in (system.unit_dir / "bananawiki-agent.service").read_text()
    assert not (root / "config/failed-convergence.json").exists()


def test_a_restart_that_installs_the_units_ends_a_paused_convergence(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    older_units(system)
    system.unhealthy_revisions.add(hosting.old)
    with pytest.raises(RuntimeError):
        manager.update()
    assert (root / "config/failed-convergence.json").exists()
    system.unhealthy_revisions.clear()
    cli.lifecycle(manager, argparse.Namespace(command="restart"))
    assert not (root / "config/failed-convergence.json").exists()
    assert manager.status()["units_current"] is True


# BuildKit --------------------------------------------------------------------------------------------------------


def test_an_update_without_buildkit_stops_nothing_and_names_the_package(hosting):
    root, system, manager = hosting.root, hosting.system, hosting.manager
    new = hosting.upstream.commit("1.7.1")
    original = system.runner

    def runner(command, **options):
        if command[:3] == ["docker", "buildx", "version"]:
            return subprocess.CompletedProcess(command, 1, "", "docker: 'buildx' is not a docker command.\n")
        return original(command, **options)

    system.runner = runner
    stopped = stops(system)
    with pytest.raises(BuildKitUnavailable) as error:
        manager.update()
    assert "apt install docker-buildx" in str(error.value) and "docker-buildx-plugin" in str(error.value)
    assert system.prepared == [] and stops(system) == stopped
    assert not (root / "config/failed-revision.json").exists(), "not the revision's failure"
    assert read_json(root / "config/status.json")["outcome"] == "failed"
    system.runner = original
    assert manager.update()["outcome"] == "complete" and (root / "current").resolve().name == new


def test_the_tenant_image_is_built_with_buildkit_and_a_failure_shows_its_cause(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "Dockerfile.tenant").write_text("# syntax=docker/dockerfile:1\nFROM scratch\n")
    seen: list[tuple[list[str], dict]] = []

    def runner(command, **options):
        seen.append((command, options))
        if command[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(command, 1, "#5 [media-builder 2/6] RUN --mount=type=secret\n",
                                               "#5 ERROR: process did not complete\n------\n"
                                               "ERROR: failed to solve: E: Unable to fetch some archives\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    system = System(runner=runner, log_dir=tmp_path / "logs")
    settings = {"mode": "hosting", "revision": "a" * 40, "service": "bananawiki", "root": str(tmp_path)}
    with pytest.raises(RuntimeError) as error:
        system.build_tenant_image(settings, release)
    message = str(error.value)
    assert message.startswith("docker build failed (exit 1): ")
    assert "failed to solve: E: Unable to fetch some archives" in message and "private log" in message
    command, options = next((command, options) for command, options in seen if command[:2] == ["docker", "build"])
    assert options["env"]["DOCKER_BUILDKIT"] == "1"
    assert options["env"]["PATH"] == os.environ["PATH"]  # the build keeps the controller's environment (DOCKER_HOST)


def test_a_build_that_leaves_no_tenant_image_fails_before_anything_stops(tmp_path):
    """A buildx builder other than Docker's own (docker-container driver) keeps its result in its own cache."""
    release = tmp_path / "release"
    release.mkdir()
    (release / "Dockerfile.tenant").write_text("# syntax=docker/dockerfile:1\nFROM scratch\n")
    tag = "bananawiki-tenant:" + "c" * 40

    def runner(command, **options):
        if command[:3] == ["docker", "image", "inspect"] and command[-1] == tag:
            return subprocess.CompletedProcess(command, 1, "", f"Error: No such image: {tag}\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    settings = {"mode": "hosting", "revision": "c" * 40, "service": "bananawiki", "root": str(tmp_path)}
    with pytest.raises(RuntimeError, match="docker buildx use default"):
        System(runner=runner, log_dir=tmp_path / "logs").build_tenant_image(settings, release)


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.tenant"])
def test_the_images_build_without_fetching_a_dockerfile_frontend(name):
    """A '# syntax=' line makes BuildKit look the frontend up on Docker Hub at every build, so a build with the
    registry unreachable failed, though an update promises to build on the base image it has (docs/deployment.md).
    The built-in frontend (Docker 20.10 and later) has RUN --mount, for which BuildKit is still required."""
    from bananawiki.ops.system import needs_buildkit

    dockerfile = Path(__file__).resolve().parents[2] / name
    assert not any(line.strip().lower().startswith("# syntax=")
                   for line in dockerfile.read_text(encoding="utf-8").splitlines())
    assert needs_buildkit(dockerfile)


def test_an_older_release_is_still_built_with_whatever_builder_docker_has(tmp_path):
    """A rollback to a release from before 1.7, whose Dockerfile needs no BuildKit, works without buildx."""
    release = tmp_path / "release"
    release.mkdir()
    (release / "Dockerfile.tenant").write_text("FROM python:3.12-slim-bookworm\nRUN pip install --no-cache-dir x\n")
    seen: list[tuple[list[str], dict]] = []

    def runner(command, **options):
        seen.append((command, options))
        return subprocess.CompletedProcess(command, 1 if command[:2] == ["docker", "buildx"] else 0, "", "")

    settings = {"mode": "hosting", "revision": "b" * 40, "service": "bananawiki", "root": str(tmp_path)}
    assert System(runner=runner).build_tenant_image(settings, release) == []
    assert not any(command[:2] == ["docker", "buildx"] for command, _ in seen)
    assert "env" not in next(options for command, options in seen if command[:2] == ["docker", "build"])


# Transitions the older controllers leave ------------------------------------------------------------------------


def test_a_failed_unit_convergence_keeps_the_portal_out_of_the_docker_group(tmp_path):
    system = FakeSystem(tmp_path / "host")
    upstream = Upstream(tmp_path)
    root = tmp_path / "opt" / "bananawiki"
    new = upstream.commit("1.7 hosting")
    legacy_install(root, system, upstream, new, mode="hosting")
    manager = Manager(root, system=system)
    manager.update()  # converged: the agent drives Docker, the portal is not in the docker group
    assert system.docker_group is False
    for unit in system.unit_dir.glob("bananawiki*.service"):  # units of the previous generation
        unit.write_text(unit.read_text().replace("# bananawiki-ops: 3", "# bananawiki-ops: 2"))
    system.unhealthy_revisions.add(new)
    with pytest.raises(RuntimeError):
        manager.update()
    assert "SupplementaryGroups=docker" not in (system.unit_dir / "bananawiki.service").read_text()
    assert system.docker_group is False, "the internet-facing portal must never get the Docker socket"


def test_a_release_an_older_controller_prepared_is_prepared_again(tmp_path):
    """Staged by a pre-1.7 updater (a venv from requirements.txt ranges, an image without fresh updates)."""
    system = FakeSystem(tmp_path / "host")
    upstream = Upstream(tmp_path)
    root = tmp_path / "opt" / "bananawiki"
    old = upstream.commit("1.6 hosting")
    legacy_install(root, system, upstream, old, mode="hosting")
    manager = Manager(root, system=system)
    manager.update()
    new = upstream.commit("1.7 with locks", extra={"requirements.lock": "x==1 --hash=sha256:00\n",
                                                   "requirements-build.lock": "pip==26.2.1 --hash=sha256:00\n"})
    release = root / "releases" / new
    release.mkdir()
    subprocess.run(["sh", "-c", f"git -C {upstream.work} archive {new} | tar -x -C {release}"], check=True)
    write_json(release / ".release.json", {"revision": new, "product": "BananaWiki"})
    result = manager.update()
    assert result["outcome"] == "complete" and result["revision"] == new
    assert system.prepared == [new]
    assert read_json(release / ".release.json")["prepared_by"] == 3
    # Prepared by this controller, a release is reused as it is.
    assert manager.stage({**manager.settings(), "revision": new}) == release and system.prepared == [new]


# Installing a new hosting platform -------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def new_platform(tmp_path: Path, *, xfs: bool) -> tuple[Manager, Upstream, list[Path]]:
    upstream = Upstream(tmp_path)
    upstream.commit("1.7.0", extra=MODES)
    system = FakeSystem(tmp_path / "host")
    checked: list[Path] = []

    def storage(instances):
        checked.append(instances)
        if not xfs:
            raise OSError(95, EXT4)

    system.quota_storage = storage  # type: ignore[method-assign]
    manager = Manager(tmp_path / "opt/bananawiki", system=system)
    manager.layout()
    manager.configure_source(url=str(upstream.bare), allow_local=True)
    return manager, upstream, checked


def test_a_new_hosting_platform_installs_on_any_storage_and_says_which_limits_apply(tmp_path, capsys):
    """No XFS needed: auto, the default, holds each wiki to its plan with the application's own checks."""
    manager, upstream, checked = new_platform(tmp_path, xfs=False)
    result = manager.install(upstream.work, mode="hosting", port=free_port())
    assert result["outcome"] == "complete" and checked[0] == manager.root / "data"
    assert read_environment(manager.config_dir / "app.env")["BW_HOSTING_TENANT_QUOTAS"] == "auto"
    storage = result["tenant_storage"]
    assert storage == {"setting": "auto", "hard_quotas": False, "refused": False, "lost": False,
                       "reason": storage["reason"]} and EXT4 in storage["reason"]
    # Which limits apply, and how to get hard ones.
    assert "Hard per-wiki storage quotas are not enforced" in result["storage_note"]
    assert "prjquota" in result["storage_note"] and "storage_warning" not in result
    assert "Note: Hard per-wiki storage quotas are not enforced" in capsys.readouterr().err
    assert manager.status()["tenant_storage"]["setting"] == "auto"


def test_a_new_hosting_platform_requires_hard_quotas_only_when_the_operator_asks(tmp_path):
    manager, upstream, checked = new_platform(tmp_path, xfs=False)
    port = free_port()
    with pytest.raises(TenantStorageUnsupported, match="--tenant-quotas auto") as error:
        manager.install(upstream.work, mode="hosting", port=port, tenant_quotas_mode="required")
    assert "BW_HOSTING_TENANT_QUOTAS=required" in str(error.value) and "Nothing was installed" in str(error.value)
    assert checked == [manager.root / "data"] and manager.system.prepared == []
    assert not list(manager.system.unit_dir.glob("bananawiki*.service"))
    result = manager.install(upstream.work, mode="hosting", port=port, tenant_quotas_mode="off", reuse_data=True)
    assert result["outcome"] == "complete"
    assert read_environment(manager.config_dir / "app.env")["BW_HOSTING_TENANT_QUOTAS"] == "off"
    assert manager.status()["tenant_storage"]["setting"] == "off"


@pytest.mark.parametrize("asked", [None, "required"])
def test_a_new_hosting_platform_on_xfs_uses_its_quotas(tmp_path, capsys, asked):
    manager, upstream, _checked = new_platform(tmp_path, xfs=True)
    result = manager.install(upstream.work, mode="hosting", port=free_port(), tenant_quotas_mode=asked)
    assert result["outcome"] == "complete"
    assert read_environment(manager.config_dir / "app.env")["BW_HOSTING_TENANT_QUOTAS"] == (asked or "auto")
    assert result["tenant_storage"]["hard_quotas"] is True
    assert "storage_note" not in result and "storage_warning" not in result
    assert "Hard per-wiki storage quotas will be enforced" in capsys.readouterr().err


def test_a_restore_does_not_silently_ignore_a_quota_setting(tmp_path):
    """A package keeps its own BW_HOSTING_TENANT_QUOTAS; --tenant-quotas with --restore would be ignored."""
    manager = Manager(tmp_path / "opt/bananawiki", system=FakeSystem(tmp_path / "host"))
    restored: list[Path] = []
    manager.restore = lambda package, **_options: restored.append(package)  # type: ignore[method-assign]
    args = cli.parser().parse_args(["install", "--restore", str(tmp_path / "package.tar.gz"),
                                    "--tenant-quotas", "required"])
    with pytest.raises(ValueError, match="config set tenant-quotas"):
        cli.dispatch(manager, args)
    assert restored == []
    cli.dispatch(manager, cli.parser().parse_args(["install", "--restore", str(tmp_path / "package.tar.gz")]))
    assert restored == [tmp_path / "package.tar.gz"]


# bananawiki config set tenant-quotas ------------------------------------------------------------------------------


def test_the_setting_is_changed_with_a_command_and_checked_first(hosting, capsys):
    root, manager = hosting.root, hosting.manager
    hosting.upstream.commit("1.7.0", extra=MODES)
    with_project_quotas(hosting, enforced=False)
    manager.update()
    assert cli.config_command(manager, ["get", "tenant-quotas"])["tenant_quotas"]["setting"] == "auto"
    with pytest.raises(TenantStorageUnsupported):
        cli.config_command(manager, ["set", "tenant-quotas", "required"])
    assert read_environment(root / "config/app.env")["BW_HOSTING_TENANT_QUOTAS"] == "auto"  # the default, kept
    result = cli.config_command(manager, ["set", "tenant-quotas", "off"])
    assert result["tenant_quotas"]["setting"] == "off"
    assert read_environment(root / "config/app.env")["BW_HOSTING_TENANT_QUOTAS"] == "off"
    assert read_json(root / "config/status.json")["operation"] == "config"
    with pytest.raises(ValueError, match="auto, required or off"):
        cli.config_command(manager, ["set", "tenant-quotas", "maybe"])
    with_project_quotas(hosting, enforced=True)
    # Units an older controller wrote do not let the agent use quotas: "required" would keep every wiki down.
    unit = hosting.system.unit_dir / "bananawiki-agent.service"
    current = unit.read_text()
    unit.write_text(current.replace("# bananawiki-ops: 3", "# bananawiki-ops: 2"))
    with pytest.raises(ValueError, match="restart"):
        cli.config_command(manager, ["set", "tenant-quotas", "required"])
    unit.write_text(current)
    result = cli.config_command(manager, ["set", "tenant-quotas", "required"])
    assert result["tenant_quotas"]["hard_quotas"] is True and "restarts" in result["note"]
    assert cli.config_command(manager, ["check"]) is None  # other config commands go to the application
