"""Administrator pages: access control, account moderation, impersonation, wiki lifecycle and settings."""

from __future__ import annotations

import pytest

from .hosting_support import PASSWORD

ADMIN_GETS = ["/admin", "/admin/settings", "/global-settings", "/admin/banners", "/admin/moderation",
              "/admin/merge-requests", "/admin/merge-accounts", "/admin/instances/import"]


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_regular_accounts_are_refused(web, make_account, login, path):
    login(web, make_account())
    assert web.get(path).status_code == 403


def test_admin_actions_refuse_regular_accounts(portal, make_account, make_wiki, login, runtime, query):
    user, victim = make_account(), make_account()
    wiki = make_wiki(victim)
    client = portal.test_client()
    login(client, user)
    assert client.post(f"/admin/instances/{wiki['id']}/terminate").status_code == 403
    assert client.post(f"/admin/accounts/{victim['id']}/toggle-admin").status_code == 403
    assert client.post("/admin/settings", data={"action": "save_api", "api_enabled": "1"}).status_code == 403
    assert query("SELECT is_admin FROM accounts WHERE id = ?", (user["id"],), one=True)["is_admin"] == 0
    assert query("SELECT status FROM instances", one=True)["status"] == "running"


def test_admin_suspends_and_reactivates_an_account(portal, make_account, make_wiki, login, runtime, query):
    admin, user = make_account(admin=True), make_account()
    make_wiki(user, "user-wiki")
    admin_client, user_client = portal.test_client(), portal.test_client()
    login(admin_client, admin)
    login(user_client, user)
    admin_client.post(f"/admin/accounts/{user['id']}/suspend", data={
        "suspend_duration": "permanent", "suspend_reason": "abuse", "suspend_reason_visible": "1",
        "suspend_instances": "1", "notify_user": "0"})
    assert query("SELECT suspended FROM accounts WHERE id = ?", (user["id"],), one=True)["suspended"] == 1
    assert query("SELECT status FROM instances", one=True)["status"] == "suspended"
    assert "/login" in user_client.get("/dashboard").headers["Location"], "suspension ends every session"
    response = user_client.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.headers["Location"].endswith("/account-suspended")
    assert "abuse" in user_client.get("/account-suspended").get_data(as_text=True)

    admin_client.post(f"/admin/accounts/{user['id']}/unsuspend")
    assert query("SELECT suspended FROM accounts WHERE id = ?", (user["id"],), one=True)["suspended"] == 0
    assert user_client.post("/login", data={"username": user["username"], "password": PASSWORD}).status_code == 302


def test_last_admin_cannot_lose_admin_rights(web, make_account, login, query):
    admin = make_account(admin=True)
    login(web, admin)
    web.post(f"/admin/accounts/{admin['id']}/toggle-admin")
    assert query("SELECT is_admin FROM accounts WHERE id = ?", (admin["id"],), one=True)["is_admin"] == 1


def test_impersonation_acts_as_the_user_and_is_logged(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "impersonated")
    login(web, admin)
    web.post(f"/admin/accounts/{user['id']}/impersonate")
    page = web.get("/dashboard").get_data(as_text=True)
    assert "impersonated" in page and user["username"] in page
    assert web.get("/admin").status_code == 403, "an impersonated session has the user's rights"
    assert web.get(f"/instances/{wiki['id']}").status_code == 200
    web.post("/admin/stop-impersonating")
    assert web.get("/admin").status_code == 200
    log = query("SELECT admin_account_id, target_account_id, ended_at FROM hosting_impersonation_logs", one=True)
    assert log["admin_account_id"] == admin["id"] and log["target_account_id"] == user["id"] and log["ended_at"]


def test_admins_cannot_impersonate_other_admins(web, make_account, login):
    admin, other_admin = make_account(admin=True), make_account(admin=True)
    login(web, admin)
    web.post(f"/admin/accounts/{other_admin['id']}/impersonate")
    assert web.get("/admin").status_code == 200


