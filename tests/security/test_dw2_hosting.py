"""Re-audit pass 22 regressions for the hosting portal (DW2-x)."""
from tests.hosting.conftest import make_account, make_wiki, portal, query, runtime  # noqa: F401


def test_dw2_1_an_account_merge_is_a_change_of_owner(portal, make_account, make_wiki, query):  # noqa: F811
    from bananawiki.hosting import domains, instances, merges
    from bananawiki.hosting.db import connection_scope

    old, new, admin = make_account("oldowner"), make_account("newowner"), make_account("adminacct", admin=True)
    wiki = make_wiki(old)
    query("INSERT INTO instance_custom_domains (domain, instance_id, verification_token, created_at, verified_at, "
          "verified_until, last_checked_at) VALUES ('docs.oldowner.example', ?, 'tok', '2026-01-01 00:00:00', "
          "'2026-01-01 00:00:00', '2999-01-01 00:00:00', '2026-01-01 00:00:00')", (wiki["id"],))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        merges.admin_merge("oldowner", "newowner", admin)
        assert instances.get(wiki["id"])["account_id"] == new["id"]
        assert domains.binding(wiki["id"]) is None
        assert instances._owner_changed(instances.get(wiki["id"]))
    assert query("SELECT 1 FROM hosting_events WHERE action = 'domain.removed' AND subject_id = ?",
                 (wiki["id"],), one=True)
