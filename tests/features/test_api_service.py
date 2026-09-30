"""REST API: token authentication, scopes, account gates, rate limit, audit log, tokens and userbot."""

from __future__ import annotations

import json

import pytest

from bananawiki.core import crypto
from bananawiki.core.timeutil import sql_in
from bananawiki.wiki.features.api_service import audit, tokens

from .api_support import call, enable_api, issue
from .pages_support import in_app, set_feature


@pytest.fixture
def api_app(app, db):
    enable_api(app, db)
    return app


# ── Authentication ────────────────────────────────────────────────────────────


def test_status_and_openapi_need_no_token(api_app, client):
    status = call(client, "GET", "/status")
    assert status.status_code == 200 and status.json["api_enabled"] is True
    spec = call(client, "GET", "/openapi.json").json
    assert spec["openapi"].startswith("3.") and "/pages/{slug}" in spec["paths"]
    assert spec["paths"]["/pages"]["post"]["x-scope"] == "pages"


def test_missing_and_invalid_tokens_are_401_with_stable_codes(api_app, client, admin):
    assert call(client, "GET", "/pages").json["code"] == "missing_token"
    response = call(client, "GET", "/pages", "not-a-token")
    assert response.status_code == 401 and response.json == {**response.json, "ok": False, "code": "invalid_token"}


def test_a_session_cookie_is_not_enough(api_app, client, admin, login):
    login(client, admin)
    assert call(client, "GET", "/pages").status_code == 401


def test_bearer_write_needs_no_csrf_token(api_app, csrf_client, admin):
    token = issue(api_app, admin, ["pages"])
    response = call(csrf_client, "POST", "/pages", token, json={"title": "No CSRF needed"})
    assert response.status_code == 201


def test_tokens_are_stored_as_1x_compatible_hmac(api_app, db, admin):
    raw = issue(api_app, admin)
    stored = db.column("SELECT token_hash FROM api_service__tokens")
    expected = crypto.token_digest(api_app.config["BW"].secret_key, crypto.TOKEN_LABEL_API, raw)
    assert stored == [expected] and raw not in json.dumps(db.all("SELECT * FROM api_service__tokens"))


def test_revoked_expired_and_suspended_are_refused(api_app, client, db, admin, make_user):
    revoked = issue(api_app, admin)
    db.execute("UPDATE api_service__tokens SET active = 0")
    assert call(client, "GET", "/pages", revoked).status_code == 401
    expired = issue(api_app, admin, expires_at=sql_in(minutes=-1))
    assert call(client, "GET", "/pages", expired).status_code == 401
    member = make_user("member", api_access_enabled=1)
    token = issue(api_app, member)
    db.execute("UPDATE users SET suspended = 1 WHERE id = ?", (member["id"],))
    response = call(client, "GET", "/pages", token)
    assert response.status_code == 403 and response.json["code"] == "account_blocked"


def test_legacy_iso_expiry_still_works(api_app, client, db, admin):
    token = issue(api_app, admin)
    db.execute("UPDATE api_service__tokens SET expires_at = '2999-01-01T00:00'")
    assert call(client, "GET", "/pages", token).status_code == 200


def test_api_off_answers_503_and_plugin_off_404(api_app, client, db, admin):
    token = issue(api_app, admin)
    db.execute("UPDATE site_settings SET api_service_enabled = 0")
    assert call(client, "GET", "/pages", token).status_code == 503
    set_feature(api_app, "api_service", False)
    assert call(client, "GET", "/pages", token).status_code == 404


def test_members_need_api_access(api_app, client, db, make_user):
    member = make_user("member")
    token = issue(api_app, member)
    assert call(client, "GET", "/pages", token).json["code"] == "api_access_disabled"
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (member["id"],))
    assert call(client, "GET", "/pages", token).status_code == 200


