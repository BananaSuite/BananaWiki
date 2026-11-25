"""The wiki side of the BananaSuite sign-in round 3 (SIGNIN-SPEC.md, section E): confirming with BananaWiki
Hosting instead of the password (C1), no wiki two-step code after a hosting sign-in (C2), one Sign-in methods
section (C3), connecting from the settings and on the first sign-in (C4), and hosting administrators acting
as a member (A2)."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import PASSWORD

from bananawiki.wiki.features.auth import platform_oauth

from .test_auth_oauth import OAUTH_ENV, FakePortal, _callback, _start
from .test_auth_two_step import _next_code, enrol, signed_in

NEW_PASSWORD = "a brand new passphrase 77"


@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda url: False)
    return fake


@pytest.fixture
def oauth_app(app_factory):
    return app_factory(environ=OAUTH_ENV)


@pytest.fixture
def oauth_client(oauth_app):
    return oauth_app.test_client()


@pytest.fixture
def oauth_db(oauth_app):
    from bananawiki.core.sqlite import Session

    session = Session(oauth_app.extensions["bananawiki.database"].connect())
    yield session
    session.conn.close()


def _link(oauth_db, portal, user, account_id: str = "acct-1", name: str = "portaluser") -> None:
    oauth_db.execute("INSERT INTO platform_oauth_links (account_id, user_id, portal_username, linked_at) "
                     "VALUES (?, ?, ?, '2026-01-01 00:00:00')", (account_id, user["id"], name))
    portal.links[user["id"]] = account_id


def _hosting_sign_in(client):
    response = _callback(client, _start(client))
    assert response.status_code == 302
    return response


def _acting(portal, *, seconds: int = 900, admin: str = "chief-admin") -> None:
    portal.profile.update(act={"sub": "admin-1", "preferred_username": admin},
                          act_expires_at=int(time.time()) + seconds)


def _confirm(client, portal, *, auth_time: int | None, back: str = "/settings/security"):
    """Start "Confirm with BananaWiki Hosting" and come back with a portal sign-in made at *auth_time*."""
    started = client.post("/settings/confirm-with-hosting", data={"confirm_next": back})
    assert started.status_code == 302
    query = parse_qs(urlsplit(started.headers["Location"]).query)
    assert query["prompt"] == ["login"] and query["max_age"] == ["0"]
    portal.profile.pop("auth_time", None)
    if auth_time is not None:
        portal.profile["auth_time"] = auth_time
    return client.get(f"/platform-oauth/confirm-callback?code=abc&state={query['state'][0]}"), query


# C1: confirming with BananaWiki Hosting


def test_a_fresh_hosting_sign_in_stands_for_the_password(oauth_app, oauth_client, oauth_db, portal, make_user, login):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    login(oauth_client, user)
    page = oauth_client.get("/settings").get_data(as_text=True)
    assert "Confirm with BananaWiki Hosting" in page and 'name="password"' in page
    answer, _query = _confirm(oauth_client, portal, auth_time=int(time.time()))
    assert answer.headers["Location"].endswith("/settings/security")
    assert oauth_db.scalar("SELECT reverified_at FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL",
                           (user["id"],))
    assert "no password needed for 10 min" in oauth_client.get("/settings").get_data(as_text=True)
    oauth_client.post("/settings/username", data={"new_username": "bobby", "password": ""})
    assert oauth_db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == "bobby"
    assert "user.reverified" in oauth_db.column("SELECT action FROM audit_log")


def test_a_stale_or_missing_auth_time_is_refused(oauth_client, oauth_db, portal, make_user, login):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    login(oauth_client, user)
    for auth_time in (int(time.time()) - 3600, None):
        _confirm(oauth_client, portal, auth_time=auth_time)
        assert oauth_db.scalar("SELECT reverified_at FROM user_sessions WHERE user_id = ?", (user["id"],)) is None
    oauth_client.post("/settings/username", data={"new_username": "bobby", "password": ""})
    assert oauth_db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == "bob"


def test_the_confirmation_must_come_from_the_linked_account_and_this_session(oauth_app, oauth_client, oauth_db,
                                                                             portal, make_user, login):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    login(oauth_client, user)
    portal.profile["sub"] = "acct-other"
    _confirm(oauth_client, portal, auth_time=int(time.time()))
    assert oauth_db.scalar("SELECT reverified_at FROM user_sessions WHERE user_id = ?", (user["id"],)) is None
    portal.profile["sub"] = "acct-1"
    # A state made for another session (here: the same browser, signed in again) is refused.
    started = oauth_client.post("/settings/confirm-with-hosting", data={"confirm_next": "/settings"})
    state = parse_qs(urlsplit(started.headers["Location"]).query)["state"][0]
    login(oauth_client, user)
    portal.profile["auth_time"] = int(time.time())
    oauth_client.get(f"/platform-oauth/confirm-callback?code=abc&state={state}")
    assert oauth_db.scalar("SELECT COUNT(*) FROM user_sessions WHERE reverified_at IS NOT NULL") == 0
    # A forged or reused state too.
    oauth_client.get("/platform-oauth/confirm-callback?code=abc&state=forged")
    assert oauth_db.scalar("SELECT COUNT(*) FROM user_sessions WHERE reverified_at IS NOT NULL") == 0


def test_the_confirmation_lasts_ten_minutes(oauth_client, oauth_db, portal, make_user, login):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    login(oauth_client, user)
    _confirm(oauth_client, portal, auth_time=int(time.time()))
    oauth_db.execute("UPDATE user_sessions SET reverified_at = datetime('now', '-11 minutes')")
    oauth_client.post("/settings/username", data={"new_username": "bobby", "password": ""})
    assert oauth_db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == "bob"


def test_unlinked_accounts_only_get_the_password_field(oauth_client, make_user, login):
    login(oauth_client, make_user("bob"))
    page = oauth_client.get("/settings").get_data(as_text=True)
    assert "Confirm with BananaWiki Hosting" not in page
    assert oauth_client.post("/settings/confirm-with-hosting", data={"confirm_next": "/settings"}).headers[
        "Location"].endswith("/settings")


def test_an_account_made_through_hosting_has_no_password_and_sets_one(oauth_client, oauth_db, portal):
    oauth_db.execute("UPDATE site_settings SET open_signup = 1")
    _hosting_sign_in(oauth_client)
    oauth_client.post("/platform-oauth/signup", data={"username": "portaluser"})
    user = oauth_db.one("SELECT * FROM users WHERE username = 'portaluser'")
    assert user["password_set"] == 0
    page = oauth_client.get("/settings/security").get_data(as_text=True)
    assert "Not set" in page and "Set password" in page and 'name="current_password" type="password"' not in page
    assert "Confirm with BananaWiki Hosting" in page
    assert "set a password first" in page.lower(), "it cannot disconnect yet"
    refused = oauth_client.post("/settings/password", data={"current_password": "", "new_password": NEW_PASSWORD,
                                                          "confirm_password": NEW_PASSWORD, "return_to": "security"})
    assert refused.status_code == 302
    assert oauth_db.scalar("SELECT password_set FROM users WHERE id = ?", (user["id"],)) == 0
    _confirm(oauth_client, portal, auth_time=int(time.time()))
    oauth_client.post("/settings/password", data={"current_password": "", "new_password": NEW_PASSWORD,
                                                  "confirm_password": NEW_PASSWORD, "return_to": "security"})
    assert oauth_db.scalar("SELECT password_set FROM users WHERE id = ?", (user["id"],)) == 1


# C2: two-step codes are for password sign-ins


def test_two_step_is_skipped_only_for_hosting_sign_ins(oauth_app, oauth_client, oauth_db, portal, make_user, login):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    owner = oauth_app.test_client()
    login(owner, user)
    secret, _codes = enrol(owner)
    response = _hosting_sign_in(oauth_client)
    assert "/login/two-step" not in response.headers["Location"]
    assert signed_in(oauth_client)
    assert oauth_db.scalar("SELECT auth_method FROM user_sessions WHERE user_id = ? ORDER BY created_at DESC, "
                           "rowid DESC LIMIT 1", (user["id"],)) == "platform_oauth"
    other = oauth_app.test_client()
    response = other.post("/login", data={"username": "bob", "password": PASSWORD})
    assert response.headers["Location"].endswith("/login/two-step")
    assert not signed_in(other)
    assert other.post("/login/two-step", data={"code": _next_code(secret)}).status_code == 302
    assert signed_in(other)


def test_a_required_two_step_set_up_is_not_asked_of_hosting_sessions(oauth_client, oauth_db, portal, make_user):
    admin = make_user("chief", role="admin")
    _link(oauth_db, portal, admin)
    oauth_db.execute("UPDATE site_settings SET two_step_required_for_admins = 1")
    response = _hosting_sign_in(oauth_client)
    assert "two-step" not in response.headers["Location"]
    assert oauth_client.get("/settings").status_code == 200


# C3: Sign-in methods


def test_one_sign_in_methods_section(oauth_client, oauth_db, portal, make_user, login):
    user = make_user("bob", email="bob@example.org")
    _link(oauth_db, portal, user)
    login(oauth_client, user)
    page = oauth_client.get("/settings/security").get_data(as_text=True)
    assert page.count("Sign-in methods") == 1
    for part in ("BananaWiki Hosting", "As portaluser, since", "Disconnect", "Change password", "Two-step codes",
                 "For password sign-ins", "bob@example.org"):
        assert part in page, part


def test_disconnecting_needs_a_password_and_a_wiki_that_takes_passwords(oauth_app, oauth_client, oauth_db, portal,
                                                                        make_user, login):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    login(oauth_client, user)
    oauth_db.execute("UPDATE users SET password_set = 0 WHERE id = ?", (user["id"],))
    oauth_client.post("/settings/unlink-platform-account", data={"password": PASSWORD})
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 1, "refused without a password"
    oauth_db.execute("UPDATE users SET password_set = 1 WHERE id = ?", (user["id"],))
    oauth_db.execute("UPDATE site_settings SET signin_mode = 'platform'")
    oauth_client.post("/settings/unlink-platform-account", data={"password": PASSWORD})
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 1, "refused in hosting-only mode"
    oauth_db.execute("UPDATE site_settings SET signin_mode = 'both'")
    oauth_client.post("/settings/unlink-platform-account", data={"password": PASSWORD})
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0


# C4: connecting


def test_connecting_from_settings_refuses_a_hosting_account_linked_elsewhere(oauth_client, oauth_db, portal,
                                                                             make_user, login):
    other, user = make_user("alice"), make_user("bob")
    _link(oauth_db, portal, other)
    login(oauth_client, user)
    started = oauth_client.post("/settings/link-platform-account", data={"password": PASSWORD})
    query = parse_qs(urlsplit(started.headers["Location"]).query)
    answer = _callback(oauth_client, query, "/platform-oauth/link-callback")
    assert "/settings/security" in answer.headers["Location"]
    assert oauth_db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-1'") == other["id"]
    page = oauth_client.get("/settings/security").get_data(as_text=True)
    assert "already connected to another account on this wiki" in page


def test_the_first_sign_in_offers_two_clear_choices_and_keeps_what_was_typed(oauth_client, oauth_db, portal,
                                                                             make_user):
    make_user("existing")
    oauth_db.execute("UPDATE site_settings SET open_signup = 1")
    _hosting_sign_in(oauth_client)
    page = oauth_client.get("/platform-oauth/connect").get_data(as_text=True)
    assert page.index("I already have an account here") < page.index("Create a new account")
    assert "You signed in to BananaWiki Hosting as portaluser" in page
    refused = oauth_client.post("/platform-oauth/merge", data={"username": "existing", "password": "nope"})
    body = refused.get_data(as_text=True)
    assert refused.status_code == 401 and 'value="existing"' in body and 'id="existing-error"' in body
    taken = oauth_client.post("/platform-oauth/signup", data={"username": "existing"})
    body = taken.get_data(as_text=True)
    assert taken.status_code == 400 and 'value="existing"' in body and 'id="create-error"' in body


# A2: hosting administrators acting as a member


def test_an_acting_administrator_gets_a_banner_and_an_audited_session(oauth_client, oauth_db, portal, make_user):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    _acting(portal)
    response = _hosting_sign_in(oauth_client)
    assert "/login" not in response.headers["Location"]
    page = oauth_client.get("/settings").get_data(as_text=True)
    assert "chief-admin (hosting administrator) is signed in as bob." in page
    row = oauth_db.one("SELECT * FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL", (user["id"],))
    assert row["acting_admin_name"] == "chief-admin" and row["remember_me"] == 0
    assert row["expires_at"] <= row["acting_expires_at"]
    entry = oauth_db.one("SELECT * FROM audit_log WHERE action = 'user.acting_signin'")
    assert '"hosting_admin": "chief-admin"' in entry["details"]


def test_the_acting_session_ends_with_the_acting(oauth_client, oauth_db, portal, make_user):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    _acting(portal)
    _hosting_sign_in(oauth_client)
    assert signed_in(oauth_client)
    oauth_db.execute("UPDATE user_sessions SET expires_at = datetime('now', '-1 second') WHERE user_id = ?",
                     (user["id"],))
    assert not signed_in(oauth_client)


def test_sensitive_changes_are_refused_while_acting(oauth_client, oauth_db, portal, make_user):
    user = make_user("bob", email="bob@example.org")
    _link(oauth_db, portal, user)
    _acting(portal)
    _hosting_sign_in(oauth_client)
    page = oauth_client.get("/settings/security").get_data(as_text=True)
    assert "so this cannot be changed now" in page and "/settings/unlink-platform-account" not in page
    oauth_client.post("/settings/password", data={"current_password": PASSWORD, "new_password": NEW_PASSWORD,
                                                  "confirm_password": NEW_PASSWORD, "return_to": "security"})
    assert oauth_db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],)) == user["password"]
    assert oauth_client.post("/settings/two-step", data={"action": "enable", "password": PASSWORD,
                                                         "code": "000000"}).status_code in (302, 400, 403)
    assert oauth_db.scalar("SELECT COUNT(*) FROM auth__two_step") == 0
    assert oauth_client.post("/settings/notifications", data={"email": "evil@example.org"}).status_code == 403
    assert oauth_db.scalar("SELECT email FROM users WHERE id = ?", (user["id"],)) == "bob@example.org"
    oauth_client.post("/settings/unlink-platform-account", data={"password": PASSWORD})
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 1
    assert oauth_client.post("/settings/delete", data={"password": PASSWORD}).status_code == 403
    assert oauth_db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)) == 1
    assert oauth_client.post("/settings/api-tokens/create", data={"name": "x"}).status_code in (403, 404)
    started = oauth_client.post("/settings/confirm-with-hosting", data={"confirm_next": "/settings"})
    assert "/oauth/authorize" not in started.headers["Location"], "no confirmation on someone's behalf"


def test_no_linking_or_creation_while_acting(oauth_client, oauth_db, portal, make_user):
    oauth_db.execute("UPDATE site_settings SET open_signup = 1")
    make_user("portaluser")
    _acting(portal)
    response = _hosting_sign_in(oauth_client)
    assert response.headers["Location"].endswith("/login"), "unknown sub with act: refused, no connect page"
    page = oauth_client.get("/login").get_data(as_text=True)
    assert "Only its owner can connect or create one" in page
    assert oauth_client.get("/platform-oauth/connect").status_code == 302
    assert oauth_db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    assert oauth_db.scalar("SELECT COUNT(*) FROM users") == 1


def test_the_wiki_can_opt_out_of_acting_administrators(oauth_client, oauth_db, portal, make_user):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    oauth_db.execute("UPDATE site_settings SET platform_allow_acting = 0")
    _acting(portal)
    assert _hosting_sign_in(oauth_client).headers["Location"].endswith("/login")
    assert not signed_in(oauth_client)
    # Switching it off also ends sessions already open.
    oauth_db.execute("UPDATE site_settings SET platform_allow_acting = 1")
    _hosting_sign_in(oauth_client)
    assert signed_in(oauth_client)
    oauth_db.execute("UPDATE site_settings SET platform_allow_acting = 0")
    assert not signed_in(oauth_client)


def test_an_invalid_act_is_refused(oauth_client, oauth_db, portal, make_user):
    user = make_user("bob")
    _link(oauth_db, portal, user)
    portal.profile.update(act={"sub": "admin-1", "preferred_username": "chief"}, act_expires_at=int(time.time()) - 5)
    assert _hosting_sign_in(oauth_client).headers["Location"].endswith("/login")
    portal.profile.update(act="admin-1", act_expires_at=int(time.time()) + 60)
    assert _hosting_sign_in(oauth_client).headers["Location"].endswith("/login")
    assert not signed_in(oauth_client)


def test_the_setting_is_on_by_default_and_offered_with_platform_sign_in(oauth_client, oauth_db, make_user, login):
    assert oauth_db.scalar("SELECT platform_allow_acting FROM site_settings") == 1
    login(oauth_client, make_user("chief", role="admin"))
    page = oauth_client.get("/admin/settings?section=security").get_data(as_text=True)
    assert 'name="platform_allow_acting"' in page and "Allow hosting administrators to sign in as users" in page
