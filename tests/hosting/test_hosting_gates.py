"""Account states that hold a signed-in account on one page, and where they lead.

A portal account can be in several blocking states at once: scheduled for
deletion, a flagged email address, a missing contact address, an unverified
address, approval pending or denied, an administrator without the required
two-step verification, terms it has not accepted yet. A suspended account is
signed out instead and sees the suspension page. (The portal has no "change
your password" state: an administrator who sets a password just sets it.)

Whatever the combination, the account must land on one page that resolves
the first state in ``auth.GATES`` order, that page must work (for POST too),
and no combination may bounce between two pages ("too many redirects"):
every combination is checked against every page of the portal in memory,
and the combinations most likely to loop are walked through over HTTP.
Leaving (signing out, deleting the account) and revoking credentials (API
tokens, wiki sign-in tokens) always work.
"""

from __future__ import annotations

import itertools
from urllib.parse import urlsplit

import pytest
from flask import url_for

from bananawiki.hosting import accounts, auth, email
from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, build_portal, portal_environ

EMAIL_STATES = ("verified", "unverified", "missing", "flagged")
APPROVAL_STATES = ("approved", "pending", "denied")
PAGES = {
    "deletion": "/pending-deletion", "email_flag": "/account/email-flagged",
    "contact_email": "/account/contact-email", "verification": "/account/verify-email",
    "approval:pending": "/activation-pending", "approval:denied": "/activation-denied",
    "admin_mfa": "/account/mfa", "terms": "/account/terms",
}
combinations = itertools.count(1)


@pytest.fixture(scope="module")
def strict(tmp_path_factory):
    """Every gate switched on: email required and verified, two-step for admins, new terms."""
    tmp_path = tmp_path_factory.mktemp("gates")
    portal = build_portal(tmp_path, environ=portal_environ(
        tmp_path, HOSTING_TERMS_REQUIRED_VERSION=accounts.TERMS_VERSION))
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        accounts.create("root", PASSWORD, is_admin=True)
        db.execute("UPDATE hosting_settings SET email_required = 1, ask_email_existing_users = 1, "
                   "email_verification_required = 1, admin_mfa_required = 1, api_enabled = 1, "
                   "platform_oauth_enabled = 1 WHERE id = 1")
    return portal


def _account(portal, *, mail: str, approval: str = "approved", deletion: bool = False, admin_mfa: bool = False,
             terms: bool = False, suspended: bool = False) -> dict:
    number = next(combinations)
    fields: dict = {"approval_status": approval, "is_admin": 1 if admin_mfa else 0,
                    "terms_version": "" if terms else accounts.TERMS_VERSION}
    if mail == "verified":
        fields.update(email=f"user{number}@example.org", email_verified_at="2026-01-01 00:00:00")
    elif mail == "unverified":
        fields.update(email=f"user{number}@example.org", email_claimed_at="2026-01-01 00:00:00")
    elif mail == "flagged":
        fields.update(email="", email_flagged_invalid=1, email_flagged_previous=f"old{number}@example.org")
    if deletion:
        fields.update(pending_deletion=1, pending_deletion_at="2026-01-01 00:00:00",
                      pending_deletion_seconds=10 * 365 * 86400)
    if approval == "denied":
        fields.update(denied_at="2026-01-01 00:00:00")
    if suspended:
        fields.update(suspended=1)
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        account = accounts.create(f"user{number}", PASSWORD)
        db.update("accounts", fields, "id = ?", (account["id"],))
        return accounts.get(account["id"])


def _client(portal):
    client = portal.test_client()
    number = next(combinations)
    client.environ_base["REMOTE_ADDR"] = f"10.{number // 65536 % 256}.{number // 256 % 256}.{number % 256}"
    return client


def _sign_in(portal, account: dict):
    client = _client(portal)
    response = client.post("/login", data={"username": account["username"], "password": PASSWORD})
    assert response.status_code == 302, response.status_code
    return client, urlsplit(response.headers["Location"]).path


