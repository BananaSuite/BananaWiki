"""Hosting operations against wikis that do not come back: one tenant never holds the whole platform.

Every operation on a hosting server stops the tenant containers, puts the
platform in maintenance and waits for readiness before it lifts maintenance.
These tests run the real readiness check, transaction journal and container
commands against a simulated host (a portal maintenance service that brings
back each wiki running in its database, and wikis whose ``/health`` the test
controls) and check that a wiki failing for reasons of its own (whatever its
health check answers, or a container that no longer starts) is reported
instead of keeping every other wiki in maintenance, that a rollback is put
back once, and that containers removed meanwhile are not an error.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from ops_fakes import FakeSystem, Upstream, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops.cli import dispatch, lifecycle, parser
from bananawiki.ops.files import (
    MAINTENANCE_MARKER,
    read_environment,
    read_json,
    write_environment,
    write_json,
)
from bananawiki.ops.manager import Manager
from bananawiki.ops.project_quota import QuotaError
from bananawiki.ops.system import DockerUnavailable, System, TenantStorageUnsupported, _http_status


class Wikis:
    """The portal's maintenance service (it brings back each wiki running in its database) and ``/health``."""

    def __init__(self, root: Path, system: FakeSystem):
        self.root, self.system = root, system
        self.failing: set[str] = set()  # answer 503
        self.failing_in_maintenance: set[str] = set()  # answer 503 while their maintenance marker exists
        self.failing_under: dict[str, str] = {}  # slug -> the release under which it answers 503
        self.answered_by: dict[str, str] = {}  # slug -> a real server whose reply it sends instead
        self.address = 1
        system.probe = self.probe
        system.on_start.append(self.on_start)

    def directory(self, slug: str) -> Path:
        return (self.root / "data/instances" / slug).resolve()

    def rows(self, status: str = "running") -> list[str]:
        connection = sqlite3.connect(self.root / "data/hosting.db")
        try:
            return [row[0] for row in connection.execute(
                "SELECT subdomain FROM instances WHERE status = ? ORDER BY subdomain", (status,))]
        finally:
            connection.close()

    def set_status(self, slug: str, status: str) -> None:
        connection = sqlite3.connect(self.root / "data/hosting.db")
        connection.execute("UPDATE instances SET status = ? WHERE subdomain = ?", (status, slug))
        connection.commit()
        connection.close()

    def on_start(self, names: list[str]) -> None:
        if "bananawiki-maintenance" in names:
            self.recover()

    def recover(self) -> None:
        """The runtime agent's recovery: a running container is left alone, any other is replaced by a new one."""
        for slug in self.rows():
            directory = self.directory(slug)
            if not directory.is_dir():
                continue  # the agent refuses a wiki whose data directory is missing
            existing = [item for item in self.system.tenant_containers if item["data_dir"] == str(directory)]
            if any(item["running"] for item in existing):
                continue  # a gated container among them still looks "starting" to the agent
            stale = {item["id"] for item in existing}
            self.system.tenant_containers = [item for item in self.system.tenant_containers
                                             if item["id"] not in stale]
            self.address += 1
            self.system.tenant_containers.append({
                "id": f"{slug}-{self.address}", "running": True, "addresses": [f"172.18.0.{self.address}"],
                "internal_port": 5001, "data_dir": str(directory)})

    def probe(self, url: str) -> int:
        if url.startswith("http://127.0.0.1:"):
            return 200  # the portal
        address = url.split("//")[1].split(":")[0]
        item = next(item for item in self.system.tenant_containers if address in item["addresses"])
        if item.get("gated"):
            raise ConnectionRefusedError(url)  # brought back by ``docker start``: its mount gate stays shut
        slug = Path(item["data_dir"]).name
        if slug in self.answered_by:
            return _http_status(self.answered_by[slug])
        if slug in self.failing or self.failing_under.get(slug) == (self.root / "current").resolve().name:
            return 503
        if slug in self.failing_in_maintenance and (Path(item["data_dir"]) / MAINTENANCE_MARKER).exists():
            return 503
        return 200

    def markers(self) -> list[str]:
        return sorted(path.parent.name for path in (self.root / "data/instances").glob("*/" + MAINTENANCE_MARKER))


@pytest.fixture
def hosting(tmp_path):
    """A converged 1.6 hosting server (port mode) running the wikis ``acme`` and ``broken``."""
    system = FakeSystem(tmp_path / "host")
    upstream = Upstream(tmp_path)
    root = tmp_path / "opt/bananawiki"
    old = upstream.commit("1.6 hosting")
    legacy_install(root, system, upstream, old, mode="hosting")
    environment = root / "config/app.env"
    write_environment(environment, {**read_environment(environment), "HOSTING_MODE": "port"})
    database = sqlite3.connect(root / "data/hosting.db")
    database.execute("CREATE TABLE instances (subdomain TEXT, domain_mode TEXT, status TEXT)")
    database.executemany("INSERT INTO instances VALUES (?, 'subdomain', 'running')", [("acme",), ("broken",)])
    database.commit()
    database.close()
    for slug in ("acme", "broken"):
        (root / "data/instances" / slug).mkdir(parents=True)
        (root / "data/instances" / slug / "page.txt").write_text(slug)
    wikis = Wikis(root, system)
    wikis.recover()
    manager = Manager(root, system=system)
    assert manager.update()["units_converged"] is True
    return SimpleNamespace(root=root, system=system, upstream=upstream, manager=manager, wikis=wikis, old=old)


