"""Security regressions for the API Service (/api/v1/).

Each test pins down one hole a security review found, so the fix cannot
quietly come undone:

* PUT /api/v1/settings wrote any database column: setup state, the restart
  cooldown, the platform's remote GPU settings (an SSRF that leaked the
  platform's GPU token) and values that broke the API itself.
* Any user with API access could mint an admin-scope token and switch on
  banana mode for the whole wiki.
* Tokens kept working through a forced password change, onboarding, a
  password change made through the API and maintenance mode.
* User management skipped the superuser lock and the username and password
  rules of the web forms.
* Page writes skipped the editor's size, title and slug rules, deletes
  skipped page.delete, and the bulk endpoints had no type checks or caps.
"""

import io
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db


@pytest.fixture(autouse=True)
def api_enabled():
    """Every test here talks to the API Service."""
    db.update_site_settings(api_service_enabled=1)


def _token(user_id, scopes, *, write=True):
    """Return a raw token for *user_id* with the given scopes."""
    return db.create_api_token(user_id, name="security test", permissions={
        "read": True, "write": write, "scopes": list(scopes),
    })


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _member_with_api_access(name="member", role="user"):
    user_id = db.create_user(name, generate_password_hash("member-pass-1"), role=role)
    db.update_user(user_id, api_access_enabled=1)
    return user_id


def _token_from_flash(response):
    """Pull the one-time token out of the creation flash message."""
    match = re.search(rb"(?:<code>|&lt;code&gt;)([A-Za-z0-9_\-]{40,})", response.data)
    assert match, "the new token was not shown"
    return match.group(1).decode()


# PUT /api/v1/settings

