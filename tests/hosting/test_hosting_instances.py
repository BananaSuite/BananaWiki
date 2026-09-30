"""Wiki lifecycle from the dashboard, tenant isolation, collaborators and ownership transfers."""

from __future__ import annotations

import re

import pytest

USE_CASE = "A small team handbook for our volunteers."


def _create(client, slug: str, **extra):
    data = {"subdomain": slug, "declared_use_case": USE_CASE, "compliance_declared": "1"}
    data.update(extra)
    return client.post("/instances/create", data=data)


def test_create_shows_credentials_once_and_never_stores_them(web, make_account, login, runtime, query):
    owner = make_account()
    login(web, owner)
    response = _create(web, "handbook")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    html = response.get_data(as_text=True)
    row = query("SELECT id, subdomain, status, port, admin_username, admin_password_plain FROM instances", one=True)
    assert row["subdomain"] == "handbook" and row["status"] == "running"
    assert row["admin_password_plain"] is None
    password = runtime.tenants["handbook"].passwords[row["admin_username"]]
    assert password in html
    assert runtime.called("provision") == ["handbook"]
    assert password not in web.get(f"/instances/{row['id']}").get_data(as_text=True)


@pytest.mark.parametrize("extra", [
    {"subdomain": "ab"}, {"subdomain": "Bad_Name"}, {"subdomain": "double--dash"}, {"subdomain": "admin"},
    {"declared_use_case": "short"}, {"compliance_declared": ""}, {"domain_mode": "apex"},
])
def test_create_validates_name_use_case_and_compliance(web, make_account, login, query, extra):
    owner = make_account()
    login(web, owner)
    assert _create(web, extra.pop("subdomain", "valid-name"), **extra).status_code == 400
    assert query("SELECT COUNT(*) AS n FROM instances", one=True)["n"] == 0


def test_create_rolls_back_when_the_runtime_fails(web, make_account, login, runtime, query):
    owner = make_account()
    login(web, owner)
    runtime.fail_next("provision", "no_space")
    response = _create(web, "failing")
    assert response.status_code == 400
    assert "disk space" in response.get_data(as_text=True)
    row = query("SELECT status, terminated_reason FROM instances", one=True)
    assert row == {"status": "terminated", "terminated_reason": "provisioning_failed"}
    assert _create(web, "failing").status_code == 200, "the name is free again"


def test_account_limit_is_enforced(tmp_path, make_account):
    from .hosting_support import build_portal, portal_environ

    app = build_portal(tmp_path / "limit", environ=portal_environ(tmp_path / "limit", MAX_INSTANCES_PER_ACCOUNT="1"))
    client = app.test_client()
    with app.app_context():
        from bananawiki.hosting import accounts
        from bananawiki.hosting.db import connection_scope

        with connection_scope():
            accounts.create("limited", "correct horse 42")
    client.post("/login", data={"username": "limited", "password": "correct horse 42"})
    assert _create(client, "first-wiki").status_code == 200
    assert _create(client, "second-wiki").status_code == 400


def test_other_accounts_cannot_see_or_touch_a_wiki(portal, make_account, make_wiki, login, runtime):
    owner, stranger = make_account(), make_account()
    wiki = make_wiki(owner, "private-wiki")
    client = portal.test_client()
    login(client, stranger)
    for path in ("", "/collaborators", "/analytics", "/domain"):
        assert client.get(f"/instances/{wiki['id']}{path}").status_code == 404, path
    for action in ("stop", "restart", "terminate", "reset-password", "reset-wiki", "download", "transfer"):
        assert client.post(f"/instances/{wiki['id']}/{action}").status_code == 404, action
    assert client.post(f"/instances/{wiki['id']}/collaborators/add", data={"username": stranger["username"]}).status_code == 404
    client.post("/instances/bulk-stop", data={"instance_ids": [wiki["id"]]})
    assert runtime.called("stop") == []
    assert "private-wiki" not in client.get("/dashboard").get_data(as_text=True)


def test_stop_restart_and_terminate(web, make_account, make_wiki, login, runtime, query):
    owner = make_account()
    wiki = make_wiki(owner, "cycle")
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/stop")
    assert query("SELECT status FROM instances", one=True)["status"] == "stopped"
    assert runtime.tenants["cycle"].running is False
    web.post(f"/instances/{wiki['id']}/restart")
    assert query("SELECT status FROM instances", one=True)["status"] == "running"
    web.post(f"/instances/{wiki['id']}/terminate")
    row = query("SELECT subdomain, status, terminated_at FROM instances", one=True)
    assert row["status"] == "terminated" and row["terminated_at"]
    assert re.fullmatch(r"cycle--terminated-[0-9a-z]{8}", row["subdomain"])
    assert web.get(f"/instances/{wiki['id']}").status_code == 404


def test_reset_admin_password_shows_a_new_password(web, make_account, make_wiki, login, runtime):
    owner = make_account()
    wiki = make_wiki(owner, "resetme")
    login(web, owner)
    response = web.post(f"/instances/{wiki['id']}/reset-password")
    assert response.status_code == 200
    username = wiki["admin_username"]
    assert runtime.tenants["resetme"].passwords[username] in response.get_data(as_text=True)