@pytest.fixture
def redirecting():
    """A real HTTP server that answers every request with a redirect to ``server.location``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(302)
            self.send_header("Location", self.server.location)  # type: ignore[attr-defined]
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.location = "http://["  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def history(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "config/history.jsonl").read_text().splitlines()]


def assert_in_service(root: Path, wikis: Wikis) -> None:
    assert not (root / "config/transaction.json").exists()
    assert not (root / "data" / MAINTENANCE_MARKER).exists()
    assert wikis.markers() == []


# R-07: a wiki's own failure is reported, the platform leaves maintenance -------------------------


def test_a_wiki_failing_only_in_maintenance_mode_does_not_hold_backups(hosting):
    """A plugin can make a wiki answer 503 while its maintenance marker exists: it looks healthy otherwise."""
    hosting.wikis.failing_in_maintenance.add("broken")
    assert hosting.manager.backup().is_file()
    assert_in_service(hosting.root, hosting.wikis)
    status = read_json(hosting.root / "config/status.json")
    assert status["outcome"] == "complete" and "unready_tenants" not in status


def test_a_wiki_that_does_not_come_back_after_a_backup_is_reported(hosting):
    hosting.system.on_start.append(lambda names: hosting.wikis.failing.add("broken"))
    hosting.manager.backup()
    assert_in_service(hosting.root, hosting.wikis)
    assert read_json(hosting.root / "config/status.json")["unready_tenants"] == ["broken"]


def test_an_update_that_breaks_a_running_wiki_is_rolled_back_and_the_platform_returns(hosting):
    root = hosting.root
    new = hosting.upstream.commit("1.6.1")
    hosting.wikis.failing_under["broken"] = new
    with pytest.raises(RuntimeError, match="broken"):
        hosting.manager.update()
    assert (root / "current").resolve().name == hosting.old
    assert read_json(root / "config/status.json")["outcome"] == "rolled_back"
    assert read_json(root / "config/failed-revision.json") == {"revision": new}
    assert_in_service(root, hosting.wikis)
    assert {"bananawiki", "bananawiki-maintenance", "bananawiki-agent"} <= hosting.system.running


def test_a_rollback_completes_although_a_wiki_stays_broken_and_is_never_repeated(hosting):
    root, wikis = hosting.root, hosting.wikis
    new = hosting.upstream.commit("1.6.1")
    hosting.system.on_start.append(lambda names: wikis.failing.add("broken"))
    with pytest.raises(RuntimeError, match="broken"):
        hosting.manager.update()
    assert (root / "current").resolve().name == hosting.old
    assert_in_service(root, wikis)
    recovered = [event for event in history(root) if event["operation"] == "recover"]
    assert recovered[-1]["outcome"] == "complete" and recovered[-1]["unready_tenants"] == ["broken"]
    assert read_json(root / "config/failed-revision.json") == {"revision": new}
    # The operator stops the broken wiki; no later command puts the old row back.
    wikis.set_status("broken", "stopped")
    hosting.manager.backup()
    assert wikis.rows("stopped") == ["broken"]
    assert_in_service(root, wikis)


def test_a_wiki_already_down_before_an_update_does_not_hold_it(hosting):
    """Its row says running but its directory is gone: the portal cannot bring it back, before or after."""
    root = hosting.root
    broken = hosting.wikis.directory("broken")
    hosting.system.tenant_containers = [item for item in hosting.system.tenant_containers
                                        if item["data_dir"] != str(broken)]
    shutil.rmtree(broken)
    new = hosting.upstream.commit("1.6.1")
    result = hosting.manager.update()
    assert result["outcome"] == "complete" and (root / "current").resolve().name == new
    assert_in_service(root, hosting.wikis)


def test_a_wiki_failing_before_an_update_does_not_hold_it_and_is_listed(hosting):
    """Running but not serving when the update began: not waited for, checked once at the end."""
    hosting.wikis.failing.add("broken")
    new = hosting.upstream.commit("1.6.1")
    result = hosting.manager.update()
    assert result["outcome"] == "complete" and (hosting.root / "current").resolve().name == new
    assert result["unready_tenants"] == ["broken"]
    assert_in_service(hosting.root, hosting.wikis)


@pytest.mark.parametrize("location", ["http://[", "http://" + "a" * 64 + ".example.org/"])
def test_a_health_check_redirected_to_a_malformed_url_is_only_a_failing_wiki(redirecting, location):
    """urllib followed such a redirect and raised ValueError or UnicodeError out of the readiness check."""
    redirecting.location = location
    port = redirecting.server_address[1]
    system = System(runner=lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""))
    container = {"id": "x", "running": True, "addresses": ["127.0.0.1"], "internal_port": port,
                 "data_dir": "/srv/instances/x"}
    assert system.serving_tenants([container]) == []
    assert system._probe_issue(f"http://127.0.0.1:{port}/health") == "HTTP 302"


@pytest.mark.parametrize("error", [ValueError("Invalid IPv6 URL"), UnicodeError("label too long"),
                                   http.client.LineTooLong("header line")])
def test_whatever_a_tenant_reply_raises_is_a_failure_of_that_wiki(error):
    def probe(url):
        raise error

    system = System(runner=lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""), probe=probe)
    assert system._probe_issue("http://172.18.0.2:5001/health") == type(error).__name__


@pytest.mark.parametrize("trickle", [True, False])
def test_a_trickled_health_reply_is_cut_off(trickle):
    """A byte every 50 ms never trips the 5 s socket timeout; the whole probe has a deadline (silence too)."""
    listener = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def answer():
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            if trickle:
                connection.sendall(b"HTTP/1.1 200 OK\r\n")
            while not stop.wait(0.05):
                try:
                    connection.sendall(b"X" if trickle else b"")
                except OSError:
                    return

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            _http_status(f"http://127.0.0.1:{listener.getsockname()[1]}/health", deadline=0.5)
        assert time.monotonic() - started < 10
    finally:
        stop.set()
        listener.close()
        thread.join(5)


def test_a_wiki_redirecting_its_health_check_never_holds_an_operation(hosting, redirecting):
    """It serves when an operation begins, then redirects once restarted under its maintenance marker."""
    root, manager, wikis = hosting.root, hosting.manager, hosting.wikis
    url = f"http://127.0.0.1:{redirecting.server_address[1]}/health"
    hosting.system.on_start.append(lambda names: wikis.answered_by.update(broken=url))
    result = dispatch(manager, argparse.Namespace(command="backup", output=None))
    assert Path(result["package"]).is_file() and result["unready_tenants"] == ["broken"]
    assert_in_service(root, wikis)
    settings = manager.settings()
    broken = str(wikis.directory("broken"))
    readiness = hosting.system.readiness(settings, manager.services(settings), [broken])
    assert readiness.tenants == {broken: "HTTP 302"}
    # An update it breaks is rolled back; the recovery ends although the wiki keeps redirecting.
    wikis.answered_by.clear()
    hosting.upstream.commit("1.6.1")
    with pytest.raises(RuntimeError, match="broken"):
        manager.update()
    recovered = [event for event in history(root) if event["operation"] == "recover"]
    assert recovered[-1]["outcome"] == "complete" and recovered[-1]["unready_tenants"] == ["broken"]
    assert_in_service(root, wikis)
    assert lifecycle(manager, argparse.Namespace(command="start"))["outcome"] == "complete"
    assert_in_service(root, wikis)


def test_docker_unreachable_when_an_operation_begins_changes_nothing(hosting):
    """The containers are listed once the maintenance service stopped; Docker is checked before anything stops."""
    root, system, wikis, manager = hosting.root, hosting.system, hosting.wikis, hosting.manager
    new = hosting.upstream.commit("1.6.1")
    original_runner, original_containers = system.runner, system.containers

    def unreachable(command, **options):  # e.g. dockerd restarting
        if command[:1] == ["docker"]:
            return subprocess.CompletedProcess(command, 1, "", "Cannot connect to the Docker daemon.\n")
        return original_runner(command, **options)

    def containers(settings):
        raise RuntimeError("docker ps failed (exit 1).")

    def prepare_release(settings, release, features):  # the real one builds the tenant image first
        system.run(["docker", "build", "--tag", "bananawiki-tenant:" + settings["revision"], str(release)])
        return original_prepare(settings, release, features)

    original_prepare = system.prepare_release
    system.runner, system.containers = unreachable, containers  # type: ignore[method-assign]
    system.prepare_release = prepare_release  # type: ignore[method-assign]
    restart, stop = argparse.Namespace(command="restart"), argparse.Namespace(command="stop")
    for operation in (manager.backup, manager.update, lambda: lifecycle(manager, restart),
                      lambda: lifecycle(manager, stop)):
        with pytest.raises(RuntimeError, match="Docker did not answer, so nothing was changed: docker ps"):
            operation()
        assert_in_service(root, wikis)
        assert {"bananawiki", "bananawiki-maintenance", "bananawiki-agent"} <= system.running
    assert not list((root / "staging").glob("snapshot-*"))
    assert not [event for event in history(root) if event["operation"] == "recover"]
    # Not the revision's failure: it is not recorded as failed, so automatic updates try it again.
    assert [event["outcome"] for event in history(root) if event["operation"] == "update"][-1] == "failed"
    assert not (root / "config/failed-revision.json").exists()
    assert (root / "current").resolve().name == hosting.old
    system.runner, system.containers = original_runner, original_containers  # type: ignore[method-assign]
    system.prepare_release = original_prepare  # type: ignore[method-assign]
    assert manager.update()["outcome"] == "complete" and (root / "current").resolve().name == new


def test_docker_failing_while_an_operation_quiesces_still_brings_the_platform_back(hosting):
    """No container was listed (so none stopped) and nothing is to be put back: Docker is not needed to recover."""
    root, system, wikis = hosting.root, hosting.system, hosting.wikis
    original = system.containers

    def containers(settings):
        if (root / "config/transaction.json").exists():
            raise subprocess.TimeoutExpired(["docker", "ps"], 600)
        return original(settings)

    system.containers = containers  # type: ignore[method-assign]
    with pytest.raises(subprocess.TimeoutExpired):
        hosting.manager.backup()
    assert_in_service(root, wikis)
    assert {"bananawiki", "bananawiki-maintenance", "bananawiki-agent"} <= system.running
    assert all(item["running"] for item in system.tenant_containers)
    recovered = [event for event in history(root) if event["operation"] == "recover"]
    assert recovered[-1]["outcome"] == "complete" and recovered[-1]["data_restored"] is False


@pytest.mark.parametrize("error", [RuntimeError("docker ps failed (exit 1)."),
                                   subprocess.TimeoutExpired(["docker", "ps"], 600)])
def test_the_recover_command_brings_the_platform_back_while_docker_is_down(hosting, error):
    """Interrupted before quiesce listed any wiki: ``bananawiki recover`` needs no Docker and reports success."""
    root, system, wikis, manager = hosting.root, hosting.system, hosting.wikis, hosting.manager
    settings = manager.settings()
    journal = manager.journal_state(settings)
    write_json(root / "config/transaction.json", journal)
    (root / "data" / MAINTENANCE_MARKER).write_text("Maintenance in progress\n")
    manager.tenant_maintenance(settings, True)
    system.stop(journal["services"])

    def down(settings):
        raise error

    system.containers = down  # type: ignore[method-assign]  # e.g. dockerd restarting
    result = dispatch(manager, parser().parse_args(["recover"]))
    assert result["outcome"] == "complete" and result["recovered"] is True
    assert result["data_restored"] is False and "unready_tenants" not in result
    assert {event["outcome"] for event in history(root) if event["operation"] == "recover"} == {"complete"}
    assert_in_service(root, wikis)
    assert {"bananawiki", "bananawiki-maintenance", "bananawiki-agent"} <= system.running
    assert all(item["running"] for item in system.tenant_containers)  # left as they were


def test_a_failure_after_an_update_committed_is_not_a_rollback(hosting, caplog):
    """The wikis that were failing before are checked once the update ended: Docker hanging then changes nothing."""
    root, system = hosting.root, hosting.system
    hosting.wikis.failing.add("broken")
    new = hosting.upstream.commit("1.6.1")
    original = system.containers

    def containers(settings):
        if not (root / "config/transaction.json").exists() and (root / "current").resolve().name == new:
            raise subprocess.TimeoutExpired(["docker", "ps"], 600)
        return original(settings)

    system.containers = containers  # type: ignore[method-assign]
    result = hosting.manager.update()
    assert result["outcome"] == "complete" and (root / "current").resolve().name == new
    assert not (root / "config/failed-revision.json").exists()
    assert Path(result["backup"]).is_file()
    assert read_json(root / "config/last-update.json")["revision"] == new
    assert_in_service(root, hosting.wikis)
    assert "unready_tenants" not in result  # not checked, and that is logged rather than reported
    assert "were not fully checked: Cannot inspect required tenants: TimeoutExpired" in caplog.text


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_the_check_after_a_restore_never_loses_the_previous_package(hosting, error):
    root, system, wikis, manager = hosting.root, hosting.system, hosting.wikis, hosting.manager
    package = manager.backup()
    wikis.failing.add("broken")  # running but not serving: checked once the restore ended
    original = system.tenant_issues

    def tenant_issues(settings, services, tenants, platform):
        if not (root / "config/transaction.json").exists():
            raise error("interrupted")
        return original(settings, services, tenants, platform)

    system.tenant_issues = tenant_issues  # type: ignore[method-assign]
    if error is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            manager.restore(package)
    else:
        assert manager.restore(package)["outcome"] == "complete"
        assert read_json(root / "config/status.json")["operation"] == "restore"
    assert len(list((root / "backups").glob("before-restore-*.tar.gz"))) == 1
    assert not [event for event in history(root) if event["operation"] == "recover"]
    assert_in_service(root, wikis)


def test_a_restored_state_is_put_back_once(hosting):
    """Interrupted after the data came back: the next recovery starts it again and keeps later changes."""
    root, manager = hosting.root, hosting.manager
    new = hosting.upstream.commit("1.6.1")
    hosting.system.unhealthy_revisions.add(new)
    original = manager.finish
    interrupted: list[bool] = []

    def interrupt_the_restored_start(journal, settings, **options):
        if journal.get("phase") == "restored" and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt  # the operator's session died while the restored release started
        return original(journal, settings, **options)

    manager.finish = interrupt_the_restored_start  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        manager.update()
    journal = read_json(root / "config/transaction.json")
    assert journal["phase"] == "restored" and (root / "current").resolve().name == hosting.old
    hosting.wikis.set_status("broken", "stopped")  # changed by the operator meanwhile
    assert Manager(root, system=hosting.system).recover() is True
    assert hosting.wikis.rows("stopped") == ["broken"]
    assert read_json(root / "config/status.json")["data_restored"] is True
    assert_in_service(root, hosting.wikis)


def test_a_rollback_that_does_not_start_ends_in_maintenance_until_start(hosting):
    root, system, wikis = hosting.root, hosting.system, hosting.wikis
    new = hosting.upstream.commit("1.6.1")
    system.unhealthy_revisions.update({new, hosting.old})
    with pytest.raises(RuntimeError, match="put back, but it did not start"):
        hosting.manager.update()
    assert (root / "current").resolve().name == hosting.old
    assert not (root / "config/transaction.json").exists()
    assert (root / "data" / MAINTENANCE_MARKER).exists() and wikis.markers() == ["acme", "broken"]
    assert [event["outcome"] for event in history(root) if event["operation"] == "recover"][-1] == "failed"
    # Nothing is pending any more: the next command does not restore the snapshot again.
    wikis.set_status("broken", "stopped")
    system.unhealthy_revisions.clear()
    result = lifecycle(Manager(root, system=system), argparse.Namespace(command="start"))
    assert result["outcome"] == "complete" and result["recovered"] is False
    assert wikis.rows("stopped") == ["broken"]
    assert_in_service(root, wikis)


def test_recover_abandon_drops_a_transaction_that_cannot_be_put_back(hosting):
    root, manager = hosting.root, hosting.manager
    write_json(root / "config/transaction.json", {
        **manager.journal_state(manager.settings()), "phase": "snapshotted",
        "snapshot": str(root / "staging/snapshot-deleted")})
    (root / "data" / MAINTENANCE_MARKER).write_text("Maintenance in progress\n")
    with pytest.raises(ValueError, match="missing"):
        manager.recover()
    args = argparse.Namespace(command="recover", abandon=True)
    result = dispatch(manager, args)
    assert result["outcome"] == "abandoned" and result["phase"] == "snapshotted"
    assert not (root / "config/transaction.json").exists()
    assert (root / "data" / MAINTENANCE_MARKER).exists()
    assert dispatch(manager, args)["outcome"] == "nothing to abandon"
    lifecycle(manager, argparse.Namespace(command="start"))
    assert_in_service(root, hosting.wikis)
    # A journal that cannot even be read is exactly the one recover cannot finish.
    (root / "config/transaction.json").write_text("{\"settings\": ")
    with pytest.raises(ValueError):
        manager.recover()
    assert dispatch(manager, args)["phase"] == "unreadable"
    assert not (root / "config/transaction.json").exists()


# R-13: containers that disappear during or after an operation ----------------------------------


def test_a_wiki_terminated_while_the_maintenance_service_stops_does_not_block_operations(hosting):
    root, system, wikis = hosting.root, hosting.system, hosting.wikis
    original = system.runner

    def terminate_on_stop(command, **options):
        if command[:2] == ["systemctl", "stop"] and "bananawiki-maintenance" in command and \
                wikis.rows() == ["acme", "broken"]:
            # The maintenance service finishes terminating an expired wiki as it is asked to stop.
            wikis.set_status("broken", "terminated")
            system.tenant_containers = [item for item in system.tenant_containers
                                        if not item["data_dir"].endswith("/broken")]
        return original(command, **options)

    system.runner = terminate_on_stop
    removed = next(item["id"] for item in system.tenant_containers if item["data_dir"].endswith("/broken"))
    hosting.manager.backup()
    assert_in_service(root, wikis)
    assert read_json(root / "config/status.json")["outcome"] == "complete"
    assert not any(removed in command for command in system.commands if command[:1] == ["docker"])


def test_recovery_removes_the_containers_that_still_exist(hosting):
    root, manager, system = hosting.root, hosting.manager, hosting.system
    containers = [dict(item) for item in system.tenant_containers]
    gone = {**containers[0], "id": "removed-meanwhile"}
    write_json(root / "config/transaction.json", {
        **manager.journal_state(manager.settings()), "containers": [gone, *containers],
        "serving": [item["data_dir"] for item in containers]})
    assert manager.recover() is True
    assert ["docker", "rm", "--force", *(item["id"] for item in containers)] in system.commands
    assert not [command for command in system.commands if "removed-meanwhile" in command]
    assert not [command for command in system.commands if command[:2] == ["docker", "start"]]
    # The portal started the wikis again, in new containers.
    assert all(item["running"] for item in system.tenant_containers)
    assert not {item["id"] for item in system.tenant_containers} & {item["id"] for item in containers}
    assert "unready_tenants" not in read_json(root / "config/status.json")
    assert_in_service(root, hosting.wikis)


def test_a_wiki_whose_data_directory_is_gone_does_not_fail_a_backup(hosting):
    """Its row says running but its data directory is gone: the portal cannot start it again."""
    root, system, wikis = hosting.root, hosting.system, hosting.wikis
    broken = wikis.directory("broken")
    acme = next(item["id"] for item in system.tenant_containers if item["data_dir"] != str(broken))
    shutil.rmtree(broken)
    result = dispatch(hosting.manager, argparse.Namespace(command="backup", output=None))
    assert Path(result["package"]).is_file() and result["unready_tenants"] == ["broken"]
    serving = [item for item in system.tenant_containers if item["data_dir"] == str(wikis.directory("acme"))]
    assert [item["running"] for item in serving] == [True] and serving[0]["id"] != acme
    assert not [event for event in history(root) if event["operation"] == "recover"]  # nothing was restored
    assert_in_service(root, wikis)
    assert {"bananawiki", "bananawiki-maintenance", "bananawiki-agent"} <= system.running


def test_recovery_of_an_operation_interrupted_while_quiescing_starts_the_platform(hosting):
    """Phase "preparing" (no snapshot yet) and one wiki whose data directory is gone: the platform still returns."""
    root, manager, system, wikis = hosting.root, hosting.manager, hosting.system, hosting.wikis
    containers = [dict(item) for item in system.tenant_containers]
    write_json(root / "config/transaction.json", {
        **manager.journal_state(manager.settings()), "containers": containers,
        "serving": [item["data_dir"] for item in containers]})
    # The interrupted operation had stopped the services and the wikis; one wiki's directory is gone since.
    system.running.clear()
    for item in system.tenant_containers:
        item["running"] = False
    shutil.rmtree(wikis.directory("broken"))
    result = lifecycle(manager, argparse.Namespace(command="recover"))
    assert result["outcome"] == "complete" and result["recovered"] is True
    assert result["data_restored"] is False and result["unready_tenants"] == ["broken"]
    assert read_json(root / "config/status.json")["unready_tenants"] == ["broken"]
    assert_in_service(root, wikis)
    assert {"bananawiki", "bananawiki-maintenance", "bananawiki-agent"} <= system.running


def test_backups_and_recoveries_never_start_tenant_containers_again_themselves(hosting):
    """The runtime agent starts each wiki behind a mount gate that only its own ``tenant.start`` releases.

    A ``docker start`` would bring the container back inert (and the agent's
    recovery would take it for a wiki still starting): every wiki that
    served must come back through the portal, in a new container.
    """
    root, manager, system, wikis = hosting.root, hosting.manager, hosting.system, hosting.wikis

    def check(before: set[str], operation: str) -> None:
        status = read_json(root / "config/status.json")
        assert status["operation"] == operation and status["outcome"] == "complete"
        assert "unready_tenants" not in status
        assert not [command for command in system.commands if command[:2] == ["docker", "start"]]
        assert sorted(Path(item["data_dir"]).name for item in system.tenant_containers
                      if item["running"] and not item.get("gated")) == ["acme", "broken"]
        assert not {item["id"] for item in system.tenant_containers} & before
        assert_in_service(root, wikis)

    before = {item["id"] for item in system.tenant_containers}
    assert manager.backup().is_file()
    check(before, "backup")
    # An operation interrupted after it stopped the wikis, before its snapshot: nothing is put back.
    containers = [dict(item) for item in system.tenant_containers]
    write_json(root / "config/transaction.json", {
        **manager.journal_state(manager.settings()), "containers": containers,
        "serving": [item["data_dir"] for item in containers]})
    system.running.clear()
    for item in system.tenant_containers:
        item["running"] = False
    assert manager.recover() is True
    check({item["id"] for item in containers}, "recover")


def test_readiness_waits_only_for_wikis_still_running_in_the_restored_database(hosting):
    """A journal of an earlier controller lists every container it saw: terminated wikis are not required."""
    root, manager, wikis = hosting.root, hosting.manager, hosting.wikis
    settings = manager.settings()
    write_json(root / "config/transaction.json", {
        "settings": settings, "services": manager.names(settings), "active": manager.names(settings),
        "containers": [dict(item) for item in hosting.system.tenant_containers], "backup": None,
        "snapshot": None, "candidate": None, "phase": "preparing"})
    wikis.set_status("broken", "terminated")
    wikis.failing.add("broken")
    assert manager.recover() is True
    assert "unready_tenants" not in read_json(root / "config/status.json")
    assert_in_service(root, wikis)


def test_container_commands_skip_containers_that_are_gone():
    calls: list[list[str]] = []
    present = ["a" * 64]

    def runner(command, **_):
        calls.append(command)
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "".join(item + "\n" for item in present), "")
        missing = [part for part in command[2:] if len(part) == 64 and part not in present]
        stderr = "".join(f"Error response from daemon: No such container: {item}\n" for item in missing)
        return subprocess.CompletedProcess(command, 1 if missing else 0, "", stderr)

    system = System(runner=runner)
    containers = [{"id": "a" * 64, "running": True, "data_dir": "/srv/instances/a"},
                  {"id": "b" * 64, "running": True, "data_dir": "/srv/instances/b"}]
    system.stop_containers(containers)
    assert calls[-1] == ["docker", "stop", "--time", "30", "a" * 64]
    present.clear()
    calls.clear()
    system.remove_containers(containers)
    assert [command[:2] for command in calls] == [["docker", "rm"], ["docker", "ps"]]

    def failing(command, **_):
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "a" * 64 + "\n", "")
        return subprocess.CompletedProcess(command, 1, "", "Error response from daemon: cannot stop container")

    with pytest.raises(RuntimeError, match="docker stop failed"):
        System(runner=failing).stop_containers(containers)
    with pytest.raises(RuntimeError, match="docker rm failed"):
        System(runner=failing).remove_containers(containers)


def test_the_docker_check_before_an_operation_only_lists_ids():
    """The maintenance service still runs then: a wiki it removes between ``ps`` and ``inspect`` must not fail it."""
    calls: list[list[str]] = []

    def runner(command, **_):
        calls.append(command)
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "a" * 64 + "\n", "")
        return subprocess.CompletedProcess(command, 1, "", "Error: No such object: " + "a" * 64)

    System(runner=runner).check_docker({"mode": "hosting"})
    assert [command[:2] for command in calls] == [["docker", "ps"]]
    System(runner=runner).check_docker({"mode": "wiki"})
    assert len(calls) == 1

    def hung(command, **options):
        raise subprocess.TimeoutExpired(command, options["timeout"])

    with pytest.raises(DockerUnavailable, match="nothing was changed"):
        System(runner=hung).check_docker({"mode": "hosting"})


def test_inspect_failing_for_a_wiki_removed_after_the_listing_lists_again():
    """The maintenance service may remove a wiki between ``docker ps`` and ``docker inspect`` while it runs."""
    present = ["a" * 12, "b" * 12]
    calls: list[list[str]] = []

    def runner(command, **_):
        calls.append(command)
        if command[:2] == ["docker", "ps"]:
            listed = "".join(item + "\n" for item in present)
            if "b" * 12 in present:
                present.remove("b" * 12)  # terminated before the inspect
            return subprocess.CompletedProcess(command, 0, listed, "")
        missing = [item for item in command[2:] if item not in present]
        found = [{"Id": item, "State": {"Running": True}, "NetworkSettings": {"Networks": {}},
                  "Config": {"Labels": {"org.bananawiki.data-dir": f"/srv/data/instances/{item[0]}"}}}
                 for item in command[2:] if item in present]
        return subprocess.CompletedProcess(command, 1 if missing else 0, json.dumps(found),
                                           "".join(f"Error: No such object: {item}\n" for item in missing))

    settings = {"mode": "hosting", "root": "/srv"}
    assert [item["id"] for item in System(runner=runner).containers(settings)] == ["a" * 12]
    assert [command[:2] for command in calls] == [["docker", "ps"], ["docker", "inspect"]] * 2

    def failing(command, **_):  # a Docker that keeps failing still fails the operation
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "a" * 12 + "\n", "")
        return subprocess.CompletedProcess(command, 1, "", "Error response from daemon: busy")

    with pytest.raises(RuntimeError, match="docker inspect failed"):
        System(runner=failing).containers(settings)


# Before anything stops, and after the services start -------------------------------------------


def outdate_units(hosting) -> None:
    unit = hosting.system.unit_dir / "bananawiki.service"
    unit.write_text(unit.read_text() + "# written by an older controller\n")


def stops(system: FakeSystem) -> int:
    return len([command for command in system.commands if command[:2] == ["systemctl", "stop"]])


def test_convergence_lists_the_wikis_once_the_maintenance_service_stopped(hosting):
    """Until it stops, the maintenance service can remove a wiki between the listing and the inspection (R-13)."""
    system = hosting.system
    outdate_units(hosting)
    listed_while: list[bool] = []
    original = system.containers

    def containers(settings):
        listed_while.append("bananawiki-maintenance" in system.running)
        return original(settings)

    system.containers = containers  # type: ignore[method-assign]
    result = hosting.manager.update()
    assert result["outcome"] == "current" and result["units_converged"] is True
    assert listed_while[0] is False  # the wikis that serve, listed before anything starts again
    assert_in_service(hosting.root, hosting.wikis)


def test_convergence_with_docker_down_changes_nothing(hosting):
    root, system = hosting.root, hosting.system
    outdate_units(hosting)
    before = (system.unit_dir / "bananawiki.service").read_text()
    original = system.runner

    def unreachable(command, **options):
        if command[:1] == ["docker"]:
            return subprocess.CompletedProcess(command, 1, "", "Cannot connect to the Docker daemon.\n")
        return original(command, **options)

    system.runner = unreachable
    stopped = stops(system)
    with pytest.raises(DockerUnavailable, match="nothing was changed"):
        hosting.manager.update()
    assert stops(system) == stopped
    assert (system.unit_dir / "bananawiki.service").read_text() == before
    assert {"bananawiki", "bananawiki-maintenance", "bananawiki-agent"} <= system.running
    assert_in_service(root, hosting.wikis)


QUOTAS = {"bananawiki/ops/project_quota.py": "# XFS project quotas\n"}


def with_project_quotas(hosting, *, enforced: bool) -> list[Path]:
    """The storage as the runtime agent of a release with ``project_quota.py`` sees it before starting a wiki."""
    checked: list[Path] = []

    def quota_storage(instances):
        checked.append(instances)
        if not enforced:
            raise QuotaError("Tenant storage must be an XFS filesystem with enforced project quotas")

    hosting.system.quota_storage = quota_storage  # type: ignore[method-assign]
    return checked


def test_an_update_to_a_release_with_quotas_on_storage_without_them_stops_nothing(hosting):
    """Every wiki it stopped would stay down until the readiness checks gave up, then the update rolled back."""
    root, system, wikis, manager = hosting.root, hosting.system, hosting.wikis, hosting.manager
    new = hosting.upstream.commit("1.6.1", extra=QUOTAS)
    checked = with_project_quotas(hosting, enforced=False)
    stopped = stops(system)
    with pytest.raises(TenantStorageUnsupported, match="confirm that the wiki storage .* enforces XFS project quotas"):
        manager.update()
    assert checked == [root / "data/instances"]
    assert stops(system) == stopped and all(item["running"] for item in system.tenant_containers)
    assert (root / "current").resolve().name == hosting.old
    assert not list((root / "staging").glob("snapshot-*"))
    assert not [event for event in history(root) if event["operation"] == "recover"]
    # Not the revision's failure: the next automatic run tries it again.
    assert read_json(root / "config/status.json")["outcome"] == "failed"
    assert not (root / "config/failed-revision.json").exists()
    assert_in_service(root, wikis)
    with_project_quotas(hosting, enforced=True)
    assert manager.update()["outcome"] == "complete" and (root / "current").resolve().name == new


def test_backups_and_restarts_under_a_release_with_quotas_check_the_storage_first(hosting):
    root, system, wikis, manager = hosting.root, hosting.system, hosting.wikis, hosting.manager
    hosting.upstream.commit("1.6.1", extra=QUOTAS)
    with_project_quotas(hosting, enforced=True)
    manager.update()
    checked = with_project_quotas(hosting, enforced=False)  # e.g. remounted without prjquota
    for operation in (manager.backup, lambda: lifecycle(manager, argparse.Namespace(command="restart"))):
        stopped = stops(system)
        with pytest.raises(TenantStorageUnsupported):
            operation()
        assert stops(system) == stopped and all(item["running"] for item in system.tenant_containers)
        assert_in_service(root, wikis)
    assert len(checked) == 2
    # No running wiki: nothing would be kept down, and nothing is checked.
    lifecycle(manager, argparse.Namespace(command="stop"))
    original = system._docker

    def running_only(command):  # ``docker ps`` without ``--all``
        if command[:2] == ["docker", "ps"] and "--all" not in command:
            return 0, "".join(item["id"] + "\n" for item in system.tenant_containers if item["running"]), ""
        return original(command)

    system._docker = running_only  # type: ignore[method-assign]
    assert manager.backup().is_file()
    assert len(checked) == 2


def test_convergence_checks_the_storage_only_when_it_restarts_the_services(hosting):
    """Started again, the maintenance service restarts every running wiki whose storage quota is not verified."""
    root, system, manager = hosting.root, hosting.system, hosting.manager
    hosting.upstream.commit("1.6.1", extra=QUOTAS)
    with_project_quotas(hosting, enforced=True)
    manager.update()
    checked = with_project_quotas(hosting, enforced=False)
    shutil.rmtree(system.routes_directory(manager.settings()))  # converged but for the agent's routes directory
    stopped = stops(system)
    assert manager.update()["outcome"] == "current"
    assert checked == [] and stops(system) == stopped
    outdate_units(hosting)
    before = (system.unit_dir / "bananawiki.service").read_text()
    with pytest.raises(TenantStorageUnsupported):
        manager.update()
    assert checked == [root / "data/instances"] and stops(system) == stopped
    assert (system.unit_dir / "bananawiki.service").read_text() == before
    assert_in_service(root, hosting.wikis)


def test_start_after_stop_reports_the_wikis_that_did_not_come_back(hosting):
    """After ``stop`` no wiki serves, so ``start`` waits for none: those that do not serve are reported."""
    root, wikis, manager = hosting.root, hosting.wikis, hosting.manager
    lifecycle(manager, argparse.Namespace(command="stop"))
    hosting.system.on_start.append(lambda names: wikis.failing.add("broken"))
    result = lifecycle(manager, argparse.Namespace(command="start"))
    assert result["outcome"] == "complete" and result["unready_tenants"] == ["broken"]
    assert read_json(root / "config/status.json")["unready_tenants"] == ["broken"]
    assert_in_service(root, wikis)
    wikis.failing.clear()
    hosting.system.on_start.clear()
    hosting.system.on_start.append(wikis.on_start)
    assert "unready_tenants" not in lifecycle(manager, argparse.Namespace(command="start"))


def test_a_restore_on_a_new_server_reports_the_wikis_that_did_not_come_back(hosting, tmp_path):
    package = hosting.manager.backup()
    system = FakeSystem(tmp_path / "new-host")
    root = tmp_path / "srv/bananawiki"
    wikis = Wikis(root, system)
    system.on_start.append(lambda names: wikis.failing.add("broken"))
    result = Manager(root, system=system).restore(package, new=True)
    assert result["outcome"] == "complete" and result["unready_tenants"] == ["broken"]
    assert sorted(Path(item["data_dir"]).name for item in system.tenant_containers) == ["acme", "broken"]
    assert_in_service(root, wikis)
