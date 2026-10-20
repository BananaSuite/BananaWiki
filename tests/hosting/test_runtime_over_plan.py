"""A wiki the runtime agent adopted above its plan starts, and the operator and its owner hear about it once.

1.6 let wikis hold more than their plan. When 1.7 puts such a wiki under hard
storage quotas, the agent adopts it at what it holds plus a small headroom
and says ``over_plan``. The portal records an event for the operator and
emails the owner the first time, and again only after the wiki was back
within its plan in between.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bananawiki.hosting import email, instances
from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.runtime.agent import AgentRuntime

from .agent_fakes import FakeAgent

MIB = 1024 * 1024


class OverPlanAgent(FakeAgent):
    over = True

    def tenant_quota(self, args: dict[str, Any]) -> dict[str, Any]:
        result = super().tenant_quota(args)
        if args.get("prepare"):
            plan = args.get("limits", {}).get("storage_bytes", 0)
            result["over_plan"] = self.over
            if self.over:
                result["plan"] = {"storage_bytes": plan, "inodes": 100_000}
                result["storage_quota"] = {**result["storage_quota"], "byte_limit": plan + 64 * MIB}
        return result


def test_a_wiki_adopted_above_its_plan_starts_and_is_reported_once(portal, make_account, make_wiki, query):
    owner = make_account("planowner", email="owner@example.org")
    inst = make_wiki(owner, "big")
    query("UPDATE instances SET storage_limit_mb = 100 WHERE id = ?", (inst["id"],))
    cfg = portal.config["HOSTING"]
    (Path(cfg.instances_dir) / "big").mkdir(parents=True, exist_ok=True)
    agent = OverPlanAgent(Path(cfg.instances_dir))
    runtime = AgentRuntime(cfg, client=agent, probe=lambda _address, _port: True)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        spec = instances.spec(instances.get(inst["id"]))
        with email.capture_outbox() as outbox:
            runtime.start(spec)
            runtime.restart(spec)
        assert agent.containers["big"]["running"]
        assert [message.to for message in outbox] == ["owner@example.org"]
        assert "big" in outbox[0].subject and "100 MB" in outbox[0].text
        agent.over = False
        with email.capture_outbox() as outbox:
            runtime.restart(spec)
            agent.over = True
            runtime.restart(spec)
        assert len(outbox) == 1, "over its plan again after it was back within it"
    events = query("SELECT action, reason FROM hosting_events WHERE subject_id = ? AND action = 'storage.over_plan'",
                   (inst["id"],))
    assert len(events) == 2 and "100 MB" in events[0]["reason"]


def test_a_wiki_the_recovery_adopts_above_its_plan_is_reported_too(portal, make_account, make_wiki, query):
    # After an update, the maintenance service's recovery starts every wiki on worker threads, which have no
    # portal application context: the report is made once they are done, so it is not lost on that path.
    owner = make_account("recoverowner", email="recover@example.org")
    inst = make_wiki(owner, "huge")
    query("UPDATE instances SET storage_limit_mb = 100 WHERE id = ?", (inst["id"],))
    cfg = portal.config["HOSTING"]
    (Path(cfg.instances_dir) / "huge").mkdir(parents=True, exist_ok=True)
    agent = OverPlanAgent(Path(cfg.instances_dir))
    runtime = AgentRuntime(cfg, client=agent, probe=lambda _address, _port: True)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        spec = instances.spec(instances.get(inst["id"]))
        with email.capture_outbox() as outbox:
            assert runtime.recover([spec]) == 1
        assert agent.containers["huge"]["running"]
        assert [message.to for message in outbox] == ["recover@example.org"]
    events = query("SELECT reason FROM hosting_events WHERE subject_id = ? AND action = 'storage.over_plan'",
                   (inst["id"],))
    assert len(events) == 1 and "100 MB" in events[0]["reason"]


def test_the_limit_a_wiki_was_adopted_at_bounds_its_archives(portal, make_account, make_wiki, query):
    """Platform backups, downloads and plugin snapshots hold such a wiki to the hard limit it was adopted at,
    which the portal records here; once it is back within its plan, to its plan again."""
    from bananawiki.hosting.runtime import platform_backup

    owner = make_account("budgetowner")
    inst = make_wiki(owner, "held")
    query("UPDATE instances SET storage_limit_mb = 100 WHERE id = ?", (inst["id"],))
    cfg = portal.config["HOSTING"]
    (Path(cfg.instances_dir) / "held").mkdir(parents=True, exist_ok=True)
    agent = OverPlanAgent(Path(cfg.instances_dir))
    runtime = AgentRuntime(cfg, client=agent, probe=lambda _address, _port: True)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        spec = instances.spec(instances.get(inst["id"]))
        runtime.start(spec)
        assert platform_backup.held_limit(cfg.platform_state_dir, inst["id"], 100 * MIB) == 164 * MIB
        assert platform_backup.held_limit(cfg.platform_state_dir, inst["id"], 200 * MIB) == 200 * MIB
        assert platform_backup.held_limit(cfg.platform_state_dir, inst["id"], None) is None, "no limit"
        agent.over = False
        runtime.restart(spec)
        assert platform_backup.held_limit(cfg.platform_state_dir, inst["id"], 100 * MIB) == 100 * MIB
