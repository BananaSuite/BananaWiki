"""Hosted wikis on storage without XFS project quotas: ``BW_HOSTING_TENANT_QUOTAS`` (auto, required, off).

An older installed updater cannot check anything a new release needs: on a
server whose wiki storage is ext4, it stops the wikis, writes its own
(generation 2) units, starts the new agent, portal and maintenance service,
and waits for readiness. In ``auto`` mode the agent starts every wiki without
hard quotas instead of refusing the storage, so that update completes.

Here the real runtime agent runs against a simulated Docker host, with what
an old updater leaves behind: the agent's generation 2 sandbox (no
``CAP_SYS_PTRACE``, so ``/proc/<pid>/root`` of a tenant is out of reach, and
no quota system calls), tenant networks created by the 1.6 agent (Docker's
``br-<id>`` bridges) and routes Caddy never acknowledged.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from bananawiki.hosting.runtime import RuntimeFailure, TenantPolicy, TenantSpec
from bananawiki.hosting.runtime.agent import AgentRuntime
from bananawiki.ops import runtime_agent
from bananawiki.ops.files import write_environment, write_json
from bananawiki.ops.runtime_agent import (
    AgentError,
    MountUninspectable,
    TenantRuntime,
    bridge_name,
    container_name,
)

from .agent_fakes import IMAGE, make_runtime, make_spec, provision, runtime_config
from .docker_host import DockerHost
from .quota_fakes import FakeProjectQuota

EXT4 = "Tenant storage must be an XFS filesystem with enforced project quotas"
LOST = "Hard quotas were enforced on this platform before and are no longer available: "
GIB, MIB = 1024 ** 3, 1024 ** 2
WIKIS = {"acme__apex": "acme.example.org", "team-wiki": "team-wiki.acme.example.org"}


def denied(*_args, **_kwargs):
    raise MountUninspectable("quota_unavailable",
                             "A live tenant mount could not be inspected: [Errno 13] Permission denied")


class Ext4Platform:
    """The server right after an old updater switched it to this release (see the module docstring)."""

    def __init__(self, tmp_path: Path, *, setting: str | None = None, problem: str | None = EXT4,
                 caddy: bool = False, quotas_before: bool = False):
        self.cfg = runtime_config(tmp_path)
        self.instances = Path(self.cfg.instances_dir)
        self.host = DockerHost(caddy=caddy)
        self.routes = tmp_path / "routes"
        self.routes.mkdir()
        # What the 1.6 portal published and its agent rendered; without Caddy no reload was ever acknowledged.
        (self.routes / "routes.json").write_text(json.dumps({name: [host] for name, host in WIKIS.items()}))
        (self.routes / "tenants.caddy").write_text("# Managed by the BananaWiki runtime agent.\n")
        for name in WIKIS:
            (self.instances / name).mkdir(parents=True)
            (self.instances / name / "bananawiki.db").write_bytes(b"SQLite format 3\0" + b"x" * 100)
            self.host.legacy_network(container_name(str((self.instances / name).resolve())) + "-net")
        self.state = tmp_path / "quotas"
        if quotas_before:  # the agent put a wiki under quota here before (its private quota registry)
            self.state.mkdir()
            (self.state / "allocations.sqlite3").write_bytes(b"")
        owner = (self.instances / "team-wiki").stat()
        config = {"service": "bananawiki", "service_uid": owner.st_uid, "service_gid": owner.st_gid,
                  "instances_dir": str(self.instances), "routes_dir": str(self.routes), "image": IMAGE,
                  "portal": "127.0.0.1:5099",
                  "quota": {"state_dir": str(self.state), "max_bytes": 10 * GIB, "max_inodes": 100_000,
                            "project_start": 1_000_000, "storage_reserve_bytes": 256 * MIB},
                  "limits": {"memory_mb": 4096, "cpus": 4.0, "pids": 4096, "nofile": 65536}}
        if setting is not None:
            config["tenant_quotas"] = setting
        private = tmp_path / "agent-private"
        private.mkdir(mode=0o700)
        self.agent = TenantRuntime(config, runner=self.host, private_dir=private)
        # What the storage looks like to the agent (the real check needs root and XFS).
        self.agent._storage_probe = lambda: problem
        # The generation 2 agent unit has no CAP_SYS_PTRACE: tenant mounts cannot be inspected through /proc.
        self.agent._mounted_identity = denied
        self.agent._verify_mount_descriptor = denied
        self.portal = AgentRuntime(self.cfg, client=self.agent, probe=lambda _address, _port: True)

    def specs(self) -> list[TenantSpec]:
        return [
            TenantSpec(instance_id="apex0001", slug="acme", domain_mode="apex", data_dir_name="acme__apex",
                       port=None, url="https://acme.example.org", hostnames=("acme.example.org",),
                       policy=TenantPolicy(storage_limit_bytes=0)),  # an apex wiki has no plan
            TenantSpec(instance_id="team0001", slug="team-wiki", domain_mode="hosting", data_dir_name="team-wiki",
                       port=None, url="https://" + WIKIS["team-wiki"], hostnames=(WIKIS["team-wiki"],),
                       policy=TenantPolicy(storage_limit_bytes=500 * MIB)),
        ]

    def container(self, name: str) -> dict:
        return self.host.containers[container_name(str((self.instances / name).resolve()))]


# Storage without quotas, after an update by an older updater


def test_after_an_old_updater_install_the_maintenance_service_starts_every_wiki_without_hard_quotas(tmp_path):
    server = Ext4Platform(tmp_path)  # BW_HOSTING_TENANT_QUOTAS unset: auto
    assert server.portal.recover(server.specs()) == 2
    assert server.host.running() == sorted(container_name(str((server.instances / name).resolve()))
                                           for name in WIKIS)
    for name in WIKIS:
        container = server.container(name)
        assert container["running"] and not container["gated"], "the mount gate was released"
        assert container["sysctls"] == runtime_agent.IPV6_SYSCTLS
        assert not any(label.startswith("org.bananawiki.quota-") for label in container["labels"])
        network = container_name(str((server.instances / name).resolve())) + "-net"
        assert server.host.networks[network]["bridge"] == bridge_name(network), "the 1.6 network was replaced"
    # Nothing the old agent unit forbids was attempted, and no quota registry was created.
    assert not server.state.exists()
    # Without hard quotas the agent holds a wiki without a plan to the host ceiling, through the wiki itself.
    assert "BW_STORAGE_LIMIT_BYTES=10737418240\n" in server.container("acme__apex")["env"]
    assert f"BW_STORAGE_LIMIT_BYTES={500 * MIB}\n" in server.container("team-wiki")["env"]
    # The routes now point at the new containers (what the old updater's readiness check looks for).
    rendered = (server.routes / "tenants.caddy").read_text()
    for name in WIKIS:
        assert f"reverse_proxy {server.container(name)['address']}:5001 " in rendered
    # Every maintenance pass recovers again: running wikis without hard quotas are not restarted in a loop.
    runs = len(server.host.runs())
    assert server.portal.recover(server.specs()) == 0
    assert len(server.host.runs()) == runs
    listed = server.agent.call("tenant.list", {})["tenants"]
    assert {item["storage_quota_mode"] for item in listed} == {"unenforced"}
    assert not any(item["storage_quota_verified"] for item in listed)


def test_a_host_without_caddy_does_not_log_routing_warnings(tmp_path, caplog):
    """Another reverse proxy routes the wikis: Caddy never acknowledges a routing change, and nothing is wrong."""
    server = Ext4Platform(tmp_path)
    with caplog.at_level(logging.INFO, logger="bananawiki.agent"):
        assert server.portal.recover(server.specs()) == 2
        server.portal.stop(server.specs()[1])
        server.portal.start(server.specs()[1])
    assert not [record.getMessage() for record in caplog.records
                if record.levelno >= logging.WARNING and ("route" in record.getMessage() or "Caddy" in record.getMessage())]
    assert not (server.routes / "routes.reloaded.sha256").exists()


def test_without_hard_quotas_tasks_and_stops_work_under_the_old_agent_unit(tmp_path):
    server = Ext4Platform(tmp_path)
    assert server.portal.recover(server.specs()) == 2
    apex, other = server.specs()
    assert server.portal.list_users(other)[1] == 0  # a task in the running wiki: docker exec
    server.portal.stop(other)
    assert container_name(str((server.instances / "team-wiki").resolve())) not in server.host.containers
    assert server.agent.call("tenant.task", {"tenant": "team-wiki", "request": {"action": "list_users"}})["result"]
    server.portal.start(other)
    server.portal.restart(apex)
    assert len(server.host.running()) == 2


def test_a_new_wiki_can_be_created_without_hard_quotas(tmp_path):
    server = Ext4Platform(tmp_path)
    spec = make_spec("fresh", port=None, policy=TenantPolicy(storage_limit_bytes=100 * MIB))
    provision(server.portal, spec)
    assert server.container("fresh")["running"]


def test_a_new_wiki_needs_free_space_when_quotas_are_not_enforced(tmp_path):
    server = Ext4Platform(tmp_path)
    (server.instances / "fresh").mkdir()
    server.agent._free_bytes = lambda _path: 256 * MIB + 99 * MIB  # less than the reserve plus its 100 MiB plan
    with pytest.raises(AgentError) as error:
        server.agent.call("tenant.quota", {"tenant": "fresh", "prepare": True,
                                           "limits": {"storage_bytes": 100 * MIB}})
    assert error.value.code == "no_space"
    # Wikis already on the volume are never refused for it: they are already there.
    answer = server.agent.call("tenant.quota", {"tenant": "team-wiki", "prepare": True,
                                                "limits": {"storage_bytes": 500 * MIB}})
    assert answer["storage_quota_mode"] == "unenforced" and answer["over_plan"] is False
    server.agent._free_bytes = lambda _path: 256 * MIB + 100 * MIB
    assert server.agent.call("tenant.quota", {"tenant": "fresh", "prepare": True,
                                              "limits": {"storage_bytes": 100 * MIB}})["tenant"] == "fresh"
    # The portal shows the agent's refusal as a lack of space.
    server.agent._free_bytes = lambda _path: 0
    with pytest.raises(RuntimeFailure) as failure:
        provision(server.portal, make_spec("second", port=None))
    assert failure.value.code == "no_space"


# Without hard quotas the actual mount is still compared with the tenant directory


def current_units(server: Ext4Platform) -> None:
    """The 1.7 agent unit (CAP_SYS_PTRACE): the agent sees by inode what Docker actually mounted at /data."""

    def identity(pid):
        container = next(item for item in server.host.containers.values() if item["pid"] == pid)
        source = dict(part.split("=", 1) for part in container["mount"].split(","))["src"]
        info = os.stat(source)  # dockerd resolved the bind source, links included, when it created the container
        return info.st_dev, info.st_ino

    server.agent._mounted_identity = identity


def swap_at_docker_run(server: Ext4Platform, tenant: str, target: Path) -> None:
    """A compromised portal (it owns data/instances) replaces the directory after the agent's last check."""
    inner = server.agent.runner

    def runner(command, **options):
        if command[:2] == ["docker", "run"] and f"/{tenant}," in " ".join(command):
            path = server.instances / tenant
            path.rename(server.instances / (tenant + ".moved"))
            path.symlink_to(target)
        return inner(command, **options)

    server.agent.runner = runner


