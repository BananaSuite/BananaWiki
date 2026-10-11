"""Throwaway sign-ups cannot fill the platform, and the per-account wiki limit
holds for received and suspended wikis too."""

from __future__ import annotations

import pytest

from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.errors import ServiceError

from .hosting_support import PASSWORD, build_portal, portal_environ


def _settings(query, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    query(f"UPDATE hosting_settings SET {assignments} WHERE id = 1", tuple(values.values()))


def _signup(portal, username: str, address: str = "10.1.0.1"):
    client = portal.test_client()
    client.environ_base["REMOTE_ADDR"] = address
    response = client.post("/signup", data={"username": username, "password": PASSWORD,
                                            "confirm_password": PASSWORD, "accept_terms": "1"})
    return client, response


def _account(query, username: str) -> dict:
    return query("SELECT * FROM accounts WHERE username = ?", (username,), one=True)


def _create(portal, account: dict, slug: str):
    from bananawiki.hosting import instances

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        return instances.create(account, slug)[0]


@pytest.fixture
def platform(portal, make_account):
    make_account("root", admin=True)  # sign-ups after the first one are customers
    return portal


def test_a_new_self_service_account_gets_one_wiki_in_its_first_days(platform, query):
    _signup(platform, "newcomer")
    newcomer = _account(query, "newcomer")
    _create(platform, newcomer, "first")
    with pytest.raises(ServiceError) as caught:
        _create(platform, newcomer, "second")
    assert caught.value.key == "hosting.instances.new_account_limit"
    query("UPDATE accounts SET created_at = datetime('now', '-8 days') WHERE username = 'newcomer'")
    assert _create(platform, _account(query, "newcomer"), "second")["status"] == "running"


def test_an_account_an_administrator_approved_uses_the_normal_limit(platform, query):
    _signup(platform, "vetted")
    root = _account(query, "root")
    query("UPDATE accounts SET approved_by = ? WHERE username = 'vetted'", (root["id"],))
    vetted = _account(query, "vetted")
    _create(platform, vetted, "one")
    assert _create(platform, vetted, "two")["status"] == "running"


def test_new_accounts_can_fill_only_their_share_of_the_platform(platform, query, make_account):
    _settings(query, global_limit_max_instances=4)
    for number in range(2):
        _signup(platform, f"burst{number}", address=f"10.1.1.{number}")
        _create(platform, _account(query, f"burst{number}"), f"burst-wiki-{number}")
    _signup(platform, "burst9", address="10.1.1.9")
    with pytest.raises(ServiceError) as caught:
        _create(platform, _account(query, "burst9"), "burst-wiki-9")
    assert caught.value.key == "hosting.instances.new_accounts_full"
    established = make_account()
    assert _create(platform, established, "regular")["status"] == "running"


def test_sign_ups_from_one_address_are_capped_per_day(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_SIGNUPS_PER_IP_PER_DAY="2"))
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import accounts

        accounts.create("root", PASSWORD, is_admin=True)
    assert _signup(portal, "first")[1].status_code == 302
    assert _signup(portal, "second")[1].status_code == 302
    _client, refused = _signup(portal, "third")
    assert refused.status_code == 429
    assert "Too many accounts" in refused.get_data(as_text=True)
    assert _signup(portal, "elsewhere", address="10.9.9.9")[1].status_code == 302
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as session:
        assert session.scalar("SELECT COUNT(*) FROM accounts WHERE username = 'third'") == 0


def test_accepting_a_transfer_respects_the_recipients_wiki_limit(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, MAX_INSTANCES_PER_ACCOUNT="1"))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        from bananawiki.hosting import accounts, collaborators, instances

        giver, receiver = accounts.create("giver", PASSWORD), accounts.create("receiver", PASSWORD)
        gift = instances.create(giver, "gift")[0]
        instances.create(receiver, "own")
        collaborators.start_transfer(gift, giver, "receiver")
        transfer = db.one("SELECT id FROM instance_ownership_transfers")
        with pytest.raises(ServiceError) as caught:
            collaborators.accept_transfer(transfer["id"], accounts.get(receiver["id"]))
        assert caught.value.key == "hosting.instances.account_limit"
        assert instances.get(gift["id"])["account_id"] == giver["id"]
        assert db.scalar("SELECT status FROM instance_ownership_transfers") == "pending"


def test_suspended_wikis_count_toward_the_account_limit(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, MAX_INSTANCES_PER_ACCOUNT="1"))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        from bananawiki.hosting import accounts, instances

        owner = accounts.create("owner", PASSWORD)
        instances.create(owner, "held")
        db.execute("UPDATE instances SET status = 'suspended', suspended_at = datetime('now')")
        with pytest.raises(ServiceError) as caught:
            instances.create(owner, "another")
        assert caught.value.key == "hosting.instances.account_limit"
