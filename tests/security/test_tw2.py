"""Regression tests for the TW2 finding of the full re-audit pass 14 (admin reset after a change of owner)."""

from __future__ import annotations

from bananawiki.ops import tenant_task
from tests.hosting.conftest import make_account, make_wiki, portal, query, runtime  # noqa: F401


def test_tw2_1_a_reset_after_a_change_of_owner_covers_every_administrator(app, make_user, login):
    from tests.conftest import PASSWORD

    named = make_user("wikiadmin", role="owner")
    backdoor = make_user("backdoor", role="admin")
    member = make_user("reader", role="user")
    other = app.test_client()
    login(other, backdoor)
    assert other.get("/admin/users").status_code == 200

    # Without the flag (no change of owner) only the named account is reset, as before.
    plain = tenant_task.reset_admin(app.config["BW"], {"username": "wikiadmin", "password": "Temporary pass 12345"})
    assert plain["user_ids"] == [named["id"]]
    assert other.get("/admin/users").status_code == 200

    result = tenant_task.reset_admin(app.config["BW"], {"username": "wikiadmin", "password": "Temporary pass 67890",
                                                        "every_admin": True})
    assert set(result["user_ids"]) == {named["id"], backdoor["id"]} and member["id"] not in result["user_ids"]
    assert result["username"] == "wikiadmin"
    assert other.get("/admin/users").status_code != 200  # its session ended
    fresh = app.test_client()
    fresh.post("/login", data={"username": "backdoor", "password": PASSWORD})
    assert fresh.get("/admin/users").status_code != 200  # its old password no longer works


def test_tw2_1_the_portal_resets_every_administrator_once_the_wiki_changed_owner(  # noqa: F811
        portal, runtime, make_account, make_wiki, query):  # noqa: F811
    from bananawiki.hosting import events, instances
    from bananawiki.hosting.db import connection_scope

    owner = make_account("owner1")
    wiki = make_wiki(owner)

    def last_reset():
        return [call[0] for call in runtime.calls if call[0].startswith("reset_admins_")][-1]

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.reset_admin_password(instances.get(wiki["id"]), actor_id=owner["id"])
        assert last_reset() == "reset_admins_named"
        events.record("instance", wiki["id"], "instance.owner_changed", None, "newowner")
        instances.reset_admin_password(instances.get(wiki["id"]), actor_id=owner["id"])
        assert last_reset() == "reset_admins_every"