def test_without_hard_quotas_a_directory_swapped_at_start_is_refused(tmp_path):
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    server = Ext4Platform(tmp_path)
    current_units(server)
    assert server.portal.recover(server.specs()[:1]) == 1  # the actual mount is checked, and matches
    assert server.host.released == [container_name(str((server.instances / "acme__apex").resolve()))]
    swap_at_docker_run(server, "team-wiki", victim)
    with pytest.raises(RuntimeFailure, match="changed while"):
        server.portal.start(server.specs()[1])
    assert len(server.host.released) == 1, "the gate of the swapped container was never released"
    assert server.host.running() == [container_name(str((server.instances / "acme__apex").resolve()))]


def test_without_hard_quotas_a_task_on_a_swapped_directory_is_refused(tmp_path):
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    server = Ext4Platform(tmp_path)
    current_units(server)
    swap_at_docker_run(server, "team-wiki", victim)
    with pytest.raises(AgentError, match="changed while") as error:
        server.agent.call("tenant.task", {"tenant": "team-wiki", "request": {"action": "list_users"}})
    assert error.value.code == "invalid_tenant"
    assert not [call for call in server.host.calls if call[:2] == ["docker", "exec"]]
    assert not server.host.containers, "the task container was removed"


def test_an_agent_unit_that_cannot_inspect_mounts_starts_wikis_unverified_and_says_so(tmp_path, caplog):
    """Units an older updater wrote (no CAP_SYS_PTRACE): the comparison is impossible until they converge."""
    server = Ext4Platform(tmp_path)
    with caplog.at_level(logging.WARNING, logger="bananawiki.agent"):
        assert server.portal.recover(server.specs()) == 2
    warnings = [record.getMessage() for record in caplog.records if "cannot be compared" in record.getMessage()]
    assert len(warnings) == 1 and "'bananawiki update'" in warnings[0]


