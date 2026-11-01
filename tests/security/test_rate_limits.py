"""One test per documented abuse limit (docs/security.md, docs/api.md), through HTTP.

Each test uses up a budget, checks that the next request is refused (and that
nothing happened), then checks the budget's key: another account, address or
network still gets through. Large limits are scaled down with monkeypatch and
the in-memory limiter's clock is stopped (``frozen_limiter_clock``), so the
tests check the limit, not the host's speed.

Already covered elsewhere in tests/security (not repeated here):

* sign-in, per account from all addresses: ``test_wauth.py::test_w1_1_*``;
* setup token guesses: ``test_wauth.py::test_w3_4_setup_token_guesses_are_limited``;
* global per-client limit counting requests CSRF refuses:
  ``test_audit_regressions.py::test_w1_2_requests_refused_by_csrf_count_against_the_global_limit``
  (here only its per-network key);
* page protections: ``test_wcontent.py::test_w2_11_one_editor_cannot_protect_every_page``;
* the daily image upload quota: ``test_wcontent.py::test_w4_3_canvas_image_uploads_count_against_the_daily_upload_quota``.
"""

from __future__ import annotations

import io

import pytest

from bananawiki.wiki.db import connection_scope
from tests.conftest import PASSWORD
from tests.features.api_support import call, enable_api, issue
from tests.features.chat_support import JSON, send, start_dm
from tests.features.pages_support import make_page

NEW_PASSWORD = "a brand new passphrase"


def _sign_in(client, username, password=PASSWORD, address="127.0.0.1"):
    return client.post("/login", data={"username": username, "password": password},
                       environ_base={"REMOTE_ADDR": address})


@pytest.fixture
def signin_limits(monkeypatch):
    from bananawiki.wiki.features.auth import signin

    def scale(**values):
        for name, value in values.items():
            monkeypatch.setattr(signin, name, value)
    return scale


# Sign-in


def test_signin_failures_per_address_and_ipv6_network(app, make_user, signin_limits):
    """20 failures per client network in 15 minutes (``login:ip``; IPv6: the /64)."""
    signin_limits(MAX_PER_IP=3)
    make_user("alice")
    client = app.test_client()
    for i in range(3):
        assert _sign_in(client, f"ghost{i}", "wrong", "10.0.0.1").status_code == 401
    assert _sign_in(client, "alice", PASSWORD, "10.0.0.1").status_code == 429, "any account, same address"
    assert _sign_in(app.test_client(), "alice", PASSWORD, "10.0.0.2").status_code == 302

    for i in range(3):
        assert _sign_in(client, f"ghost{i}", "wrong", f"2001:db8:1:1::{i + 1}").status_code == 401
    assert _sign_in(client, "alice", PASSWORD, "2001:db8:1:1::ffff").status_code == 429, "same /64"
    assert _sign_in(app.test_client(), "alice", PASSWORD, "2001:db8:1:2::1").status_code == 302


def test_signin_failures_per_account_from_one_address(app, make_user, signin_limits):
    """8 failures per account name from one address (``login:account-ip``)."""
    signin_limits(MAX_PER_ACCOUNT=3)
    make_user("victim")
    make_user("bystander")
    for _ in range(3):
        assert _sign_in(app.test_client(), "victim", "wrong", "10.0.0.1").status_code == 401
    assert _sign_in(app.test_client(), "victim", PASSWORD, "10.0.0.1").status_code == 429
    assert _sign_in(app.test_client(), "bystander", PASSWORD, "10.0.0.1").status_code == 302
    assert _sign_in(app.test_client(), "victim", PASSWORD, "10.0.0.2").status_code == 302