def test_forced_steps_and_maintenance(api_app, client, db, admin, make_user):
    token = issue(api_app, admin)
    db.execute("UPDATE users SET force_password_change = 1 WHERE id = ?", (admin["id"],))
    assert call(client, "GET", "/pages", token).json["code"] == "password_change_required"
    db.execute("UPDATE users SET force_password_change = 0, onboarding_required = 1 WHERE id = ?", (admin["id"],))
    assert call(client, "GET", "/pages", token).json["code"] == "onboarding_required"
    db.execute("UPDATE users SET onboarding_required = 0 WHERE id = ?", (admin["id"],))
    editor = make_user("editor1", role="editor", api_access_enabled=1)
    editor_token = issue(api_app, editor)
    db.execute("UPDATE site_settings SET maintenance_mode = 1")
    assert call(client, "GET", "/pages", editor_token).status_code == 503
    assert call(client, "GET", "/pages", token).status_code == 200


def test_other_browser_origins_are_refused(api_app, client, admin):
    token = issue(api_app, admin)
    assert call(client, "GET", "/pages", token, headers={"Origin": "https://evil.example"}).status_code == 403
    assert call(client, "GET", "/pages", token, headers={"Origin": "http://localhost"}).status_code == 200


# ── Scopes and flags ──────────────────────────────────────────────────────────


def test_scope_and_write_flag_are_enforced_for_admins_too(api_app, client, admin):
    read_only = issue(api_app, admin, ["pages"], write=False)
    assert call(client, "GET", "/pages", read_only).status_code == 200
    refused = call(client, "POST", "/pages", read_only, json={"title": "x"})
    assert refused.status_code == 403 and refused.json["code"] == "scope_missing" and refused.json["write"] is True
    other_scope = issue(api_app, admin, ["categories"])
    assert call(client, "GET", "/pages", other_scope).json["scope"] == "pages"


def test_malformed_permissions_grant_nothing(api_app, client, db, admin):
    token = issue(api_app, admin)
    db.execute("UPDATE api_service__tokens SET permissions = '{\"read\": \"yes\", \"scopes\": \"pages\"}'")
    assert call(client, "GET", "/pages", token).status_code == 403


def test_admin_scopes_on_a_members_token_do_not_help(api_app, client, make_user):
    member = make_user("member", api_access_enabled=1)
    token = issue(api_app, member, ["admin", "users", "settings"])
    for path in ("/users", "/settings", "/admin/tokens"):
        assert call(client, "GET", path, token).json["code"] == "admin_required"


# ── Rate limit and audit ──────────────────────────────────────────────────────


def test_rate_limit_per_account(api_app, client, db, make_user):
    member = make_user("member", api_access_enabled=1)
    db.execute("UPDATE site_settings SET api_service_rate_limit = 2")
    first, second = issue(api_app, member), issue(api_app, member)
    assert call(client, "GET", "/pages", first).status_code == 200
    assert call(client, "GET", "/pages", second).status_code == 200
    limited = call(client, "GET", "/pages", first)
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


def test_bad_stored_rate_limit_does_not_break_the_api(api_app, client, db, admin):
    db.execute("UPDATE site_settings SET api_service_admin_rate_limit = -5")
    assert call(client, "GET", "/pages", issue(api_app, admin)).status_code == 200


def test_audit_log_redacts_secrets_and_content(api_app, client, db, admin):
    token = issue(api_app, admin, ["users", "pages"])
    call(client, "POST", "/users", token, json={"username": "newbie", "password": "a very secret password"})
    call(client, "POST", "/pages", token, json={"title": "Audited", "content": "top secret body " * 50})
    rows = db.all("SELECT method, endpoint, status_code, request_body FROM api_service__audit_log ORDER BY id")
    assert [(r["method"], r["endpoint"], r["status_code"]) for r in rows] == [
        ("POST", "/api/v1/users", 201), ("POST", "/api/v1/pages", 201)]
    everything = json.dumps(rows)
    assert "a very secret password" not in everything and "top secret body" not in everything
    assert '"password":"[redacted]"' in rows[0]["request_body"] and "characters]" in rows[1]["request_body"]


