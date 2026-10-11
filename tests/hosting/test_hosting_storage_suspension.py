"""The automatic storage protection suspends a wiki and lifts that suspension by itself.

A wiki that uses more than 110% of its storage limit is suspended by the
maintenance service and its owner is told. When it fits within its limit
again (usually because an administrator raised the limit, as the terms
promise, so that the owner can free space), maintenance reopens it and tells
the owner. Only a suspension that is the storage protection and nothing else
is lifted. An administrator who reopens a wiki that is still over its limit
is warned that maintenance would suspend it again.
"""

from __future__ import annotations

import pytest

from bananawiki.hosting import email, instances, maintenance
from bananawiki.hosting.db import connection_scope

MB = 1024 * 1024


def _scope(portal):
    return connection_scope(portal.extensions["bananawiki.hosting.database"])


@pytest.fixture
def wiki(portal, make_account, make_wiki, runtime, query):
    owner = make_account("storageowner", email="owner@example.org")
    inst = make_wiki(owner, "full")
    query("UPDATE instances SET storage_limit_mb = 100 WHERE id = ?", (inst["id"],))
    return inst


def _use(runtime, inst, megabytes: float) -> None:
    runtime.tenants[inst["subdomain"]].usage = int(megabytes * MB)


def _pass(portal) -> tuple[dict, list]:
    with email.capture_outbox() as outbox:
        results = maintenance.run_once(portal)
    return results, outbox


def _status(query, inst) -> str:
    return query("SELECT status FROM instances WHERE id = ?", (inst["id"],), one=True)["status"]


def test_an_overrun_suspends_the_wiki_and_tells_its_owner(portal, runtime, wiki, query):
    _use(runtime, wiki, 105)
    assert _pass(portal)[1] == [] and _status(query, wiki) == "running", "within the 10% margin"
    _use(runtime, wiki, 120)
    results, outbox = _pass(portal)
    assert results["storage quotas"] == 1 and _status(query, wiki) == "suspended"
    assert [message.to for message in outbox] == ["owner@example.org"]
    assert "full" in outbox[0].subject and "120.0 MB" in outbox[0].text and "100 MB" in outbox[0].text


def test_the_suspension_is_lifted_once_the_wiki_fits_again(portal, runtime, wiki, query):
    _use(runtime, wiki, 120)
    _pass(portal)
    _use(runtime, wiki, 104)
    results, outbox = _pass(portal)
    assert results["storage suspensions"] == 0 and _status(query, wiki) == "suspended", \
        "only within the limit itself, so that it does not flap at the margin"
    assert outbox == []
    _use(runtime, wiki, 90)
    results, outbox = _pass(portal)
    assert results["storage suspensions"] == 1 and _status(query, wiki) == "running"
    assert [message.to for message in outbox] == ["owner@example.org"]
    assert "full" in outbox[0].subject and "90.0 MB" in outbox[0].text
    row = query("SELECT * FROM instances WHERE id = ?", (wiki["id"],), one=True)
    assert not row["suspended_at"] and not row["suspend_reason"]
    history = query("SELECT action, performed_by FROM instance_suspension_audit WHERE instance_id = ? ORDER BY id",
                    (wiki["id"],))
    assert [(item["action"], item["performed_by"]) for item in history] == [("suspend", None), ("unsuspend", None)]


def test_raising_the_limit_reopens_the_wiki_at_once(portal, runtime, wiki, make_account, login, query):
    _use(runtime, wiki, 120)
    _pass(portal)
    admin = make_account(admin=True)
    client = portal.test_client()
    login(client, admin)
    with email.capture_outbox() as outbox:
        response = client.post(f"/admin/instances/{wiki['id']}/storage-limit", data={"storage_limit_mb": "200"},
                               follow_redirects=True)
    assert _status(query, wiki) == "running"
    assert "reopened" in response.get_data(as_text=True)
    assert [message.to for message in outbox] == ["owner@example.org"]
    unsuspend = query("SELECT performed_by FROM instance_suspension_audit WHERE instance_id = ? AND action = "
                      "'unsuspend'", (wiki["id"],), one=True)
    assert unsuspend["performed_by"] == admin["id"]


def test_an_administrators_suspension_is_never_lifted_automatically(portal, runtime, wiki, query):
    with portal.test_request_context("/"), _scope(portal):
        instances.suspend(instances.get(wiki["id"]), actor_id=None, reason="Abuse report", duration_label="permanent")
    _use(runtime, wiki, 1)
    assert _pass(portal)[0]["storage suspensions"] == 0 and _status(query, wiki) == "suspended"


