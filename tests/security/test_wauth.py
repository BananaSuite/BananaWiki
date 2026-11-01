"""Regression tests for the security audit findings W1-1, W1-3..W1-7, W3-3, W3-4 and W4-5 (AUDIT.md).

Each test names its finding and reproduces the attack: it failed before the
fix and must keep passing.
"""

from __future__ import annotations

from tests.conftest import PASSWORD

# W1-1: the per-account sign-in limit follows the account, not the spelling


def _sign_in(client, username, password, address):
    return client.post("/login", data={"username": username, "password": password},
                       environ_base={"REMOTE_ADDR": address})


def test_w1_1_padded_or_recased_names_share_the_account_budget(app, make_user, monkeypatch):
    from bananawiki.wiki.features.auth import signin

    monkeypatch.setattr(signin, "MAX_PER_ACCOUNT_TOTAL", 3)
    make_user("victim")
    client = app.test_client()
    for i in range(3):
        assert _sign_in(client, "victim", "wrong-password", f"10.0.0.{i + 1}").status_code == 401
    assert _sign_in(client, "victim", PASSWORD, "10.9.9.9").status_code == 429
    assert _sign_in(client, "VICTIM", PASSWORD, "10.9.9.10").status_code == 429
    for i, padding in enumerate((" ", "　", " ", " ")):
        padded = "victim" + padding * 44 + "X"  # stripped, then cut to 50: the name and trailing spaces
        response = _sign_in(client, padded, PASSWORD, f"10.8.{i}.1")
        assert response.status_code != 302, padding
    # A name no account can have is never looked up, so it cannot sign in either.
    make_user("other")
    assert _sign_in(client, "other" + "　" * 45 + "X", PASSWORD, "10.7.0.1").status_code == 401


def test_w1_1_a_valid_name_still_signs_in(app, make_user):
    make_user("Plain_Name-1")
    assert _sign_in(app.test_client(), "plain_name-1", PASSWORD, "10.1.0.1").status_code == 302


# W1-3: an administrator's export of another account is audited


def test_w1_3_admin_export_of_another_account_is_audited(admin_client, make_user, db):
    alice = make_user("alice")
    response = admin_client.get(f"/admin/users/{alice['id']}/export")
    assert response.status_code == 200
    response.get_data()
    rows = db.all("SELECT target_id, details FROM audit_log WHERE action = 'user.data_exported'")
    assert [row["target_id"] for row in rows] == [alice["id"]]
    assert "alice" in rows[0]["details"]


# W1-4: editing or deleting a custom role follows the hierarchy for every holder


def _superuser_with_custom_role(make_user, db):
    holder = make_user("trusted", role="editor")
    db.execute("INSERT INTO custom_roles (name, description, base_role, read_restricted, write_restricted, "
               "created_at, updated_at) VALUES ('Support', '', 'editor', 0, 0, '2026-01-01 00:00:00', "
               "'2026-01-01 00:00:00')")
    role_id = db.scalar("SELECT id FROM custom_roles")
    db.execute("UPDATE users SET custom_role_id = ?, is_superuser = 1 WHERE id = ?", (role_id, holder["id"]))
    return holder, role_id


def test_w1_4_plain_admin_cannot_demote_a_superuser_through_its_custom_role(app, make_user, login, db):
    holder, role_id = _superuser_with_custom_role(make_user, db)
    client = app.test_client()
    login(client, make_user("plainadmin", role="admin"))

    assert client.post(f"/admin/roles/{role_id}", data={"name": "Support", "base_role": "user"}).status_code == 400
    assert db.scalar("SELECT role FROM users WHERE id = ?", (holder["id"],)) == "editor"
    client.post(f"/admin/roles/{role_id}/delete", data={"replacement_role_id": "none"})
    assert db.scalar("SELECT custom_role_id FROM users WHERE id = ?", (holder["id"],)) == role_id


def test_w1_4_roles_held_only_by_manageable_accounts_still_change(app, make_user, login, db):
    holder, role_id = _superuser_with_custom_role(make_user, db)
    db.execute("UPDATE users SET is_superuser = 0 WHERE id = ?", (holder["id"],))
    client = app.test_client()
    login(client, make_user("plainadmin", role="admin"))

    assert client.post(f"/admin/roles/{role_id}", data={"name": "Support", "base_role": "user"}).status_code == 302
    assert db.scalar("SELECT role FROM users WHERE id = ?", (holder["id"],)) == "user"


# W1-5: a temporary administrator cannot pause their own revert by switching the feature off


def _temporary_admin(app, make_user, db, *, original_role="user"):
    from datetime import timedelta

    from bananawiki.core.timeutil import to_sql, utcnow

    owner = make_user("theowner", role="owner")
    temp = make_user("tempadmin", role="admin")
    db.execute("INSERT INTO plugins (id, name, version, builtin, enabled, installed_at) "
               "VALUES ('temporary_accounts', 'x', '1', 1, 1, '2026-01-01 00:00:00') "
               "ON CONFLICT(id) DO UPDATE SET enabled = 1")
    past = to_sql(utcnow() - timedelta(minutes=1))
    db.execute("INSERT INTO temp_roles (user_id, original_role, expires_at, show_countdown, set_by, created_at, "
               "updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)", (temp["id"], original_role, past, owner["id"], past, past))
    return owner, temp