def _walk(client, path: str) -> list[tuple[str, int]]:
    """Follow redirects from *path*; fail on a cycle."""
    seen: list[tuple[str, int]] = []
    for _step in range(12):
        response = client.get(path)
        seen.append((path, response.status_code))
        if response.status_code not in (301, 302, 303):
            return seen
        path = urlsplit(response.headers["Location"]).path
        assert path not in [visited for visited, _status in seen], f"redirect loop: {seen + [(path, 0)]}"
    raise AssertionError(f"too many redirects: {seen}")


def _expected(*, mail: str, approval: str, deletion: bool, admin_mfa: bool, terms: bool, suspended: bool) -> str:
    if suspended:
        return "/account-suspended"
    states = [
        ("deletion", deletion), ("email_flag", mail == "flagged"), ("contact_email", mail == "missing"),
        ("verification", mail == "unverified"), (f"approval:{approval}", approval != "approved"),
        ("admin_mfa", admin_mfa), ("terms", terms),
    ]
    return next((PAGES[gate] for gate, blocked in states if blocked), "/dashboard")


def test_every_gate_page_is_exempt_from_its_own_gate(strict):
    """The page a state sends an account to must never send it elsewhere for that same state."""
    endpoints = {"deletion": ["auth.pending_deletion"], "email_flag": ["account.email_flagged"],
                 "contact_email": ["account.contact_email"], "verification": ["account.verify_email_pending"],
                 "approval": ["auth.activation_pending", "auth.activation_denied"], "admin_mfa": ["account.mfa"],
                 "terms": ["account.terms"]}
    assert sorted(endpoints) == sorted(auth.GATES)
    for gate, names in endpoints.items():
        for name in names:
            assert auth.is_exempt(strict.view_functions[name], gate), (gate, name)


def _states():
    for mail, approval in itertools.product(EMAIL_STATES, APPROVAL_STATES):
        for deletion, admin_mfa, terms in itertools.product((False, True), repeat=3):
            yield {"mail": mail, "approval": approval, "deletion": deletion, "admin_mfa": admin_mfa, "terms": terms}


def _in_memory(row: dict, *, mail: str, approval: str, deletion: bool, admin_mfa: bool, terms: bool) -> dict:
    account = {**row, "approval_status": approval, "pending_deletion": int(deletion), "is_admin": int(admin_mfa),
               "totp_enabled": 0, "terms_version": "" if terms else accounts.TERMS_VERSION,
               "email": "", "email_verified_at": None, "email_flagged_invalid": 0}
    if mail in ("verified", "unverified"):
        account.update(email="someone@example.org",
                       email_verified_at="2026-01-01 00:00:00" if mail == "verified" else None)
    elif mail == "flagged":
        account.update(email_flagged_invalid=1)
    return account


def test_no_combination_of_states_sends_any_page_round_in_circles(strict):
    """Every combination of states, from every page: the page an account is sent to keeps it."""
    pages = {endpoint: view for endpoint, view in strict.view_functions.items() if endpoint != "static"}
    with strict.test_request_context("/"), connection_scope(strict.extensions["bananawiki.hosting.database"]) as db:
        row = db.one("SELECT * FROM accounts WHERE username = 'root'")
        with email.capture_outbox():  # the portal can send email, so verification is enforced
            for state in _states():
                account = _in_memory(row, **state)
                expected = _expected(**state, suspended=False)
                for endpoint, view in pages.items():
                    target = auth.pending_gate(account, view)
                    if target is None:
                        continue
                    assert url_for(target) == expected, (state, endpoint)
                    assert auth.pending_gate(account, strict.view_functions[target]) is None, (state, endpoint)


