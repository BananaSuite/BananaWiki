"""Account settings, sessions, profiles and the member list."""

from __future__ import annotations

import io
import json

from conftest import PASSWORD

from .people_support import (
    edit_page,
    image_bytes,
    make_category,
    make_page,
    publish,
    restrict_read,
    set_feature,
)

NEW_PASSWORD = "an entirely new secret"


# ── Account settings ──────────────────────────────────────────────────────────


def test_settings_requires_login(client):
    assert "/login" in client.get("/settings").headers["Location"]


def test_legacy_account_urls_redirect(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/account").headers["Location"].endswith("/settings")
    assert client.get("/account/settings").status_code == 301


def test_settings_page_lists_sections(client, make_user, login):
    login(client, make_user("bob"))
    page = client.get("/settings")
    assert page.status_code == 200
    assert b"Change username" in page.data and b"Your data" in page.data


def test_change_username_needs_password(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    client.post("/settings/username", data={"new_username": "robert", "password": "wrong-password"})
    assert db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == "bob"


def test_change_username_records_history_and_rewrites_mentions(app, client, make_user, login, db):
    user = make_user("bob")
    page = make_page(app, "Team", "Ask @bob about it.")
    login(client, user)
    response = client.post("/settings/username", data={"new_username": "robert", "password": PASSWORD})
    assert response.status_code == 302
    assert db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == "robert"
    assert db.scalar("SELECT old_username FROM username_history WHERE user_id = ?", (user["id"],)) == "bob"
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (page["id"],)) == "Ask @robert about it."
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],)) == 2


def test_change_username_refuses_taken_name(client, make_user, login, db):
    user = make_user("bob")
    make_user("alice")
    login(client, user)
    client.post("/settings/username", data={"new_username": "ALICE", "password": PASSWORD})
    assert db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == "bob"


def test_change_password_keeps_this_session_and_ends_others(app, make_user, login, db):
    user = make_user("bob")
    first, second = app.test_client(), app.test_client()
    login(first, user)
    login(second, user)
    response = first.post("/settings/password", data={
        "current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert response.status_code == 302
    assert first.get("/settings").status_code == 200
    assert "/login" in second.get("/settings").headers["Location"]


def test_change_password_validation(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    before = db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],))
    client.post("/settings/password", data={"current_password": "wrong", "new_password": NEW_PASSWORD,
                                             "confirm_password": NEW_PASSWORD})
    client.post("/settings/password", data={"current_password": PASSWORD, "new_password": NEW_PASSWORD,
                                             "confirm_password": "different"})
    client.post("/settings/password", data={"current_password": PASSWORD, "new_password": "short",
                                             "confirm_password": "short"})
    assert db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],)) == before


def test_wrong_passwords_are_rate_limited(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    for _ in range(10):
        client.post("/settings/username", data={"new_username": "robert", "password": "wrong-password"})
    client.post("/settings/username", data={"new_username": "robert", "password": PASSWORD})
    assert db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == "bob"


def test_delete_own_account(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    client.post("/settings/delete", data={"password": "wrong-password"})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (user["id"],))
    response = client.post("/settings/delete", data={"password": PASSWORD})
    assert response.headers["Location"].endswith("/login")
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (user["id"],)) is None