def test_a_storage_suspension_overridden_by_another_one_stays(portal, runtime, wiki, make_account, query):
    admin = make_account(admin=True)
    _use(runtime, wiki, 120)
    _pass(portal)
    with portal.test_request_context("/"), _scope(portal):
        instances.suspend(instances.get(wiki["id"]), actor_id=admin["id"], reason="Account review",
                          duration_label="account")
    _use(runtime, wiki, 1)
    assert _pass(portal)[0]["storage suspensions"] == 0 and _status(query, wiki) == "suspended"


def test_nothing_reopens_while_the_owners_account_is_suspended(portal, runtime, wiki, query):
    _use(runtime, wiki, 120)
    _pass(portal)
    query("UPDATE accounts SET suspended = 1, suspended_at = datetime('now') WHERE username = 'storageowner'")
    _use(runtime, wiki, 1)
    assert _pass(portal)[0]["storage suspensions"] == 0 and _status(query, wiki) == "suspended"


def test_a_storage_suspension_from_1_4_is_lifted_too(portal, runtime, wiki, query):
    # 1.4 recorded "system:storage-quota" as the actor, which the takeover cleared, and no duration label.
    query("UPDATE instances SET status = 'suspended', suspended_at = datetime('now'), suspend_reason = ?, "
          "suspend_reason_visible = 1 WHERE id = ?",
          ("Automatic storage protection: 130.0 MB used, 100 MB allowed. Contact the platform administrator.",
           wiki["id"]))
    query("INSERT INTO instance_suspension_audit (instance_id, action, reason, reason_visible, performed_by, "
          "created_at) VALUES (?, 'suspend', 'Automatic storage protection: 130.0 MB used, 100 MB allowed. "
          "Contact the platform administrator.', 1, NULL, datetime('now'))", (wiki["id"],))
    _use(runtime, wiki, 50)
    assert _pass(portal)[0]["storage suspensions"] == 1 and _status(query, wiki) == "running"


def test_an_unmeasurable_wiki_stays_suspended(portal, runtime, wiki, query, monkeypatch):
    _use(runtime, wiki, 120)
    _pass(portal)
    from bananawiki.hosting.runtime import RuntimeFailure

    def broken(_spec):
        raise RuntimeFailure("failed", "simulated")

    monkeypatch.setattr(runtime, "usage", broken)
    assert _pass(portal)[0]["storage suspensions"] == 0 and _status(query, wiki) == "suspended"


def test_reopening_a_wiki_still_over_its_limit_warns_the_administrator(portal, runtime, wiki, make_account, login,
                                                                      query):
    _use(runtime, wiki, 150)
    _pass(portal)
    admin = make_account(admin=True)
    client = portal.test_client()
    login(client, admin)
    page = client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert "reopens it by itself" in page
    response = client.post(f"/admin/instances/{wiki['id']}/unsuspend", follow_redirects=True)
    text = response.get_data(as_text=True)
    assert _status(query, wiki) == "running"
    assert "150.0 MB" in text and "100 MB" in text and "alert--warning" in text
    client.post("/language", data={"language": "it"})
    _pass(portal)
    italian = client.post(f"/admin/instances/{wiki['id']}/unsuspend", follow_redirects=True).get_data(as_text=True)
    assert "150.0 MB" in italian and "manutenzione" in italian


def test_reopening_a_wiki_within_its_limit_does_not_warn(portal, runtime, wiki, make_account, login, query):
    with portal.test_request_context("/"), _scope(portal):
        instances.suspend(instances.get(wiki["id"]), actor_id=None, reason="Check", duration_label="permanent")
    _use(runtime, wiki, 10)
    admin = make_account(admin=True)
    client = portal.test_client()
    login(client, admin)
    text = client.post(f"/admin/instances/{wiki['id']}/unsuspend", follow_redirects=True).get_data(as_text=True)
    assert _status(query, wiki) == "running" and "alert--warning" not in text


def test_the_owner_sees_how_a_storage_suspension_ends(portal, runtime, wiki, login, query):
    _use(runtime, wiki, 120)
    _pass(portal)
    client = portal.test_client()
    owner = query("SELECT * FROM accounts WHERE username = 'storageowner'", one=True)
    login(client, owner)
    page = client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "reopens by itself" in page