def _feature_enabled(db) -> bool:
    return bool(db.scalar("SELECT enabled FROM plugins WHERE id = 'temporary_accounts'"))


def test_w1_5_temporary_admin_cannot_switch_the_feature_off(app, make_user, login, db):
    _owner, temp = _temporary_admin(app, make_user, db)
    client = app.test_client()
    login(client, temp)

    assert client.post("/admin/plugins/temporary_accounts/disable", data={"force": "1"}).status_code == 302
    assert _feature_enabled(db)
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="temporary_accounts.expire")
    assert db.scalar("SELECT role FROM users WHERE id = ?", (temp["id"],)) == "user"


def test_w1_5_another_plain_admin_cannot_pause_the_revert_either(app, make_user, login, db):
    _temporary_admin(app, make_user, db)
    client = app.test_client()
    login(client, make_user("friend", role="admin"))

    client.post("/admin/plugins/temporary_accounts/disable", data={"force": "1"})
    assert _feature_enabled(db)
    # Nor through the onboarding wizard, which switches every feature it offers.
    client.post("/onboarding", data={"site_name": "W", "mode": "advanced", "features": [],
                                     "default_theme_mode": "dark"})
    assert _feature_enabled(db)


def test_w1_5_owners_still_switch_the_feature_off(app, make_user, login, db):
    owner, _temp = _temporary_admin(app, make_user, db)
    client = app.test_client()
    login(client, owner)

    client.post("/admin/plugins/temporary_accounts/disable", data={"force": "1"})
    assert not _feature_enabled(db)


def test_w1_5_plain_admin_switches_off_when_only_user_schedules_wait(app, make_user, login, db):
    _owner, temp = _temporary_admin(app, make_user, db)
    db.execute("UPDATE users SET role = 'editor' WHERE id = ?", (temp["id"],))
    client = app.test_client()
    login(client, make_user("plainadmin", role="admin"))

    client.post("/admin/plugins/temporary_accounts/disable", data={"force": "1"})
    assert not _feature_enabled(db)


# W1-6: the merge-request form asks for the password first and cannot block other people's merges


def _request_merge(client, other, password=PASSWORD):
    return client.post("/settings/merge-request", data={"other_username": other, "direction": "into_mine",
                                                        "reason": "", "password": password})


def test_w1_6_merge_form_tells_nothing_about_the_other_account_without_the_password(app, make_user, login):
    make_user("theowner", role="owner", is_superuser=1)
    make_user("member")
    client = app.test_client()
    login(client, make_user("prober"))

    for name in ("theowner", "member", "nobody_here"):
        page = _request_merge(client, name, password="wrong-password").get_data(as_text=True)
        assert "Your current password is not correct." in page, name
    page = _request_merge(client, "theowner").get_data(as_text=True)
    assert "Protected accounts" not in page and "last owner" not in page
    assert "These accounts cannot be merged." in page


def test_w1_6_an_unconfirmed_request_does_not_block_the_other_account(app, make_user, login, db):
    victim = make_user("victim")
    make_user("friend")
    stranger = app.test_client()
    login(stranger, make_user("stranger"))
    _request_merge(stranger, "victim")
    stranger_id = db.scalar("SELECT id FROM account_merge_requests")

    own = app.test_client()
    login(own, victim)
    _request_merge(own, "friend")
    assert db.scalar("SELECT COUNT(*) FROM account_merge_requests WHERE created_by = ?", (victim["id"],)) == 1
    # Once the victim has committed to one merge, the stranger's request cannot be confirmed as well.
    own.post(f"/settings/merge/approve/{stranger_id}", data={"password": PASSWORD})
    assert db.scalar("SELECT status FROM account_merge_requests WHERE id = ?", (stranger_id,)) == "pending"


# W1-7: a merge does not move the source's hosting-portal sign-in to the target


def _merge_linked_account(app, make_user, db, name, *, delete_source):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.users import merge

    source, target = make_user(f"{name}_old"), make_user(f"{name}_new")
    admin = make_user(f"{name}_boss", role="owner")
    db.execute("INSERT INTO platform_oauth_links (account_id, user_id, portal_username, linked_at) "
               "VALUES (?, ?, 'old', '2026-01-01 00:00:00')", (f"portal-{name}", source["id"]))
    with app.test_request_context(), connection_scope():
        merge.execute(source, target, admin, delete_source=delete_source)
    return source, target


def test_w1_7_merge_keeps_the_portal_link_with_the_source(app, make_user, db):
    source, target = _merge_linked_account(app, make_user, db, "locked", delete_source=False)
    assert db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'portal-locked'") == source["id"]
    assert db.scalar("SELECT COUNT(*) FROM platform_oauth_links WHERE user_id = ?", (target["id"],)) == 0

    _source, target = _merge_linked_account(app, make_user, db, "deleted", delete_source=True)
    assert db.scalar("SELECT COUNT(*) FROM platform_oauth_links WHERE account_id = 'portal-deleted'") == 0