# The three settings


@pytest.mark.parametrize("operation", ["tenant.quota", "tenant.start", "tenant.task"])
def test_required_refuses_on_storage_without_quotas_and_touches_no_container(tmp_path, operation):
    server = Ext4Platform(tmp_path, setting="required")
    with pytest.raises(AgentError) as error:
        server.agent.call(operation, {"tenant": "team-wiki", "prepare": True, "request": {"action": "list_users"}})
    assert error.value.code == "quota_unavailable"
    assert "BW_HOSTING_TENANT_QUOTAS=required" in error.value.message and EXT4 in error.value.message
    assert not [call for call in server.host.calls if call[:2] in (["docker", "run"], ["docker", "rm"],
                                                                    ["docker", "exec"])]


def test_auto_starts_the_wikis_once_quotas_are_lost_with_a_warning(tmp_path, caplog):
    """Remounted without prjquota (or the volume replaced): the wikis keep serving, never a silent downgrade."""
    server = Ext4Platform(tmp_path, quotas_before=True)
    with caplog.at_level(logging.INFO, logger="bananawiki.agent"):
        runtime_agent.log_storage_policy(server.agent)
        assert server.portal.recover(server.specs()) == 2
    listed = server.agent.call("tenant.list", {})["tenants"]
    assert {item["storage_quota_mode"] for item in listed} == {"unenforced"}
    assert server.agent.call("ping", {})["storage"] == {
        "setting": "auto", "hard_quotas": False, "refused": False, "lost": True, "reason": EXT4}
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
                and "no longer available" in record.getMessage()]
    assert len(warnings) == 1 and warnings[0].startswith(LOST + EXT4), "said once, at the start"
    assert "config set tenant-quotas required" in warnings[0]
    # required: hard quotas or no wiki starts.
    server.agent.config["tenant_quotas"] = "required"
    with pytest.raises(AgentError) as error:
        server.agent.call("tenant.start", {"tenant": "team-wiki"})
    assert error.value.code == "quota_unavailable" and "BW_HOSTING_TENANT_QUOTAS=required" in error.value.message


