"""Re-audit pass 16 regressions for the hosting portal (VW2-x)."""
from tests.hosting.conftest import make_account, make_wiki, portal, query, runtime  # noqa: F401


def test_vw2_2_pruning_the_moderation_log_keeps_the_fact_that_a_wiki_changed_owner(
        portal, runtime, make_account, make_wiki):  # noqa: F811
    from bananawiki.hosting import accounts, events, instances
    from bananawiki.hosting.db import connection_scope, db

    old, new = make_account("ownerold"), make_account("ownernew")
    wiki = make_wiki(old)

    def last_reset():
        return [call[0] for call in runtime.calls if call[0].startswith("reset_admins_")][-1]

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.move_to_owner(instances.get(wiki["id"]), accounts.get(new["id"]), actor_id=None)
        db.execute("UPDATE hosting_events SET created_at = datetime('now', '-40 days')")
        assert events.prune(30) > 0
        kept = db.one("SELECT actor_id, reason FROM hosting_events WHERE action = 'instance.owner_changed'")
        assert kept is not None and kept["actor_id"] is None and kept["reason"] == ""
        instances.reset_admin_password(instances.get(wiki["id"]), actor_id=new["id"])
        assert last_reset() == "reset_admins_every"


def test_bw2_1_a_restorable_terminated_wiki_keeps_the_fact_that_it_changed_owner(
        portal, runtime, make_account, make_wiki):  # noqa: F811
    from bananawiki.hosting import accounts, events, instances
    from bananawiki.hosting.db import connection_scope, db

    old, new, admin = make_account("ownerold"), make_account("ownernew"), make_account("boss", admin=True)
    wiki = make_wiki(old)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.move_to_owner(instances.get(wiki["id"]), accounts.get(new["id"]), actor_id=None)
        instances.terminate(instances.get(wiki["id"]), actor_id=new["id"], reason="manual")
        db.execute("UPDATE hosting_events SET created_at = datetime('now', '-40 days')")
        events.prune(30)
        instances.restore(instances.get(wiki["id"]), actor_id=admin["id"])
        instances.reset_admin_password(instances.get(wiki["id"]), actor_id=new["id"])
        assert [call[0] for call in runtime.calls if call[0].startswith("reset_admins_")][-1] == "reset_admins_every"