def test_deleting_an_account_terminates_its_wikis(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    make_wiki(user, "doomed")
    login(web, admin)
    web.post(f"/admin/accounts/{user['id']}/delete", data={"instance_action": "terminate"})
    assert query("SELECT deleted_at FROM accounts WHERE id = ?", (user["id"],), one=True)["deleted_at"]
    assert query("SELECT status FROM instances", one=True)["status"] == "terminated"


def test_deleting_an_account_can_transfer_its_wikis(web, make_account, make_wiki, login, query):
    admin, user, heir = make_account(admin=True), make_account(), make_account()
    make_wiki(user, "inherited")
    login(web, admin)
    web.post(f"/admin/accounts/{user['id']}/delete",
             data={"instance_action": "transfer", "transfer_username": heir["username"]})
    row = query("SELECT account_id, status FROM instances", one=True)
    assert row == {"account_id": heir["id"], "status": "running"}


def test_admin_wiki_lifecycle_and_restore(web, make_account, make_wiki, login, runtime, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "managed")
    login(web, admin)
    web.post(f"/admin/instances/{wiki['id']}/stop")
    assert query("SELECT status FROM instances", one=True)["status"] == "stopped"
    web.post(f"/admin/instances/{wiki['id']}/terminate")
    row = query("SELECT status, subdomain, data_retained_until FROM instances", one=True)
    assert row["status"] == "terminated" and row["data_retained_until"]
    web.post(f"/admin/instances/{wiki['id']}/restore", data={"extend_days": "7"})
    row = query("SELECT status, subdomain FROM instances", one=True)
    assert row["subdomain"] == "managed" and row["status"] in ("running", "stopped")


def test_paused_deletion_countdown_blocks_owner_downloads_and_says_so(portal, make_account, make_wiki, login, query,
                                                                     runtime):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner, "on-hold")
    query("UPDATE hosting_settings SET allow_owner_download_expired = 1 WHERE id = 1")
    admin_client, owner_client = portal.test_client(), portal.test_client()
    login(admin_client, admin)
    login(owner_client, owner)
    admin_client.post(f"/admin/instances/{wiki['id']}/terminate")
    page = admin_client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert "the owner cannot download the data" in page
    admin_client.post(f"/admin/instances/{wiki['id']}/grace-suspend")
    page = admin_client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert "countdown paused" in page and "the owner cannot download it in the meantime" in page
    assert "countdown paused" in owner_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert owner_client.post(f"/instances/{wiki['id']}/download").status_code == 302
    assert runtime.called("export_archive") == []
    again = admin_client.post(f"/admin/instances/{wiki['id']}/grace-suspend", follow_redirects=True)
    assert "The deletion countdown was already paused." in again.get_data(as_text=True)
    resumed = admin_client.post(f"/admin/instances/{wiki['id']}/grace-unsuspend", follow_redirects=True)
    assert "the paused time was added" in resumed.get_data(as_text=True)
    assert owner_client.post(f"/instances/{wiki['id']}/download").status_code == 200
    again = admin_client.post(f"/admin/instances/{wiki['id']}/grace-unsuspend", follow_redirects=True)
    assert "The deletion countdown was not paused." in again.get_data(as_text=True)


def test_admin_suspends_a_wiki_with_an_expiry(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "paused")
    login(web, admin)
    web.post(f"/admin/instances/{wiki['id']}/suspend", data={
        "suspend_duration": "custom_relative", "suspend_rel_days": "0", "suspend_rel_hours": "2",
        "suspend_rel_minutes": "0", "suspend_reason": "check"})
    row = query("SELECT status, suspended_until FROM instances", one=True)
    assert row["status"] == "suspended" and row["suspended_until"]
    web.post(f"/admin/instances/{wiki['id']}/unsuspend")
    assert query("SELECT status FROM instances", one=True)["status"] == "running"


def test_settings_save_and_gpu_token_is_encrypted(web, make_account, login, query, portal):
    admin = make_account(admin=True)
    login(web, admin)
    web.post("/admin/settings", data={"action": "save_tts_gpu", "global_tts_gpu_enabled": "1",
                                      "global_tts_gpu_url": "https://gpu.example.org",
                                      "global_tts_gpu_auth_token": "top-secret-token"})
    row = query("SELECT global_tts_gpu_enabled, global_tts_gpu_auth_token FROM hosting_settings", one=True)
    assert row["global_tts_gpu_enabled"] == 1
    assert "top-secret-token" not in row["global_tts_gpu_auth_token"]
    page = web.get("/admin/settings").get_data(as_text=True)
    assert "top-secret-token" not in page
    web.post("/admin/settings", data={"action": "save_tts_gpu", "global_tts_gpu_url": "ftp://nope"})
    assert query("SELECT global_tts_gpu_url FROM hosting_settings", one=True)["global_tts_gpu_url"] == "https://gpu.example.org"


def test_admin_mfa_requirement_needs_the_admins_own_mfa(web, make_account, login, query):
    admin = make_account(admin=True)
    login(web, admin)
    web.post("/admin/settings", data={"action": "save_admin_mfa", "admin_mfa_required": "1"})
    assert query("SELECT admin_mfa_required FROM hosting_settings", one=True)["admin_mfa_required"] == 0


def test_signup_mode_and_invites(web, make_account, login, query):
    admin = make_account(admin=True)
    login(web, admin)
    web.post("/admin/signup-mode", data={"signup_mode": "invite"})
    assert query("SELECT signup_mode FROM hosting_settings", one=True)["signup_mode"] == "invite"
    web.post("/admin/invites/create", data={"custom_code": "TEAM2026", "max_uses": "3"})
    assert query("SELECT code, max_uses FROM hosting_invite_codes", one=True) == {"code": "TEAM2026", "max_uses": 3}
    web.post("/admin/signup-mode", data={"signup_mode": "bogus"})
    assert query("SELECT signup_mode FROM hosting_settings", one=True)["signup_mode"] == "invite"


def test_banner_is_shown_to_its_audience(portal, make_account, login):
    admin, user = make_account(admin=True), make_account()
    admin_client, user_client = portal.test_client(), portal.test_client()
    login(admin_client, admin)
    login(user_client, user)
    admin_client.post("/admin/banners/create", data={
        "content": "Planned maintenance tonight", "color": "orange", "visibility": "both", "audience_mode": "all"})
    assert "Planned maintenance tonight" in user_client.get("/dashboard").get_data(as_text=True)
    assert "Planned maintenance tonight" in portal.test_client().get("/login").get_data(as_text=True)


def test_moderation_log_records_admin_actions(web, make_account, login):
    admin, user = make_account(admin=True), make_account()
    login(web, admin)
    web.post(f"/admin/accounts/{user['id']}/suspend", data={"suspend_duration": "permanent", "notify_user": "0"})
    assert "account.suspend" in web.get("/admin/moderation").get_data(as_text=True)