def test_signin_known_device_has_its_own_budget(app, make_user, signin_limits):
    """A browser with the account's device cookie is not locked out by strangers, and has a budget of 8 itself."""
    signin_limits(MAX_PER_ACCOUNT=2, MAX_PER_ACCOUNT_TOTAL=3)
    make_user("owner")
    browser = app.test_client()
    assert _sign_in(browser, "owner", PASSWORD, "10.1.0.1").status_code == 302
    browser.post("/logout")

    for i in range(3):  # strangers from many addresses use up the shared budget
        assert _sign_in(app.test_client(), "owner", "wrong", f"10.2.0.{i + 1}").status_code == 401
    assert _sign_in(app.test_client(), "owner", PASSWORD, "10.2.1.1").status_code == 429
    assert _sign_in(browser, "owner", PASSWORD, "10.1.0.2").status_code == 302, "the owner's browser still signs in"
    browser.post("/logout")

    for i in range(2):  # the device budget is a budget too, whatever the address
        assert _sign_in(browser, "owner", "wrong", f"10.3.0.{i + 1}").status_code == 401
    assert _sign_in(browser, "owner", PASSWORD, "10.3.1.1").status_code == 429


def test_two_step_codes_per_account_for_browsers_without_a_device_cookie(app, make_user, signin_limits):
    """10 wrong codes in 5 minutes; browsers that never signed in share one budget per account."""
    from bananawiki.core import totp
    from bananawiki.wiki.features.auth import two_step

    signin_limits(TWO_STEP_MAX=3)
    victim = make_user("victim")
    secret = totp.generate_secret()
    with app.test_request_context(), connection_scope():
        codes = two_step.enable(victim["id"], secret, 0)
    first = app.test_client()
    assert _sign_in(first, "victim").status_code == 302
    for _ in range(3):
        assert first.post("/login/two-step", data={"code": "000000"}).status_code == 401
    assert first.post("/login/two-step", data={"code": codes[0]}).status_code == 429

    second = app.test_client()  # a fresh browser does not get a fresh budget
    assert _sign_in(second, "victim", address="10.4.0.1").status_code == 302
    assert second.post("/login/two-step", data={"code": codes[0]}).status_code == 429
    assert second.get("/_probe/private").status_code == 302, "not signed in"


# Sign-up and invites


def test_signup_attempts_per_address(app, client, db, admin, monkeypatch):
    """10 sign-up attempts per client network an hour (``signup:ip``, in the database): invite guessing too."""
    from bananawiki.wiki.features.auth import routes_signup

    monkeypatch.setattr(routes_signup, "MAX_PER_IP", 3)
    db.execute("UPDATE site_settings SET bot_protection_enabled = 0")
    db.insert("invite_codes", {"code": "ABCD-1234", "created_by": admin["id"], "max_uses": 5, "use_count": 0})

    def signup(name, code, address):
        return client.post("/signup", data={"username": name, "password": PASSWORD, "confirm_password": PASSWORD,
                                            "invite_code": code}, environ_base={"REMOTE_ADDR": address})

    for i in range(3):
        assert signup(f"guess{i}", f"WXYZ-000{i}", "10.0.0.1").status_code == 400
    assert signup("newbie", "ABCD-1234", "10.0.0.1").status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie'") == 0
    assert signup("newbie", "ABCD-1234", "10.0.0.2").status_code == 302


def test_group_invite_code_guesses_per_account_and_address(client, login, make_user, db, monkeypatch):
    """Group joins by code: 10 per account and 30 per address in 15 minutes (database)."""
    from bananawiki.wiki.features.chat import routes_groups

    monkeypatch.setattr(routes_groups, "JOIN_MAX_PER_USER", 3)
    monkeypatch.setattr(routes_groups, "JOIN_MAX_PER_IP", 5)
    owner, guesser, other = make_user("gowner"), make_user("guesser"), make_user("other")
    login(client, owner)
    group = client.post("/groups/new", data={"name": "Team"}, headers=JSON).json["group_id"]
    code = db.scalar("SELECT invite_code FROM group_chats WHERE id = ?", (group,))
    client.post("/logout")

    def join(value):
        return client.post("/groups/join", data={"invite_code": value}, headers=JSON)

    login(client, guesser)
    for _ in range(3):
        assert join("wrong-code").status_code == 404
    assert join(code).status_code == 429
    client.post("/logout")
    login(client, other)  # same address: its own account budget, but the address has 2 left
    assert [join("wrong-code").status_code for _ in range(2)] == [404, 404]
    assert join(code).status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id IN (?, ?)",
                     (group, guesser["id"], other["id"])) == 0


