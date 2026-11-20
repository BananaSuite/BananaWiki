"""Apps (BananaChat, BananaGit, …) that sign people in with their hosting account: registering, the protocol,
revoking, and accounts that go away or merge."""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, REDIRECT
from .hosting_support import count as _count

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
VERIFIED = "2026-01-01 00:00:00"


def _authorize_url(app, **extra):
    params = {"response_type": "code", "client_id": app["client_id"], "redirect_uri": REDIRECT, "state": "xyz",
              "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "profile"}
    params.update(extra)
    return "/oauth/authorize?" + urlencode({k: v for k, v in params.items() if v is not None})


def _allow(client, app, **extra):
    response = client.post(_authorize_url(app, **extra), data={"action": "allow"})
    assert response.status_code == 302, response.status_code
    return parse_qs(urlparse(response.headers["Location"]).query)


def _token(client, app, code, *, verifier=VERIFIER, secret=None, redirect=REDIRECT):
    basic = base64.b64encode(f"{app['client_id']}:{secret or app['secret']}".encode()).decode()
    return client.post("/oauth/token", headers={"Authorization": f"Basic {basic}"}, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "code_verifier": verifier})


def _sign_in_to(web, app, user, login, **extra):
    login(web, user)
    response = _token(web, app, _allow(web, app, **extra)["code"][0])
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _userinfo(portal, token):
    return portal.test_client().get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"})


# Metadata


def test_metadata_is_hidden_while_platform_sign_in_is_off(web):
    assert web.get("/.well-known/oauth-authorization-server").status_code == 404


def test_metadata_describes_the_endpoints(web, oauth_on):
    response = web.get("/.well-known/oauth-authorization-server")
    data = response.get_json()
    assert response.status_code == 200
    assert data["authorization_endpoint"].endswith("/oauth/authorize")
    assert data["token_endpoint"].endswith("/oauth/token") and data["userinfo_endpoint"].endswith("/oauth/userinfo")
    assert data["code_challenge_methods_supported"] == ["S256"] and data["scopes_supported"] == ["profile", "email"]
    assert data["issuer"] == data["authorization_endpoint"].rsplit("/oauth/", 1)[0]


# Registering apps


def test_an_account_registers_an_app_that_waits_for_review(make_app, make_account, query):
    app = make_app(make_account())
    assert app["status"] == "pending" and app["client_id"].startswith("bwa_") and len(app["client_id"]) == 32
    row = query("SELECT client_secret_hash FROM hosting_oauth_apps", one=True)
    assert app["secret"] not in row["client_secret_hash"] and "$" in row["client_secret_hash"]
    assert _count(query, "hosting_attention_events") == 1


def test_an_administrators_app_is_approved_at_once(make_app, make_account):
    assert make_app(make_account(admin=True))["status"] == "approved"


@pytest.mark.parametrize("uris", [
    "http://git.example.org/cb",
    "https://git.example.org/cb#fragment",
    "https://user:pw@git.example.org/cb",
    "ftp://git.example.org/cb",
    "https://git.example.org/ cb",
    "",
    "\n\n",
    "\n".join(f"https://a{i}.example.org/cb" for i in range(6)),
])
def test_redirect_uris_are_checked(portal, make_account, oauth_on, uris):
    from bananawiki.hosting.errors import ServiceError

    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        with pytest.raises(ServiceError):
            oauth_apps.create(make_account(), "App", "", "", uris, actor_id=None)


def test_loopback_http_redirects_are_allowed_for_development(make_app, make_account):
    app = make_app(make_account(), redirects="http://localhost:3000/cb\nhttp://127.0.0.1:3000/cb\nhttp://[::1]:3000/cb")
    assert len(app["redirect_uris"]) == 3


def test_duplicate_redirect_uris_are_kept_once(make_app, make_account):
    assert make_app(make_account(), redirects=f"{REDIRECT}\n{REDIRECT}\n")["redirect_uris"] == [REDIRECT]


@pytest.mark.parametrize("name", ["x", " ", "a" * 61, "bad\x00name"])
def test_names_are_checked(portal, make_account, oauth_on, name):
    from bananawiki.hosting.errors import ServiceError

    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        with pytest.raises(ServiceError):
            oauth_apps.create(make_account(), name, "", "", REDIRECT, actor_id=None)


def test_homepage_must_be_https(portal, make_account, oauth_on):
    from bananawiki.hosting.errors import ServiceError

    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        with pytest.raises(ServiceError):
            oauth_apps.create(make_account(), "App", "", "http://example.org", REDIRECT, actor_id=None)


