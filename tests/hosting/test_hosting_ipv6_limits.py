"""The portal's per-address limits count an IPv6 client as its whole /64, as the wiki does.

One IPv6 subscriber is usually given a whole /64: a limit keyed on the full
address would give them as many budgets as they have addresses.
"""

from __future__ import annotations

from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, build_portal, portal_environ

NETWORK = "2001:db8:5:6"


def _address(number: int, network: str = NETWORK) -> dict[str, str]:
    return {"REMOTE_ADDR": f"{network}::{number:x}"}


def test_the_global_request_limit_counts_an_ipv6_network_once(portal, monkeypatch):
    from bananawiki.hosting import app as hosting_app

    monkeypatch.setattr(hosting_app, "GLOBAL_RATE_LIMIT", 3)
    client = portal.test_client()
    assert [client.get("/login", environ_base=_address(number)).status_code for number in range(1, 4)] == [200] * 3
    assert client.get("/login", environ_base=_address(99)).status_code == 429
    assert client.get("/login", environ_base=_address(1, "2001:db8:5:7")).status_code == 200, "another /64"


def test_sign_in_failures_count_an_ipv6_network_once(portal, web):
    from bananawiki.hosting.routes.auth import LOGIN_MAX_PER_IP

    for number in range(1, LOGIN_MAX_PER_IP + 1):
        assert web.post("/login", data={"username": f"nobody{number}", "password": "wrong"},
                        environ_base=_address(number)).status_code == 401
    assert web.post("/login", data={"username": "nobody-else", "password": "wrong"},
                    environ_base=_address(0xFFFF)).status_code == 429
    assert web.post("/login", data={"username": "nobody-else", "password": "wrong"},
                    environ_base=_address(1, "2001:db8:5:7")).status_code == 401, "another /64 has its own budget"


def test_failures_for_one_account_count_an_ipv6_network_once(portal, web, make_account):
    from bananawiki.hosting.routes.auth import LOGIN_MAX_PER_ACCOUNT

    account = make_account()
    for number in range(1, LOGIN_MAX_PER_ACCOUNT + 1):
        assert web.post("/login", data={"username": account["username"], "password": "wrong"},
                        environ_base=_address(number)).status_code == 401
    assert web.post("/login", data={"username": account["username"], "password": PASSWORD},
                    environ_base=_address(0xFFFF)).status_code == 429
    assert web.post("/login", data={"username": account["username"], "password": PASSWORD},
                    environ_base=_address(1, "2001:db8:5:7")).status_code == 302


def test_a_successful_sign_in_clears_its_own_network_budget(portal, web, make_account, query):
    account = make_account()
    for number in range(1, 4):
        web.post("/login", data={"username": account["username"], "password": "wrong"}, environ_base=_address(number))
    assert web.post("/login", data={"username": account["username"], "password": PASSWORD},
                    environ_base=_address(77)).status_code == 302
    left = query("SELECT COUNT(*) AS n FROM hosting_rate_limit_hits WHERE bucket = 'login-account-ip'", one=True)
    assert left["n"] == 0


def test_view_limits_count_an_ipv6_network_once(portal):
    client = portal.test_client()
    statuses = [client.post("/forgot-username", data={"email": "someone@example.org"},
                            environ_base=_address(number)).status_code for number in range(1, 7)]
    assert statuses == [200] * 5 + [429]


def test_daily_sign_ups_count_an_ipv6_network_once(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_SIGNUPS_PER_IP_PER_DAY="2"))
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import accounts

        accounts.create("root", PASSWORD, is_admin=True)

    def sign_up(username: str, address: dict[str, str]):
        return portal.test_client().post("/signup", data={"username": username, "password": PASSWORD,
                                                          "confirm_password": PASSWORD, "accept_terms": "1"},
                                         environ_base=address)

    assert sign_up("first", _address(1)).status_code == 302
    assert sign_up("second", _address(2)).status_code == 302
    assert sign_up("third", _address(3)).status_code == 429
    assert sign_up("elsewhere", _address(3, "2001:db8:5:7")).status_code == 302
