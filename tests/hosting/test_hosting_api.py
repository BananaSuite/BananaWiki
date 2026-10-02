"""REST API (/api/v1) with personal access tokens (``bwh_…``) and scopes."""

from __future__ import annotations

import re

import pytest

from .hosting_support import PASSWORD


@pytest.fixture
def api_on(query):
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")


def _new_token(client, scopes: list[str], name: str = "script") -> str:
    html = client.get("/account").get_data(as_text=True)
    nonce = re.search(r'name="form_nonce" value="([^"]+)"', html).group(1)
    response = client.post("/account/api-tokens", data={
        "form_nonce": nonce, "current_password": PASSWORD, "name": name, "expires_in": "30", "scopes": scopes})
    match = re.search(r"bwh_[A-Za-z0-9_-]{40}", response.get_data(as_text=True))
    assert match, "token not shown"
    return match.group(0)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_api_is_hidden_while_disabled(web):
    assert web.get("/api/v1/status").status_code == 404


def test_token_is_stored_as_a_digest_and_shown_once(web, make_account, login, query, api_on):
    login(web, make_account())
    token = _new_token(web, ["account:read"])
    stored = query("SELECT * FROM hosting_api_tokens", one=True)
    assert token not in str(stored)
    assert token not in web.get("/account").get_data(as_text=True)


def test_me_and_scopes(portal, web, make_account, login, api_on):
    user = make_account()
    login(web, user)
    token = _new_token(web, ["account:read"])
    api = portal.test_client()
    me = api.get("/api/v1/me", headers=_bearer(token))
    assert me.status_code == 200 and me.get_json()["account"]["username"] == user["username"]
    assert api.get("/api/v1/instances", headers=_bearer(token)).status_code == 403
    assert api.get("/api/v1/me").status_code == 401
    assert api.get("/api/v1/me", headers=_bearer("bwh_" + "x" * 40)).status_code == 401


def test_instances_are_limited_to_the_tokens_account(portal, web, make_account, make_wiki, login, runtime, api_on):
    user, other = make_account(), make_account()
    mine = make_wiki(user, "mine")
    theirs = make_wiki(other, "theirs")
    login(web, user)
    token = _new_token(web, ["instances:read", "instances:manage"])
    api = portal.test_client()
    listing = api.get("/api/v1/instances", headers=_bearer(token)).get_json()
    assert [item["subdomain"] for item in listing["instances"]] == ["mine"]
    assert api.get(f"/api/v1/instances/{theirs['id']}", headers=_bearer(token)).status_code == 404
    assert api.post(f"/api/v1/instances/{theirs['id']}/pause", headers=_bearer(token)).status_code == 404
    assert runtime.called("stop") == []
    paused = api.post(f"/api/v1/instances/{mine['id']}/pause", headers=_bearer(token))
    assert paused.status_code == 200 and paused.get_json()["instance"]["status"] == "stopped"
    assert api.post(f"/api/v1/instances/{mine['id']}/pause", headers=_bearer(token)).status_code == 409
    assert api.post(f"/api/v1/instances/{mine['id']}/resume", headers=_bearer(token)).status_code == 200


def test_admin_scope_needs_an_admin_account(portal, web, make_account, login, api_on):
    user = make_account()
    login(web, user)
    web_page = web.get("/account").get_data(as_text=True)
    assert 'value="admin:read"' not in web_page
    html_nonce = re.search(r'name="form_nonce" value="([^"]+)"', web_page).group(1)
    web.post("/account/api-tokens", data={"form_nonce": html_nonce, "current_password": PASSWORD, "name": "x",
                                          "expires_in": "30", "scopes": ["admin:read"]})
    admin = make_account(admin=True)
    admin_client = portal.test_client()
    login(admin_client, admin)
    token = _new_token(admin_client, ["admin:read"])
    assert portal.test_client().get("/api/v1/admin/instances", headers=_bearer(token)).status_code == 200


def test_revoked_tokens_and_password_changes_end_access(portal, web, make_account, login, query, api_on):
    user = make_account()
    login(web, user)
    token = _new_token(web, ["account:read"])
    token_id = query("SELECT id FROM hosting_api_tokens", one=True)["id"]
    web.post(f"/account/api-tokens/{token_id}/revoke")
    assert portal.test_client().get("/api/v1/me", headers=_bearer(token)).status_code == 401

    second = _new_token(web, ["account:read"], name="second")
    web.post("/account/change-password", data={"current_password": PASSWORD, "new_password": "fresh pass 11",
                                               "confirm_new_password": "fresh pass 11"})
    assert portal.test_client().get("/api/v1/me", headers=_bearer(second)).status_code == 401


def test_suspended_accounts_are_refused(portal, web, make_account, login, query, api_on):
    user = make_account()
    login(web, user)
    token = _new_token(web, ["account:read"])
    query("UPDATE accounts SET suspended = 1 WHERE id = ?", (user["id"],))
    assert portal.test_client().get("/api/v1/me", headers=_bearer(token)).status_code == 403


def test_nonce_prevents_double_submission(web, make_account, login, query, api_on):
    login(web, make_account())
    html = web.get("/account").get_data(as_text=True)
    nonce = re.search(r'name="form_nonce" value="([^"]+)"', html).group(1)
    data = {"form_nonce": nonce, "current_password": PASSWORD, "name": "once", "expires_in": "30",
            "scopes": ["account:read"]}
    web.post("/account/api-tokens", data=data)
    web.post("/account/api-tokens", data=data)
    assert query("SELECT COUNT(*) AS n FROM hosting_api_tokens", one=True)["n"] == 1


def test_unicode_form_nonce_is_rejected_without_creating_a_token(web, make_account, login, query, api_on):
    login(web, make_account())
    web.get("/account")
    response = web.post("/account/api-tokens", data={"form_nonce": "é", "current_password": PASSWORD,
                                                  "name": "invalid", "expires_in": "30", "scopes": "account:read"})
    assert response.status_code == 302
    assert query("SELECT COUNT(*) AS n FROM hosting_api_tokens", one=True)["n"] == 0
