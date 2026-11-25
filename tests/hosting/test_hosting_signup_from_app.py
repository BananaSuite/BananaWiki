"""Sign-up from a wiki or an app (``prompt=create``): the portal's own sign-up form and rules, then the
authorize request continues; the pages a new account may have to pass first lead back to the wiki or app;
the OAuth metadata says whether sign-up is open."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from bananawiki.hosting import email
from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD

VERIFIER = "c" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def wiki(portal, make_account, make_wiki, query):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    make_account("chief", admin=True)
    inst = make_wiki(make_account(), "garden")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import oauth, urls

        client_id, _secret = oauth.rotate_credentials(inst["id"])
        redirect_uri = urls.instance_url(inst) + "/platform-oauth/callback"
        signin = oauth.signin_url({**inst, "oauth_client_id": client_id})
    return {"id": client_id, "redirect_uri": redirect_uri, "signin": signin}


def _url(wiki, **extra) -> str:
    return "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": wiki["id"], "redirect_uri": wiki["redirect_uri"], "state": "st",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", **extra})


def _settings(query, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    query(f"UPDATE hosting_settings SET {assignments} WHERE id = 1", tuple(values.values()))


def _to_signup(web, wiki) -> str:
    """Start prompt=create without a portal session: the sign-up page's address."""
    response = web.get(_url(wiki, prompt="create"))
    assert response.status_code == 302
    location = response.headers["Location"]
    assert urlparse(location).path == "/signup"
    return location


def _sign_up(web, location: str, username: str = "newbie", **extra):
    target = parse_qs(urlparse(location).query)["next"][0]
    data = {"username": username, "password": PASSWORD, "confirm_password": PASSWORD, "accept_terms": "1",
            "next": target, **extra}
    return web.post("/signup", data=data), target


def test_prompt_create_shows_the_sign_up_form_naming_the_wiki(web, wiki):
    location = _to_signup(web, wiki)
    target = parse_qs(urlparse(location).query)["next"][0]
    assert "prompt" not in parse_qs(urlparse(target).query), "the request continues without prompt=create"
    page = web.get(location).get_data(as_text=True)
    assert 'action="/signup"' in page and 'name="confirm_password"' in page
    assert "After this, you continue to garden." in page
    assert f'name="next" value="{target}"'.replace("&", "&amp;") in page
    assert "/login?next=" in page, "Already have an account? keeps the request"


def test_the_sign_up_continues_the_authorize_request(web, wiki, query):
    response, target = _sign_up(web, _to_signup(web, wiki))
    assert response.status_code == 302 and response.headers["Location"].endswith(target)
    consent = web.get(target)
    assert consent.status_code == 200 and "newbie" in consent.get_data(as_text=True)
    allowed = web.post(target, data={"action": "allow"})
    assert "code=" in allowed.headers["Location"] and allowed.headers["Location"].startswith(wiki["redirect_uri"])
    assert query("SELECT is_admin FROM accounts WHERE username = 'newbie'", one=True)["is_admin"] == 0


def test_prompt_create_with_a_portal_session_is_a_normal_authorize(web, wiki, make_account, login):
    login(web, make_account("already"))
    response = web.get(_url(wiki, prompt="create"))
    assert response.status_code == 200 and "already" in response.get_data(as_text=True)


def test_invite_mode_asks_for_the_code(web, wiki, query):
    _settings(query, signup_mode="invite")
    admin = query("SELECT id FROM accounts WHERE username = 'chief'", one=True)
    query("INSERT INTO hosting_invite_codes (code, created_by, max_uses, current_uses, created_at) "
          "VALUES ('WELCOME1', ?, 1, 0, '2026-01-01 00:00:00')", (admin["id"],))
    location = _to_signup(web, wiki)
    assert 'name="invite_code"' in web.get(location).get_data(as_text=True)
    assert _sign_up(web, location)[0].status_code == 400
    response, target = _sign_up(web, location, invite_code="WELCOME1")
    assert response.status_code == 302 and response.headers["Location"].endswith(target)


