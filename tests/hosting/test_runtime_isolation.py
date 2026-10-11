"""The portal never starts again by itself a wiki an update isolated; an explicit start does (OPS2-08).

An update that keeps its release although a few wikis do not serve under it
stops those and records them in ``isolated-wikis.json``. The maintenance
service's recovery (after its own restart or a host reboot) leaves them
stopped; starting one from the portal (or ``restart``) lifts the isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

from .agent_fakes import make_runtime, make_spec, provision


def isolate(runtime, *names: str) -> Path:
    path = Path(runtime._cfg().platform_state_dir) / "isolated-wikis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "tenants": {name: "readiness timed out" for name in names}}))
    return path


def test_recovery_leaves_an_isolated_wiki_stopped_until_it_is_started_explicitly(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    acme, broken = make_spec("acme"), make_spec("broken", port=6002)
    provision(runtime, acme)
    provision(runtime, broken)
    agent.containers.clear()  # the host restarted
    path = isolate(runtime, "broken")
    assert runtime.recover([acme, broken]) == 1
    assert set(agent.containers) == {"acme"}
    runtime.start(broken)
    assert set(agent.containers) == {"acme", "broken"}
    assert json.loads(path.read_text())["tenants"] == {}
    agent.containers.clear()
    assert runtime.recover([acme, broken]) == 2


def test_a_wiki_isolated_while_recovery_runs_is_not_started_by_it(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    broken = make_spec("broken")
    provision(runtime, broken)
    agent.containers.clear()
    original = agent.tenant_stop

    def stop(args):
        isolate(runtime, "broken")  # the update isolates it between the recovery's stop and start
        return original(args)

    agent.tenant_stop = stop
    assert runtime.recover([broken]) == 0
    assert "broken" not in agent.containers


def test_a_wiki_isolated_while_its_container_starts_is_stopped_again(tmp_path):
    # The update's stop came before the recovery started the container: the recovery itself stops it again.
    runtime, agent = make_runtime(tmp_path)
    broken = make_spec("broken")
    provision(runtime, broken)
    agent.containers.clear()
    original = agent.tenant_start

    def start(args):
        result = original(args)
        isolate(runtime, "broken")  # readiness timed out while this start was in progress
        return result

    agent.tenant_start = start
    assert runtime.recover([broken]) == 0
    assert not agent.containers.get("broken", {}).get("running")


def test_a_policy_change_does_not_start_an_isolated_wiki(tmp_path):
    # A changed policy (an account made administrator, an approved feature request, a new global
    # setting) restarts the running wikis to apply it. That is not someone starting the wiki: one an
    # update isolated stays stopped and isolated, and its next explicit start uses the new policy.
    runtime, agent = make_runtime(tmp_path)
    acme, broken = make_spec("acme"), make_spec("broken", port=6002)
    provision(runtime, acme)
    provision(runtime, broken)
    runtime.stop(broken)  # the update stopped it ...
    path = isolate(runtime, "broken")  # ... and recorded why
    for spec in (acme, broken):
        runtime.apply_limits(spec)
        runtime.restart(spec, force=True, policy_change=True)
    assert set(agent.containers) == {"acme"}
    assert set(json.loads(path.read_text())["tenants"]) == {"broken"}
    runtime.restart(broken, force=True)  # an explicit restart (portal, hosting-admin) still lifts it
    assert set(agent.containers) == {"acme", "broken"}
    assert json.loads(path.read_text())["tenants"] == {}


def test_the_portal_applies_a_policy_without_starting_wikis_explicitly(portal, make_account, make_wiki, runtime):
    from bananawiki.hosting import instances
    from bananawiki.hosting.db import connection_scope

    wiki = make_wiki(make_account())
    calls = []
    original = runtime.restart

    def restart(spec, *, force=False, policy_change=False):
        calls.append((spec.data_dir_name, force, policy_change))
        return original(spec, force=force, policy_change=policy_change)

    runtime.restart = restart
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        assert instances.apply_policy(instances.get(wiki["id"]), actor_id=None)
        instances.restart(instances.get(wiki["id"]), actor_id=None)
    assert [policy_change for _name, _force, policy_change in calls] == [True, False]