def test_audit_summary_is_bounded():
    summary = audit.summarise_body({"items": [{"name": "x" * 500, "api_key": "k"}] * 100, "nested": {"a": {"b": {"c": 1}}}})
    assert len(summary) <= audit.SUMMARY_LIMIT and "[redacted]" in summary and "x" * 200 not in summary


def test_audit_log_endpoint_and_superuser_clear(api_app, client, db, admin):
    token = issue(api_app, admin, ["admin", "pages"])
    call(client, "GET", "/pages", token)
    listing = call(client, "GET", "/admin/audit-log?limit=-1", token).json
    assert listing["limit"] == 1 and listing["total"] >= 1
    assert call(client, "DELETE", "/admin/audit-log", token).json["code"] == "superuser_required"
    db.execute("UPDATE users SET is_superuser = 1 WHERE id = ?", (admin["id"],))
    assert call(client, "DELETE", "/admin/audit-log?before_days=1", token).status_code == 200


# ── Tokens ────────────────────────────────────────────────────────────────────


def test_child_tokens_cannot_exceed_their_parent(api_app, client, db, admin):
    parent = issue(api_app, admin, ["tokens", "pages"], expires_at=sql_in(days=2))
    wider = call(client, "POST", "/tokens", parent, json={"permissions": {"read": True, "write": True,
                                                                         "scopes": ["users"]}})
    assert wider.status_code == 403 and wider.json["code"] == "exceeds_parent"
    later = call(client, "POST", "/tokens", parent, json={"expires_at": sql_in(days=5).replace(" ", "T") + "Z",
                                                          "permissions": {"read": True, "scopes": ["pages"]}})
    assert later.json["code"] == "outlives_parent"
    child = call(client, "POST", "/tokens", parent, json={"name": "child", "permissions": {
        "read": True, "write": False, "scopes": ["pages"]}})
    assert child.status_code == 201 and child.json["expires_at"] is not None
    assert call(client, "GET", "/pages", child.json["token"]).status_code == 200
    assert call(client, "POST", "/pages", child.json["token"], json={"title": "x"}).status_code == 403
    assert call(client, "POST", "/tokens", parent, json={"expires_at": "yesterday"}).json["code"] == "invalid_expiry"


def test_members_cannot_mint_admin_scopes(api_app, client, make_user):
    member = make_user("member", api_access_enabled=1)
    parent = issue(api_app, member, ["tokens", "admin"])
    response = call(client, "POST", "/tokens", parent, json={"permissions": {"read": True, "scopes": ["admin"]}})
    assert response.status_code == 403 and response.json["code"] == "admin_scope_forbidden"


def test_token_quota_and_own_revocation(api_app, client, db, admin, make_user):
    db.execute("UPDATE site_settings SET api_service_max_tokens_per_user = 2")
    token = issue(api_app, admin, ["tokens", "pages"])
    assert call(client, "POST", "/tokens", token, json={}).status_code == 201
    assert call(client, "POST", "/tokens", token, json={}).json["code"] == "token_limit"
    listed = call(client, "GET", "/tokens", token).json["tokens"]
    assert len(listed) == 2 and "token_hash" not in listed[0]
    other = make_user("other", role="admin")
    foreign = issue(api_app, other)
    foreign_id = db.scalar("SELECT id FROM api_service__tokens WHERE user_id = ?", (other["id"],))
    assert call(client, "DELETE", f"/tokens/{foreign_id}", token).status_code == 404
    assert call(client, "GET", "/pages", foreign).status_code == 200