def test_suspended_wiki_is_locked_for_its_owner(web, make_account, make_wiki, login, query, runtime):
    owner = make_account()
    wiki = make_wiki(owner, "locked")
    query("UPDATE instances SET status = 'suspended' WHERE id = ?", (wiki["id"],))
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/restart")
    web.post(f"/instances/{wiki['id']}/terminate")
    assert query("SELECT status FROM instances", one=True)["status"] == "suspended"
    assert runtime.called("start") == []


# ── Collaborators ─────────────────────────────────────────────────────────────


def test_collaborator_gets_only_the_granted_permissions(portal, make_account, make_wiki, login, runtime, query):
    owner, helper = make_account(), make_account()
    wiki = make_wiki(owner, "shared")
    owner_client, helper_client = portal.test_client(), portal.test_client()
    login(owner_client, owner)
    login(helper_client, helper)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add",
                      data={"username": helper["username"], "role": "custom", "permissions": ["start_stop"]})
    assert helper_client.get(f"/instances/{wiki['id']}").status_code == 200
    assert "shared" in helper_client.get("/dashboard").get_data(as_text=True)
    helper_client.post(f"/instances/{wiki['id']}/stop")
    assert runtime.called("stop") == ["shared"]
    assert helper_client.post(f"/instances/{wiki['id']}/terminate").status_code == 404
    assert helper_client.get(f"/instances/{wiki['id']}/collaborators").status_code == 404
    assert query("SELECT status FROM instances", one=True)["status"] == "stopped"


def test_collaborator_manager_cannot_grant_more_than_they_hold(portal, make_account, make_wiki, login, query):
    owner, manager, newcomer = make_account(), make_account(), make_account()
    wiki = make_wiki(owner, "delegated")
    owner_client, manager_client = portal.test_client(), portal.test_client()
    login(owner_client, owner)
    login(manager_client, manager)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add", data={
        "username": manager["username"], "role": "custom", "permissions": ["manage_collaborators", "start_stop"]})
    manager_client.post(f"/instances/{wiki['id']}/collaborators/add",
                        data={"username": newcomer["username"], "role": "full_access"})
    manager_client.post(f"/instances/{wiki['id']}/collaborators/add",
                        data={"username": newcomer["username"], "role": "custom", "permissions": ["terminate"]})
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators WHERE account_id = ?",
                 (newcomer["id"],), one=True)["n"] == 0
    manager_client.post(f"/instances/{wiki['id']}/collaborators/add",
                        data={"username": newcomer["username"], "role": "custom", "permissions": ["start_stop"]})
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators WHERE account_id = ?",
                 (newcomer["id"],), one=True)["n"] == 1


def test_owner_can_remove_a_collaborator(portal, make_account, make_wiki, login):
    owner, helper = make_account(), make_account()
    wiki = make_wiki(owner, "removal")
    owner_client, helper_client = portal.test_client(), portal.test_client()
    login(owner_client, owner)
    login(helper_client, helper)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add",
                      data={"username": helper["username"], "role": "full_access"})
    assert helper_client.get(f"/instances/{wiki['id']}").status_code == 200
    owner_client.post(f"/instances/{wiki['id']}/collaborators/{helper['id']}/remove")
    assert helper_client.get(f"/instances/{wiki['id']}").status_code == 404


# ── Ownership transfer ────────────────────────────────────────────────────────


def test_transfer_needs_the_recipients_acceptance(portal, make_account, make_wiki, login, query):
    owner, recipient, stranger = make_account(), make_account(), make_account()
    wiki = make_wiki(owner, "gift")
    owner_client, recipient_client, stranger_client = (portal.test_client() for _ in range(3))
    login(owner_client, owner)
    login(recipient_client, recipient)
    login(stranger_client, stranger)
    owner_client.post(f"/instances/{wiki['id']}/transfer", data={"username": recipient["username"]})
    transfer = query("SELECT id FROM instance_ownership_transfers", one=True)
    assert transfer is not None
    stranger_client.post(f"/transfers/{transfer['id']}/accept")
    assert query("SELECT account_id FROM instances", one=True)["account_id"] == owner["id"]
    recipient_client.post(f"/transfers/{transfer['id']}/accept")
    assert query("SELECT account_id FROM instances", one=True)["account_id"] == recipient["id"]
    # As in 1.4, the previous owner stays on as a full-access collaborator.
    row = query("SELECT role FROM instance_collaborators WHERE account_id = ?", (owner["id"],), one=True)
    assert row == {"role": "full_access"}
    assert stranger_client.get(f"/instances/{wiki['id']}").status_code == 404


def test_transfer_to_a_suspended_account_is_refused(web, make_account, make_wiki, login, query):
    owner, blocked = make_account(), make_account(suspended=1)
    wiki = make_wiki(owner, "nogift")
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/transfer", data={"username": blocked["username"]})
    assert query("SELECT COUNT(*) AS n FROM instance_ownership_transfers", one=True)["n"] == 0
