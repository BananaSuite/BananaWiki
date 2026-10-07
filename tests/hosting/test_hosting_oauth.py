"""The OAuth provider hosted wikis use for "sign in with your hosting account"."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from bananawiki.hosting.db import connection_scope

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def wiki_client(portal, make_account, make_wiki, query):
    """A wiki with OAuth credentials, plus the wiki's redirect URI."""
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    owner = make_account()
    wiki = make_wiki(owner, "signin")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth, urls

        client_id, secret = oauth.rotate_credentials(wiki["id"])
        redirect_uri = urls.instance_url(wiki) + "/auth/platform/callback"
    return {"id": client_id, "secret": secret, "redirect_uri": redirect_uri, "wiki": wiki}


def _authorize(client, wiki, **extra):
    params = {"response_type": "code", "client_id": wiki["id"], "redirect_uri": wiki["redirect_uri"], "state": "xyz",
              "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    params.update(extra)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.post(f"/oauth/authorize?{query}", data={"action": "allow"})
    assert response.status_code == 302, response.status_code
    return parse_qs(urlparse(response.headers["Location"]).query)


def _token(client, wiki, code, verifier=VERIFIER, secret=None):
    auth = base64.b64encode(f"{wiki['id']}:{secret or wiki['secret']}".encode()).decode()
    return client.post("/oauth/token", headers={"Authorization": f"Basic {auth}"}, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": wiki["redirect_uri"],
        "code_verifier": verifier})


def test_endpoints_are_hidden_while_platform_sign_in_is_off(web):
    assert web.get("/oauth/authorize").status_code == 404
    assert web.post("/oauth/token").status_code == 404


def test_full_flow_with_pkce(web, make_account, login, wiki_client):
    user = make_account()
    login(web, user)
    result = _authorize(web, wiki_client)
    assert result["state"] == ["xyz"]
    response = _token(web, wiki_client, result["code"][0])
    assert response.status_code == 200
    token = response.get_json()["access_token"]
    info = web.get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert info["sub"] == user["id"] and info["username"] == user["username"]
    assert web.get("/oauth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_code_works_once_and_needs_the_right_verifier(web, make_account, login, wiki_client):
    login(web, make_account())
    code = _authorize(web, wiki_client)["code"][0]
    assert _token(web, wiki_client, code, verifier="w" * 64).status_code == 400
    code = _authorize(web, wiki_client)["code"][0]
    assert _token(web, wiki_client, code).status_code == 200
    assert _token(web, wiki_client, code).status_code == 400


def test_oauth_code_with_unicode_redirect_path_can_be_redeemed(web, make_account, login, wiki_client):
    login(web, make_account())
    wiki = {**wiki_client, "redirect_uri": wiki_client["redirect_uri"] + "/caffè"}
    code = _authorize(web, wiki)["code"][0]
    assert _token(web, wiki, code).status_code == 200


@pytest.mark.parametrize("verifier", ["é" * 64, "!" * 64])
def test_pkce_rejects_invalid_character_sets(verifier):
    from bananawiki.hosting import oauth

    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "replace")).digest()).decode().rstrip("=")
    assert oauth.pkce_matches(challenge, verifier) is False


def test_authorization_rejects_unicode_pkce_challenge(web, make_account, login, wiki_client):
    login(web, make_account())
    params = {"response_type": "code", "client_id": wiki_client["id"],
              "redirect_uri": wiki_client["redirect_uri"], "code_challenge": "é" * 43,
              "code_challenge_method": "S256"}
    assert web.get("/oauth/authorize?" + urlencode(params)).status_code == 400