def test_accounts_have_a_limit_but_administrators_do_not(make_app, make_account, query):
    from bananawiki.hosting.errors import ServiceError

    query("UPDATE hosting_settings SET oauth_apps_per_account = 2 WHERE id = 1")
    owner = make_account()
    make_app(owner, "One")
    make_app(owner, "Two")
    with pytest.raises(ServiceError) as error:
        make_app(owner, "Three")
    assert error.value.key == "hosting.apps.error.limit"
    admin = make_account(admin=True)
    for number in range(4):
        make_app(admin, f"Admin {number}")


def test_registering_in_a_loop_stops_even_when_apps_are_deleted(portal, make_app, make_account, query):
    from bananawiki.hosting.errors import ServiceError

    query("UPDATE hosting_settings SET oauth_apps_per_account = 50 WHERE id = 1")
    owner = make_account()
    for number in range(10):
        app = make_app(owner, f"App {number}")
        with portal.test_request_context("/"), connection_scope():
            from bananawiki.hosting import oauth_apps

            oauth_apps.delete(oauth_apps.get(app["id"]), actor_id=owner["id"])
    with pytest.raises(ServiceError) as error:
        make_app(owner, "One too many")
    assert error.value.key == "hosting.apps.error.too_many"


# Pages


def _nonce(client, path="/account/developer-apps"):
    return re.search(r'name="form_nonce" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def test_developer_pages_are_hidden_while_platform_sign_in_is_off(web, make_account, login):
    login(web, make_account())
    assert web.get("/account/developer-apps").status_code == 404
    assert web.get("/account/developer-apps/abc").status_code == 404


def test_an_account_creates_manages_and_deletes_an_app_in_the_portal(web, make_account, login, oauth_on, query):
    login(web, make_account())
    page = web.get("/account/developer-apps")
    assert page.status_code == 200 and "Register an app" in page.get_data(as_text=True)
    form = {"name": "My Forge", "description": "Our forge", "homepage_url": "https://forge.example.org",
            "redirect_uris": REDIRECT, "form_nonce": _nonce(web)}
    created = web.post("/account/developer-apps", data=form)
    html = created.get_data(as_text=True)
    assert created.status_code == 200 and "My Forge" in html and "not shown again" in html
    assert created.headers["Cache-Control"] == "no-store"
    secret = re.search(r'class="secret-box">([^<]+)<', html).group(1)
    assert len(secret) > 40 and secret not in web.get("/account/developer-apps").get_data(as_text=True)
    app = query("SELECT * FROM hosting_oauth_apps", one=True)
    assert app["status"] == "pending"
    again = web.post("/account/developer-apps", data=form)  # the form's nonce works once
    assert again.status_code == 302 and _count(query, "hosting_oauth_apps") == 1
    assert secret not in web.get(f"/account/developer-apps/{app['id']}").get_data(as_text=True)
    wrong = web.post(f"/account/developer-apps/{app['id']}/delete", data={"current_password": "nope"})
    assert wrong.status_code == 302 and _count(query, "hosting_oauth_apps") == 1
    gone = web.post(f"/account/developer-apps/{app['id']}/delete", data={"current_password": PASSWORD})
    assert gone.status_code == 302 and _count(query, "hosting_oauth_apps") == 0


def test_a_bad_form_keeps_what_was_typed(web, make_account, login, oauth_on):
    login(web, make_account())
    response = web.post("/account/developer-apps", data={"name": "My Forge", "redirect_uris": "http://x.example/cb",
                                                         "form_nonce": _nonce(web)})
    assert response.status_code == 400 and "My Forge" in response.get_data(as_text=True)


def test_only_the_owner_sees_an_app(web, make_app, make_account, login, query):
    app = make_app(make_account())
    login(web, make_account())
    assert web.get(f"/account/developer-apps/{app['id']}").status_code == 404
    for path in ("edit", "rotate-secret", "delete"):
        assert web.post(f"/account/developer-apps/{app['id']}/{path}").status_code == 404
    assert _count(query, "hosting_oauth_apps") == 1


def test_registration_can_be_switched_off_but_existing_apps_stay_manageable(web, make_app, make_account, login,
                                                                          query):
    owner = make_account()
    app = make_app(owner)
    query("UPDATE hosting_settings SET oauth_apps_enabled = 0 WHERE id = 1")
    login(web, owner)
    assert web.get(f"/account/developer-apps/{app['id']}").status_code == 200
    page = web.get("/account/developer-apps")
    assert page.status_code == 200 and "Register an app" not in page.get_data(as_text=True)
    created = web.post("/account/developer-apps", data={"name": "Another", "redirect_uris": REDIRECT,
                                                        "form_nonce": "anything"})
    assert created.status_code == 404 and _count(query, "hosting_oauth_apps") == 1


def test_renaming_an_approved_app_sends_it_back_but_redirects_do_not(portal, make_app, make_account):
    admin, owner = make_account(admin=True), make_account()
    app = make_app(owner)
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        oauth_apps.review(oauth_apps.get(app["id"]), "approve", actor_id=admin["id"])
        current = oauth_apps.get(app["id"])
        same = oauth_apps.update(current, current["name"], "", "", "https://other.example.org/cb", actor=owner)
        assert same["status"] == "approved" and same["redirect_uris"] == ["https://other.example.org/cb"]
        renamed = oauth_apps.update(same, "Bananagit Official", "", "", "https://other.example.org/cb", actor=owner)
        assert renamed["status"] == "pending"
        oauth_apps.review(renamed, "approve", actor_id=admin["id"])
        again = oauth_apps.update(oauth_apps.get(app["id"]), "Final name", "", "", "https://other.example.org/cb",
                                  actor=admin)
        assert again["status"] == "approved"  # an administrator's own change needs no second review


def test_changing_redirects_never_leaves_the_old_ones_usable(web, portal, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        oauth_apps.update(oauth_apps.get(app["id"]), app["name"], "", "", "https://new.example.org/cb", actor=owner)
    login(web, owner)
    assert web.get(_authorize_url(app)).status_code == 400


# Authorize


def test_authorize_refuses_what_is_not_registered(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    assert web.get(_authorize_url(app, client_id="bwa_" + "a" * 28)).status_code == 400
    assert web.get(_authorize_url(app, redirect_uri="https://evil.example/cb")).status_code == 400
    for uri in (REDIRECT + "/", REDIRECT + "?x=1", REDIRECT.replace("git.", "GIT."), REDIRECT[:-3],
                "https://git.example.org.evil.example/user/oauth2/bananawiki/callback"):
        assert web.get(_authorize_url(app, redirect_uri=uri)).status_code == 400, uri
    assert web.get(_authorize_url(app, response_type="token")).status_code == 400


def test_apps_must_use_pkce(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    assert web.get(_authorize_url(app, code_challenge=None, code_challenge_method=None)).status_code == 400
    assert web.get(_authorize_url(app, code_challenge_method="plain")).status_code == 400
    assert web.get(_authorize_url(app, code_challenge="short")).status_code == 400
    assert web.get(_authorize_url(app)).status_code == 200


def test_authorize_sends_people_to_log_in_first(web, make_app, make_account):
    app = make_app(make_account(admin=True))
    response = web.get(_authorize_url(app))
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_a_pending_app_works_for_its_owner_and_administrators_only(portal, web, make_app, make_account, login):
    owner, admin, other = make_account(), make_account(admin=True), make_account()
    app = make_app(owner)
    assert app["status"] == "pending"
    login(web, other)
    refused = web.get(_authorize_url(app))
    assert refused.status_code == 403 and "waiting for" in refused.get_data(as_text=True)
    assert web.post(_authorize_url(app), data={"action": "allow"}).status_code == 403
    for account in (owner, admin):
        client = portal.test_client()
        login(client, account)
        page = client.get(_authorize_url(app))
        assert page.status_code == 200 and "have not reviewed" in page.get_data(as_text=True)


@pytest.mark.parametrize("status", ["denied", "suspended"])
def test_denied_and_suspended_apps_sign_nobody_in(web, make_app, make_account, login, status):
    owner = make_account(admin=True)
    app = make_app(owner, status=status)
    login(web, owner)
    assert web.get(_authorize_url(app)).status_code == 403
    assert web.post(_authorize_url(app), data={"action": "allow"}).status_code == 403


def test_an_apps_owner_in_trouble_takes_the_app_with_them(portal, web, make_app, make_account, login, query):
    admin, owner, user = make_account(admin=True), make_account(), make_account()
    app = make_app(owner)
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        oauth_apps.review(oauth_apps.get(app["id"]), "approve", actor_id=admin["id"])
    login(web, user)
    assert web.get(_authorize_url(app)).status_code == 200
    query("UPDATE accounts SET suspended = 1 WHERE id = ?", (owner["id"],))
    assert web.get(_authorize_url(app)).status_code == 403


def test_consent_page_names_the_app_its_owner_and_what_it_gets(web, make_app, make_account, login):
    owner = make_account("forgeowner", admin=True)
    app = make_app(owner, description="Our forge", homepage="https://forge.example.org")
    login(web, make_account("alice"))
    html = web.get(_authorize_url(app)).get_data(as_text=True)
    assert "BananaGit" in html and "alice" in html and "forgeowner" in html and "Our forge" in html
    assert "username and your account ID" in html


def test_consent_is_asked_once_then_remembered_per_scope(web, make_app, make_account, login):
    app = make_app(make_account(admin=True))
    login(web, make_account())
    assert web.get(_authorize_url(app)).status_code == 200
    assert "code" in _allow(web, app)
    again = web.get(_authorize_url(app))  # remembered: the code comes straight back
    assert again.status_code == 302 and "code=" in again.headers["Location"]
    assert web.get(_authorize_url(app, scope="profile email")).status_code == 200  # more than allowed: asks again
    assert "code" in _allow(web, app, scope="profile email")
    assert web.get(_authorize_url(app, scope="profile email")).status_code == 302
    assert web.get(_authorize_url(app, prompt="consent")).status_code == 200


def test_denying_returns_access_denied_with_the_state(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    response = web.post(_authorize_url(app), data={"action": "deny"})
    assert parse_qs(urlparse(response.headers["Location"]).query) == {"error": ["access_denied"], "state": ["xyz"]}


def test_consent_form_may_only_go_back_to_the_registered_redirect(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    policy = web.get(_authorize_url(app)).headers["Content-Security-Policy"]
    sources = [d for d in policy.split(";") if d.strip().startswith("form-action")][0]
    assert "https://git.example.org" in sources


# Token and userinfo


def test_full_flow_gives_an_app_the_profile_and_no_admin_flag(portal, web, make_app, make_account, login):
    owner = make_account("root", admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)
    assert token["scope"] == "profile" and token["token_type"] == "Bearer" and token["expires_in"] == 3600
    info = _userinfo(portal, token["access_token"]).get_json()
    assert info["sub"] == owner["id"] and info["preferred_username"] == "root" and info["name"] == "root"
    assert "is_admin" not in info and "email" not in info and "email_verified" not in info


def test_email_comes_only_with_the_scope_and_only_when_verified(portal, web, make_app, make_account, login, query):
    app = make_app(make_account(admin=True))
    user = make_account("alice", email="alice@example.org", email_verified_at=VERIFIED)
    login(web, user)
    plain = _token(web, app, _allow(web, app)["code"][0]).get_json()["access_token"]
    assert "email" not in _userinfo(portal, plain).get_json()
    with_email = _token(web, app, _allow(web, app, scope="profile email")["code"][0]).get_json()
    assert with_email["scope"] == "profile email"
    info = _userinfo(portal, with_email["access_token"]).get_json()
    assert info["email"] == "alice@example.org" and info["email_verified"] is True
    query("UPDATE accounts SET email_verified_at = NULL WHERE id = ?", (user["id"],))
    assert "email" not in _userinfo(portal, with_email["access_token"]).get_json()
    query("UPDATE accounts SET email_verified_at = ?, email_flagged_invalid = 1 WHERE id = ?", (VERIFIED, user["id"]))
    assert "email" not in _userinfo(portal, with_email["access_token"]).get_json()


def test_unknown_scopes_are_ignored(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    token = _token(web, app, _allow(web, app, scope="profile admin openid")["code"][0]).get_json()
    assert token["scope"] == "profile"


def test_token_checks_pkce_redirect_secret_and_single_use(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    assert _token(web, app, _allow(web, app)["code"][0], verifier="w" * 64).status_code == 400
    assert _token(web, app, _allow(web, app)["code"][0], redirect=REDIRECT + "/").status_code == 400
    code = _allow(web, app)["code"][0]
    assert _token(web, app, code, secret="wrong").status_code == 401
    assert _token(web, app, code).status_code == 200
    assert _token(web, app, code).status_code == 400


def test_a_code_cannot_be_redeemed_by_another_app(web, make_app, make_account, login):
    admin = make_account(admin=True)
    first, second = make_app(admin, "First"), make_app(admin, "Second")
    login(web, admin)
    assert _token(web, second, _allow(web, first)["code"][0]).status_code == 400


def test_a_replayed_code_revokes_the_token_it_made(portal, web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    code = _allow(web, app)["code"][0]
    token = _token(web, app, code).get_json()["access_token"]
    assert _userinfo(portal, token).status_code == 200
    assert _token(web, app, code).status_code == 400
    assert _userinfo(portal, token).status_code == 401


def test_tokens_in_the_query_string_are_not_accepted(portal, web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)["access_token"]
    client = portal.test_client()
    assert client.get(f"/oauth/userinfo?access_token={token}").status_code == 401
    assert client.get("/oauth/userinfo", headers={"Authorization": "Basic " + token}).status_code == 401


def test_client_secret_may_come_in_the_form_too(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    response = web.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": _allow(web, app)["code"][0], "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER, "client_id": app["client_id"], "client_secret": app["secret"]})
    assert response.status_code == 200


def test_app_credentials_do_not_open_the_wiki_endpoints(portal, web, make_app, make_account, login, query):
    owner = make_account(admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)["access_token"]
    basic = {"Authorization": "Basic " + base64.b64encode(f"{app['client_id']}:{app['secret']}".encode()).decode()}
    client = portal.test_client()
    assert client.get("/oauth/link-status?account_id=" + owner["id"], headers=basic).status_code == 401
    assert client.get("/oauth/signouts?after=0", headers=basic).status_code == 401
    assert client.post("/oauth/link", headers=basic, json={"account_id": owner["id"], "wiki_user_id": "1",
                                                           "access_token": token}).status_code == 403
    assert client.post("/oauth/unlink", headers=basic, json={"account_id": owner["id"]}).status_code == 403
    assert _count(query, "hosting_oauth_account_links") == 0


def test_wiki_credentials_still_work_and_wikis_still_see_the_admin_flag(portal, web, make_account, make_wiki, login,
                                                                         oauth_on):
    admin = make_account(admin=True)
    wiki = make_wiki(admin, "stillworks")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth, urls

        client_id, secret = oauth.rotate_credentials(wiki["id"])
        redirect_uri = urls.instance_url(wiki) + "/auth/platform/callback"
    login(web, admin)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri}
    location = web.post("/oauth/authorize?" + urlencode(params), data={"action": "allow"}).headers["Location"]
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    token = web.post("/oauth/token", headers={"Authorization": f"Basic {basic}"}, data={
        "grant_type": "authorization_code", "code": parse_qs(urlparse(location).query)["code"][0],
        "redirect_uri": redirect_uri}).get_json()
    assert token["scope"] == "openid profile"
    assert _userinfo(portal, token["access_token"]).get_json()["is_admin"] is True


# Revoking


def test_suspending_an_app_ends_its_tokens_at_once(portal, web, make_app, make_account, login):
    admin = make_account(admin=True)
    app = make_app(admin)
    token = _sign_in_to(web, app, admin, login)["access_token"]
    assert _userinfo(portal, token).status_code == 200
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        oauth_apps.review(oauth_apps.get(app["id"]), "suspend", actor_id=admin["id"], note="abuse")
    assert _userinfo(portal, token).status_code == 401
    assert web.get(_authorize_url(app)).status_code == 403


def test_a_token_stops_when_the_app_is_suspended_even_if_cleanup_did_not_run(portal, web, make_app, make_account,
                                                                            login, query):
    admin = make_account(admin=True)
    app = make_app(admin)
    token = _sign_in_to(web, app, admin, login)["access_token"]
    query("UPDATE hosting_oauth_apps SET status = 'suspended' WHERE id = ?", (app["id"],))
    assert _userinfo(portal, token).status_code == 401


def test_rotating_the_secret_retires_the_old_one_and_the_tokens(portal, web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)["access_token"]
    unused = _allow(web, app)["code"][0]
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        new_secret = oauth_apps.rotate_secret(oauth_apps.get(app["id"]), actor_id=owner["id"])
    assert _userinfo(portal, token).status_code == 401
    assert _token(web, app, unused, secret=new_secret).status_code == 400  # the unused code went too
    assert _token(web, app, _allow(web, app)["code"][0], secret=app["secret"]).status_code == 401
    assert _token(web, app, _allow(web, app)["code"][0], secret=new_secret).status_code == 200


def test_rotating_in_the_portal_needs_the_password(web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    login(web, owner)
    refused = web.post(f"/account/developer-apps/{app['id']}/rotate-secret", data={"current_password": "nope"})
    assert refused.status_code == 302
    done = web.post(f"/account/developer-apps/{app['id']}/rotate-secret", data={"current_password": PASSWORD})
    assert done.status_code == 200 and "secret-box" in done.get_data(as_text=True)
    assert done.headers["Cache-Control"] == "no-store"


def test_deleting_an_app_revokes_everything(portal, web, make_app, make_account, login, query):
    owner = make_account(admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)["access_token"]
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        oauth_apps.delete(oauth_apps.get(app["id"]), actor_id=owner["id"])
    assert _userinfo(portal, token).status_code == 401
    assert _count(query, "hosting_oauth_app_grants") == 0 and _count(query, "hosting_oauth_access_tokens") == 0


def test_removing_access_forgets_the_consent_and_signs_out(portal, web, make_app, make_account, login):
    admin, user = make_account(admin=True), make_account()
    app = make_app(admin)
    token = _sign_in_to(web, app, user, login)["access_token"]
    page = web.get("/account/wikis").get_data(as_text=True)
    assert "BananaGit" in page and "Username, account ID" in page
    assert web.post(f"/account/apps/{app['id']}/remove").status_code == 302
    assert _userinfo(portal, token).status_code == 401
    assert web.get(_authorize_url(app)).status_code == 200  # asks again
    assert web.post(f"/account/apps/{app['id']}/remove").status_code == 302  # nothing left: a notice, not an error


def test_the_connected_page_lists_apps_with_what_they_get(web, make_app, make_account, login):
    app = make_app(make_account(admin=True))
    login(web, make_account(email="u@example.org", email_verified_at=VERIFIED))
    _allow(web, app, scope="profile email")
    assert "Username, account ID, email address" in web.get("/account/wikis").get_data(as_text=True)


def test_signing_out_everywhere_also_ends_app_tokens(portal, web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)["access_token"]
    assert web.post("/oauth/revoke").status_code == 302
    assert _userinfo(portal, token).status_code == 401


def test_a_password_change_ends_app_tokens_and_codes(portal, web, make_app, make_account, login):
    owner = make_account(admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)["access_token"]
    unused = _allow(web, app)["code"][0]
    with portal.app_context(), connection_scope():
        from bananawiki.hosting import accounts

        accounts.set_password(owner["id"], "replacement password 42", actor_id=owner["id"], reason="reset")
    assert _userinfo(portal, token).status_code == 401
    assert _token(web, app, unused).status_code == 400


def test_a_suspended_account_gets_nothing_from_an_app_token(portal, web, make_app, make_account, login, query):
    owner = make_account(admin=True)
    app = make_app(owner)
    token = _sign_in_to(web, app, owner, login)["access_token"]
    query("UPDATE accounts SET suspended = 1 WHERE id = ?", (owner["id"],))
    assert _userinfo(portal, token).status_code == 403


# Accounts going away and merging


def test_deleting_an_account_removes_its_apps_and_consent(portal, web, make_app, make_account, login, query):
    admin, owner, user = make_account(admin=True), make_account(), make_account()
    make_app(owner)
    theirs = make_app(admin)
    token = _sign_in_to(web, theirs, user, login)["access_token"]
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import accounts

        accounts.delete(user["id"])
        accounts.delete(owner["id"])
    assert _userinfo(portal, token).status_code == 401
    assert query("SELECT id FROM hosting_oauth_apps") == [{"id": theirs["id"]}]
    assert _count(query, "hosting_oauth_app_grants") == 0


def test_a_merge_moves_apps_and_widens_consent(portal, web, make_app, make_account, login, query):
    app = make_app(make_account(admin=True))
    source = make_account("source", email="s@example.org", email_verified_at=VERIFIED)
    target = make_account("target")
    own = make_app(source, "Source app")
    login(web, source)
    _allow(web, app, scope="profile email")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        oauth_apps.merge_accounts(source["id"], target["id"])
    owner = query("SELECT account_id FROM hosting_oauth_apps WHERE id = ?", (own["id"],), one=True)
    assert owner == {"account_id": target["id"]}
    assert query("SELECT account_id, scope FROM hosting_oauth_app_grants") == [
        {"account_id": target["id"], "scope": "profile email"}]


def test_merging_keeps_the_widest_consent_of_the_two(portal, make_app, make_account, query):
    app = make_app(make_account(admin=True))
    source, target = make_account(), make_account()
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth_apps

        oauth_apps.remember_consent(source["id"], app["id"], "profile email")
        oauth_apps.remember_consent(target["id"], app["id"], "profile")
        oauth_apps.merge_accounts(source["id"], target["id"])
    assert query("SELECT account_id, scope FROM hosting_oauth_app_grants") == [
        {"account_id": target["id"], "scope": "profile email"}]