def test_admin_token_management(api_app, client, db, admin, make_user):
    token = issue(api_app, admin, ["admin"])
    member = make_user("member")
    member_token = issue(api_app, member)
    member_token_id = db.scalar("SELECT id FROM api_service__tokens WHERE user_id = ?", (member["id"],))
    assert any(t["username"] == "member" for t in call(client, "GET", "/admin/tokens", token).json["tokens"])
    assert call(client, "PUT", f"/admin/users/{member['id']}/api-access", token,
                json={"enabled": "yes"}).status_code == 400
    assert call(client, "PUT", f"/admin/users/{member['id']}/api-access", token, json={"enabled": True}).json["ok"]
    assert call(client, "GET", "/pages", member_token).status_code == 200
    assert call(client, "POST", f"/admin/tokens/{member_token_id}/revoke", token).status_code == 200
    assert call(client, "GET", "/pages", member_token).status_code == 401


def test_password_change_anywhere_revokes_tokens(api_app, client, admin, make_user):
    from bananawiki.wiki import accounts

    member = make_user("member", api_access_enabled=1)
    token = issue(api_app, member)
    in_app(api_app, lambda: accounts.set_password(member["id"], "another fine password"))
    assert call(client, "GET", "/pages", token).status_code == 401


def _while_api_is_off(app, action):
    """Run *action* with the api_service feature switched off (its event handlers do not run)."""
    set_feature(app, "api_service", False)
    in_app(app, action)
    set_feature(app, "api_service", True)


def test_password_change_while_the_feature_is_off_still_revokes(api_app, client, db, make_user):
    from bananawiki.wiki import accounts

    member = make_user("member", api_access_enabled=1)
    token = issue(api_app, member)
    _while_api_is_off(api_app, lambda: accounts.set_password(member["id"], "another fine password"))
    assert call(client, "GET", "/pages", token).status_code == 401
    assert db.scalar("SELECT active FROM api_service__tokens") == 0, "revoked for good"
    fresh = issue(api_app, db.one("SELECT * FROM users WHERE id = ?", (member["id"],)))
    assert call(client, "GET", "/pages", fresh).status_code == 200


def test_admin_reset_and_suspension_while_the_feature_is_off_still_revoke(api_app, client, db, admin, make_user):
    from bananawiki.wiki.features.admin import service as admin_service

    reset = make_user("reset_me", api_access_enabled=1)
    suspended = make_user("suspend_me", api_access_enabled=1)
    reset_token, suspended_token = issue(api_app, reset), issue(api_app, suspended)

    def act() -> None:
        admin_service.reset_password(admin, reset, "a brand new password", require_change=False, keep_original=False)
        admin_service.suspend(admin, suspended, until=None, label="permanent", reason="", reason_visible=False,
                              time_visible=False)
        admin_service.unsuspend(admin, db.one("SELECT * FROM users WHERE id = ?", (suspended["id"],)))

    _while_api_is_off(api_app, act)
    assert call(client, "GET", "/pages", reset_token).status_code == 401
    assert call(client, "GET", "/pages", suspended_token).status_code == 401


def test_tokens_issued_by_1x_are_stamped_with_the_current_password(api_app, db, make_user):
    from bananawiki.wiki.features.api_service import schema

    member = make_user("member", api_access_enabled=1)
    issue(api_app, member)
    db.execute("UPDATE api_service__tokens SET credential_stamp = NULL")
    schema.upgrade_v4(db.conn)
    stamp = db.scalar("SELECT credential_stamp FROM api_service__tokens")
    assert stamp == tokens.credential_stamp(db.scalar("SELECT password FROM users WHERE id = ?", (member["id"],)))


