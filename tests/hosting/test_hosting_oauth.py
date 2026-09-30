"""The OAuth provider hosted wikis use for "sign in with your hosting account"."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

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