@pytest.mark.parametrize("state", [
    # Combinations most likely to loop, then one of each state on its own.
    {"mail": "missing", "admin_mfa": True}, {"mail": "unverified", "admin_mfa": True},
    {"mail": "flagged", "admin_mfa": True}, {"mail": "missing", "terms": True}, {"mail": "unverified", "terms": True},
    {"mail": "flagged", "terms": True}, {"mail": "verified", "admin_mfa": True, "terms": True},
    {"mail": "verified", "deletion": True, "terms": True}, {"mail": "verified", "approval": "pending", "terms": True},
    {"mail": "verified", "approval": "denied", "admin_mfa": True},
    {"mail": "unverified", "approval": "pending", "admin_mfa": True, "terms": True},
    {"mail": "unverified", "suspended": True, "terms": True}, {"mail": "verified", "suspended": True},
])
def test_signing_in_lands_on_one_page_that_works(strict, state):
    state = {"mail": "verified", "approval": "approved", "deletion": False, "admin_mfa": False, "terms": False,
             "suspended": False, **state}
    account = _account(strict, **state)
    expected = _expected(**state)
    with email.capture_outbox():
        client, first = _sign_in(strict, account)
        walked = _walk(client, first)
        assert walked[-1] == (expected, 403 if state["suspended"] else 200), walked
        if not state["suspended"]:
            assert _walk(client, "/dashboard")[-1] == walked[-1]
            # The page works for every method: the portal lets a POST through to it.
            landed = client.post(expected, data={})
            target = urlsplit(landed.headers.get("Location") or "").path
            assert target not in set(PAGES.values()) - {expected}, (expected, target)


@pytest.mark.parametrize("state", [
    {"mail": "unverified", "terms": True}, {"mail": "flagged", "terms": True}, {"mail": "missing", "terms": True},
    {"mail": "unverified", "admin_mfa": True}, {"mail": "missing", "admin_mfa": True},
    {"mail": "flagged", "admin_mfa": True}, {"mail": "verified", "admin_mfa": True, "terms": True},
])
def test_each_state_can_be_resolved_in_turn(strict, state):
    """Resolving the first state moves the account on to the next one, until the dashboard."""
    account = _account(strict, **state)
    with email.capture_outbox() as outbox:
        client, first = _sign_in(strict, account)
        for _step in range(4):
            page = _walk(client, first)[-1][0]
            if page == "/dashboard":
                break
            if page == "/account/email-flagged":
                assert client.post(page, data={"email": f"new-{account['id']}@example.org",
                                               "current_password": PASSWORD}).status_code == 302
            elif page == "/account/contact-email":
                assert client.post(page, data={"email": f"set-{account['id']}@example.org",
                                               "current_password": PASSWORD}).status_code == 302
            elif page == "/account/verify-email":
                assert client.post("/account/resend-verification").headers["Location"].endswith(page)
                link = outbox[-1].action_url
                assert client.get(urlsplit(link).path + "?" + urlsplit(link).query).status_code == 302
            elif page == "/account/mfa":
                with strict.app_context(), connection_scope(strict.extensions["bananawiki.hosting.database"]) as db:
                    db.execute("UPDATE accounts SET totp_enabled = 1 WHERE id = ?", (account["id"],))
            elif page == "/account/terms":
                assert client.post(page, data={"accept_terms": "1"}).status_code == 302
            else:
                raise AssertionError(page)
            first = "/dashboard"
        assert _walk(client, "/dashboard")[-1] == ("/dashboard", 200), state


@pytest.mark.parametrize("state", [
    {"mail": "unverified", "terms": True}, {"mail": "flagged"}, {"mail": "missing", "admin_mfa": True},
    {"mail": "verified", "approval": "pending", "terms": True}, {"mail": "verified", "deletion": True},
])
def test_a_blocked_account_can_always_sign_out_or_delete_itself(strict, state):
    account = _account(strict, **state)
    with email.capture_outbox():
        client, _first = _sign_in(strict, account)
        assert urlsplit(client.post("/logout").headers["Location"]).path == "/login"
        client, _first = _sign_in(strict, account)
        deleted = client.post("/account/delete", data={"current_password": PASSWORD})
    assert urlsplit(deleted.headers["Location"]).path == "/login"
    with strict.app_context(), connection_scope(strict.extensions["bananawiki.hosting.database"]) as db:
        assert db.scalar("SELECT deleted_at FROM accounts WHERE id = ?", (account["id"],))