class TestSettingsSchema:

    def _put(self, client, admin_user, payload):
        return client.put("/api/v1/settings", headers=_auth(_token(admin_user, ["settings"])),
                          json=payload)

    @pytest.mark.parametrize("key, value", [
        ("setup_done", 0),
        ("last_server_restart_at", ""),
        ("last_chat_cleanup_at", ""),
        ("list_order_version", 99),
        ("chat_cleanup_split_configured", 1),
        ("platform_upload_blacklist", "zip"),
        ("devtools_enabled", 1),
        ("tts_gpu_enabled", 1),
        ("tts_gpu_url", "http://127.0.0.1:2375/internal?x="),
        ("tts_gpu_timeout", 5),
        ("tts_gpu_auth_token", "attacker-token"),
        ("docs_category_id", 1),
        ("interface_languages_json", "{}x"),
    ])
    def test_internal_and_platform_settings_are_refused(self, client, admin_user, key, value):
        before = db.get_site_settings()[key]
        response = self._put(client, admin_user, {key: value})
        assert response.status_code == 403
        assert key in response.get_json()["refused"]
        assert db.get_site_settings()[key] == before

    def test_setup_cannot_be_reopened_and_the_wiki_keeps_working(self, client, admin_user):
        response = self._put(client, admin_user, {"setup_done": 0, "site_name": "Still here"})
        assert response.status_code == 403
        # Nothing from a refused request is applied.
        assert db.get_site_settings()["site_name"] != "Still here"
        assert db.get_site_settings()["setup_done"] == 1
        assert client.get("/api/v1/pages", headers=_auth(_token(admin_user, ["pages"]))).status_code == 200

    def test_restart_cooldown_cannot_be_cleared(self, client, admin_user):
        assert db.check_and_claim_server_restart(60) is True
        assert self._put(client, admin_user, {"last_server_restart_at": ""}).status_code == 403
        assert db.check_and_claim_server_restart(60) is False

    @pytest.mark.parametrize("hosted", [False, True])
    def test_remote_gpu_settings_are_never_writable(self, client, admin_user, monkeypatch, tmp_path, hosted):
        """The worker sends the stored GPU token to the stored URL, so a
        tenant that could set the URL could read the platform's token and
        reach internal addresses."""
        if hosted:
            monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
        db.update_site_settings(tts_gpu_enabled=1, tts_gpu_url="http://gpu.example:8787",
                                tts_gpu_auth_token="PLATFORM-GPU-TOKEN")
        response = self._put(client, admin_user, {
            "tts_gpu_url": "http://127.0.0.1:9/portal?x=", "tts_gpu_timeout": 5,
        })
        assert response.status_code == 403
        settings = db.get_site_settings()
        assert settings["tts_gpu_url"] == "http://gpu.example:8787"
        assert settings["tts_gpu_timeout"] == 120

    def test_secret_values_cannot_be_probed(self, client, admin_user):
        """Sending the stored secret back must not answer differently from a
        wrong guess, or a token could guess it one request at a time."""
        db.update_site_settings(tts_gpu_auth_token="PLATFORM-GPU-TOKEN")
        right = self._put(client, admin_user, {"tts_gpu_auth_token": "PLATFORM-GPU-TOKEN"})
        wrong = self._put(client, admin_user, {"tts_gpu_auth_token": "guess"})
        assert right.status_code == wrong.status_code == 403
        settings = self._put(client, admin_user, {"site_name": "x"})
        assert settings.status_code == 200
        listed = client.get("/api/v1/settings", headers=_auth(_token(admin_user, ["settings"])))
        assert "tts_gpu_auth_token" not in listed.get_json()["settings"]
        assert "telegram_sync_token" not in listed.get_json()["settings"]

    def test_bad_rate_limit_value_is_refused(self, client, admin_user):
        response = self._put(client, admin_user, {"api_service_rate_limit": "abc"})
        assert response.status_code == 400
        assert "api_service_rate_limit" in response.get_json()["invalid"]
        assert client.get("/api/v1/pages", headers=_auth(_token(admin_user, ["pages"]))).status_code == 200

    def test_bad_stored_rate_limit_does_not_break_the_api(self, client, admin_user):
        """A value written before the schema existed must not turn every call into a 500."""
        with db.get_db_context() as conn:
            conn.execute("UPDATE site_settings SET api_service_rate_limit='abc', "
                         "api_service_max_tokens_per_user='-4' WHERE id=1")
            conn.commit()
        settings = db.get_api_service_settings()
        assert settings["rate_limit"] == 60
        assert settings["max_tokens_per_user"] == 1
        assert client.get("/api/v1/pages", headers=_auth(_token(admin_user, ["pages"]))).status_code == 200

    def test_api_service_limits_are_clamped_like_the_admin_form(self, client, admin_user):
        response = self._put(client, admin_user, {
            "api_service_rate_limit": 10**9,
            "api_service_admin_rate_limit": 0,
            "api_service_max_tokens_per_user": 5000,
        })
        assert response.status_code == 200
        settings = db.get_api_service_settings()
        assert settings["rate_limit"] == 10000
        assert settings["admin_rate_limit"] == 1
        assert settings["max_tokens_per_user"] == 100

    @pytest.mark.parametrize("payload", [
        {"primary_color": "red"},
        {"timezone": "Mars/Olympus"},
        {"default_theme_mode": "sepia"},
        {"site_name": "x" * 101},
        {"site_name": {"nested": True}},
        {"maintenance_mode": "maybe"},
        {"interface_language": "xx"},
        {"interface_language": ["en"]},
        {"timezone": "America"},
        {"no_such_setting": 1},
        # str.isdigit() accepts these, int() does not; they used to be a 500.
        {"auto_logout_hour": "²"},
        {"auto_logout_hour": "--5"},
        {"auto_logout_hour": "9" * 5000},
        # Moving this to UTC passes year 9999; it used to be a 500.
        {"public_mode_until": "9999-12-31T23:59:59-05:00"},
    ])
    def test_invalid_values_are_refused_before_anything_is_written(self, client, admin_user, payload):
        response = self._put(client, admin_user, {**payload, "maintenance_message": "should not be saved"})
        assert response.status_code == 400
        assert db.get_site_settings()["maintenance_message"] == ""

    def test_upload_size_is_owned_by_the_platform_on_hosted_instances(self, client, admin_user,
                                                                       monkeypatch, tmp_path):
        assert self._put(client, admin_user, {"upload_max_size_mb": 50}).status_code == 200
        assert db.get_site_settings()["upload_max_size_mb"] == 50
        monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
        response = self._put(client, admin_user, {"upload_max_size_mb": 2048})
        assert response.status_code == 403
        assert db.get_site_settings()["upload_max_size_mb"] == 50

    def test_plugin_settings_need_the_plugin(self, client, admin_user):
        db.disable_plugin("kanban")
        response = self._put(client, admin_user, {"kanban_access": "all"})
        assert response.status_code == 403
        assert db.get_site_settings()["kanban_access"] == "admin"

    def test_easywiki_keeps_tts_settings_off(self, client, admin_user, monkeypatch):
        monkeypatch.setenv("BW_EASY_WIKI", "1")
        response = self._put(client, admin_user, {"tts_auto_generate_enabled": 1})
        assert response.status_code == 403
        assert db.get_site_settings()["tts_auto_generate_enabled"] == 0

    @pytest.mark.parametrize("flag, key", [
        ("MANAGED_TTS_DISABLED", "tts_auto_generate_enabled"),
        ("FORBID_PUBLIC_MODE", "public_mode"),
        ("FORBID_PAGE_BUILDER", "page_builder_enabled"),
    ])
    def test_features_the_platform_switched_off_stay_off(self, client, admin_user, monkeypatch, flag, key):
        monkeypatch.setattr(config, flag, True)
        response = self._put(client, admin_user, {key: True})
        assert response.status_code == 403
        assert not db.get_site_settings()[key]

    def test_a_naive_far_future_expiry_west_of_utc_is_refused(self, client, admin_user):
        """Read in a site time zone behind UTC, year 9999 ends up past the calendar."""
        db.update_site_settings(timezone="America/New_York")
        response = self._put(client, admin_user, {"open_signup_until": "9999-12-31T23:59:59"})
        assert response.status_code == 400
        assert db.get_site_settings()["open_signup_until"] in ("", None)

    def test_a_read_modify_write_round_trip_works(self, client, admin_user):
        """Clients may send back what GET returned with their own change applied."""
        token = _token(admin_user, ["settings"])
        current = client.get("/api/v1/settings", headers=_auth(token)).get_json()["settings"]
        current["site_name"] = "Round trip"
        response = client.put("/api/v1/settings", headers=_auth(token), json=current)
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["updated"] == ["site_name"]
        assert db.get_site_settings()["site_name"] == "Round trip"

    def test_legitimate_updates_still_work(self, client, admin_user):
        response = self._put(client, admin_user, {
            "site_name": "  Team wiki  ",
            "primary_color": "#123abc",
            "maintenance_message": "Back soon",
            "chat_dm_message_retention_days": 99999,
            "intro_role_switching_roles": ["editor", "user"],
        })
        assert response.status_code == 200, response.get_json()
        settings = db.get_site_settings()
        assert settings["site_name"] == "Team wiki"
        assert settings["primary_color"] == "#123abc"
        assert settings["chat_dm_message_retention_days"] == 3650
        assert settings["intro_role_switching_roles"] == "user,editor"
        # Saving the split chat settings retires the legacy fallback, as the form does.
        assert settings["chat_cleanup_split_configured"] == 1