def test_quotas_lost_while_the_agent_runs_are_logged_when_it_happens(tmp_path, caplog):
    """A remount without prjquota while the agent runs: a warning is logged once, and again if it happens again."""
    found: list[str | None] = [None]
    server = Ext4Platform(tmp_path, quotas_before=True)
    server.agent._storage_probe = lambda: found[0]
    with caplog.at_level(logging.INFO, logger="bananawiki.agent"):
        runtime_agent.log_storage_policy(server.agent)
        assert server.agent.storage_policy().hard_quotas
        found[0] = EXT4
        for _request in range(3):
            assert server.agent.storage_policy().lost
        found[0] = None
        assert server.agent.storage_policy().hard_quotas
        found[0] = EXT4
        assert server.agent.storage_policy().lost
    lost = [record for record in caplog.records if "no longer available" in record.getMessage()]
    assert len(lost) == 2 and {record.levelno for record in lost} == {logging.WARNING}


def test_a_quota_registry_that_cannot_be_read_counts_as_used(tmp_path):
    """Never a quiet notice when it cannot be told: Python 3.14's Path.exists() answers False for such a path."""
    server = Ext4Platform(tmp_path, quotas_before=True)
    server.state.chmod(0)
    try:
        policy = server.agent.storage_policy()
    finally:
        server.state.chmod(0o700)
    assert policy.lost and not policy.refused and policy.warning().startswith(LOST)


