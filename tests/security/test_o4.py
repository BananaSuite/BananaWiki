"""Regression tests for the o4 findings of the full re-audit pass 9 (hosting portal requests and sign-ins)."""

from __future__ import annotations

from bananawiki.hosting import collaborators as collab
from bananawiki.hosting import features, merges
from bananawiki.hosting.routes import auth as auth_routes
from tests.hosting.conftest import PASSWORD, login, make_account, make_wiki, portal, query, web  # noqa: F401

REASON = "r" * 2000


def _net(n: int) -> dict[str, str]:
    """A client network of its own per cycle, so the per-network route limits never answer first."""
    return {"REMOTE_ADDR": f"203.0.113.{n + 1}"}


# OW4-1: feature requests, merge requests and transfer offers are bounded per day; the history is paged


def test_ow4_1_feature_requests_are_bounded_per_day(portal, make_account, make_wiki, login, query):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner)
    client = portal.test_client()
    login(client, owner)
    url = f"/instances/{wiki['id']}/features/public_access/requests"
    for n in range(features.MAX_REQUESTS_PER_DAY):
        assert client.post(url, data={"reason": REASON}, environ_base=_net(n)).status_code == 302
        row = query("SELECT id FROM instance_feature_requests WHERE instance_id = ? AND status = 'pending'",
                    (wiki["id"],), one=True)
        assert row, n
        client.post(f"/instances/{wiki['id']}/feature-requests/{row['id']}/cancel", environ_base=_net(n))
    client.post(url, data={"reason": REASON}, environ_base=_net(50))
    rows = query("SELECT COUNT(*) AS n FROM instance_feature_requests WHERE instance_id = ?", (wiki["id"],), one=True)
    assert rows["n"] == features.MAX_REQUESTS_PER_DAY
    # Requests older than a day no longer count.
    query("UPDATE instance_feature_requests SET requested_at = '2000-01-01 00:00:00' WHERE instance_id = ?",
          (wiki["id"],))
    assert client.post(url, data={"reason": REASON}, environ_base=_net(51)).status_code == 302
    assert query("SELECT 1 FROM instance_feature_requests WHERE instance_id = ? AND status = 'pending'",
                 (wiki["id"],), one=True)


def test_ow4_1_feature_history_is_paged_on_the_wiki_pages(portal, make_account, make_wiki, login, query):  # noqa: F811
    owner = make_account()
    admin = make_account(admin=True)
    wiki = make_wiki(owner)
    for n in range(features.HISTORY_LIMIT + 30):
        query("INSERT INTO instance_feature_requests (instance_id, feature, requested_by, reason, status, requested_at) "
              "VALUES (?, 'public_access', ?, ?, 'cancelled', ?)",
              (wiki["id"], owner["id"], f"old-reason-{n:03d}-" + "x" * 30, f"2001-01-01 00:{n // 60:02d}:{n % 60:02d}"))
    # The oldest request is still waiting: it stays listed although it is not among the latest.
    query("INSERT INTO instance_feature_requests (instance_id, feature, requested_by, reason, status, requested_at) "
          "VALUES (?, 'page_builder', ?, 'still-waiting-reason-xxxxxxxx', 'pending', '2000-01-01 00:00:00')",
          (wiki["id"], owner["id"]))
    browser = portal.test_client()
    login(browser, admin)
    page = browser.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert page.count("old-reason-") == features.HISTORY_LIMIT
    assert f"old-reason-{features.HISTORY_LIMIT + 29:03d}-" in page and "old-reason-000-" not in page
    assert "still-waiting-reason" in page
    mine = portal.test_client()
    login(mine, owner)
    page = mine.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert page.count("/feature-requests/") == 1  # the waiting request's Cancel button is still there


def test_ow4_1_merge_requests_are_bounded_per_day(portal, make_account, login, query):  # noqa: F811
    me, other = make_account(), make_account()
    client = portal.test_client()
    login(client, me)
    form = {"target_username": other["username"], "reason": "m" * 4000}
    for n in range(merges.MAX_REQUESTS_PER_DAY):
        assert client.post("/account/merge-request", data=form, environ_base=_net(n)).status_code == 302
        row = query("SELECT id FROM hosting_account_merge_requests WHERE status = 'pending'", one=True)
        assert client.post(f"/account/merge/cancel/{row['id']}", environ_base=_net(n)).status_code == 302
    refused = client.post("/account/merge-request", data=form, environ_base=_net(50))
    assert refused.status_code == 400
    rows = query("SELECT COUNT(*) AS n FROM hosting_account_merge_requests", one=True)
    assert rows["n"] == merges.MAX_REQUESTS_PER_DAY


def test_ow4_1_transfer_offers_are_bounded_per_day(portal, make_account, make_wiki, login, query):  # noqa: F811
    owner, other = make_account(), make_account()
    wiki = make_wiki(owner)
    client = portal.test_client()
    login(client, owner)
    for n in range(collab.MAX_TRANSFERS_PER_DAY + 3):
        client.post(f"/instances/{wiki['id']}/transfer", data={"username": other["username"]}, environ_base=_net(n))
    rows = query("SELECT COUNT(*) AS n FROM instance_ownership_transfers WHERE instance_id = ?", (wiki["id"],), one=True)
    assert rows["n"] == collab.MAX_TRANSFERS_PER_DAY
    # The last offer made stays open.
    assert query("SELECT 1 FROM instance_ownership_transfers WHERE instance_id = ? AND status = 'pending'",
                 (wiki["id"],), one=True)


# OW4-2: successful sign-ins have budgets per account and per network


def _sign_in(portal, username: str, address: str, password: str = PASSWORD) -> int:  # noqa: F811
    return portal.test_client().post("/login", data={"username": username, "password": password},
                                     environ_base={"REMOTE_ADDR": address}).status_code


def test_ow4_2_successful_sign_ins_are_bounded_per_account(portal, make_account, query, monkeypatch):  # noqa: F811
    monkeypatch.setattr(auth_routes, "LOGIN_MAX_SIGN_INS_PER_ACCOUNT", 3)
    bob = make_account()
    pending = make_account(approval_status="pending")
    assert [_sign_in(portal, bob["username"], f"198.51.100.{n}") for n in range(4)] == [302, 302, 302, 429]
    assert [_sign_in(portal, pending["username"], "198.51.100.50") for _n in range(4)] == [302, 302, 302, 429]
    sessions = query("SELECT COUNT(*) AS n FROM hosting_account_sessions WHERE account_id IN (?, ?)",
                     (bob["id"], pending["id"]), one=True)
    assert sessions["n"] == 6


def test_ow4_2_successful_sign_ins_are_bounded_per_network(portal, make_account, monkeypatch):  # noqa: F811
    monkeypatch.setattr(auth_routes, "LOGIN_MAX_SIGN_INS_PER_IP", 3)
    names = [make_account()["username"] for _n in range(4)]
    assert [_sign_in(portal, name, "198.51.100.7") for name in names] == [302, 302, 302, 429]
    assert _sign_in(portal, names[3], "198.51.100.8") == 302  # another network is not affected


def test_ow4_2_failed_sign_ins_do_not_use_up_the_success_budget(portal, make_account, monkeypatch):  # noqa: F811
    monkeypatch.setattr(auth_routes, "LOGIN_MAX_SIGN_INS_PER_ACCOUNT", 2)
    bob = make_account()
    for n in range(5):
        assert _sign_in(portal, bob["username"], f"198.51.100.{n + 20}", "wrong-password-1") == 401
    assert [_sign_in(portal, bob["username"], "198.51.100.30") for _n in range(2)] == [302, 302]
