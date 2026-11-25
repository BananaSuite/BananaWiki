"""The provider side of the BananaSuite sign-in round 3: fresh sign-in on request (``prompt=login``,
``max_age``, ``auth_time``, ``amr``), administrators acting as an account in wikis and apps (``act``), and the
account lookup for the linking tools of apps (``accounts:lookup``)."""

from __future__ import annotations

import base64
import hashlib
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, REDIRECT

VERIFIER = "r" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def wiki(portal, make_account, make_wiki, query):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    inst = make_wiki(make_account(), "roundthree")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth, urls

        client_id, secret = oauth.rotate_credentials(inst["id"])
        redirect_uri = urls.instance_url(inst) + "/platform-oauth/callback"
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"id": client_id, "redirect_uri": redirect_uri, "inst": inst, "auth": {"Authorization": f"Basic {basic}"}}


def _params(wiki, **extra):
    return {"response_type": "code", "client_id": wiki["id"], "redirect_uri": wiki["redirect_uri"], "state": "st",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", **extra}


def _url(wiki, **extra) -> str:
    return "/oauth/authorize?" + urlencode(_params(wiki, **extra))


def _code(client, url: str) -> str:
    response = client.post(url, data={"action": "allow"})
    assert response.status_code == 302, response.status_code
    query = parse_qs(urlparse(response.headers["Location"]).query)
    assert "code" in query, query
    return query["code"][0]


def _token(client, wiki, code):
    return client.post("/oauth/token", headers=wiki["auth"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": wiki["redirect_uri"],
        "code_verifier": VERIFIER})


def _userinfo(client, token):
    return client.get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"})


def _enable_two_step(portal, account) -> str:
    with portal.app_context(), connection_scope():
        from bananawiki.hosting import mfa
        from bananawiki.hosting.db import db

        secret = mfa.generate_secret()
        db.update("accounts", {"totp_enabled": 1, "totp_secret_encrypted": mfa.encrypt_secret(secret)}, "id = ?",
                  (account["id"],))
    return secret


def _totp(secret: str, offset: int = 0) -> str:
    from bananawiki.hosting import mfa

    return mfa.totp_code(secret, time.time() + offset)


# A1: fresh sign-in on request


def test_prompt_login_asks_a_signed_in_account_to_sign_in_again(web, make_account, login, wiki, query):
    user = make_account()
    login(web, user)
    query("UPDATE hosting_account_sessions SET created_at = '2026-01-01 00:00:00'")
    response = web.get(_url(wiki, prompt="login"))
    assert response.status_code == 302
    location = response.headers["Location"]
    assert urlparse(location).path == "/login" and "again=1" in location
    target = parse_qs(urlparse(location).query)["next"][0]
    assert "prompt" not in target and "client_id" in target, "the fresh sign-in returns without prompt=login"
    page = web.get(location).get_data(as_text=True)
    assert "Sign in again" in page and user["username"] in page, "the form is shown although a session exists"
    assert web.get("/dashboard").status_code == 200, "the session lasts until the password is right"
    signed = web.post("/login", data={"username": user["username"], "password": PASSWORD, "again": "1",
                                      "next": target})
    assert signed.status_code == 302 and signed.headers["Location"].endswith(target)
    sessions = query("SELECT revoked_at FROM hosting_account_sessions ORDER BY created_at")
    assert sessions[0]["revoked_at"] is not None and sessions[-1]["revoked_at"] is None
    before = int(time.time())
    token = _token(web, wiki, _code(web, target)).get_json()
    assert token["auth_time"] >= before - 5 and token["amr"] == ["pwd"]
    info = _userinfo(web, token["access_token"]).get_json()
    assert info["auth_time"] == token["auth_time"] and info["amr"] == ["pwd"] and "act" not in info


def test_a_wrong_password_on_the_fresh_sign_in_keeps_the_session(web, make_account, login, wiki):
    user = make_account()
    login(web, user)
    location = web.get(_url(wiki, prompt="login")).headers["Location"]
    target = parse_qs(urlparse(location).query)["next"][0]
    assert web.post("/login", data={"username": user["username"], "password": "wrong", "again": "1",
                                    "next": target}).status_code == 401
    assert web.get("/dashboard").status_code == 200


def test_max_age_asks_again_only_when_the_sign_in_is_older(web, make_account, login, wiki, query):
    login(web, make_account())
    assert web.get(_url(wiki, max_age="3600")).status_code == 200, "a fresh session is young enough"
    query("UPDATE hosting_account_sessions SET created_at = datetime('now', '-10 seconds')")
    assert "again=1" in web.get(_url(wiki, max_age="0")).headers["Location"]
    assert "again=1" in web.get(_url(wiki, max_age="5")).headers["Location"]
    assert web.get(_url(wiki, max_age="60")).status_code == 200
    for bad in ("-1", "86401", "soon", "1e3"):
        assert web.get(_url(wiki, max_age=bad)).status_code == 400


def test_prompt_login_and_consent_keeps_the_consent(web, make_account, login, wiki):
    login(web, make_account())
    location = web.get(_url(wiki, prompt="login consent")).headers["Location"]
    target = parse_qs(urlparse(location).query)["next"][0]
    assert parse_qs(urlparse(target).query)["prompt"] == ["consent"]


def test_signing_in_from_scratch_never_loops(web, make_account, wiki):
    user = make_account()
    location = web.get(_url(wiki, prompt="login", max_age="0")).headers["Location"]
    assert "again" not in location
    target = parse_qs(urlparse(location).query)["next"][0]
    assert "prompt" not in target and "max_age" not in target
    web.post("/login", data={"username": user["username"], "password": PASSWORD, "next": target})
    assert web.get(target).status_code == 200


def test_amr_names_the_two_step_code(portal, web, make_account, wiki):
    user = make_account()
    secret = _enable_two_step(portal, user)
    assert web.post("/login", data={"username": user["username"], "password": PASSWORD}).status_code == 302
    assert web.post("/login/mfa", data={"code": _totp(secret)}).status_code == 302
    token = _token(web, wiki, _code(web, _url(wiki))).get_json()
    assert token["amr"] == ["pwd", "otp"]


def test_apps_get_auth_time_and_amr_too(web, make_account, login, make_app):
    owner = make_account()
    app = make_app(owner, status="approved")
    login(web, owner)
    params = {"response_type": "code", "client_id": app["client_id"], "redirect_uri": REDIRECT, "state": "s",
              "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "profile"}
    code = _code(web, "/oauth/authorize?" + urlencode(params))
    basic = base64.b64encode(f"{app['client_id']}:{app['secret']}".encode()).decode()
    token = web.post("/oauth/token", headers={"Authorization": f"Basic {basic}"}, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER}).get_json()
    assert isinstance(token["auth_time"], int) and token["amr"] == ["pwd"]
    info = _userinfo(web, token["access_token"]).get_json()
    assert info["auth_time"] == token["auth_time"] and "is_admin" not in info


def test_metadata_lists_the_new_parameters_and_claims(web, wiki):
    data = web.get("/.well-known/oauth-authorization-server").get_json()
    assert {"login", "consent"} <= set(data["prompt_values_supported"])
    assert {"auth_time", "amr", "act", "act_expires_at"} <= set(data["claims_supported"])


# A2: acting administrators


def _act(client, target, **data):
    return client.post(f"/admin/accounts/{target['id']}/impersonate",
                       data={"current_password": PASSWORD, **data})


def test_acting_needs_the_password_again_and_no_reason(web, make_account, login, query):
    admin, user = make_account(admin=True), make_account()
    login(web, admin)
    page = web.get(f"/admin/accounts/{user['id']}").get_data(as_text=True)
    assert 'id="act-password"' in page and 'id="act-reason"' not in page
    _act(web, user, current_password="wrong")
    assert web.get("/admin").status_code == 200, "did not start acting"
    _act(web, user)
    assert web.get("/admin").status_code == 403
    log = query("SELECT * FROM hosting_impersonation_logs", one=True)
    assert log["reason"] == "" and log["expires_at"] > log["started_at"]
    events = query("SELECT action, actor_id FROM hosting_events WHERE action LIKE 'account.imperson%'")
    assert events == [{"action": "account.impersonated", "actor_id": admin["id"]}]
    assert "This ends at" in web.get("/dashboard").get_data(as_text=True)


def test_acting_with_a_two_step_code_instead_of_the_password(portal, web, make_account):
    admin, user = make_account(admin=True), make_account()
    secret = _enable_two_step(portal, admin)
    web.post("/login", data={"username": admin["username"], "password": PASSWORD})
    web.post("/login/mfa", data={"code": _totp(secret, -30)})
    _act(web, user, current_password="", code="000000")
    assert web.get("/admin").status_code == 200
    _act(web, user, current_password="", code=_totp(secret))
    assert web.get("/admin").status_code == 403


def test_administrators_are_never_acted_as(web, make_account, login, query):
    admin, other = make_account(admin=True), make_account(admin=True)
    login(web, admin)
    _act(web, other)
    assert web.get("/admin").status_code == 200
    assert 'id="act"' not in web.get(f"/admin/accounts/{other['id']}").get_data(as_text=True)
    assert query("SELECT COUNT(*) AS n FROM hosting_impersonation_logs", one=True)["n"] == 0


def test_acting_ends_by_itself(web, make_account, login, query):
    admin, user = make_account(admin=True), make_account()
    login(web, admin)
    _act(web, user)
    query("UPDATE hosting_impersonation_logs SET expires_at = '2026-01-01 00:00:00'")
    page = web.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert "its time was up" in page
    assert web.get("/admin").status_code == 200, "back to the administrator's own account"
    log = query("SELECT ended_at, end_reason FROM hosting_impersonation_logs", one=True)
    assert log["ended_at"] and log["end_reason"] == "expired"
    assert query("SELECT reason FROM hosting_events WHERE action = 'account.impersonation_ended'",
                 one=True)["reason"] == "expired"


def test_the_account_can_be_told(web, make_account, login, query):
    from bananawiki.hosting import email

    admin, user = make_account(admin=True), make_account(email="member@example.org")
    query("UPDATE hosting_settings SET admin_act_notify = 1, admin_act_minutes = 15 WHERE id = 1")
    login(web, admin)
    with email.capture_outbox() as sent:
        _act(web, user)
    assert [message.to for message in sent] == ["member@example.org"]
    assert admin["username"] in sent[0].text and "15 minutes" in sent[0].text


def test_acting_reaches_wikis_unless_switched_off(web, make_account, login, wiki, query):
    admin, user = make_account(admin=True), make_account()
    login(web, admin)
    _act(web, user)
    query("UPDATE hosting_settings SET admin_app_signin = 0 WHERE id = 1")
    assert web.get(_url(wiki)).status_code == 403
    query("UPDATE hosting_settings SET admin_app_signin = 1 WHERE id = 1")
    page = web.get(_url(wiki)).get_data(as_text=True)
    assert "You are acting as" in page and "Use another account" not in page
    token = _token(web, wiki, _code(web, _url(wiki))).get_json()
    assert token["act"] == {"sub": admin["id"], "preferred_username": admin["username"]}
    log = query("SELECT expires_at FROM hosting_impersonation_logs", one=True)
    assert token["act_expires_at"] > time.time() and token["expires_in"] <= 30 * 60
    info = _userinfo(web, token["access_token"]).get_json()
    assert info["sub"] == user["id"] and info["act"]["sub"] == admin["id"] and info["is_admin"] is False
    assert info["act_expires_at"] == token["act_expires_at"] and log["expires_at"]
    assert query("SELECT COUNT(*) AS n FROM hosting_oauth_grants", one=True)["n"] == 0, "no consent recorded"
    signin = query("SELECT * FROM hosting_events WHERE action = 'account.acting_signin'", one=True)
    assert signin["subject_id"] == user["id"] and signin["actor_id"] == admin["id"] and signin["reason"] == wiki["inst"]["subdomain"]
    # Stopping ends what the sign-in gave.
    web.post("/admin/stop-impersonating")
    assert _userinfo(web, token["access_token"]).status_code == 401


def test_acting_tokens_stop_when_the_setting_goes_off(web, make_account, login, wiki, query):
    admin, user = make_account(admin=True), make_account()
    query("UPDATE hosting_settings SET admin_app_signin = 1 WHERE id = 1")
    login(web, admin)
    _act(web, user)
    code = _code(web, _url(wiki))
    query("UPDATE hosting_settings SET admin_app_signin = 0 WHERE id = 1")
    assert _token(web, wiki, code).status_code == 400
    query("UPDATE hosting_settings SET admin_app_signin = 1 WHERE id = 1")
    token = _token(web, wiki, _code(web, _url(wiki))).get_json()["access_token"]
    query("UPDATE accounts SET is_admin = 0 WHERE id = ?", (admin["id"],))
    assert _userinfo(web, token).status_code == 401, "the administrator lost the role"


def test_no_fresh_sign_in_and_no_link_while_acting(web, make_account, login, wiki, query):
    admin, user = make_account(admin=True), make_account()
    query("UPDATE hosting_settings SET admin_app_signin = 1 WHERE id = 1")
    login(web, admin)
    _act(web, user)
    refused = web.get(_url(wiki, prompt="login"))
    assert parse_qs(urlparse(refused.headers["Location"]).query)["error"] == ["login_required"]
    token = _token(web, wiki, _code(web, _url(wiki))).get_json()["access_token"]
    linked = web.post("/oauth/link", headers=wiki["auth"], json={"access_token": token, "wiki_user_id": "u1",
                                                                 "wiki_username": "member"})
    assert linked.status_code == 403
    by_id = web.post("/oauth/link", headers=wiki["auth"], json={"account_id": user["id"], "wiki_user_id": "u1",
                                                                "wiki_username": "member"})
    assert by_id.status_code == 403, "an acting token never counts as the person's own sign-in"
    assert query("SELECT COUNT(*) AS n FROM hosting_oauth_account_links", one=True)["n"] == 0


def test_the_settings_page_saves_the_acting_settings(web, make_account, login, query):
    login(web, make_account(admin=True))
    web.post("/admin/settings", data={"action": "save_acting", "admin_act_minutes": "45", "admin_act_notify": "1",
                                      "admin_app_signin": "1"})
    row = query("SELECT admin_act_minutes, admin_act_notify, admin_app_signin FROM hosting_settings", one=True)
    assert row == {"admin_act_minutes": 45, "admin_act_notify": 1, "admin_app_signin": 1}
    web.post("/admin/settings", data={"action": "save_acting", "admin_act_minutes": "2"})
    assert query("SELECT admin_act_minutes FROM hosting_settings", one=True)["admin_act_minutes"] == 45


def test_defaults(query):
    row = query("SELECT admin_act_minutes, admin_act_notify, admin_app_signin FROM hosting_settings", one=True)
    assert row == {"admin_act_minutes": 30, "admin_act_notify": 0, "admin_app_signin": 1}


def test_version_17_turns_app_sign_in_on_once():
    import sqlite3

    from bananawiki.hosting.migrations import v17_acting_app_signin

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE hosting_settings (id INTEGER PRIMARY KEY, admin_app_signin INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO hosting_settings (id) VALUES (1)")
    v17_acting_app_signin.upgrade(conn)
    assert conn.execute("SELECT admin_app_signin FROM hosting_settings").fetchone() == (1,)


# A3: account lookup


@pytest.fixture
def api_on(query):
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")


def _new_token(client, scopes: list[str]) -> tuple[int, str]:
    html = client.get("/account").get_data(as_text=True)
    nonce = re.search(r'name="form_nonce" value="([^"]+)"', html).group(1)
    response = client.post("/account/api-tokens", data={
        "form_nonce": nonce, "current_password": PASSWORD, "name": "linker", "expires_in": "30", "scopes": scopes})
    match = re.search(r"bwh_[A-Za-z0-9_-]{40}", response.get_data(as_text=True))
    return response.status_code, match.group(0) if match else ""


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def lookup(web, make_account, login, api_on):
    login(web, make_account("chief", admin=True))
    _status, token = _new_token(web, ["accounts:lookup"])
    return token


def test_only_administrators_get_the_lookup_scope(web, make_account, login, api_on, query):
    login(web, make_account())
    assert "accounts:lookup" not in web.get("/account").get_data(as_text=True)
    _status, token = _new_token(web, ["accounts:lookup"])
    assert token == "" and query("SELECT COUNT(*) AS n FROM hosting_api_tokens", one=True)["n"] == 0


def test_lookup_by_name_and_verified_address(portal, web, make_account, lookup, query):
    make_account("Alice", email="alice@example.org", email_verified_at="2026-01-01 00:00:00")
    make_account("bob", email="bob@example.org")  # not verified
    make_account("carol", suspended=1)
    make_account("dave", approval_status="pending")
    api = portal.test_client()
    one = api.get("/api/v1/admin/accounts/lookup?username=ALICE", headers=_bearer(lookup)).get_json()
    assert one["accounts"][0]["username"] == "Alice" and one["accounts"][0]["email"] == "alice@example.org"
    assert one["accounts"][0]["status"] == "active" and one["not_found"] == []
    answer = api.post("/api/v1/admin/accounts/lookup", headers=_bearer(lookup), json={
        "usernames": ["bob", "carol", "dave", "nobody"],
        "emails": ["ALICE@example.org", "bob@example.org"]}).get_json()
    found = {row["username"]: row for row in answer["accounts"]}
    assert set(found) == {"Alice", "bob", "carol", "dave"}
    assert "email" not in found["bob"] and found["bob"]["email_verified"] is False
    assert found["carol"]["status"] == "suspended" and found["dave"]["status"] == "pending"
    assert set(answer["not_found"]) == {"nobody", "bob@example.org"}, "unverified addresses never match"
    audit = query("SELECT reason FROM hosting_events WHERE action = 'api.accounts.lookup' ORDER BY id")
    assert len(audit) == 2 and "4 names, 2 addresses, 4 found" in audit[1]["reason"]


def test_lookup_refusals(portal, web, make_account, login, lookup, api_on):
    api = portal.test_client()
    url = "/api/v1/admin/accounts/lookup"
    assert api.get(url).status_code == 401
    assert api.get(url, headers=_bearer(lookup)).status_code == 400
    assert api.get(url + "?username=a&email=b@example.org", headers=_bearer(lookup)).status_code == 400
    too_many = {"usernames": [f"user{n}" for n in range(300)], "emails": [f"u{n}@example.org" for n in range(201)]}
    assert api.post(url, headers=_bearer(lookup), json=too_many).status_code == 400
    assert api.post(url, headers=_bearer(lookup), json={"usernames": "alice"}).status_code == 400
    _status, other = _new_token(web, ["admin:read"])
    assert api.get(url + "?username=chief", headers=_bearer(other)).status_code == 403


def test_lookup_is_limited_per_token(portal, lookup):
    api = portal.test_client()
    statuses = [api.get("/api/v1/admin/accounts/lookup?username=chief", headers=_bearer(lookup)).status_code
                for _ in range(61)]
    assert statuses[:60] == [200] * 60 and statuses[60] == 429


def test_lookup_stops_when_the_account_is_no_longer_an_administrator(portal, lookup, query):
    query("UPDATE accounts SET is_admin = 0 WHERE username = 'chief'")
    assert portal.test_client().get("/api/v1/admin/accounts/lookup?username=chief",
                                    headers=_bearer(lookup)).status_code == 403


def test_version_15_adds_its_columns_to_an_older_portal():
    import sqlite3

    from bananawiki.hosting.migrations import v15_signin_round3

    conn = sqlite3.connect(":memory:")
    for table in ("hosting_settings", "hosting_impersonation_logs", "hosting_oauth_authorization_codes",
                  "hosting_oauth_access_tokens"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO hosting_settings (id) VALUES (1)")
    v15_signin_round3.upgrade(conn)
    v15_signin_round3.upgrade(conn)  # idempotent
    assert conn.execute("SELECT admin_app_signin, admin_act_minutes, admin_act_notify FROM hosting_settings"
                        ).fetchone() == (0, 30, 0)
    for table in ("hosting_oauth_authorization_codes", "hosting_oauth_access_tokens"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert {"auth_time", "amr", "act_account_id", "act_expires_at", "act_log_id"} <= columns
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hosting_impersonation_logs)")}
    assert {"reason", "expires_at", "end_reason"} <= columns