# Scopes and banana mode

class TestScopesFollowTheRole:

    def test_member_cannot_mint_admin_scopes_through_the_form(self, client, admin_user):
        member = _member_with_api_access()
        client.post("/login", data={"username": "member", "password": "member-pass-1"})
        client.post("/settings/api-tokens/create",
                    data={"name": "x", "scopes": ["admin", "settings", "users", "pages"]})
        stored = json.loads(db.list_user_tokens(member)[0]["permissions"])
        assert stored["scopes"] == ["pages"]

    def test_member_admin_scope_token_cannot_touch_banana_mode(self, client, admin_user):
        """Tokens made before the fix may still carry the admin scope."""
        member = _member_with_api_access()
        token = _token(member, ["admin"])
        assert client.get("/api/v1/banana-mode", headers=_auth(token)).status_code == 403
        assert client.post("/api/v1/banana-mode", headers=_auth(token)).status_code == 403
        assert db.get_site_settings()["banana_mode"] == 0

    def test_member_cannot_issue_an_admin_scope_child_token(self, client, admin_user):
        member = _member_with_api_access()
        token = _token(member, ["tokens", "admin"])
        response = client.post("/api/v1/tokens", headers=_auth(token), json={
            "permissions": {"read": True, "write": True, "scopes": ["admin"]}})
        assert response.status_code == 403

    def test_admin_still_gets_admin_scopes_and_banana_mode(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = client.post("/settings/api-tokens/create",
                               data={"name": "ops", "scopes": ["admin", "users"]}, follow_redirects=True)
        token = _token_from_flash(response)
        assert client.post("/api/v1/banana-mode", headers=_auth(token)).get_json()["banana_mode"] is True


class TestTokenForm:

    def _create(self, client, **form):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = client.post("/settings/api-tokens/create", data=form, follow_redirects=True)
        return response

    def test_a_read_write_token_can_read(self, client, admin_user):
        token = _token_from_flash(self._create(client, name="rw", scopes=["pages"]))
        assert client.get("/api/v1/pages", headers=_auth(token)).status_code == 200
        created = client.post("/api/v1/pages", headers=_auth(token), json={"title": "Via form token"})
        assert created.status_code == 201

    def test_a_read_only_token_cannot_write(self, client, admin_user):
        token = _token_from_flash(self._create(client, name="ro", scopes=["pages"], read_only="1"))
        assert client.get("/api/v1/pages", headers=_auth(token)).status_code == 200
        assert client.post("/api/v1/pages", headers=_auth(token), json={"title": "No"}).status_code == 403

    def test_an_expiry_from_the_form_is_honoured(self, client, admin_user):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
        token = _token_from_flash(self._create(client, name="exp", scopes=["pages"], expires_at=future))
        assert client.get("/api/v1/pages", headers=_auth(token)).status_code == 200
        stored = db.list_user_tokens(admin_user)[0]["expires_at"]
        assert stored.endswith("+00:00")

    def test_a_past_expiry_is_refused(self, client, admin_user):
        self._create(client, name="old", scopes=["pages"], expires_at="2001-01-01T10:00")
        assert db.list_user_tokens(admin_user) == []

    def test_an_expiry_past_the_calendar_is_refused(self, client, admin_user):
        """Year 9999 read in a time zone behind UTC used to be a 500."""
        db.update_site_settings(timezone="America/New_York")
        response = self._create(client, name="far", scopes=["pages"], expires_at="9999-12-31T23:59")
        assert response.status_code == 200
        assert db.list_user_tokens(admin_user) == []

    def test_a_child_token_expiry_past_the_calendar_is_refused(self, client, admin_user):
        token = _token(admin_user, ["tokens"])
        response = client.post("/api/v1/tokens", headers=_auth(token), json={
            "permissions": {"read": True, "write": False, "scopes": ["tokens"]},
            "expires_at": "9999-12-31T23:59:59-05:00",
        })
        assert response.status_code == 400

    def test_tokens_stored_without_an_offset_still_work(self, client, admin_user):
        """Older versions of the form stored the datetime-local value as is."""
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
        token = db.create_api_token(admin_user, permissions={"read": True, "write": False, "scopes": ["pages"]},
                                    expires_at=future)
        assert client.get("/api/v1/pages", headers=_auth(token)).status_code == 200


# Account state

class TestTokensFollowTheAccount:

    def test_forced_password_change_blocks_tokens(self, client, admin_user):
        token = _token(admin_user, ["users"])
        db.update_user(admin_user, password=generate_password_hash("reset-by-platform"),
                       force_password_change=1)
        assert client.get("/api/v1/users", headers=_auth(token)).status_code == 403
        db.update_user(admin_user, force_password_change=0)
        assert client.get("/api/v1/users", headers=_auth(token)).status_code == 200

    def test_pending_onboarding_blocks_admin_tokens(self, client, admin_user):
        token = _token(admin_user, ["pages"])
        db.update_user(admin_user, onboarding_required=1)
        assert client.get("/api/v1/pages", headers=_auth(token)).status_code == 403

    def test_maintenance_mode_stops_non_admin_tokens(self, client, admin_user, editor_user):
        db.update_user(editor_user, api_access_enabled=1)
        editor_token = _token(editor_user, ["pages"])
        admin_token = _token(admin_user, ["pages"])
        db.update_site_settings(maintenance_mode=1)
        response = client.post("/api/v1/pages", headers=_auth(editor_token), json={"title": "During maintenance"})
        assert response.status_code == 503
        assert db.get_page_by_slug("during-maintenance") is None
        assert client.get("/api/v1/pages", headers=_auth(admin_token)).status_code == 200

    def test_revoke_user_api_service_tokens_revokes_everything(self, admin_user):
        first = _token(admin_user, ["pages"])
        _token(admin_user, ["tokens"])
        db.create_userbot_token(admin_user)
        assert db.revoke_user_api_service_tokens(admin_user, reason="test") == 3
        assert db.verify_api_service_token(first) == (None, None)
        assert db.count_user_tokens(admin_user) == 0
        assert db.revoke_user_api_service_tokens(admin_user) == 0

    def test_password_change_through_the_api_revokes_the_targets_tokens_and_sessions(
            self, client, admin_user, regular_user):
        db.update_user(regular_user, api_access_enabled=1)
        victim_token = _token(regular_user, ["pages"])
        db.create_user_session(regular_user)
        admin_token = _token(admin_user, ["users"])
        response = client.put(f"/api/v1/users/{regular_user}", headers=_auth(admin_token),
                              json={"password": "fresh-password-1"})
        assert response.status_code == 200
        assert response.get_json()["api_tokens_revoked"] == 1
        assert check_password_hash(db.get_user_by_id(regular_user)["password"], "fresh-password-1")
        assert client.get("/api/v1/pages", headers=_auth(victim_token)).status_code == 401
        with db.get_db_context() as conn:
            live = conn.execute("SELECT COUNT(*) FROM user_sessions WHERE user_id=? AND revoked_at IS NULL",
                                (regular_user,)).fetchone()[0]
        assert live == 0


# User management

class TestUserManagementRules:

    @pytest.fixture
    def superuser(self):
        user_id = db.create_user("protected", generate_password_hash("protected-pass-1"), role="admin")
        db.update_user(user_id, is_superuser=1)
        return user_id

    def test_superuser_cannot_be_changed(self, client, admin_user, superuser):
        token = _token(admin_user, ["users", "admin"])
        response = client.put(f"/api/v1/users/{superuser}", headers=_auth(token),
                              json={"role": "user", "password": "short", "suspended": True})
        assert response.status_code == 403
        target = db.get_user_by_id(superuser)
        assert target["role"] == "admin" and not target["suspended"]
        assert client.delete(f"/api/v1/users/{superuser}", headers=_auth(token)).status_code == 403
        assert client.put(f"/api/v1/admin/users/{superuser}/api-access", headers=_auth(token),
                          json={"enabled": False}).status_code == 403
        assert db.get_user_by_id(superuser) is not None

    @pytest.mark.parametrize("username", ["a‮b\nc", "has space", "ab", "x" * 51, "semi;colon"])
    def test_usernames_follow_the_account_form(self, client, admin_user, username):
        token = _token(admin_user, ["users"])
        response = client.post("/api/v1/users", headers=_auth(token),
                               json={"username": username, "password": "long-enough-1"})
        assert response.status_code == 400
        assert db.get_user_by_username(username) is None

    @pytest.mark.parametrize("password", ["a", "seven77", "x" * 1025])
    def test_passwords_follow_the_account_form(self, client, admin_user, regular_user, password):
        token = _token(admin_user, ["users"])
        created = client.post("/api/v1/users", headers=_auth(token),
                              json={"username": "newcomer", "password": password})
        assert created.status_code == 400
        changed = client.put(f"/api/v1/users/{regular_user}", headers=_auth(token), json={"password": password})
        assert changed.status_code == 400
        assert check_password_hash(db.get_user_by_id(regular_user)["password"], "user123")

    def test_valid_accounts_are_still_created(self, client, admin_user):
        token = _token(admin_user, ["users"])
        response = client.post("/api/v1/users", headers=_auth(token),
                               json={"username": "x" * 50, "password": "long-enough-1", "role": "editor"})
        assert response.status_code == 201

    def test_bulk_user_creation_is_capped(self, client, admin_user):
        token = _token(admin_user, ["users"])
        users = [{"username": f"bulk{i}", "password": "long-enough-1"} for i in range(21)]
        response = client.post("/api/v1/users/bulk", headers=_auth(token), json={"users": users})
        assert response.status_code == 400
        assert db.get_user_by_username("bulk0") is None
        response = client.post("/api/v1/users/bulk", headers=_auth(token), json={"users": users[:20]})
        assert response.status_code == 201
        assert len(response.get_json()["created"]) == 20

    def test_bulk_user_items_must_be_objects(self, client, admin_user):
        token = _token(admin_user, ["users"])
        response = client.post("/api/v1/users/bulk", headers=_auth(token), json={
            "users": [{"username": "fine", "password": "long-enough-1"}, "not an object"]})
        assert response.status_code == 400
        assert db.get_user_by_username("fine") is None


# Pages

class TestPageRules:

    def test_page_limits_match_the_editor(self, client, admin_user):
        token = _token(admin_user, ["pages"])
        too_big = client.post("/api/v1/pages", headers=_auth(token),
                              json={"title": "Big", "content": "a" * 1_000_001})
        assert too_big.status_code == 400
        too_long = client.post("/api/v1/pages", headers=_auth(token), json={"title": "T" * 201})
        assert too_long.status_code == 400
        odd_slug = client.post("/api/v1/pages", headers=_auth(token),
                               json={"title": "Fine", "slug": "a/../b c"})
        assert odd_slug.status_code == 201
        assert odd_slug.get_json()["page"]["slug"] == "ab-c"
        empty_title = client.put("/api/v1/pages/ab-c", headers=_auth(token), json={"title": "   "})
        assert empty_title.status_code == 400
        assert db.get_page_by_slug("ab-c")["title"] == "Fine"
        long_slug = client.post("/api/v1/pages", headers=_auth(token),
                                json={"title": "Long slug", "slug": "s" * 201})
        assert long_slug.status_code == 400
        assert db.get_page_by_slug("s" * 201) is None

    @pytest.mark.parametrize("category_id", [
        # The JSON parser turns Infinity into a float, and ids past SQLite's
        # integer range overflow in the database; both used to be a 500.
        "Infinity", "1e300", str(2**64), '"²"', "1.5", "-3", "true",
    ])
    def test_category_ids_are_checked_before_the_database(self, client, admin_user, category_id):
        token = _token(admin_user, ["pages", "categories"])
        headers = {**_auth(token), "Content-Type": "application/json"}
        created = client.post("/api/v1/pages", headers=headers,
                              data='{"title": "Odd id", "category_id": %s}' % category_id)
        assert created.status_code == 400
        assert db.get_page_by_slug("odd-id") is None
        db.create_page("Target", "target", "x")
        moved = client.put("/api/v1/pages/target", headers=headers,
                           data='{"category_id": %s}' % category_id)
        assert moved.status_code == 400
        assert db.get_page_by_slug("target")["category_id"] is None
        category = client.post("/api/v1/categories", headers=headers,
                               data='{"name": "Odd parent", "parent_id": %s}' % category_id)
        assert category.status_code == 400

    def test_bulk_create_checks_every_item_before_writing(self, client, admin_user):
        token = _token(admin_user, ["pages"])
        response = client.post("/api/v1/pages/bulk", headers=_auth(token),
                               json={"pages": [{"title": "ok1"}, {"title": 5}]})
        assert response.status_code == 400
        assert db.get_page_by_slug("ok1") is None
        response = client.post("/api/v1/pages/bulk", headers=_auth(token),
                               json={"pages": [{"title": "Z", "category_id": 999999},
                                               {"title": "Kept", "slug": "Kept Slug"}]})
        assert response.status_code == 201
        body = response.get_json()
        assert body["errors"] == [{"title": "Z", "error": "Category does not exist"}]
        assert body["created"][0]["slug"] == "kept-slug"

    def test_bulk_edit_checks_every_item_before_writing(self, client, admin_user):
        db.create_page("One", "one", "x")
        token = _token(admin_user, ["pages"])
        response = client.post("/api/v1/pages/bulk-edit", headers=_auth(token), json={
            "edits": [{"slug": "one", "title": "Changed"}, {"slug": "one", "content": 7}]})
        assert response.status_code == 400
        assert db.get_page_by_slug("one")["title"] == "One"

    def test_request_bodies_are_capped_even_without_a_length(self, client, admin_user):
        token = _token(admin_user, ["pages"])
        body = json.dumps({"title": "Huge", "content": "b" * (17 * 1024 * 1024)}).encode()
        response = client.open(
            "/api/v1/pages", method="POST",
            headers={**_auth(token), "Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            input_stream=io.BytesIO(body), environ_overrides={"wsgi.input_terminated": True},
        )
        assert response.status_code == 413
        assert db.get_page_by_slug("huge") is None

    def test_a_lower_app_wide_limit_is_kept(self, client, admin_user, monkeypatch):
        """The API's own cap must not raise a smaller limit the operator chose."""
        monkeypatch.setitem(client.application.config, "MAX_CONTENT_LENGTH", 64 * 1024)
        token = _token(admin_user, ["pages"])
        body = json.dumps({"title": "Mid", "content": "b" * (128 * 1024)}).encode()
        response = client.open(
            "/api/v1/pages", method="POST",
            headers={**_auth(token), "Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            input_stream=io.BytesIO(body), environ_overrides={"wsgi.input_terminated": True},
        )
        assert response.status_code == 413
        assert db.get_page_by_slug("mid") is None

    def test_api_delete_tells_plugins(self, client, admin_user):
        from bananawiki_sdk._hooks import _hook_lock, _hook_registry
        seen = []
        with _hook_lock:
            _hook_registry["after_page_delete"].append(lambda **kwargs: seen.append(kwargs["page"]["slug"]))
        db.create_page("Gone", "gone", "x")
        token = _token(admin_user, ["pages"])
        assert client.delete("/api/v1/pages/gone", headers=_auth(token)).status_code in (200, 202)
        assert seen == ["gone"]


class TestBulkDelete:

    def test_input_is_type_checked_and_capped(self, client, admin_user):
        token = _token(admin_user, ["pages"])
        assert client.post("/api/v1/pages/bulk-delete", headers=_auth(token),
                           json={"slugs": [{"x": 1}]}).status_code == 400
        assert client.post("/api/v1/pages/bulk-delete", headers=_auth(token),
                           json={"ids": ["one"]}).status_code == 400
        assert client.post("/api/v1/pages/bulk-delete", headers=_auth(token),
                           json={"slugs": [f"p{i}" for i in range(101)]}).status_code == 400
        huge = client.post("/api/v1/pages/bulk-delete", headers=_auth(token), json={"ids": [2**80]})
        assert huge.status_code == 200 and huge.get_json()["not_found"] == 1
        # Digit strings: too long to be an id counts as not found, and digits
        # int() cannot read are refused instead of raising a 500.
        long_digits = client.post("/api/v1/pages/bulk-delete", headers=_auth(token),
                                  json={"ids": ["9" * 5000]})
        assert long_digits.status_code == 200 and long_digits.get_json()["not_found"] == 1
        assert client.post("/api/v1/pages/bulk-delete", headers=_auth(token),
                           json={"ids": ["²"]}).status_code == 400

    def test_docs_pages_keep_their_grace_period_unless_bypass_is_on(self, client, admin_user):
        db.enable_plugin("deletion_slowdown")
        docs = db.create_category("Docs")
        db.create_page("Guide", "guide", "x", category_id=docs)
        with db.get_db_context() as conn:
            conn.execute("UPDATE site_settings SET docs_category_id=?, "
                         "docs_bypass_deletion_slowdown=0 WHERE id=1", (docs,))
            conn.commit()
        token = _token(admin_user, ["pages"])
        response = client.post("/api/v1/pages/bulk-delete", headers=_auth(token), json={"slugs": ["guide"]})
        assert response.get_json()["deleted"] == 1
        page = db.get_page_by_slug("guide")
        assert page is not None and page["pending_deletion"]

    def test_locked_and_scheduled_pages_are_skipped(self, client, admin_user, editor_user):
        db.enable_plugin("page_governance")
        db.update_site_settings(page_protection_enabled=1)
        locked = db.create_page("Locked", "locked", "x")
        db.set_page_protection(locked, editor_user)
        scheduled = db.create_page("Scheduled", "scheduled", "x")
        db.set_page_expiry(scheduled, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        token = _token(admin_user, ["pages"])
        response = client.post("/api/v1/pages/bulk-delete", headers=_auth(token),
                               json={"slugs": ["locked", "scheduled"]})
        assert response.get_json()["skipped"] == 2
        assert not db.get_page_by_slug("locked")["pending_deletion"]
        assert not db.get_page_by_slug("scheduled")["pending_deletion"]


# Other reads

class TestReads:

    def test_audit_log_limit_cannot_be_disabled(self, client, admin_user):
        token = _token(admin_user, ["admin"])
        for _ in range(3):
            db.log_api_call(token_id=None, user_id=admin_user, username="admin",
                            endpoint="/x", method="GET", status_code=200)
        body = client.get("/api/v1/admin/audit-log?limit=-1&offset=-5", headers=_auth(token)).get_json()
        assert body["limit"] == 1 and body["offset"] == 0
        assert len(body["entries"]) == 1

    def test_categories_follow_the_read_restrictions(self, client, admin_user):
        visible = db.create_category("Visible")
        hidden = db.create_category("Hidden")
        reader = db.create_user("reader", generate_password_hash("reader-pass-1"), role="editor")
        db.update_user(reader, api_access_enabled=1)
        db.set_user_permissions(reader, {"page.view_all"}, read_restricted=True,
                                read_category_ids=[visible], write_restricted=True,
                                write_category_ids=[visible])
        body = client.get("/api/v1/categories", headers=_auth(_token(reader, ["categories"]))).get_json()
        ids = {category["id"] for category in body["categories"]}
        assert visible in ids and hidden not in ids