# W3-3: the "two-step verification required for administrators" gate applies to API tokens


def test_w3_3_admin_token_stops_until_two_step_is_turned_on(app, db, make_user, client):
    from tests.features.api_support import call, enable_api, issue

    enable_api(app, db)
    admin = make_user("boss", role="admin")
    token = issue(app, admin, scopes=("users", "admin", "tokens"))
    assert call(client, "GET", "/users", token).status_code == 200
    db.execute("UPDATE site_settings SET two_step_required_for_admins = 1 WHERE id = 1")

    response = call(client, "GET", "/users", token)
    assert response.status_code == 403
    assert response.json["code"] == "two_step_required"
    created = call(client, "POST", "/users", token,
                   json={"username": "newadmin", "password": "Another-strong-pass-123", "role": "admin"})
    assert created.status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'newadmin'") == 0


# W3-4: the setup token cannot be guessed without limit, and production refuses a short one


def test_w3_4_setup_token_guesses_are_limited(app_factory, monkeypatch):
    from bananawiki.wiki.features.auth import routes

    monkeypatch.setattr(routes, "SETUP_ATTEMPTS", 3)
    client = app_factory(setup_done=False).test_client()
    for _ in range(3):
        assert client.post("/setup", data={"setup_token": "guess"}).status_code == 403
    assert client.post("/setup", data={"setup_token": "test-setup-token"}).status_code == 429
    other = client.post("/setup", data={"setup_token": "test-setup-token"},
                        environ_base={"REMOTE_ADDR": "10.20.30.40"})
    assert other.status_code == 200


def test_w3_4_production_refuses_a_short_setup_token(app_factory):
    import dataclasses

    application = app_factory(setup_done=False)
    application.config["BW"] = dataclasses.replace(application.config["BW"], env="production",
                                                   setup_token="short")
    client = application.test_client()
    response = client.post("/setup", data={"setup_token": "short"})
    assert response.status_code == 403
    assert "too short" in response.get_data(as_text=True)
    assert client.post("/setup", data={"setup_token": "short", "step": "create", "username": "boss",
                                       "password": PASSWORD, "confirm_password": PASSWORD}).status_code == 403


# W4-5: deleting an account erases the addresses of its group messages


def test_w4_5_deleted_accounts_group_messages_lose_their_address(app, make_user, login, db):
    from tests.features.chat_support import JSON, create_group

    alice = make_user("alice")
    client = app.test_client()
    login(client, alice)
    group_id = create_group(client)
    client.post(f"/groups/{group_id}/send", data={"content": "hi"}, headers=JSON,
                environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert db.scalar("SELECT ip_address FROM group_messages WHERE sender_id = ?", (alice["id"],)) == "198.51.100.7"

    boss = app.test_client()
    login(boss, make_user("theowner", role="owner"))
    boss.post(f"/admin/users/{alice['id']}/edit", data={"action": "delete"})
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (alice["id"],)) == 0
    assert db.column("SELECT ip_address FROM group_messages WHERE content = 'hi'") == [""]


def test_w4_5_audit_addresses_of_deleted_accounts_are_for_owners_only(app, make_user, login, db):
    former = make_user("formeradmin", role="admin")
    app.test_client().post("/login", data={"username": "formeradmin", "password": PASSWORD},
                           environ_base={"REMOTE_ADDR": "203.0.113.9"})
    owner = make_user("theowner", role="owner")
    boss = app.test_client()
    login(boss, owner)
    boss.post(f"/admin/users/{former['id']}/edit", data={"action": "delete"})
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (former["id"],)) == 0

    plain = app.test_client()
    login(plain, make_user("plainadmin", role="admin"))
    assert "203.0.113.9" not in plain.get("/admin/audit").get_data(as_text=True)
    assert "203.0.113.9" in boss.get("/admin/audit").get_data(as_text=True)


# Account deletion removes the avatar at once, whoever deletes the account


def test_w1_coverage_admin_deletion_removes_profile_images(app, make_user, login, db):
    import io
    from pathlib import Path

    from tests.features.people_support import image_bytes

    bob = make_user("bob")
    client = app.test_client()
    login(client, bob)
    client.post("/settings/profile", data={"real_name": "Bob", "avatar": (io.BytesIO(image_bytes("JPEG")), "me.jpg")},
                content_type="multipart/form-data")
    avatar = Path(app.config["BW"].folders.uploads) / db.scalar(
        "SELECT avatar_filename FROM user_profiles WHERE user_id = ?", (bob["id"],))
    assert avatar.is_file()

    boss = app.test_client()
    login(boss, make_user("theowner", role="owner"))
    boss.post(f"/admin/users/{bob['id']}/edit", data={"action": "delete"})
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (bob["id"],)) == 0
    assert not avatar.exists()
