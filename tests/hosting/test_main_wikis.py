"""Main wikis: kept for the platform, never expired, terminated, reset or deleted with their owner."""

from __future__ import annotations

from bananawiki.hosting.db import connection_scope


def _mark(web, wiki, on=True):
    return web.post(f"/admin/instances/{wiki['id']}/main", data={"main": "1" if on else "0"})


def _row(query, wiki):
    return query("SELECT * FROM instances WHERE id = ?", (wiki["id"],), one=True)


def test_an_administrator_keeps_a_wiki_for_the_platform(web, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    assert _row(query, wiki)["expires_at"], "a customer's wiki has an end date"
    login(web, admin)
    assert _mark(web, wiki).status_code == 302
    row = _row(query, wiki)
    assert row["is_main"] == 1 and row["expires_at"] is None and row["main_since"]
    events = [event["action"] for event in query(
        "SELECT action FROM hosting_events WHERE subject_type = 'instance' AND subject_id = ?", (wiki["id"],))]
    assert "instance.main_set" in events
    page = web.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert "Main wiki" in page and "This is a main wiki, owned by" in page and "Never (main wiki)" in page
    overview = web.get("/admin").get_data(as_text=True)
    assert 'id="main-wikis"' in overview and f"Main wiki of {owner['username']}" in overview


def test_an_administrators_main_wiki_is_shown_as_the_platforms(web, make_account, make_wiki, login):
    admin = make_account(admin=True)
    wiki = make_wiki(admin, "handbook")
    login(web, admin)
    _mark(web, wiki)
    assert "Platform wiki" in web.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert "This is one of the platform&#39;s wikis." in web.get(f"/admin/instances/{wiki['id']}").get_data(
        as_text=True)


def test_owners_and_other_people_are_not_shown_the_mark(portal, web, make_account, make_wiki, login):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    login(web, admin)
    _mark(web, wiki)
    customer = portal.test_client()
    login(customer, owner)
    for path in ("/dashboard", f"/instances/{wiki['id']}"):
        page = customer.get(path).get_data(as_text=True)
        assert "Main wiki" not in page and "Platform wiki" not in page and "main-wiki" not in page
    assert customer.post(f"/admin/instances/{wiki['id']}/main", data={"main": "0"}).status_code in (302, 403, 404)
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        assert db.scalar("SELECT is_main FROM instances WHERE id = ?", (wiki["id"],)) == 1


def test_a_main_wiki_is_never_terminated_reset_or_given_an_end_date(portal, web, make_account, make_wiki, login,
                                                                      query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    login(web, admin)
    _mark(web, wiki)
    web.post(f"/admin/instances/{wiki['id']}/terminate", data={"notify_user": "1"})
    web.post(f"/admin/instances/{wiki['id']}/extend", data={"extra_days": "3"})
    web.post(f"/admin/instances/{wiki['id']}/reset-wiki")
    customer = portal.test_client()
    login(customer, owner)
    customer.post(f"/instances/{wiki['id']}/terminate", data={"confirm_slug": wiki["subdomain"]})
    row = _row(query, wiki)
    assert row["status"] == "running" and row["expires_at"] is None
    import pytest

    from bananawiki.hosting.errors import ServiceError

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import instances

        for action in (lambda i: instances.terminate(i, actor_id=None),
                       lambda i: instances.reset_content(i, actor_id=admin["id"])):
            with pytest.raises(ServiceError) as refused:
                action(instances.get(wiki["id"]))
            assert refused.value.key == "hosting.instances.main_protected"
    # Even an end date written behind the portal's back does not end it.
    query("UPDATE instances SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (wiki["id"],))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import instances

        assert instances.terminate_expired() == 0
    assert _row(query, wiki)["status"] == "running"


def test_taking_the_mark_away_lets_the_wiki_end_again(web, make_account, make_wiki, login, query):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner)
    login(web, admin)
    _mark(web, wiki)
    _mark(web, wiki, on=False)
    row = _row(query, wiki)
    assert row["is_main"] == 0 and row["expires_at"] is None, "no end date until an administrator sets one"
    web.post(f"/admin/instances/{wiki['id']}/terminate", data={})
    assert _row(query, wiki)["status"] == "terminated"


def test_the_owners_account_cannot_be_deleted_while_it_holds_a_main_wiki(web, make_account, make_wiki, login,
                                                                         query):
    admin, owner, heir = make_account(admin=True), make_account(), make_account()
    wiki = make_wiki(owner)
    login(web, admin)
    _mark(web, wiki)
    web.post(f"/admin/accounts/{owner['id']}/delete", data={"instance_action": "terminate"})
    assert query("SELECT deleted_at FROM accounts WHERE id = ?", (owner["id"],), one=True)["deleted_at"] is None
    assert _row(query, wiki)["status"] == "running"
    web.post(f"/admin/accounts/{owner['id']}/schedule-deletion", data={"deletion_days": "1"})
    assert query("SELECT pending_deletion FROM accounts WHERE id = ?", (owner["id"],), one=True)[
        "pending_deletion"] == 0
    web.post(f"/admin/accounts/{owner['id']}/delete", data={"instance_action": "transfer",
                                                            "transfer_username": heir["username"]})
    row = _row(query, wiki)
    assert row["account_id"] == heir["id"] and row["is_main"] == 1 and row["expires_at"] is None, \
        "moved with the account's other wikis, still a main wiki without an end date"


def test_main_wikis_start_first_and_are_never_suspended_for_storage(portal, runtime, make_account, make_wiki,
                                                                    query):
    owner = make_account()
    first, main = make_wiki(owner, "early"), make_wiki(owner, "kept")
    query("UPDATE instances SET is_main = 1, expires_at = NULL, storage_limit_mb = 1 WHERE id = ?", (main["id"],))
    runtime.tenants[main["subdomain"]].usage = 50 * 1024 * 1024
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        from bananawiki.hosting import instances, maintenance

        rows = db.all("SELECT * FROM instances WHERE status = 'running' ORDER BY is_main DESC, created_at, id")
        assert [row["id"] for row in rows][:1] == [main["id"]]
        captured = []
        runtime.recover = lambda specs: captured.extend(spec.data_dir_name for spec in specs) or 0
        maintenance._start_running(portal)
        assert captured[0] == main["subdomain"] and first["subdomain"] in captured
        assert instances.enforce_storage_quotas() == 0
    assert _row(query, main)["status"] == "running"


def test_the_command_line_marks_main_wikis(portal, make_account, make_wiki, query, capsys):
    from bananawiki.hosting import admin as cli

    wiki = make_wiki(make_account(), "cli-kept")
    assert cli.main(["instance", "main", wiki["id"], "on"], app=portal) == 0
    assert _row(query, wiki)["is_main"] == 1 and "is a main wiki" in capsys.readouterr().out
    assert cli.main(["instance", "terminate", wiki["id"], "--yes"], app=portal) == 1, "refused while main"
    assert cli.main(["instance", "main", wiki["id"], "off"], app=portal) == 0
    assert _row(query, wiki)["is_main"] == 0


def test_an_owner_deleting_their_account_is_not_told_about_the_mark(portal, web, make_account, make_wiki, login,
                                                                    query):
    from .hosting_support import PASSWORD

    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner, "kept-one")
    login(web, admin)
    _mark(web, wiki)
    customer = portal.test_client()
    login(customer, owner)
    page = customer.post("/account/delete", data={"current_password": PASSWORD},
                         follow_redirects=True).get_data(as_text=True)
    assert "kept by the platform&#39;s administrators" in page
    assert "main wiki" not in page.lower() and "main mark" not in page.lower()
    assert query("SELECT deleted_at FROM accounts WHERE id = ?", (owner["id"],), one=True)["deleted_at"] is None