# Passwords


def test_password_confirmation_budget_per_account(app, client, login, make_user, db, monkeypatch):
    """10 wrong current passwords per account in 15 minutes (``account:password``), then even the right one waits."""
    from bananawiki.wiki.features.users import service

    monkeypatch.setattr(service, "PASSWORD_CHECKS", 3)
    alice, bob = make_user("alice"), make_user("bob")
    stored = db.scalar("SELECT password FROM users WHERE id = ?", (alice["id"],))

    def change(browser, current):
        return browser.post("/settings/password", data={"current_password": current, "new_password": NEW_PASSWORD,
                                                        "confirm_password": NEW_PASSWORD})

    login(client, alice)
    for i in range(3):
        change(client, f"guess {i}")
    other_browser = app.test_client()
    login(other_browser, alice)
    change(other_browser, PASSWORD)
    assert db.scalar("SELECT password FROM users WHERE id = ?", (alice["id"],)) == stored, "refused everywhere"

    bob_browser = app.test_client()
    login(bob_browser, bob)
    change(bob_browser, PASSWORD)
    assert db.scalar("SELECT password FROM users WHERE id = ?", (bob["id"],)) != bob["password"]


# Reading and searching


def test_search_per_account(app, client, login, make_user, frozen_limiter_clock):
    """30 searches a minute per account (``pages:search``)."""
    login(client, make_user("searcher"))
    statuses = [client.get("/search?q=banana").status_code for _ in range(31)]
    assert statuses.count(200) == 30 and statuses[-1] == 429
    other = app.test_client()
    login(other, make_user("neighbour"))
    assert other.get("/search?q=banana").status_code == 200


def test_api_requests_per_account_whatever_the_token(app, client, db, make_user):
    """``api_service_rate_limit`` calls a minute per account: a second token does not add a budget."""
    enable_api(app, db)
    db.execute("UPDATE site_settings SET api_service_rate_limit = 3")
    reader = make_user("reader", api_access_enabled=1)
    first, second = issue(app, reader, name="one"), issue(app, reader, name="two")
    for _ in range(3):
        assert call(client, "GET", "/pages", first).status_code == 200
    limited = call(client, "GET", "/pages", second)
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    other = issue(app, make_user("other", api_access_enabled=1))
    assert call(client, "GET", "/pages", other).status_code == 200


def test_api_tokens_per_account(app, client, db, make_user):
    """``api_service_max_tokens_per_user`` active tokens per account: a token cannot mint past it."""
    enable_api(app, db)
    db.execute("UPDATE site_settings SET api_service_max_tokens_per_user = 2")
    reader = make_user("reader", api_access_enabled=1)
    parent = issue(app, reader, ["tokens", "pages"], name="parent")
    body = {"name": "child", "permissions": {"read": True, "scopes": ["pages"]}}
    assert call(client, "POST", "/tokens", parent, json=body).status_code == 201
    refused = call(client, "POST", "/tokens", parent, json=body)
    assert refused.status_code == 400 and refused.json["code"] == "token_limit"
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens WHERE user_id = ?", (reader["id"],)) == 2


# Uploads, chats, read aloud, exports


def test_attachment_uploads_per_account(app, client, login, make_user, db, frozen_limiter_clock):
    """20 attachment uploads a minute per account (``pages:attachment-upload``)."""
    page = make_page(app, "Guide", "text")
    login(client, make_user("uploader", role="editor"))
    for _ in range(20):
        assert client.post(f"/page/{page['slug']}/attachments", data={}).status_code == 302
    refused = client.post(f"/page/{page['slug']}/attachments", data={"files": (io.BytesIO(b"x"), "a.txt")},
                          content_type="multipart/form-data")
    assert refused.status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM page_attachments") == 0
    other = app.test_client()
    login(other, make_user("colleague", role="editor"))
    other.post(f"/page/{page['slug']}/attachments", data={"files": (io.BytesIO(b"x"), "a.txt")},
               content_type="multipart/form-data")
    assert db.scalar("SELECT COUNT(*) FROM page_attachments") == 1