def test_off_never_uses_project_quotas_even_on_storage_that_supports_them(tmp_path):
    server = Ext4Platform(tmp_path, setting="off", problem=None)
    quota = FakeProjectQuota(server.instances)
    quota.failure = "project quotas must not be used"
    server.agent._injected_quota = quota
    assert server.portal.recover(server.specs()) == 2
    assert server.agent.call("ping", {})["storage"]["setting"] == "off"


def test_auto_on_storage_with_quotas_enforces_them(tmp_path):
    server = Ext4Platform(tmp_path, problem=None)
    server.agent._injected_quota = FakeProjectQuota(server.instances)
    answer = server.agent.call("tenant.quota", {"tenant": "team-wiki", "prepare": True,
                                                "limits": {"storage_bytes": 500 * MIB}})
    assert answer["storage_quota_verified"] is True and answer["storage_quota_mode"] == "enforced"
    assert answer["storage_quota"]["byte_limit"] == 500 * MIB
    assert server.agent.call("ping", {})["storage"] == {"setting": "auto", "hard_quotas": True, "refused": False,
                                                        "lost": False, "reason": ""}


@pytest.mark.parametrize(("value", "expected"), [("", "auto"), ("AUTO", "auto"), ("required", "required"),
                                                 (" off ", "off")])
def test_the_agent_reads_the_setting_from_app_env(tmp_path, monkeypatch, value, expected):
    (tmp_path / "config").mkdir()
    write_json(tmp_path / "config/installation.json", {"service": "root", "port": 5099})
    write_environment(tmp_path / "config/app.env", {"BW_HOSTING_TENANT_QUOTAS": value} if value else {})
    assert runtime_agent.load_config(tmp_path)["tenant_quotas"] == expected


def test_an_invalid_setting_is_refused_rather_than_guessed(tmp_path):
    (tmp_path / "config").mkdir()
    write_json(tmp_path / "config/installation.json", {"service": "root", "port": 5099})
    write_environment(tmp_path / "config/app.env", {"BW_HOSTING_TENANT_QUOTAS": "maybe"})
    with pytest.raises(AgentError) as error:
        runtime_agent.load_config(tmp_path)
    assert error.value.code == "not_configured" and "auto, required or off" in error.value.message


def test_the_agent_logs_its_storage_policy_when_it_starts(tmp_path, caplog):
    """Storage that never had hard quotas is a supported configuration: a note that says how to get them."""
    server = Ext4Platform(tmp_path)
    with caplog.at_level(logging.INFO, logger="bananawiki.agent"):
        runtime_agent.log_storage_policy(server.agent)
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    notice = next(record for record in caplog.records if "storage quotas" in record.getMessage())
    assert notice.levelno == logging.INFO and "Hard per-wiki storage quotas are not enforced" in notice.getMessage()
    assert EXT4 in notice.getMessage() and "prjquota" in notice.getMessage()
    assert "no longer available" not in notice.getMessage()