# Credentials outside the portal: API tokens and wiki sign-in tokens


def _api_token(portal, account: dict) -> tuple[int, str]:
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import api_tokens

        return api_tokens.create(account, "script", ["account:read", "instances:read"], None,
                                 int(account["session_version"] or 0))


def _oauth_token(portal, account: dict) -> str:
    """An access token a hosted wiki holds for *account* (as if it had signed in there)."""
    from bananawiki.core.crypto import sha256_hex
    from bananawiki.core.timeutil import now_sql, sql_in
    from bananawiki.hosting import instances, oauth

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        owner = accounts.create(f"owner{next(combinations)}", PASSWORD)
        wiki = instances.create(owner, f"wiki{next(combinations)}")[0]
        client_id, _secret = oauth.rotate_credentials(wiki["id"])
        raw = f"oauth-token-{next(combinations)}"
        db.insert("hosting_oauth_access_tokens", {"token_hash": sha256_hex(raw), "client_id": client_id,
                                                  "account_id": account["id"], "scope": oauth.SCOPE,
                                                  "expires_at": sql_in(seconds=3600), "created_at": now_sql()})
    return raw


def test_api_tokens_stop_working_until_the_new_terms_are_accepted(strict):
    account = _account(strict, mail="verified", terms=True)
    _token_id, raw = _api_token(strict, account)
    api = strict.test_client()
    refused = api.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"})
    assert refused.status_code == 403 and "terms" in refused.get_json()["error"]
    assert api.get("/api/v1/instances", headers={"Authorization": f"Bearer {raw}"}).status_code == 403
    with email.capture_outbox():
        client, _first = _sign_in(strict, account)
        assert client.post("/account/terms", data={"accept_terms": "1"}).status_code == 302
    assert api.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


def test_wiki_sign_in_tokens_stop_working_until_the_new_terms_are_accepted(strict):
    account = _account(strict, mail="verified", terms=True)
    raw = _oauth_token(strict, account)
    wiki = strict.test_client()
    for path in ("/oauth/userinfo", "/oauth/verify"):
        refused = wiki.get(path, headers={"Authorization": f"Bearer {raw}"})
        assert refused.status_code == 403 and refused.get_json()["error"] == "terms_not_accepted", path
    with email.capture_outbox():
        client, _first = _sign_in(strict, account)
        assert client.post("/account/terms", data={"accept_terms": "1"}).status_code == 302
    assert wiki.get("/oauth/userinfo", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


@pytest.mark.parametrize("state", [
    {"mail": "verified", "terms": True}, {"mail": "flagged"}, {"mail": "unverified"}, {"mail": "missing"},
    {"mail": "verified", "approval": "pending"}, {"mail": "verified", "admin_mfa": True},
    {"mail": "verified", "deletion": True},
])
def test_a_blocked_account_can_still_revoke_its_tokens(strict, state):
    """Revoking only takes access away, so no account state may stand in the way."""
    account = _account(strict, **state)
    token_id, _raw = _api_token(strict, account)
    _oauth_token(strict, account)
    with email.capture_outbox():
        client, _first = _sign_in(strict, account)
        revoked = client.post(f"/account/api-tokens/{token_id}/revoke")
        assert urlsplit(revoked.headers["Location"]).path == "/account", state
        signed_out = client.post("/oauth/revoke")
        assert urlsplit(signed_out.headers["Location"]).path == "/account", state
    with strict.app_context(), connection_scope(strict.extensions["bananawiki.hosting.database"]) as db:
        assert db.scalar("SELECT revoked_at FROM hosting_api_tokens WHERE id = ?", (token_id,)), state
        assert not db.scalar("SELECT 1 FROM hosting_oauth_access_tokens WHERE account_id = ?", (account["id"],))