def test_closed_sign_up_shows_the_sign_in_form_with_a_note(web, wiki, query):
    _settings(query, signup_mode="closed")
    location = _to_signup(web, wiki)
    page = web.get(location).get_data(as_text=True)
    assert "Sign-ups are closed." in page and 'action="/login"' in page and 'name="next"' in page
    assert "Create one" not in page
    assert _sign_up(web, location)[0].status_code == 403
    assert query("SELECT 1 FROM accounts WHERE username = 'newbie'") == []


def test_the_portal_sign_in_page_links_to_sign_up_while_it_is_open(web, query):
    assert 'href="/signup"' in web.get("/login").get_data(as_text=True)
    assert 'href="/signup?next=/dashboard"' in web.get("/login?next=/dashboard").get_data(as_text=True)
    _settings(query, signup_mode="invite")
    assert 'href="/signup"' in web.get("/login").get_data(as_text=True)
    _settings(query, signup_mode="closed")
    assert "/signup" not in web.get("/login").get_data(as_text=True)


def test_a_required_address_is_required(web, wiki, query):
    _settings(query, signup_email="required")
    location = _to_signup(web, wiki)
    assert 'name="email"' in web.get(location).get_data(as_text=True)
    assert _sign_up(web, location)[0].status_code == 400
    assert query("SELECT 1 FROM accounts WHERE username = 'newbie'") == []


def test_a_new_account_waiting_for_approval_is_shown_the_way_back(web, wiki, query):
    _settings(query, hosting_activation_required=1, signup_use_case_required=1)
    location = _to_signup(web, wiki)
    assert 'name="signup_use_case"' in web.get(location).get_data(as_text=True)
    assert _sign_up(web, location)[0].status_code == 400, "the intended use is asked for"
    response, target = _sign_up(web, location, signup_use_case="Notes for our gardening club, weekly.")
    assert response.headers["Location"].endswith("/activation-pending")
    page = web.get("/activation-pending").get_data(as_text=True)
    assert "Waiting for approval" in page
    assert f'When that is done, go back to <a href="{wiki["signin"]}">garden</a>.' in page
    held = web.get(target)
    assert held.status_code == 302 and held.headers["Location"].endswith("/activation-pending"), \
        "the wiki gets nothing before the approval"


def test_an_address_confirmed_in_the_same_browser_continues_the_request(web, wiki, query):
    _settings(query, signup_email="required", email_verification_required=1)
    with email.capture_outbox() as outbox:
        response, target = _sign_up(web, _to_signup(web, wiki), email="newbie@example.org")
    assert response.headers["Location"].endswith("/account/verify-email")
    page = web.get("/account/verify-email").get_data(as_text=True)
    assert f'go back to <a href="{wiki["signin"]}">garden</a>' in page
    link = outbox[-1].action_url
    confirmed = web.get(link[link.index("/verify-email"):])
    assert confirmed.status_code == 302 and confirmed.headers["Location"].endswith(target)


def test_an_app_is_named_and_linked_from_its_registration(web, make_app, make_account, query):
    _settings(query, hosting_activation_required=1)
    app = make_app(make_account("owner", admin=True), "BananaChat", homepage="https://chat.example.org/")
    url = "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": app["client_id"], "redirect_uri": app["redirect_uris"][0],
        "state": "st", "code_challenge": CHALLENGE, "code_challenge_method": "S256", "prompt": "create",
        "homepage": "https://evil.example/"})
    location = web.get(url).headers["Location"]
    assert "After this, you continue to BananaChat." in web.get(location).get_data(as_text=True)
    _sign_up(web, location)
    page = web.get("/activation-pending").get_data(as_text=True)
    assert 'go back to <a href="https://chat.example.org/">BananaChat</a>' in page and "evil" not in page


def test_the_metadata_says_whether_sign_up_is_open(web, query):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    data = web.get("/.well-known/oauth-authorization-server").get_json()
    assert "create" in data["prompt_values_supported"] and data["signup"] == "open"
    _settings(query, signup_mode="invite")
    data = web.get("/.well-known/oauth-authorization-server").get_json()
    assert "create" in data["prompt_values_supported"] and data["signup"] == "invite"
    _settings(query, signup_mode="closed")
    data = web.get("/.well-known/oauth-authorization-server").get_json()
    assert "create" not in data["prompt_values_supported"] and data["signup"] == "closed"
