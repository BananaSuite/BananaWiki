"""Hosting servers moving from 1.4: the agent, its routes directory and the Caddyfile follow every update.

1.4 hosting servers send every wiki host to the portal (``:443`` catch-all),
which proxied tenant traffic itself. 1.6 routes wiki hosts from Caddy straight
to the containers, through the routes file the runtime agent writes and the
managed Caddyfile imports. These tests start from the 1.4 layout (legacy units,
the 1.4 Caddyfile recorded in ``config/proxy.json``, a running tenant) and
check that the controller installs all of it, waits until the wikis are
routed, and puts the 1.4 setup back when readiness fails.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest
from ops_fakes import FakeSystem, Upstream, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops import UNIT_GENERATION
from bananawiki.ops.cli import lifecycle
from bananawiki.ops.files import digest_file, read_json, write_json
from bananawiki.ops.manager import PROXY_ROLLBACK, Manager
from bananawiki.ops.runtime_agent import render_routes

# What `bananawiki proxy --install` of 1.4 wrote for this installation.
LEGACY_CADDYFILE = (
    "# Managed by BananaSuite\n{\n\tservers {\n\t\ttimeouts {\n\t\t\tread_header 10s\n\t\t\tread_body 5m\n"
    "\t\t\tidle 2m\n\t\t}\n\t}\n\ton_demand_tls {\n\t\task http://127.0.0.1:5001/internal/domains/authorize\n"
    "\t}\n}\n\nwiki.example.org {\n\troot * /opt/bananawiki/site\n\tfile_server {\n\t\thide .git .env\n\t}\n}\n\n"
    "portal.wiki.example.org {\n\treverse_proxy 127.0.0.1:5001\n}\n\n"
    ":443 {\n\ttls {\n\t\ton_demand\n\t}\n\treverse_proxy 127.0.0.1:5001\n}\n"
)
HOST = "acme-hosting.wiki.example.org"


@pytest.fixture
def fake_system(tmp_path) -> FakeSystem:
    return FakeSystem(tmp_path / "host")


@pytest.fixture
def upstream(tmp_path) -> Upstream:
    return Upstream(tmp_path)


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "opt" / "bananawiki"


class Portal:
    """What the 1.6 maintenance service does when it starts: recover the wikis, then publish their routes."""

    def __init__(self, root: Path, system: FakeSystem):
        self.root, self.system = root, system
        self.publishes = True
        self.address = 3

    def __call__(self, names: list[str]) -> None:
        if "bananawiki-maintenance" not in names:
            return
        tenant_dir = str((self.root / "data/instances/acme").resolve())
        if not any(item["data_dir"] == tenant_dir for item in self.system.tenant_containers):
            self.address += 1
            self.system.tenant_containers.append({
                "id": f"c{self.address}", "running": True, "addresses": [f"172.18.0.{self.address}"],
                "internal_port": 5001, "data_dir": tenant_dir})
        routes = self.system.state_dir / "bananawiki-routes"
        agent_installed = (self.system.unit_dir / "bananawiki-agent.service").exists()
        if not self.publishes or not agent_installed or not routes.is_dir():
            return  # a 1.4 portal, or a 1.6 portal without its agent, publishes nothing
        (routes / "routes.json").write_text(json.dumps({"acme": [HOST]}))
        address = self.system.tenant_containers[-1]["addresses"][0]
        (routes / "tenants.caddy").write_text(render_routes({"acme": ([HOST], f"{address}:5001")}))


def hosting_1x(root: Path, system: FakeSystem, upstream: Upstream, revision: str) -> Portal:
    """A hosting server as 1.4 left it: legacy units, the 1.4 Caddyfile, one running wiki."""
    settings = legacy_install(root, system, upstream, revision, mode="hosting")
    settings["portal_domain"] = "portal.wiki.example.org"
    write_json(root / "config/installation.json", settings)
    tenant = root / "data/instances/acme"
    tenant.mkdir(parents=True)
    database = sqlite3.connect(root / "data/hosting.db")
    database.execute("CREATE TABLE instances (subdomain TEXT, domain_mode TEXT, status TEXT)")
    database.execute("INSERT INTO instances VALUES ('acme', 'subdomain', 'running')")
    database.commit()
    database.close()
    system.proxy_file.parent.mkdir(parents=True)
    system.proxy_file.write_text(LEGACY_CADDYFILE)
    write_json(root / "config/proxy.json", {"installed_sha256": digest_file(system.proxy_file), "previous": None})
    system.docker_group = True
    system.tenant_containers.append({"id": "c-1x", "running": True, "addresses": ["172.18.0.2"],
                                     "internal_port": 5001, "data_dir": str(tenant.resolve())})
    portal = Portal(root, system)
    system.on_start.append(portal)
    return portal


def caddy_reloads(system: FakeSystem) -> int:
    return system.commands.count(["systemctl", "reload-or-restart", "caddy"])


def assert_routed(root: Path, system: FakeSystem) -> None:
    caddyfile = system.proxy_file.read_text()
    routes = system.state_dir / "bananawiki-routes"
    assert f"import {routes}/*.caddy" in caddyfile
    assert "portal.wiki.example.org, http://portal.wiki.example.org {" in caddyfile
    assert read_json(root / "config/proxy.json")["installed_sha256"] == digest_file(system.proxy_file)
    assert (routes.stat().st_mode & 0o777) == 0o755
    assert f"# tenant acme\n{HOST}, http://{HOST} {{" in (routes / "tenants.caddy").read_text()
    agent = (system.unit_dir / "bananawiki-agent.service").read_text()
    assert "User=root" in agent and "StateDirectory=bananawiki-routes" in agent
    assert "SupplementaryGroups=docker" not in (system.unit_dir / "bananawiki.service").read_text()
    assert system.docker_group is False
    assert not (root / "config" / PROXY_ROLLBACK).exists()
    assert not (root / "config/transaction.json").exists()


def assert_1x_setup(root: Path, system: FakeSystem) -> None:
    assert system.proxy_file.read_text() == LEGACY_CADDYFILE
    assert read_json(root / "config/proxy.json")["installed_sha256"] == digest_file(system.proxy_file)
    assert not (system.unit_dir / "bananawiki-agent.service").exists()
    portal = (system.unit_dir / "bananawiki.service").read_text()
    assert UNIT_GENERATION not in portal and "SupplementaryGroups=docker" in portal
    assert system.docker_group is True
    assert not (system.state_dir / "bananawiki-routes").exists()
    assert not (root / "config" / PROXY_ROLLBACK).exists()
    assert not (root / "config/transaction.json").exists()


def test_update_from_1x_hosting_installs_agent_routes_and_caddyfile(root, fake_system, upstream):
    old = upstream.commit("1.4 hosting", modern=False)
    hosting_1x(root, fake_system, upstream, old)
    new = upstream.commit("1.6 hosting")
    manager = Manager(root, system=fake_system)

    result = manager.update()

    assert result["outcome"] == "complete" and result["revision"] == new
    assert result["proxy_updated"] is True
    assert ["caddy", "validate", "--config", str(root / "config/Caddyfile.next"), "--adapter", "caddyfile"] \
        in fake_system.commands
    assert caddy_reloads(fake_system) == 1
    assert_routed(root, fake_system)
    # Nothing left to converge: no second Caddy reload, no restart.
    starts = len(fake_system.started())
    again = manager.update()
    assert again["outcome"] == "current" and again["units_converged"] is False
    assert caddy_reloads(fake_system) == 1 and len(fake_system.started()) == starts


def test_failed_readiness_restores_the_1x_caddyfile_units_and_routes(root, fake_system, upstream):
    old = upstream.commit("1.4 hosting", modern=False)
    portal = hosting_1x(root, fake_system, upstream, old)
    bad = upstream.commit("1.6 whose portal never publishes routes")
    portal.publishes = False
    manager = Manager(root, system=fake_system)

    with pytest.raises(RuntimeError):
        manager.update()

    assert (root / "current").resolve().name == old
    assert read_json(root / "config/status.json")["outcome"] == "rolled_back"
    assert read_json(root / "config/failed-revision.json") == {"revision": bad}
    assert_1x_setup(root, fake_system)
    assert caddy_reloads(fake_system) == 2  # the new file, then the old one back


def test_release_installed_by_the_1x_updater_is_converged_and_routed(root, fake_system, upstream):
    new = upstream.commit("1.6 hosting, installed by the 1.4 updater")
    hosting_1x(root, fake_system, upstream, new)
    manager = Manager(root, system=fake_system)
    assert manager.status()["units_current"] is False

    result = manager.update()

    assert result["outcome"] == "current" and result["units_converged"] is True
    assert result["proxy_updated"] is True
    assert_routed(root, fake_system)
    assert manager.status()["units_current"] is True


def test_failed_convergence_restores_the_1x_caddyfile_and_units(root, fake_system, upstream):
    new = upstream.commit("1.6 hosting, installed by the 1.4 updater")
    portal = hosting_1x(root, fake_system, upstream, new)
    portal.publishes = False
    manager = Manager(root, system=fake_system)

    with pytest.raises(RuntimeError):
        manager.update()

    assert_1x_setup(root, fake_system)
    assert {"bananawiki", "bananawiki-maintenance"} <= fake_system.running


def test_restart_converges_the_caddyfile_too(root, fake_system, upstream):
    new = upstream.commit("1.6 hosting, installed by the 1.4 updater")
    hosting_1x(root, fake_system, upstream, new)
    manager = Manager(root, system=fake_system)

    result = lifecycle(manager, argparse.Namespace(command="restart"))

    assert result["outcome"] == "complete" and result["proxy_updated"] is True
    assert_routed(root, fake_system)


def test_interrupted_rollback_restores_the_caddyfile_from_the_journal(root, fake_system, upstream):
    old = upstream.commit("1.4 hosting", modern=False)
    portal = hosting_1x(root, fake_system, upstream, old)
    upstream.commit("1.6")
    portal.publishes = False
    manager = Manager(root, system=fake_system)
    original = manager.recover
    crashed: list[bool] = []

    def crash_once() -> bool:
        if not crashed and (root / "config/transaction.json").exists():
            crashed.append(True)
            raise KeyboardInterrupt  # the operator's session died before the rollback
        return original()

    manager.recover = crash_once  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        manager.update()
    journal = read_json(root / "config/transaction.json")
    assert journal["proxy"]["caddyfile"] == str(root / "config" / PROXY_ROLLBACK)
    assert fake_system.proxy_file.read_text() != LEGACY_CADDYFILE

    assert Manager(root, system=fake_system).recover() is True
    assert_1x_setup(root, fake_system)


def test_operator_caddyfile_is_left_alone_with_a_warning(root, fake_system, upstream):
    new = upstream.commit("1.6 hosting, installed by the 1.4 updater")
    hosting_1x(root, fake_system, upstream, new)
    custom = LEGACY_CADDYFILE + "\nextra.example.org {\n\trespond 204\n}\n"
    fake_system.proxy_file.write_text(custom)
    manager = Manager(root, system=fake_system)

    result = manager.update()

    assert result["units_converged"] is True and "does not import" in result["proxy_warning"]
    assert fake_system.proxy_file.read_text() == custom and caddy_reloads(fake_system) == 0
    routes = fake_system.state_dir / "bananawiki-routes"
    fake_system.proxy_file.write_text(custom + f"import {routes}/*.caddy\n")
    assert "proxy_warning" not in manager.update()


def test_proxy_email_survives_re_rendering(root, fake_system, upstream):
    new = upstream.commit("1.6 hosting, installed by the 1.4 updater")
    hosting_1x(root, fake_system, upstream, new)
    record = read_json(root / "config/proxy.json")
    write_json(root / "config/proxy.json", {**record, "email": "ops@example.org"})
    Manager(root, system=fake_system).update()
    assert "\temail ops@example.org\n" in fake_system.proxy_file.read_text()
    assert read_json(root / "config/proxy.json")["email"] == "ops@example.org"


def test_readiness_requires_each_running_wiki_routed_to_its_current_container(root, fake_system, upstream):
    new = upstream.commit("1.6 hosting, installed by the 1.4 updater")
    hosting_1x(root, fake_system, upstream, new)
    manager = Manager(root, system=fake_system)
    manager.update()
    settings = manager.settings()
    services = manager.services(settings)
    assert fake_system.route_issues(settings, services, []) == []
    fake_system.tenant_containers[-1]["addresses"] = ["172.18.0.99"]  # recreated, routes not refreshed yet
    assert fake_system.route_issues(settings, services, []) == ["Tenant is not routed to its container: acme"]
    (fake_system.state_dir / "bananawiki-routes/routes.json").unlink()
    assert fake_system.route_issues(settings, services, []) == ["The portal has not published the wiki routes yet."]
    # Port mode has no Caddy routes to wait for.
    environment = root / "config/app.env"
    environment.write_text(environment.read_text() + "HOSTING_MODE=port\n")
    assert fake_system.route_issues(settings, services, []) == []