def test_chat_sends_per_account(app, client, login, make_user, db, monkeypatch, frozen_limiter_clock):
    """30 messages a minute per account (``chat:send``)."""
    from bananawiki.wiki.features.chat import routes_dm

    monkeypatch.setattr(routes_dm, "SEND_PER_MINUTE", 3)
    alice, bob = make_user("alice"), make_user("bob")
    login(client, alice)
    chat = start_dm(client, "bob")
    assert [send(client, f"/chats/{chat}/send", f"m{i}").status_code for i in range(3)] == [200] * 3
    assert send(client, f"/chats/{chat}/send", "one too many").status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE content = 'one too many'") == 0
    other = app.test_client()
    login(other, bob)
    assert send(other, f"/chats/{chat}/send", "reply").status_code == 200


def test_read_aloud_requests_per_account(app, client, login, make_user, frozen_limiter_clock):
    """10 read-aloud requests a minute per account (``tts:generate``)."""
    page = make_page(app, "Guide", "This is a page about bananas and it has enough words to be read.")
    login(client, make_user("listener"))
    statuses = [client.post(f"/page/{page['slug']}/tts/generate", json={}).status_code for _ in range(11)]
    assert statuses[0] == 202 and set(statuses[1:10]) == {409}
    assert statuses[10] == 429
    other = app.test_client()
    login(other, make_user("neighbour"))
    assert other.post(f"/page/{page['slug']}/tts/generate", json={}).status_code == 409


def test_data_exports_per_requester(app, client, login, make_user, admin, monkeypatch, frozen_limiter_clock):
    """5 personal data exports an hour per requesting account (``export:<requester>``)."""
    from bananawiki.wiki.features.user_data_export import routes

    monkeypatch.setattr(routes, "EXPORTS_PER_HOUR", 2)
    bob = make_user("bob")
    login(client, bob)
    for _ in range(2):
        response = client.get("/settings/export")
        assert response.status_code == 200
        response.get_data()
    assert client.get("/settings/export").status_code == 429
    admin_browser = app.test_client()
    login(admin_browser, admin)
    response = admin_browser.get(f"/admin/users/{bob['id']}/export")
    assert response.status_code == 200, "counted on the requester, not the account exported"
    response.get_data()


def test_pdf_exports_per_account(app, client, login, make_user, frozen_limiter_clock):
    """5 PDF exports a minute per account (``pages:export-pdf``), counted before any work."""
    page = make_page(app, "Guide", "text")
    login(client, make_user("reader"))
    statuses = [client.get(f"/page/{page['slug']}/export-pdf").status_code for _ in range(6)]
    assert 429 not in statuses[:5] and statuses[5] == 429
    other = app.test_client()
    login(other, make_user("neighbour"))
    assert other.get(f"/page/{page['slug']}/export-pdf").status_code != 429


# Global


def test_global_limit_is_per_client_network(app, monkeypatch, frozen_limiter_clock):
    """300 requests a minute per client network and worker (IPv6: the /64)."""
    import bananawiki.wiki.app as wiki_app

    monkeypatch.setattr(wiki_app, "GLOBAL_RATE_LIMIT", 5)
    client = app.test_client()

    def get(address):
        return client.get("/login", environ_base={"REMOTE_ADDR": address}).status_code

    assert [get(f"2001:db8:5:5::{i}") for i in range(1, 6)] == [200] * 5
    assert get("2001:db8:5:5::ff") == 429
    assert get("2001:db8:5:6::1") == 200
    assert get("10.0.0.1") == 200