def test_protected_and_last_admin_accounts_cannot_be_deleted(client, make_user, login, db):
    boss = make_user("boss", role="admin")
    login(client, boss)
    client.post("/settings/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (boss["id"],))
    client.post("/logout")
    keeper = make_user("keeper", is_superuser=1)
    login(client, keeper)
    client.post("/settings/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (keeper["id"],))


def test_deleting_an_account_replaces_mentions(app, client, make_user, login, db):
    user = make_user("bob")
    page = make_page(app, "Team", "Thanks @bob!")
    login(client, user)
    client.post("/settings/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (page["id"],)) == "Thanks @account deleted!"


def test_owner_toggle(client, make_user, login, db):
    first = make_user("first_owner", role="owner")
    boss = make_user("boss", role="admin")
    login(client, boss)
    client.post("/settings/owner", data={"password": PASSWORD})
    assert db.scalar("SELECT role FROM users WHERE id = ?", (boss["id"],)) == "admin", \
        "an administrator cannot make themselves owner while the wiki has one"
    db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (first["id"],))
    client.post("/settings/owner", data={"password": PASSWORD})
    assert db.scalar("SELECT role FROM users WHERE id = ?", (boss["id"],)) == "owner", "no owner yet: allowed"
    assert db.scalar("SELECT new_role FROM role_history WHERE user_id = ?", (boss["id"],)) == "owner"
    db.execute("UPDATE users SET role = 'owner' WHERE id = ?", (first["id"],))
    client.post("/settings/owner", data={"password": PASSWORD})
    assert db.scalar("SELECT role FROM users WHERE id = ?", (boss["id"],)) == "admin", "stepping down is allowed"


def test_superusers_may_make_themselves_owner(client, make_user, login, db):
    make_user("first_owner", role="owner")
    boss = make_user("boss", role="admin", is_superuser=1)
    login(client, boss)
    client.post("/settings/owner", data={"password": PASSWORD})
    assert db.scalar("SELECT role FROM users WHERE id = ?", (boss["id"],)) == "owner"


def test_owner_toggle_is_admin_only(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    assert client.post("/settings/owner", data={"password": PASSWORD}).status_code == 403


def test_suspended_admin_can_reactivate_self(client, make_user, login, db):
    boss = make_user("boss", role="admin", suspended=1)
    login(client, boss)
    assert b"Reactivate my account" in client.get("/account-status").data
    client.post("/settings/reactivate")
    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (boss["id"],)) == 0


def test_an_owners_suspension_cannot_be_lifted_by_the_admin(client, make_user, login, db):
    owner = make_user("first_owner", role="owner")
    boss = make_user("boss", role="admin", suspended=1)
    db.execute("INSERT INTO suspension_audit (user_id, action, performed_by, created_at) "
               "VALUES (?, 'suspend', ?, '2026-01-01 00:00:00')", (boss["id"], owner["id"]))
    login(client, boss)
    assert b"Reactivate my account" not in client.get("/account-status").data
    assert client.post("/settings/reactivate").status_code == 403
    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (boss["id"],)) == 1


def test_suspended_user_cannot_reactivate(client, make_user, login, db):
    user = make_user("bob", suspended=1)
    login(client, user)
    assert client.post("/settings/reactivate").status_code == 403
    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (user["id"],)) == 1


def test_language_is_saved_on_the_account(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    client.post("/settings/language", data={"language": "it", "next": "/settings"})
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],)))
    assert saved["interface_language"] == "it"
    assert "Impostazioni" in client.get("/settings").get_data(as_text=True)


def test_legacy_language_url_and_unknown_language(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    assert client.post("/account/language", data={"language": "xx"}).status_code == 302
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],)))
    assert saved["interface_language"] == "default"


# ── Sessions ─────────────────────────────────────────────────────────────────


def test_sessions_list_and_revoke_other(app, make_user, login, db):
    user = make_user("bob")
    first, second = app.test_client(), app.test_client()
    login(first, user)
    login(second, user)
    assert first.get("/settings/sessions").data.count(b"users-session") >= 2
    other = db.all("SELECT id FROM user_sessions WHERE user_id = ? ORDER BY created_at", (user["id"],))
    ids = [row["id"] for row in other]
    first.post(f"/settings/sessions/{ids[1]}/revoke")
    assert "/login" in second.get("/settings").headers["Location"]
    assert first.get("/settings").status_code == 200


def test_cannot_revoke_someone_elses_session(app, make_user, login, db):
    bob, eve = make_user("bob"), make_user("eve")
    bob_client, eve_client = app.test_client(), app.test_client()
    login(bob_client, bob)
    login(eve_client, eve)
    bob_session = db.scalar("SELECT id FROM user_sessions WHERE user_id = ?", (bob["id"],))
    assert eve_client.post(f"/settings/sessions/{bob_session}/revoke").status_code == 404
    assert bob_client.get("/settings").status_code == 200


def test_sign_out_everywhere_and_clear_history(app, make_user, login, db):
    user = make_user("bob")
    first, second = app.test_client(), app.test_client()
    login(first, user)
    login(second, user)
    assert first.post("/settings/sessions/logout-all").headers["Location"].endswith("/login")
    assert "/login" in second.get("/settings").headers["Location"]
    login(first, user)
    first.post("/settings/sessions/history/clear")
    assert db.scalar("SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (user["id"],)) == 1


# ── Display preferences ──────────────────────────────────────────────────────


def test_display_preferences_keep_the_1x_format(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    client.post("/settings/display", data={
        "theme_mode": "light", "font_scale": "1.35", "contrast": "3", "line_height": "1", "letter_spacing": "2",
        "reduce_motion": "1", "dyslexic_font": "1", "custom_accent_on": "1", "custom_accent": "#112233",
        "custom_bg": "#ffffff", "semantic_bold": "2", "sidebar_width": "9999",
    })
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],)))
    assert saved["theme_mode"] == "light" and saved["font_scale"] == 1.35 and saved["contrast"] == 3
    assert saved["custom_accent"] == "#112233" and saved["custom_bg"] == ""
    assert saved["sidebar_width"] == 500
    html = client.get("/settings").get_data(as_text=True)
    assert 'data-theme="light"' in html and 'data-font-scale="xlarge"' in html
    assert 'data-contrast="high"' in html and 'data-dyslexic="true"' in html
    assert "--bw-accent:#112233" in html and "--font-scale:1.35" in html
    assert '<option value="light" selected>' in client.get("/settings/display").get_data(as_text=True)


