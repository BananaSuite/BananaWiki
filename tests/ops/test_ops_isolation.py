"""An update isolates a wiki that does not serve under its release only once it is really stopped.

Readiness can time out while the portal's recovery is still starting that
wiki (a slow migration). Isolation therefore records the wiki for the portal
first (its recovery never starts it again by itself), stops it, and checks
that it is stopped, again if a start in progress brought it back. A wiki that
keeps running is never reported isolated: the update is rolled back instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_ops_recovery import hosting as hosting  # noqa: F401 - the simulated hosting platform

from bananawiki.ops.files import read_environment, read_json, write_environment

ISOLATED = Path("data/platform_state/isolated-wikis.json")


def restarting(hosting, slug: str, times: int):
    """The portal's recovery, still starting *slug*: a container comes back after it is stopped, *times* times."""
    original = hosting.system._docker
    left = {"count": times}

    def docker(command):
        result = original(command)
        stopping = command[:2] == ["docker", "stop"] or command[:3] == ["docker", "rm", "--force"]
        if stopping and left["count"] and any(item.startswith(slug + "-") for item in command[2:]):
            left["count"] -= 1
            hosting.wikis.address += 1
            hosting.system.tenant_containers.append({
                "id": f"{slug}-{hosting.wikis.address}", "running": True,
                "addresses": [f"172.18.0.{hosting.wikis.address}"], "internal_port": 5001,
                "data_dir": str(hosting.wikis.directory(slug))})
        return result

    hosting.system._docker = docker
    return left


def test_a_wiki_a_start_in_progress_brings_back_is_stopped_again_before_it_is_reported(hosting):
    root = hosting.root
    new = hosting.upstream.commit("1.6.1")
    hosting.wikis.failing_under["broken"] = new
    restarting(hosting, "broken", 1)
    result = hosting.manager.update()
    assert result["outcome"] == "complete" and result["isolated_tenants"] == ["broken"]
    running = {Path(item["data_dir"]).name for item in hosting.system.tenant_containers if item["running"]}
    assert running == {"acme"}
    recorded = json.loads((root / ISOLATED).read_text())["tenants"]
    assert list(recorded) == ["broken"]


def test_a_wiki_that_cannot_be_stopped_is_never_reported_isolated(hosting):
    root = hosting.root
    new = hosting.upstream.commit("1.6.1")
    hosting.wikis.failing_under["broken"] = new
    restarting(hosting, "broken", 100)
    try:
        hosting.manager.update()
    except RuntimeError as error:
        assert "could not be stopped" in str(error)
    else:
        raise AssertionError("the update must fail")
    assert read_json(root / "config/status.json")["outcome"] == "rolled_back"
    assert (root / "current").resolve().name == hosting.old
    assert not (root / ISOLATED).exists(), "the rollback put the portal's state back"


def test_isolation_is_recorded_where_the_portal_reads_it(hosting):
    # The portal keeps platform_state/ next to its database unless HOSTING_PLATFORM_STATE_DIR says otherwise.
    # An operator who moved the database (HOSTING_DATABASE_PATH) must not get the record in a folder the
    # portal never reads: its recovery would start the isolated wiki again after a restart.
    root = hosting.root
    environment = root / "config/app.env"
    portal_database = root / "data/portal/hosting.db"
    write_environment(environment, {**read_environment(environment), "HOSTING_DATABASE_PATH": str(portal_database)})
    portal_database.parent.mkdir()
    hosting.system.mark_isolated(hosting.manager.settings(), {"broken": "readiness timed out"})
    recorded = json.loads((root / "data/portal/platform_state/isolated-wikis.json").read_text())["tenants"]
    assert recorded == {"broken": "readiness timed out"}
    assert not (root / ISOLATED).exists()
