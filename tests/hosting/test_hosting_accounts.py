"""Sign-up policies, sign-in, sessions, two-step sign-in and account recovery."""

from __future__ import annotations

import re

import pytest

from bananawiki.hosting import email, mfa

from .hosting_support import BOOTSTRAP, PASSWORD, build_portal


def _signup(client, username: str, **extra):
    data = {"username": username, "password": PASSWORD, "confirm_password": PASSWORD, "accept_terms": "1"}
    data.update(extra)
    return client.post("/signup", data=data)


def _settings(query, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    query(f"UPDATE hosting_settings SET {assignments} WHERE id = 1", tuple(values.values()))


# ── Sign-up ───────────────────────────────────────────────────────────────────


def test_first_account_needs_the_bootstrap_token_and_becomes_admin(web, query):
    assert _signup(web, "root").status_code == 404
    response = _signup(web, "root", bootstrap_token=BOOTSTRAP)
    assert response.status_code == 302
    row = query("SELECT is_admin, approval_status FROM accounts WHERE username = 'root'", one=True)
    assert row == {"is_admin": 1, "approval_status": "approved"}


def test_first_signup_is_locked_without_a_configured_token(tmp_path):
    from .hosting_support import portal_environ

    environ = portal_environ(tmp_path)
    environ.pop("HOSTING_BOOTSTRAP_TOKEN")
    app = build_portal(tmp_path, environ=environ)
    assert app.test_client().get("/signup").status_code == 503


def test_open_signup_creates_regular_account(web, make_account, query):
    make_account(admin=True)
    assert _signup(web, "alice").status_code == 302
    assert query("SELECT is_admin FROM accounts WHERE username = 'alice'", one=True)["is_admin"] == 0
    assert web.get("/dashboard").status_code == 200


def test_signup_requires_terms_and_matching_passwords(web, make_account, query):
    make_account(admin=True)
    assert _signup(web, "bob", accept_terms="").status_code == 400
    assert _signup(web, "bob", confirm_password="different 42").status_code == 400
    assert query("SELECT COUNT(*) AS n FROM accounts WHERE username = 'bob'", one=True)["n"] == 0


def test_closed_signup_refuses(web, make_account, query):
    make_account(admin=True)
    _settings(query, signup_mode="closed")
    assert _signup(web, "carol").status_code == 403
    assert query("SELECT COUNT(*) AS n FROM accounts WHERE username = 'carol'", one=True)["n"] == 0


def test_invite_mode_redeems_codes_once(web, portal, make_account, query):
    admin = make_account(admin=True)
    _settings(query, signup_mode="invite")
    query("INSERT INTO hosting_invite_codes (code, created_by, max_uses, current_uses, created_at) "
          "VALUES ('WELCOME1', ?, 1, 0, '2026-01-01 00:00:00')", (admin["id"],))
    assert _signup(web, "dave").status_code == 400
    assert _signup(web, "dave", invite_code="welcome1").status_code == 302
    other = portal.test_client()
    assert _signup(other, "erin", invite_code="WELCOME1").status_code == 400


def test_approval_mode_keeps_new_accounts_waiting(web, portal, make_account, login, query):
    admin = make_account(admin=True)
    _settings(query, signup_mode="approval", hosting_activation_required=1)
    response = _signup(web, "frank")
    assert response.headers["Location"].endswith("/activation-pending")
    assert web.get("/dashboard").headers["Location"].endswith("/activation-pending")
    frank = query("SELECT id, approval_status FROM accounts WHERE username = 'frank'", one=True)
    assert frank["approval_status"] == "pending"

    admin_client = portal.test_client()
    login(admin_client, admin)
    admin_client.post(f"/admin/accounts/{frank['id']}/approve", data={"decision_reason": ""})
    assert web.get("/dashboard").status_code == 200


def test_bot_protection_rejects_forms_without_a_signed_timestamp(tmp_path, make_account):
    app = build_portal(tmp_path / "bots", bot_protection=True)
    client = app.test_client()
    with app.app_context():
        from bananawiki.hosting import accounts
        from bananawiki.hosting.db import connection_scope

        with connection_scope():
            accounts.create("zoe", PASSWORD, is_admin=True)
    response = client.post("/login", data={"username": "zoe", "password": PASSWORD})
    assert response.status_code == 400
    token = re.search(r'name="_form_time" value="([^"]+)"', client.get("/login").get_data(as_text=True)).group(1)
    response = client.post("/login", data={"username": "zoe", "password": PASSWORD, "_form_time": token,
                                           "website": "spam"})
    assert response.status_code == 400
    response = client.post("/login", data={"username": "zoe", "password": PASSWORD, "_form_time": token})
    assert response.status_code == 302


# ── Sign-in and sessions ──────────────────────────────────────────────────────


def test_login_rejects_wrong_password_and_limits_attempts(web, make_account):
    user = make_account()
    for _ in range(8):
        assert web.post("/login", data={"username": user["username"], "password": "wrong"}).status_code == 401
    response = web.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.status_code == 429


def test_logout_ends_the_session(web, make_account, login, query):
    user = make_account()
    login(web, user)
    web.post("/logout")
    assert web.get("/dashboard").status_code == 302
    assert query("SELECT revoked_at FROM hosting_account_sessions", one=True)["revoked_at"] is not None


def test_password_change_signs_out_other_sessions(portal, make_account, login):
    user = make_account()
    first, second = portal.test_client(), portal.test_client()
    login(first, user)
    login(second, user)
    response = first.post("/account/change-password", data={
        "current_password": PASSWORD, "new_password": "brand new 99", "confirm_new_password": "brand new 99"})
    assert response.status_code == 302
    assert first.get("/dashboard").status_code == 200
    assert second.get("/dashboard").status_code == 302


def test_revoking_one_session_signs_that_device_out(portal, make_account, login, query):
    user = make_account()
    first, second = portal.test_client(), portal.test_client()
    login(first, user)
    login(second, user)
    newest = query("SELECT id FROM hosting_account_sessions ORDER BY created_at DESC, rowid DESC", one=True)["id"]
    first.post(f"/account/sessions/{newest}/revoke")
    assert second.get("/dashboard").status_code == 302
    assert first.get("/dashboard").status_code == 200


def test_cannot_revoke_someone_elses_session(portal, make_account, login, query):
    alice, bob = make_account(), make_account()
    alice_client, bob_client = portal.test_client(), portal.test_client()
    login(alice_client, alice)
    login(bob_client, bob)
    bob_session = query("SELECT id FROM hosting_account_sessions WHERE account_id = ?", (bob["id"],), one=True)["id"]
    alice_client.post(f"/account/sessions/{bob_session}/revoke")
    assert bob_client.get("/dashboard").status_code == 200


def test_suspended_account_cannot_sign_in(web, make_account):
    user = make_account(suspended=1, suspend_reason="spam", suspend_reason_visible=1)
    response = web.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.headers["Location"].endswith("/account-suspended")
    assert "spam" in web.get("/account-suspended").get_data(as_text=True)
    assert web.get("/dashboard").status_code == 302


# ── Two-step sign-in ──────────────────────────────────────────────────────────


def _enrol(portal, client) -> tuple[str, list[str]]:
    html = client.get("/account/mfa").get_data(as_text=True)
    secret = re.search(r'class="secret-box">([A-Z2-7]{32})<', html).group(1)
    with portal.app_context():
        code = mfa.totp_code(secret)
    response = client.post("/account/mfa", data={"password": PASSWORD, "code": code})
    assert response.status_code == 200
    codes = list(dict.fromkeys(re.findall(r"\b[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}\b", response.get_data(as_text=True))))
    assert len(codes) == 10
    return secret, codes


def test_mfa_enrolment_requires_a_code_at_next_sign_in(portal, web, make_account, login, query):
    user = make_account()
    login(web, user)
    secret, codes = _enrol(portal, web)
    assert query("SELECT totp_enabled FROM accounts WHERE id = ?", (user["id"],), one=True)["totp_enabled"] == 1

    other = portal.test_client()
    response = other.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.headers["Location"].endswith("/login/mfa")
    assert other.get("/dashboard").status_code == 302
    assert other.post("/login/mfa", data={"code": "000000"}).status_code == 200
    assert other.post("/login/mfa", data={"code": codes[0]}).status_code == 302
    assert other.get("/dashboard").status_code == 200

    third = portal.test_client()
    third.post("/login", data={"username": user["username"], "password": PASSWORD})
    third.post("/login/mfa", data={"code": codes[0]})
    assert third.get("/dashboard").status_code == 302, "a recovery code works only once"


def test_admin_mfa_requirement_gates_admin_pages(web, make_account, login, query):
    admin = make_account(admin=True)
    _settings(query, admin_mfa_required=1)
    login(web, admin)
    response = web.get("/admin")
    assert response.status_code == 302 and response.headers["Location"].endswith("/account/mfa")
    assert web.get("/account/mfa").status_code == 200


# ── Recovery by email ─────────────────────────────────────────────────────────


def test_password_reset_needs_a_verified_address_and_works_once(web, make_account):
    user = make_account(email="reset-me@example.org", email_verified_at="2026-01-01 00:00:00")
    with email.capture_outbox() as outbox:
        web.post("/forgot-password", data={"username": user["username"], "email": "reset-me@example.org"})
    assert len(outbox) == 1
    token = outbox[0].action_url.split("token=", 1)[1]
    data = {"token": token, "new_password": "reset pass 77", "confirm_new_password": "reset pass 77"}
    assert web.post("/reset-password", data=data).status_code == 302
    assert web.post("/login", data={"username": user["username"], "password": "reset pass 77"}).status_code == 302
    web.post("/logout")
    response = web.post("/reset-password", data={**data, "new_password": "again pass 88", "confirm_new_password": "again pass 88"})
    assert web.post("/login", data={"username": user["username"], "password": "again pass 88"}).status_code == 401
    assert response.status_code in (302, 400)


def test_password_reset_ignores_unverified_addresses(web, make_account):
    user = make_account(email="unverified@example.org")
    with email.capture_outbox() as outbox:
        response = web.post("/forgot-password", data={"username": user["username"], "email": "unverified@example.org"})
    assert response.status_code == 200
    assert outbox == []


def test_email_verification_link_marks_the_address(web, make_account, login, query):
    user = make_account()
    _settings(query, email_verification_required=1)
    login(web, user)
    with email.capture_outbox() as outbox:
        web.post("/account", data={"username": user["username"], "email": "verify-me@example.org",
                                   "current_password": PASSWORD})
    assert outbox and outbox[0].to == "verify-me@example.org"
    link = outbox[0].action_url
    assert web.get(link[link.index("/verify-email"):]).status_code == 302
    row = query("SELECT email_verified_at FROM accounts WHERE id = ?", (user["id"],), one=True)
    assert row["email_verified_at"]


@pytest.mark.parametrize("language", ["en", "it"])
def test_emails_are_rendered_in_the_visitors_language(web, make_account, language):
    user = make_account(email=f"lang-{language}@example.org", email_verified_at="2026-01-01 00:00:00")
    web.post("/language", data={"language": language})
    with email.capture_outbox() as outbox:
        web.post("/forgot-username", data={"email": f"lang-{language}@example.org"})
    assert outbox[0].detail_value == user["username"]
    expected = "nome utente" if language == "it" else "username"
    assert expected in outbox[0].subject.lower()


def test_account_deletion_needs_the_password(web, make_account, login, query):
    user = make_account()
    login(web, user)
    web.post("/account/delete", data={"current_password": "wrong"})
    assert query("SELECT deleted_at FROM accounts WHERE id = ?", (user["id"],), one=True)["deleted_at"] is None
    web.post("/account/delete", data={"current_password": PASSWORD})
    assert query("SELECT deleted_at FROM accounts WHERE id = ?", (user["id"],), one=True)["deleted_at"]
    assert web.get("/dashboard").status_code == 302
