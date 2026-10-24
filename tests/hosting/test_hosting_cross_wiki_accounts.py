"""One portal account across the platform's wikis: remembered consent, wiki sign-ins and single sign-out."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from bananawiki.hosting.db import connection_scope

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def owner(make_account):
    return make_account()


@pytest.fixture
def wiki(portal, owner, make_wiki, query):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    inst = make_wiki(owner, "shared")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth, urls

        client_id, secret = oauth.rotate_credentials(inst["id"])
        redirect_uri = urls.instance_url(inst) + "/platform-oauth/callback"
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"id": client_id, "secret": secret, "redirect_uri": redirect_uri, "inst": inst,
            "auth": {"Authorization": f"Basic {basic}"}}


def _params(wiki, **extra):
    return {"response_type": "code", "client_id": wiki["id"], "redirect_uri": wiki["redirect_uri"], "state": "s1",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", **extra}


def _authorize_get(client, wiki, **extra):
    return client.get("/oauth/authorize?" + urlencode(_params(wiki, **extra)))


def _allow(client, wiki):
    response = client.post("/oauth/authorize?" + urlencode(_params(wiki)), data={"action": "allow"})
    assert response.status_code == 302
    return parse_qs(urlparse(response.headers["Location"]).query)["code"][0]


def _token(client, wiki, code):
    return client.post("/oauth/token", headers=wiki["auth"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": wiki["redirect_uri"],
        "code_verifier": VERIFIER})


def _code_in(response):
    assert response.status_code == 302, response.status_code
    return parse_qs(urlparse(response.headers["Location"]).query).get("code", [None])[0]


def _signouts(client, wiki, after=0):
    return client.get(f"/oauth/signouts?after={after}", headers=wiki["auth"])


# Remembered consent


def test_consent_is_asked_once_per_wiki(portal, web, make_account, login, wiki, query):
    member = make_account()
    login(web, member)
    assert _authorize_get(web, wiki).status_code == 200, "the first time: the consent page"
    code = _allow(web, wiki)
    assert _token(web, wiki, code).status_code == 200
    grant = query("SELECT * FROM hosting_oauth_grants WHERE account_id = ?", (member["id"],), one=True)
    assert grant["instance_id"] == wiki["inst"]["id"] and grant["last_used_at"]
    again = _authorize_get(web, wiki)
    assert _code_in(again), "later sign-ins go straight back to the wiki"
    assert parse_qs(urlparse(again.headers["Location"]).query)["state"] == ["s1"]
    assert _token(web, wiki, _code_in(again)).status_code == 200
    assert _authorize_get(web, wiki, prompt="consent").status_code == 200, "the wiki may ask again"


def test_the_owner_is_never_asked_on_their_own_wiki(web, owner, login, wiki, query):
    login(web, owner)
    assert _code_in(_authorize_get(web, wiki))
    assert query("SELECT account_id FROM hosting_oauth_grants") == [{"account_id": owner["id"]}]


def test_a_collaborator_is_asked_like_anyone_else(portal, web, owner, make_account, login, wiki):
    """Collaborators are added without their consent, so being one gives none for sign-in."""
    helper = make_account()
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import collaborators

        collaborators.add(wiki["inst"], owner, helper["username"], "full_access", [])
    login(web, helper)
    assert _authorize_get(web, wiki).status_code == 200


def test_denying_remembers_nothing(web, make_account, login, wiki, query):
    login(web, make_account())
    denied = web.post("/oauth/authorize?" + urlencode(_params(wiki)), data={"action": "deny"})
    assert parse_qs(urlparse(denied.headers["Location"]).query)["error"] == ["access_denied"]
    assert query("SELECT * FROM hosting_oauth_grants") == []
    assert _authorize_get(web, wiki).status_code == 200


def test_codes_without_a_click_are_limited_per_account(web, make_account, login, wiki):
    login(web, make_account())
    _allow(web, wiki)
    from bananawiki.hosting import oauth

    for _ in range(oauth.AUTO_CODES_PER_MINUTE):
        assert _code_in(_authorize_get(web, wiki))
    assert _authorize_get(web, wiki).status_code == 200, "then the consent page again, which needs a click"


def test_impersonation_never_gets_a_code(portal, web, make_account, login, wiki):
    admin, member = make_account(admin=True), make_account()
    member_browser = portal.test_client()
    login(member_browser, member)
    _allow(member_browser, wiki)
    login(web, admin)
    web.post(f"/admin/accounts/{member['id']}/impersonate")
    assert _authorize_get(web, wiki).status_code == 403


@pytest.mark.parametrize("uri", ["https://shared-hosting.wiki.test/cb\r\nSet-Cookie: x=1",
                                 "https://shared-hosting.wiki.test/c\tb", "https://shared-hosting.wiki.test/\x00"])
def test_a_redirect_uri_with_control_characters_is_refused(web, make_account, login, wiki, query, uri):
    login(web, make_account())
    params = _params(wiki, redirect_uri=uri)
    assert web.get("/oauth/authorize?" + urlencode(params)).status_code == 400
    allowed = web.post("/oauth/authorize?" + urlencode(params), data={"action": "allow"})
    assert allowed.status_code in (302, 400) and "code=" not in allowed.headers.get("Location", "")
    assert query("SELECT * FROM hosting_oauth_authorization_codes") == []


# Wiki sign-ins on the account page


def test_the_wiki_sign_ins_page_lists_and_signs_out(web, make_account, login, wiki, query):
    member = make_account()
    login(web, member)
    _token(web, wiki, _allow(web, wiki))
    page = web.get("/account/wikis").get_data(as_text=True)
    assert "shared" in page and "/platform-oauth/login" in page
    response = web.post(f"/account/wikis/{wiki['inst']['id']}/sign-out")
    assert response.headers["Location"].endswith("/account/wikis")
    events = _signouts(web, wiki).get_json()["events"]
    assert [(event["account_id"], event["reason"]) for event in events] == [(member["id"], "signed_out")]
    assert query("SELECT * FROM hosting_oauth_access_tokens WHERE account_id = ?", (member["id"],)) == []
    assert query("SELECT 1 FROM hosting_oauth_grants WHERE account_id = ?", (member["id"],)), "consent stays"
    assert _code_in(_authorize_get(web, wiki))


def test_removing_access_asks_for_consent_again(web, make_account, login, wiki, query):
    member = make_account()
    login(web, member)
    _allow(web, wiki)
    web.post(f"/account/wikis/{wiki['inst']['id']}/remove")
    assert query("SELECT * FROM hosting_oauth_grants WHERE account_id = ?", (member["id"],)) == []
    assert query("SELECT * FROM hosting_oauth_authorization_codes WHERE account_id = ?", (member["id"],)) == []
    assert [event["reason"] for event in _signouts(web, wiki).get_json()["events"]] == ["access_removed"]
    assert _authorize_get(web, wiki).status_code == 200


def test_signing_out_of_a_wiki_the_account_never_used_changes_nothing(web, make_account, login, wiki):
    login(web, make_account())
    web.post(f"/account/wikis/{wiki['inst']['id']}/sign-out")
    assert _signouts(web, wiki).get_json()["events"] == []
    assert web.post("/account/wikis/nope/sign-out").status_code == 302


def test_sign_out_of_every_wiki(portal, web, owner, make_account, make_wiki, login, wiki):
    member = make_account()
    login(web, member)
    _token(web, wiki, _allow(web, wiki))
    response = web.post("/oauth/revoke", data={"next": "/account/wikis"})
    assert response.headers["Location"].endswith("/account/wikis")
    assert [event["reason"] for event in _signouts(web, wiki).get_json()["events"]] == ["signed_out"]


def test_the_wiki_sign_ins_page_is_hidden_while_platform_sign_in_is_off(web, make_account, login, query):
    login(web, make_account())
    assert web.get("/account/wikis").status_code == 404
    assert web.post("/account/wikis/x/sign-out").status_code == 404


def test_an_impersonating_administrator_cannot_sign_someone_out(portal, web, make_account, login, wiki):
    admin, member = make_account(admin=True), make_account()
    login(web, admin)
    web.post(f"/admin/accounts/{member['id']}/impersonate")
    assert web.get("/account/wikis").status_code == 200
    assert web.post(f"/account/wikis/{wiki['inst']['id']}/sign-out").status_code == 403
    assert web.post("/oauth/revoke").status_code == 403


def test_open_wiki_signs_in_through_the_portal(web, owner, login, wiki):
    login(web, owner)
    dashboard = web.get("/dashboard").get_data(as_text=True)
    assert wiki["redirect_uri"].replace("/callback", "/login") in dashboard


def test_open_wiki_is_the_plain_address_without_credentials(web, owner, make_wiki, login, query):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    make_wiki(owner, "fresh")
    query("UPDATE instances SET oauth_client_id = NULL")
    login(web, owner)
    assert "/platform-oauth/login" not in web.get("/dashboard").get_data(as_text=True)


# Single sign-out: the events wikis fetch


def test_signouts_need_the_wikis_credentials_and_show_only_its_own(portal, web, owner, make_account, make_wiki,
                                                                    login, wiki):
    assert web.get("/oauth/signouts").status_code == 401
    wrong = base64.b64encode(f"{wiki['id']}:nope".encode()).decode()
    assert web.get("/oauth/signouts", headers={"Authorization": f"Basic {wrong}"}).status_code == 401
    other = make_wiki(owner, "other")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth

        other_id, other_secret = oauth.rotate_credentials(other["id"])
    member = make_account()
    login(web, member)
    _token(web, wiki, _allow(web, wiki))
    web.post("/oauth/revoke")
    other_auth = base64.b64encode(f"{other_id}:{other_secret}".encode()).decode()
    assert web.get("/oauth/signouts", headers={"Authorization": f"Basic {other_auth}"}).get_json() == {
        "events": [], "latest": 0, "more": False}
    mine = _signouts(web, wiki).get_json()
    assert len(mine["events"]) == 1 and mine["latest"] == mine["events"][0]["id"]
    assert _signouts(web, wiki, after=mine["latest"]).get_json()["events"] == []


@pytest.mark.parametrize("after", ["-1", "x", "1" * 30, "١"])
def test_a_bad_cursor_is_refused(web, wiki, after):
    assert web.get(f"/oauth/signouts?after={after}", headers=wiki["auth"]).status_code == 400


def _known_to_wiki(web, login, wiki, account):
    """*account* signed in to the wiki once and the wiki linked it to its wiki account ``w-<username>``."""
    login(web, account)
    _token(web, wiki, _allow(web, wiki))
    linked = web.post("/oauth/link", headers=wiki["auth"], json={
        "account_id": account["id"], "wiki_user_id": f"w-{account['username']}", "wiki_username": account["username"]})
    assert linked.get_json() == {"ok": True}
    web.post("/logout")


def _events(web, wiki):
    return [(event["account_id"], event["wiki_user_id"], event["reason"])
            for event in _signouts(web, wiki).get_json()["events"]]


def test_a_new_password_signs_the_account_out_of_its_wikis(portal, web, make_account, login, wiki):
    member = make_account()
    _known_to_wiki(web, login, wiki, member)
    with portal.app_context(), connection_scope():
        from bananawiki.hosting import accounts

        accounts.set_password(member["id"], "another password 42", actor_id=member["id"], reason="test")
    assert _events(web, wiki) == [(member["id"], f"w-{member['username']}", "password_changed")]


def test_suspension_deletion_and_scheduled_deletion_sign_out(portal, web, make_account, login, wiki, query):
    suspended, deleted, scheduled, admin = make_account(), make_account(), make_account(), make_account(admin=True)
    for account in (suspended, deleted, scheduled):
        _known_to_wiki(web, login, wiki, account)
    with portal.app_context(), connection_scope():
        from bananawiki.hosting import accounts

        accounts.suspend(suspended["id"], until=None, reason="", reason_visible=False, time_visible=False,
                         actor_id=admin["id"], duration_label="")
        accounts.delete(deleted["id"])
        accounts.schedule_deletion(scheduled["id"], 3600, "", admin["id"])
    reasons = {account_id: reason for account_id, _user, reason in _events(web, wiki)}
    assert reasons == {suspended["id"]: "suspended", deleted["id"]: "deleted", scheduled["id"]: "deletion_scheduled"}
    assert query("SELECT * FROM hosting_oauth_grants WHERE account_id = ?", (deleted["id"],)) == []


def test_a_merge_signs_the_source_out_and_hands_its_consent_on(portal, web, make_account, login, wiki, query):
    source, target, admin = make_account(), make_account(), make_account(admin=True)
    _known_to_wiki(web, login, wiki, source)
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import merges

        merges.admin_merge(source["username"], target["username"], admin)
    assert _events(web, wiki) == [(source["id"], f"w-{source['username']}", "merged")]
    assert query("SELECT account_id FROM hosting_oauth_grants") == [{"account_id": target["id"]}]
    login(web, target)
    assert _code_in(_authorize_get(web, wiki)), "the target already allowed what the source allowed"


def test_old_signouts_are_pruned(portal, web, make_account, login, wiki, query):
    member = make_account()
    _known_to_wiki(web, login, wiki, member)
    login(web, member)
    web.post("/oauth/revoke")
    query("UPDATE hosting_oauth_signouts SET created_at = '2020-01-01 00:00:00'")
    with portal.app_context(), connection_scope():
        from bananawiki.hosting import signouts

        assert signouts.prune() == 1
    assert _signouts(web, wiki).get_json() == {"events": [], "latest": 0, "more": False}


def test_signouts_are_paged(portal, web, make_account, login, wiki, query):
    member = make_account()
    _known_to_wiki(web, login, wiki, member)
    from bananawiki.hosting import signouts

    with portal.app_context(), connection_scope():
        for _ in range(signouts.PAGE_SIZE + 5):
            signouts.queue(member["id"], "signed_out")
    first = _signouts(web, wiki).get_json()
    assert len(first["events"]) == signouts.PAGE_SIZE and first["more"] is True
    rest = _signouts(web, wiki, after=first["events"][-1]["id"]).get_json()
    assert len(rest["events"]) == 5 and rest["more"] is False


def test_the_data_export_lists_wiki_sign_ins(web, make_account, login, wiki):
    member = make_account()
    login(web, member)
    _allow(web, wiki)
    exported = web.post("/account/export").get_json()
    assert [row["subdomain"] for row in exported["wiki_sign_ins"]] == [wiki["inst"]["subdomain"]]