def test_replayed_code_revokes_the_token_it_produced(web, make_account, login, wiki_client):
    login(web, make_account())
    code = _authorize(web, wiki_client)["code"][0]
    token = _token(web, wiki_client, code).get_json()["access_token"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert web.get("/oauth/userinfo", headers=bearer).status_code == 200
    assert _token(web, wiki_client, code).status_code == 400
    assert web.get("/oauth/userinfo", headers=bearer).status_code == 401


def test_token_needs_the_client_secret(web, make_account, login, wiki_client):
    login(web, make_account())
    code = _authorize(web, wiki_client)["code"][0]
    assert _token(web, wiki_client, code, secret="wrong").status_code == 401


def test_foreign_redirect_uri_is_refused(web, make_account, login, wiki_client):
    login(web, make_account())
    response = web.get(f"/oauth/authorize?response_type=code&client_id={wiki_client['id']}"
                       "&redirect_uri=https://evil.example/cb")
    assert response.status_code == 400


def _form_action(response):
    """The sources of the response's CSP ``form-action`` directive."""
    for directive in response.headers["Content-Security-Policy"].split(";"):
        name, _, sources = directive.strip().partition(" ")
        if name == "form-action":
            return sources
    return None


def _consent_page(client, redirect_uri, client_id):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": "xyz",
              "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    return client.get("/oauth/authorize?" + urlencode(params))


def test_consent_page_lets_its_form_redirect_to_the_wiki_only(web, make_account, login, wiki_client):
    # Chromium checks the redirect that answers the consent form against this page's form-action.
    login(web, make_account())
    page = _consent_page(web, wiki_client["redirect_uri"], wiki_client["id"])
    assert page.status_code == 200
    wiki = urlparse(wiki_client["redirect_uri"])
    assert _form_action(page) == f"'self' {wiki.scheme}://{wiki.netloc}"
    refused = _consent_page(web, "https://evil.example/cb", wiki_client["id"])
    assert refused.status_code == 400 and _form_action(refused) == "'self'"
    assert _form_action(web.get("/dashboard")) == "'self'"
    assert _form_action(web.get("/account")) == "'self'"


def test_consent_page_allows_the_verified_custom_domain_it_answers_to(web, make_account, login, wiki_client, query):
    query("INSERT INTO instance_custom_domains (domain, instance_id, verification_token, created_at, verified_at, "
          "verified_until) VALUES ('docs.example.org', ?, 'token', '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
          "'2999-01-01 00:00:00')", (wiki_client["wiki"]["id"],))
    login(web, make_account())
    page = _consent_page(web, "https://docs.example.org/platform-oauth/callback", wiki_client["id"])
    assert page.status_code == 200
    assert _form_action(page) == "'self' https://docs.example.org"


def test_denying_consent_returns_an_error_to_the_wiki(web, make_account, login, wiki_client):
    login(web, make_account())
    response = web.post(f"/oauth/authorize?response_type=code&client_id={wiki_client['id']}"
                        f"&redirect_uri={wiki_client['redirect_uri']}&state=s1", data={"action": "deny"})
    assert parse_qs(urlparse(response.headers["Location"]).query) == {"error": ["access_denied"], "state": ["s1"]}


def test_suspended_accounts_lose_access(portal, web, make_account, login, wiki_client, query):
    user = make_account()
    login(web, user)
    token = _token(web, wiki_client, _authorize(web, wiki_client)["code"][0]).get_json()["access_token"]
    query("UPDATE accounts SET suspended = 1 WHERE id = ?", (user["id"],))
    assert portal.test_client().get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"}).status_code == 403


@pytest.mark.parametrize("fields", [
    {"suspended": 1}, {"pending_deletion": 1}, {"approval_status": "denied"},
])
def test_ineligible_accounts_cannot_verify_or_create_oauth_links(portal, web, make_account, login, wiki_client,
                                                             query, fields):
    user = make_account()
    login(web, user)
    token = _token(web, wiki_client, _authorize(web, wiki_client)["code"][0]).get_json()["access_token"]
    column, value = next(iter(fields.items()))
    query(f"UPDATE accounts SET {column} = ? WHERE id = ?", (value, user["id"]))
    client = portal.test_client()  # Wiki requests carry credentials, never a portal cookie.
    verify = client.get("/oauth/verify", headers={"Authorization": f"Bearer {token}"})
    assert verify.status_code == 403
    basic = base64.b64encode(f"{wiki_client['id']}:{wiki_client['secret']}".encode()).decode()
    linked = client.post("/oauth/link", headers={"Authorization": f"Basic {basic}"},
                         json={"account_id": user["id"], "wiki_user_id": "new-link"})
    assert linked.status_code == 403


def test_password_reset_revokes_oauth_tokens_and_unused_codes(portal, web, make_account, login, wiki_client):
    user = make_account()
    login(web, user)
    token = _token(web, wiki_client, _authorize(web, wiki_client)["code"][0]).get_json()["access_token"]
    unused_code = _authorize(web, wiki_client)["code"][0]
    with portal.app_context(), connection_scope():
        from bananawiki.hosting import accounts

        accounts.set_password(user["id"], "replacement password 42", actor_id=user["id"], reason="password reset")
    assert web.get("/oauth/verify", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert _token(web, wiki_client, unused_code).status_code == 400


def test_rotating_credentials_revokes_issued_tokens(portal, web, make_account, login, wiki_client):
    login(web, make_account())
    token = _token(web, wiki_client, _authorize(web, wiki_client)["code"][0]).get_json()["access_token"]
    with portal.app_context(), connection_scope():
        from bananawiki.hosting import oauth

        oauth.rotate_credentials(wiki_client["wiki"]["id"])
    assert web.get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_link_status_requires_the_wiki_credentials(web, wiki_client):
    response = web.get(f"/oauth/link-status?instance_id={wiki_client['wiki']['id']}&wiki_user_id=1")
    assert response.status_code == 401
    auth = base64.b64encode(f"{wiki_client['id']}:{wiki_client['secret']}".encode()).decode()
    response = web.get("/oauth/link-status?wiki_user_id=1", headers={"Authorization": f"Basic {auth}"})
    assert response.get_json() == {"linked": False}


def test_link_only_for_accounts_that_signed_in_to_that_wiki(web, make_account, login, wiki_client):
    stranger, user = make_account(), make_account()
    auth = {"Authorization": "Basic " + base64.b64encode(f"{wiki_client['id']}:{wiki_client['secret']}".encode()).decode()}
    response = web.post("/oauth/link", headers=auth, json={"account_id": stranger["id"], "wiki_user_id": "7"})
    assert response.status_code == 403
    login(web, user)
    _token(web, wiki_client, _authorize(web, wiki_client)["code"][0])
    response = web.post("/oauth/link", headers=auth, json={"account_id": user["id"], "wiki_user_id": "7",
                                                           "wiki_username": "wikiuser"})
    assert response.get_json() == {"ok": True}
    status = web.get("/oauth/link-status?wiki_user_id=7", headers=auth).get_json()
    assert status == {"linked": True, "hosting_account_id": user["id"], "hosting_username": "wikiuser"}
    web.post("/oauth/unlink", headers=auth, json={"account_id": user["id"]})
    assert web.get("/oauth/link-status?wiki_user_id=7", headers=auth).get_json() == {"linked": False}


def test_consent_post_is_csrf_protected(tmp_path, make_account):
    from .hosting_support import build_portal

    app = build_portal(tmp_path / "csrf", csrf=True)
    with app.app_context(), connection_scope():
        from bananawiki.hosting import accounts, instances, oauth, settings

        settings.update(platform_oauth_enabled=1)
        user = accounts.create("consent", "correct horse 42")
    with app.test_request_context("/"), connection_scope():
        wiki = instances.create(user, "csrfwiki")[0]
        client_id, _secret = oauth.rotate_credentials(wiki["id"])
        from bananawiki.hosting import urls

        redirect_uri = urls.instance_url(wiki) + "/cb"
    client = app.test_client()
    from .hosting_support import csrf_token

    signed_in = client.post("/login", data={"username": "consent", "password": "correct horse 42",
                                            "csrf_token": csrf_token(client)})
    assert signed_in.headers["Location"].endswith("/dashboard")
    response = client.post(f"/oauth/authorize?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}",
                           data={"action": "allow"})
    assert response.status_code == 302 and not response.headers["Location"].startswith(redirect_uri)
    with app.app_context(), connection_scope() as session:
        assert session.scalar("SELECT COUNT(*) FROM hosting_oauth_authorization_codes") == 0