def test_existing_1x_preferences_are_honoured(client, make_user, login):
    legacy = json.dumps({"theme_mode": "light", "font_scale": 1.1, "contrast": 0, "line_height": 2,
                         "reduce_motion": 1, "custom_bg": "rgb(1, 2, 3)"})
    user = make_user("bob", accessibility=legacy)
    login(client, user)
    html = client.get("/settings").get_data(as_text=True)
    assert 'data-theme="light"' in html and 'data-font-scale="large"' in html
    assert 'data-line-height="relaxed"' in html and 'data-reduce-motion="true"' in html
    assert "--bw-bg:rgb(1, 2, 3)" in html


def test_invalid_preference_values_are_dropped(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    client.post("/api/accessibility", json={"theme_mode": "neon", "custom_text": "red;}body{display:none",
                                            "contrast": 99, "background_image": "../../etc/passwd"})
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],)))
    assert saved["theme_mode"] == "default" and saved["custom_text"] == "" and saved["contrast"] == 0
    assert saved["background_image"] == ""


def test_anonymous_preferences_live_in_a_cookie(client, db):
    db.execute("UPDATE site_settings SET public_mode = 1")
    response = client.post("/settings/display", data={"theme_mode": "light", "font_scale": "1.2"})
    assert "bw_public_accessibility" in response.headers.get("Set-Cookie", "")
    assert 'data-theme="light"' in client.get("/settings/display").get_data(as_text=True)
    assert client.get("/api/accessibility").json["font_scale"] == 1.2


def test_anonymous_preferences_need_public_mode(client):
    assert "/login" in client.post("/settings/display", data={"theme_mode": "light"}).headers["Location"]


def test_theme_switch_api_and_reset(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    assert client.post("/api/accessibility", json={"theme_mode": "light"}).json["ok"]
    assert 'data-theme="light"' in client.get("/settings").get_data(as_text=True)
    client.post("/api/accessibility/reset")
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],)))
    assert saved["theme_mode"] == "default"


def test_background_upload_is_reencoded_and_removed(app, client, make_user, login, db):
    from pathlib import Path

    user = make_user("bob")
    login(client, user)
    client.post("/settings/display/background",
                data={"background": (io.BytesIO(image_bytes(size=(3000, 1000))), "bg.png")},
                content_type="multipart/form-data")
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],)))
    name = saved["background_image"]
    assert name.startswith("backgrounds/") and name.endswith(".jpg")
    path = Path(app.config["BW"].folders.uploads) / name
    from PIL import Image

    with Image.open(path) as image:
        assert max(image.size) <= app.config["BW"].background_image_max_dimension
    assert f"/static/uploads/{name}" in client.get("/settings").get_data(as_text=True)
    client.post("/settings/display/background/remove")
    assert not path.exists()


def test_background_rejects_non_images(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    client.post("/settings/display/background", data={"background": (io.BytesIO(b"not an image"), "x.png")},
                content_type="multipart/form-data")
    assert db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],)) is None


# ── Profile editing ───────────────────────────────────────────────────────────


