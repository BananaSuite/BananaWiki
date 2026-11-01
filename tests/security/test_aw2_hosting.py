"""Re-audit pass 20 regressions for the hosting portal (AW2-x)."""
from tests.hosting.conftest import make_account, make_wiki, portal, query, runtime  # noqa: F401


def test_aw2_1_a_change_of_owner_drops_the_previous_owners_custom_domain(
        portal, make_account, make_wiki, query):  # noqa: F811
    from bananawiki.hosting import accounts, domains, instances
    from bananawiki.hosting.db import connection_scope

    old, new = make_account("oldowner"), make_account("newowner")
    wiki, kept = make_wiki(old), make_wiki(old)
    for inst, name in ((wiki, "docs.oldowner.example"), (kept, "kept.oldowner.example")):
        query("INSERT INTO instance_custom_domains (domain, instance_id, verification_token, created_at, verified_at, "
              "verified_until, last_checked_at) VALUES (?, ?, 'tok', '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
              "'2999-01-01 00:00:00', '2026-01-01 00:00:00')", (name, inst["id"]))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        notes = instances.move_to_owner(instances.get(wiki["id"]), accounts.get(new["id"]), actor_id=None)
        assert "domain_removed" in notes and domains.binding(wiki["id"]) is None
        assert domains.binding(kept["id"]) is not None  # wikis that keep their owner keep their domain
        instances.move_to_owner(instances.get(kept["id"]), accounts.get(old["id"]), actor_id=None)
        assert domains.binding(kept["id"]) is not None
    assert query("SELECT 1 FROM hosting_events WHERE action = 'domain.removed' AND subject_id = ?",
                 (wiki["id"],), one=True)