# The portal


def test_the_portal_reports_the_storage_policy_for_its_administrators(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    agent.storage = {"setting": "auto", "hard_quotas": False, "refused": False, "lost": False, "reason": EXT4}
    assert runtime.storage_protection() == agent.storage
    agent.storage = {"setting": "auto", "hard_quotas": False, "refused": False, "lost": True, "reason": EXT4}
    runtime._storage_checked = None
    assert runtime.storage_protection()["lost"] is True
    agent.storage = {"setting": "auto", "hard_quotas": False, "refused": False, "reason": EXT4}  # an earlier agent
    runtime._storage_checked = None
    assert runtime.storage_protection()["lost"] is False
    agent.storage = {"setting": "required", "hard_quotas": True, "refused": False, "reason": ""}
    assert runtime.storage_protection()["hard_quotas"] is False, "cached for a minute"
    runtime._storage_checked = None
    assert runtime.storage_protection()["hard_quotas"] is True
    agent.failures["ping"] = AgentError("unavailable", "down")
    runtime._storage_checked = None
    assert runtime.storage_protection() == {}


def test_the_portal_restarts_an_unenforced_wiki_whose_sandbox_is_outdated(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    agent.storage_mode = "unenforced"
    spec = make_spec("acme")
    provision(runtime, spec)
    started = agent.started
    assert runtime.recover([spec]) == 0 and agent.started == started
    agent.containers["acme"]["quota_protected"] = False  # started by a 1.6 agent, without the seccomp profile
    assert runtime.recover([spec]) == 1 and agent.started == started + 1


NEVER = {"setting": "auto", "hard_quotas": False, "refused": False, "lost": False, "reason": EXT4}
LOST_STORAGE = {**NEVER, "lost": True}
REFUSED = {"setting": "required", "hard_quotas": False, "refused": True, "lost": False,
           "reason": "BW_HOSTING_TENANT_QUOTAS=required, but the wiki storage does not enforce XFS project quotas"}
OFF = {"setting": "off", "hard_quotas": False, "refused": False, "lost": False,
       "reason": "BW_HOSTING_TENANT_QUOTAS=off turns them off"}


def banner(client) -> str:
    """The storage notice of the administrators' dashboard ('' when none is shown)."""
    page = client.get("/admin").get_data(as_text=True)
    start = page.find('<div class="alert alert--')
    return page[start:page.index("</div></div>", start)] if 'id="storage-protection"' in page else ""


def test_administrators_are_told_when_wikis_run_without_hard_quotas(portal, runtime, make_account, login):
    client = portal.test_client()
    login(client, make_account(admin=True))
    assert banner(client) == "", "unknown: nothing shown"
    # Never had them (storage without project quotas): a note with how to get hard limits.
    runtime.storage = dict(NEVER)
    shown = banner(client)
    assert 'class="alert alert--info" id="storage-protection"' in shown
    assert "BW_HOSTING_TENANT_QUOTAS=<wbr>auto" in shown and "prjquota" in shown and EXT4 in shown
    assert "no longer available" not in shown and "Wikis cannot start" not in shown
    # It states the trade-off: the limit is only checked, so a wiki can fill the shared disk between two checks.
    assert "is still checked" in shown and "still enforced" not in shown and "at worst, fill the disk" in shown
    # Neutral about the cause (the storage, or services under units an older controller wrote): status says which.
    assert "sudo bananawiki status" in shown
    # Lost: the loss is named, as a warning.
    runtime.storage = dict(LOST_STORAGE)
    shown = banner(client)
    assert 'class="alert alert--warning" id="storage-protection"' in shown
    assert "Hard per-wiki storage quotas stopped working on this server" in shown
    assert f'Server detail: <span lang="en">{EXT4}</span>' in shown
    assert "fill the disk" in shown and "sudo bananawiki restart" in shown and "prjquota" in shown
    # Refused only under required, the operator's explicit opt-in.
    runtime.storage = dict(REFUSED)
    shown = banner(client)
    assert 'class="alert alert--error" id="storage-protection"' in shown and "Wikis cannot start" in shown
    # A phone breaks "(BW_HOSTING_TENANT_QUOTAS=required)," after the "=", not inside "required".
    assert shown.count("BW_HOSTING_TENANT_QUOTAS=<wbr>required") == 2, "the notice and the server's detail"
    # Turned off on purpose: said once, plainly, with the command that turns them on again.
    runtime.storage = dict(OFF)
    shown = banner(client)
    assert 'class="alert alert--info" id="storage-protection"' in shown
    assert "Hard per-wiki storage quotas are switched off on this server" in shown
    assert "config set tenant-quotas auto" in shown and "turns them off" not in shown
    assert "at worst, fill the disk" in shown
    # An agent from before the lost flag: as never enforced.
    runtime.storage = {key: value for key, value in NEVER.items() if key != "lost"}
    assert "alert--info" in banner(client)
    runtime.storage = {"setting": "required", "hard_quotas": True, "refused": False, "lost": False, "reason": ""}
    assert banner(client) == ""


def test_the_storage_banner_wraps_settings_and_paths_on_a_phone(portal):
    """"(BW_HOSTING_TENANT_QUOTAS=required)," and the server's detail (paths) are wider than a 375 px banner:
    a setting may break after its "=", anything else wherever it must."""
    from bananawiki.hosting.templating import wbr_settings

    css = (Path(__file__).resolve().parents[2] / "bananawiki/hosting/static/css/hosting.css").read_text(encoding="utf-8")
    assert "#storage-protection { overflow-wrap: anywhere; }" in css
    assert wbr_settings("(BW_HOSTING_TENANT_QUOTAS=off): x=1 <b>") == (
        "(BW_HOSTING_TENANT_QUOTAS=<wbr>off): x=1 &lt;b&gt;")


@pytest.mark.parametrize(("language", "never", "lost", "refused", "off", "fill", "detail"), [
    ("it", "Su questo server le wiki non hanno quote rigide di spazio",
     "Le quote rigide di spazio per ogni wiki hanno smesso di funzionare su questo server",
     "Le wiki non possono avviarsi", "Le quote rigide di spazio per ogni wiki sono disattivate su questo server",
     "riempire il disco", "Dettaglio tecnico dal server:"),
    ("de", "Auf diesem Server haben die Wikis keine festen Speicherkontingente",
     "Feste Speicherkontingente pro Wiki funktionieren auf diesem Server nicht mehr",
     "Wikis können nicht starten", "Feste Speicherkontingente pro Wiki sind auf diesem Server ausgeschaltet",
     "den Datenträger füllen", "Technische Angabe des Servers:"),
])
def test_the_storage_notices_are_translated(portal, runtime, make_account, login, language, never, lost, refused,
                                            off, fill, detail):
    client = portal.test_client()
    login(client, make_account(admin=True))
    client.post("/language", data={"language": language})
    runtime.storage = dict(NEVER)
    shown = banner(client)
    assert never in shown and fill in shown and "sudo bananawiki status" in shown
    runtime.storage = dict(LOST_STORAGE)
    shown = banner(client)
    # The agent's reason is English: labelled as the server's own words, in their language.
    assert lost in shown and f'{detail} <span lang="en">{EXT4}</span>' in shown
    assert fill in shown and "sudo bananawiki restart" in shown
    runtime.storage = dict(REFUSED)
    assert refused in banner(client)
    runtime.storage = dict(OFF)
    shown = banner(client)
    assert off in shown and fill in shown and "config set tenant-quotas auto" in shown and detail not in shown