def test_edit_profile_and_avatar(app, client, make_user, login, db):
    from pathlib import Path

    user = make_user("bob")
    login(client, user)
    client.post("/settings/profile", data={
        "real_name": "  Bob   Builder ", "bio": "Hello", "birth_date": "1990-02-03",
        "avatar": (io.BytesIO(image_bytes("JPEG")), "me.jpg")}, content_type="multipart/form-data")
    profile = db.one("SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],))
    assert profile["real_name"] == "Bob Builder" and profile["birth_date"] == "1990-02-03"
    assert profile["avatar_filename"].startswith("avatars/")
    avatar = Path(app.config["BW"].folders.uploads) / profile["avatar_filename"]
    assert avatar.is_file()
    assert client.get(f"/static/uploads/{profile['avatar_filename']}").status_code == 200
    client.post("/settings/profile/avatar/remove")
    assert not avatar.exists()


def test_profile_rejects_bad_birth_date_and_non_image_avatar(client, make_user, login, db):
    user = make_user("bob")
    login(client, user)
    client.post("/settings/profile", data={"real_name": "Bob", "birth_date": "2999-01-01"})
    client.post("/settings/profile", data={"avatar": (io.BytesIO(b"<svg/>"), "x.svg")},
                content_type="multipart/form-data")
    assert db.scalar("SELECT avatar_filename FROM user_profiles WHERE user_id = ?", (user["id"],)) in (None, "")
    assert db.scalar("SELECT real_name FROM user_profiles WHERE user_id = ?", (user["id"],)) in (None, "")


def test_profile_editing_needs_the_profiles_feature(app, client, make_user, login):
    set_feature(app, "user_profiles", False)
    login(client, make_user("bob"))
    assert client.get("/settings/profile").status_code == 403


# ── Profiles and member list ─────────────────────────────────────────────────


def test_own_profile_is_always_visible(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/users/me").headers["Location"].endswith("/users/bob")
    assert client.get("/users/bob").status_code == 200


def test_unpublished_profiles_are_hidden_from_others(client, make_user, login, db):
    bob = make_user("bob")
    login(client, make_user("eve"))
    assert client.get("/users/bob").status_code == 404
    publish(db, bob)
    assert client.get("/users/bob").status_code == 200
    db.execute("UPDATE user_profiles SET page_disabled_by_admin = 1 WHERE user_id = ?", (bob["id"],))
    assert client.get("/users/bob").status_code == 404
    assert client.get("/users/nobody").status_code == 404


def test_admin_sees_every_profile(admin_client, make_user):
    make_user("bob")
    assert admin_client.get("/users/bob").status_code == 200


def test_profiles_hidden_when_feature_off(app, client, make_user, login, db):
    publish(db, make_user("bob"))
    set_feature(app, "user_profiles", False)
    login(client, make_user("eve"))
    assert client.get("/users/bob").status_code == 404
    assert client.get("/users/eve").status_code == 200


def test_profile_contributions_respect_page_visibility(app, client, make_user, login, db):
    bob, eve = make_user("bob", role="editor"), make_user("eve")
    open_cat, secret_cat = make_category(app, "Open"), make_category(app, "Secret")
    make_page(app, "Public notes", "x", author_id=bob["id"], category_id=open_cat["id"])
    secret = make_page(app, "Hidden plans", "x", author_id=bob["id"], category_id=secret_cat["id"])
    edit_page(app, secret["id"], "y", author_id=bob["id"])
    publish(db, bob)
    restrict_read(db, eve, [open_cat["id"]])
    login(client, eve)
    html = client.get("/users/bob").get_data(as_text=True)
    assert "Public notes" in html and "Hidden plans" not in html
    assert '"total":1' in html


def test_contribution_chart_can_be_switched_off(client, make_user, login, db):
    login(client, make_user("bob"))
    db.execute("UPDATE site_settings SET profile_contribution_chart_enabled = 0")
    assert b"users-calendar-data" not in client.get("/users/bob").data


def test_member_list(client, make_user, login, db):
    alice, _hidden = make_user("alice"), make_user("hidden_person")
    publish(db, alice)
    db.execute("UPDATE user_profiles SET real_name = 'Alice Liddell' WHERE user_id = ?", (alice["id"],))
    login(client, make_user("eve"))
    html = client.get("/users").get_data(as_text=True)
    assert "Alice Liddell" in html and "hidden_person" not in html
    assert "Alice" in client.get("/users?q=lidd").get_data(as_text=True)
    assert "Alice" not in client.get("/users?q=zzz").get_data(as_text=True)


def test_member_list_admin_sees_everyone(admin_client, make_user):
    make_user("hidden_person")
    assert b"hidden_person" in admin_client.get("/users").data


def test_member_list_requires_search_permission(client, make_user, login, db):
    user = make_user("bob")
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (user["id"],))
    login(client, user)
    assert client.get("/users").status_code == 403


def test_orphan_profile_images_are_collected(app, make_user, db):
    import os
    from pathlib import Path

    from bananawiki.wiki.features.users import service

    from .people_support import in_app

    folder = Path(app.config["BW"].folders.uploads) / "avatars"
    folder.mkdir(parents=True, exist_ok=True)
    stale = folder / ("a" * 32 + ".png")
    stale.write_bytes(b"x")
    os.utime(stale, (1, 1))
    assert in_app(app, service.collect_orphan_images) == 1
    assert not stale.exists()


def test_pages_render_for_admins(admin_client, make_user):
    bob = make_user("bob")
    for url in ("/settings", "/settings/profile", "/settings/display", "/settings/sessions", "/settings/merge-request",
                f"/settings/merge-request/from/{bob['username']}", "/settings/merge/pending", "/users", "/users/bob",
                "/admin/merge-requests", "/admin/merge-requests?status=all", "/admin/users/merge",
                "/admin/badges", "/admin/profile-fields", f"/admin/users/{bob['id']}/profile-fields",
                "/settings/profile-fields", "/badges/notifications"):
        assert admin_client.get(url).status_code == 200, url


def test_administrators_cannot_erase_their_own_role_history(app, make_user):
    from bananawiki.wiki.accounts import AccountError
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.admin import service as admin_service

    boss = make_user("boss", role="admin")
    with app.test_request_context(), connection_scope():
        try:
            admin_service.delete_role_history(boss, boss)
        except AccountError as error:
            assert error.key == "admin.attributions.error.own_history"
        else:
            raise AssertionError("deleting one's own role history was allowed")
