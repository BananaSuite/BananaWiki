# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Account merges and wiki collaborators: an owner never ends up a collaborator of its own wiki (S-30)."""

from __future__ import annotations

from bananawiki.hosting import collaborators, instances, merges

SELF_COLLABORATIONS = ("SELECT COUNT(*) AS n FROM instance_collaborators c JOIN instances i ON i.id = c.instance_id "
                       "WHERE c.account_id = i.account_id")


def test_the_sources_collaboration_on_a_target_wiki_is_dropped(ctx, make_account, make_wiki, query):
    """The source handed its wiki to the target (and stayed a collaborator), then was merged into it:
    the target, now owner and collaborator at once, kept full access after an administrator moved the wiki on."""
    source, target, other, admin = make_account(), make_account(), make_account(), make_account(admin=True)
    wiki = make_wiki(target, "handed-over")
    collaborators.add(instances.get(wiki["id"]), target, source["username"], "full_access", [])
    merges.admin_merge(source["username"], target["username"], admin)
    assert collaborators.membership(wiki["id"], target["id"]) is None
    assert query(SELF_COLLABORATIONS, one=True)["n"] == 0
    instances.move_to_owner(instances.get(wiki["id"]), other, actor_id=admin["id"])
    assert collaborators.permissions_of(instances.get(wiki["id"]), target) == set()


def test_the_targets_collaboration_on_a_source_wiki_is_dropped(ctx, make_account, make_wiki, query):
    source, target, carol, admin = make_account(), make_account(), make_account(), make_account(admin=True)
    wiki = make_wiki(source, "moved-in")
    collaborators.add(instances.get(wiki["id"]), source, target["username"], "custom", ["view", "download"])
    collaborators.add(instances.get(wiki["id"]), source, carol["username"], "custom", ["view"])
    merges.admin_merge(source["username"], target["username"], admin)
    assert instances.get(wiki["id"])["account_id"] == target["id"]
    assert query(SELF_COLLABORATIONS, one=True)["n"] == 0
    assert [row["account_id"] for row in collaborators.list_for(wiki["id"])] == [carol["id"]]