def test_listings_are_bounded_even_with_limit_zero(api_app, client, db, admin, monkeypatch):
    from bananawiki.wiki.features.api_service import errors

    monkeypatch.setattr(errors, "MAX_PAGE_SIZE", 3)
    token = issue(api_app, admin, ["pages", "users", "categories"])
    for index in range(4):
        call(client, "POST", "/pages", token, json={"title": f"Bounded {index}"})
        db.execute("INSERT INTO users (id, username, password, role) VALUES (?, ?, 'x', 'user')",
                   (f"bulk{index}", f"bulk{index}"))
    first = call(client, "GET", "/pages?limit=0", token).json
    assert len(first["pages"]) == 3 and first["next_offset"] == 3 and first["limit"] == 3
    rest = call(client, "GET", "/pages?limit=100000&offset=3", token).json
    assert len(rest["pages"]) >= 1 and len(rest["pages"]) <= 3
    users = call(client, "GET", "/users", token).json
    assert len(users["users"]) == 3 and users["next_offset"] == 3
    categories = call(client, "GET", "/categories?limit=0", token).json
    assert len(categories["categories"]) <= 3 and "next_offset" in categories


# ── Web pages ─────────────────────────────────────────────────────────────────


def test_token_page_creates_and_shows_the_token_once(api_app, client, db, login, make_user):
    member = make_user("member", api_access_enabled=1)
    login(client, member)
    assert client.get("/settings/api-tokens").status_code == 200
    assert client.get("/settings/api").status_code == 301
    response = client.post("/settings/api-tokens/create", data={"name": "cli", "scopes": ["pages", "admin"],
                                                                "read_only": "1"})
    assert response.status_code == 201 and response.headers["Cache-Control"] == "no-store"
    row = db.one("SELECT * FROM api_service__tokens WHERE user_id = ?", (member["id"],))
    assert json.loads(row["permissions"]) == {"read": True, "write": False, "scopes": ["pages"]}
    raw = response.get_data(as_text=True).split('data-copy="')[1].split('"')[0]
    assert call(client, "GET", "/pages", raw).status_code == 200
    assert raw not in client.get("/settings/api-tokens").get_data(as_text=True)
    client.post(f"/settings/api-tokens/{row['id']}/revoke")
    assert call(client, "GET", "/pages", raw).status_code == 401


def test_token_page_refuses_past_expiry_and_members_without_access(api_app, client, db, login, make_user):
    member = make_user("member")
    login(client, member)
    client.post("/settings/api-tokens/create", data={"name": "x"})
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens") == 0
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (member["id"],))
    client.post("/settings/api-tokens/create", data={"name": "x", "expires_at": "2001-01-01T10:00"})
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens") == 0


def test_admin_page_is_for_admins_and_clamps_settings(api_app, client, db, login, admin, make_user):
    member = make_user("member")
    login(client, member)
    assert client.get("/admin/api-service").status_code in (302, 403)
    client.post("/logout")
    login(client, admin)
    assert client.get("/admin/api-service").status_code == 200
    client.post("/admin/api-service/settings", data={"enabled": "1", "rate_limit": "999999",
                                                     "admin_rate_limit": "0", "max_tokens_per_user": "3"})
    row = db.one("SELECT * FROM site_settings")
    assert (row["api_service_rate_limit"], row["api_service_admin_rate_limit"],
            row["api_service_max_tokens_per_user"]) == (tokens.RATE_LIMIT_BOUNDS[1], 1, 3)
    client.post(f"/admin/api-service/users/{member['id']}/toggle-access")
    assert db.scalar("SELECT api_access_enabled FROM users WHERE id = ?", (member["id"],)) == 1


def test_docs_page_is_public(api_app, client):
    page = client.get("/api-docs")
    assert page.status_code == 200 and "/api/v1/pages" in page.get_data(as_text=True)


def test_account_settings_slot(api_app, admin):
    from flask import g

    from bananawiki.wiki import registry
    from bananawiki.wiki.db import connection_scope

    with api_app.test_request_context(), connection_scope():
        g.user = admin
        html = registry.render_slot("account.settings_sections")
    assert "/settings/api-tokens" in html


# ── Userbot ───────────────────────────────────────────────────────────────────


