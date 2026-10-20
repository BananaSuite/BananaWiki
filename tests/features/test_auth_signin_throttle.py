"""Sign-in throttling: known-device budgets, IPv6 grouping and hash upgrades."""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.core import passwords
from bananawiki.wiki.features.auth import signin

DEVICE_COOKIE = "bw_session_device"


@pytest.mark.parametrize(("address", "expected"), [
    ("203.0.113.9", "203.0.113.9"),
    ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
    ("2001:DB8:1:2::1", "2001:db8:1:2::/64"),
    ("::ffff:203.0.113.9", "203.0.113.9"),
    ("unknown", "unknown"),
])
def test_rate_limit_keys_group_ipv6_clients_by_64(address, expected):
    from bananawiki.core.web import network_key

    assert network_key(address) == expected


def test_strangers_cannot_lock_an_account_out_of_a_browser_that_signed_in_before(app, make_user, monkeypatch):
    monkeypatch.setattr(signin, "MAX_PER_ACCOUNT_TOTAL", 4)
    user = make_user("bob")
    owner_address = {"REMOTE_ADDR": "203.0.113.50"}
    owner = app.test_client()
    assert owner.post("/login", data={"username": "bob", "password": PASSWORD},
                      environ_base=owner_address).status_code == 302
    first_device = owner.get_cookie(DEVICE_COOKIE).value
    owner.post("/logout", environ_base=owner_address)
    for address in range(1, 6):
        app.test_client().post("/login", data={"username": "bob", "password": "wrong password"},
                               environ_base={"REMOTE_ADDR": f"198.51.100.{address}"})
    stranger = app.test_client().post("/login", data={"username": "bob", "password": PASSWORD},
                                      environ_base={"REMOTE_ADDR": "198.51.100.99"})
    assert stranger.status_code == 429
    assert owner.post("/login", data={"username": "bob", "password": "wrong password"},
                      environ_base=owner_address).status_code == 401, "the owner's own typos have their own budget"
    assert owner.post("/login", data={"username": user["username"], "password": PASSWORD},
                      environ_base=owner_address).status_code == 302
    assert owner.get_cookie(DEVICE_COOKIE).value != first_device, "every sign-in issues a fresh device cookie"


def test_device_cookie_is_bound_to_the_account_and_its_password(app, db, make_user, monkeypatch):
    monkeypatch.setattr(signin, "MAX_PER_ACCOUNT_TOTAL", 2)
    make_user("bob")
    make_user("carol")
    browser = app.test_client()
    assert browser.post("/login", data={"username": "carol", "password": PASSWORD}).status_code == 302
    browser.post("/logout")
    for address in range(1, 4):
        app.test_client().post("/login", data={"username": "bob", "password": "wrong password"},
                               environ_base={"REMOTE_ADDR": f"198.51.100.{address}"})
    # carol's device cookie does not exempt this browser from bob's shared budget.
    assert browser.post("/login", data={"username": "bob", "password": PASSWORD},
                        environ_base={"REMOTE_ADDR": "203.0.113.7"}).status_code == 429
    # A password change retires carol's device cookie.
    db.execute("UPDATE users SET password = ? WHERE username = 'carol'",
               (passwords.hash_password("another password", "pbkdf2:sha256:1000"),))
    for address in range(4, 7):
        app.test_client().post("/login", data={"username": "carol", "password": "wrong password"},
                               environ_base={"REMOTE_ADDR": f"198.51.100.{address}"})
    assert browser.post("/login", data={"username": "carol", "password": "another password"},
                        environ_base={"REMOTE_ADDR": "203.0.113.7"}).status_code == 429


def test_one_ipv6_64_shares_the_per_source_sign_in_budget(app, make_user, monkeypatch):
    monkeypatch.setattr(signin, "MAX_PER_IP", 3)
    for name in ("alice", "bobby", "carol", "david"):
        make_user(name)
    statuses = [
        app.test_client().post("/login", data={"username": name, "password": "wrong password"},
                               environ_base={"REMOTE_ADDR": f"2001:db8:1:2::{index + 1:x}"}).status_code
        for index, name in enumerate(("alice", "bobby", "carol", "david"))
    ]
    assert statuses == [401, 401, 401, 429]
    other_network = app.test_client().post("/login", data={"username": "david", "password": PASSWORD},
                                           environ_base={"REMOTE_ADDR": "2001:db8:1:3::1"})
    assert other_network.status_code == 302


def test_legacy_hash_is_upgraded_to_the_configured_method_on_sign_in(client, db, make_user):
    user = make_user("bob")
    legacy = passwords.hash_password(PASSWORD, "pbkdf2:sha256:600")
    db.execute("UPDATE users SET password = ? WHERE id = ?", (legacy, user["id"]))
    assert client.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 302
    stored = db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],))
    assert stored != legacy and stored.startswith("pbkdf2:sha256:1000$")
    assert passwords.verify_password(stored, PASSWORD)
    assert client.get("/_probe/private").data == b"private", "the session survives the upgrade"


def test_unknown_accounts_burn_the_configured_hash_method(app, monkeypatch):
    seen: list[str] = []
    original = passwords.verify_password

    def spy(stored, password):
        seen.append((stored or "").split("$", 1)[0])
        return original(stored, password)

    monkeypatch.setattr(passwords, "verify_password", spy)
    app.test_client().post("/login", data={"username": "nobody-here", "password": "wrong password"})
    assert seen == ["pbkdf2:sha256:1000"]


def test_needs_rehash():
    stored = passwords.hash_password("secret password", "pbkdf2:sha256:1000")
    assert not passwords.needs_rehash(stored, "pbkdf2:sha256:1000")
    assert passwords.needs_rehash(stored, "pbkdf2:sha256:2000")
    assert not passwords.needs_rehash("", "pbkdf2:sha256:1000")


def test_remember_me_session_outlives_the_ordinary_session_length(client, make_user, login, monkeypatch):
    """The signed cookie must stay valid as long as the 30-day "remember me" row does."""
    import time

    import itsdangerous.timed

    login(client, make_user("bob"), remember=True)
    assert client.get("/_probe/private").data == b"private"
    real = time.time
    monkeypatch.setattr(itsdangerous.timed, "time", type("Clock", (), {"time": staticmethod(lambda: real() + 8 * 86400)}))
    assert client.get("/_probe/private").data == b"private"


def test_device_cookie_is_host_prefixed_and_secure_over_https(client, make_user):
    make_user("bob")
    response = client.post("/login", data={"username": "bob", "password": PASSWORD}, base_url="https://localhost")
    header = next(h for h in response.headers.getlist("Set-Cookie") if h.startswith("__Host-" + DEVICE_COOKIE + "="))
    assert "Secure" in header and "HttpOnly" in header and "Path=/" in header and "Domain" not in header