def test_userbot_mode_and_profile(api_app, client, db, login, make_user):
    member = make_user("botty", api_access_enabled=1)
    login(client, member)
    page = client.post("/settings/api-tokens/userbot", data={"enable": "1"})
    key = page.get_data(as_text=True).split('data-copy="')[1].split('"')[0]
    assert db.scalar("SELECT userbot_enabled FROM users WHERE id = ?", (member["id"],)) == 1
    me = call(client, "GET", "/userbot/me", key).json
    assert me["user"]["userbot_enabled"] is True and me["user"]["userbot_enable_count"] == 1
    updated = call(client, "POST", "/userbot/profile", key, json={"real_name": "Bot", "bio": "Beep",
                                                                  "page_published": True})
    assert updated.status_code == 200 and updated.json["profile"]["real_name"] == "Bot"
    assert call(client, "POST", "/userbot/profile", key, json={"page_published": "yes"}).status_code == 400
    db.execute("UPDATE user_profiles SET page_disabled_by_admin = 1 WHERE user_id = ?", (member["id"],))
    assert call(client, "POST", "/userbot/profile", key, json={"page_published": True}).status_code == 403
    client.post("/settings/api-tokens/userbot", data={"enable": "0"})
    assert call(client, "GET", "/userbot/me", key).status_code == 401


def test_userbot_profile_needs_write(api_app, client, make_user):
    member = make_user("botty", api_access_enabled=1)
    read_only = issue(api_app, member, ["userbot"], write=False)
    assert call(client, "GET", "/userbot/me", read_only).status_code == 200
    assert call(client, "POST", "/userbot/profile", read_only, json={}).status_code == 403


def test_admin_userbot_lock(api_app, client, db, login, admin, make_user):
    member = make_user("botty", api_access_enabled=1)
    login(client, admin)
    client.post(f"/admin/api-service/users/{member['id']}/userbot-lock", data={"mode": "force_disabled"})
    client.post("/logout")
    login(client, member)
    client.post("/settings/api-tokens/userbot", data={"enable": "1"})
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens WHERE user_id = ?", (member["id"],)) == 0
    db.execute("UPDATE users SET userbot_mode_lock = 'force_enabled', userbot_enabled = 1 WHERE id = ?",
               (member["id"],))
    client.post("/settings/api-tokens/userbot", data={"enable": "0"})
    assert db.scalar("SELECT userbot_enabled FROM users WHERE id = ?", (member["id"],)) == 1
    new_key = client.post("/settings/api-tokens/userbot", data={"enable": "1"})
    assert 'data-copy="' in new_key.get_data(as_text=True)


def test_admin_password_reset_and_suspension_revoke_tokens(api_app, client, admin, make_user):
    from bananawiki.wiki.registry import emit

    for event in ("user.password_reset", "user.suspended"):
        member = make_user(api_access_enabled=1)
        token = issue(api_app, member)
        in_app(api_app, lambda event=event, member=member: emit(event, user=member, actor_id=admin["id"], until=None))
        assert call(client, "GET", "/pages", token).status_code == 401, event


def test_deleting_an_account_removes_its_tokens(api_app, db, admin, make_user):
    from bananawiki.wiki import accounts

    member = make_user()
    issue(api_app, member)
    in_app(api_app, lambda: accounts.delete(member, deleted_by=admin["id"]))
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens") == 0


def test_userbot_sdk_against_a_running_wiki(api_app, make_user):
    import threading

    from werkzeug.serving import make_server

    from bananawiki.sdk.userbot import ApiError, UserbotClient

    member = make_user("sdk_bot", api_access_enabled=1)
    key = issue(api_app, member, ["userbot"])
    server = make_server("127.0.0.1", 0, api_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = UserbotClient(f"http://127.0.0.1:{server.server_port}", key)
        assert client.me()["user"]["username"] == "sdk_bot"
        assert client.update_profile(real_name="SDK")["profile"]["real_name"] == "SDK"
        try:
            client.page("home")
        except ApiError as error:
            assert error.status == 403 and error.code == "scope_missing"
        else:
            raise AssertionError("expected a scope error")
    finally:
        server.shutdown()
