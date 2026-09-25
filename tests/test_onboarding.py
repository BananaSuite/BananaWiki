from werkzeug.security import generate_password_hash

import db


def test_setup_created_admin_redirects_to_onboarding_after_login(client):
    resp = client.post(
        "/setup",
        data={
            "username": "owner",
            "password": "ownerpass123",
            "confirm_password": "ownerpass123",
            "interface_language": "en",
        },
    )
    assert resp.status_code == 302

    user = db.get_user_by_username("owner")
    assert user["onboarding_required"] == 1

    login = client.post(
        "/login",
        data={"username": "owner", "password": "ownerpass123"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/onboarding")


def test_easy_onboarding_configures_starter_features(client, admin_user):
    db.update_user(admin_user, onboarding_required=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        "/onboarding",
        data={
            "mode": "easy",
            "site_name": "Knowledge Base",
            "interface_language": "en",
            "default_theme_mode": "light",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")

    settings = db.get_site_settings()
    assert settings["site_name"] == "Knowledge Base"
    assert settings["default_theme_mode"] == "light"
    assert settings["kanban_access"] == "editor"
    assert settings["canvas_access"] == "editor"
    assert db.is_plugin_enabled("kanban")
    assert db.is_plugin_enabled("canvas")
    assert db.is_plugin_enabled("drafts")
    assert db.is_plugin_enabled("tts")
    assert db.is_plugin_enabled("page_history")
    assert not db.is_plugin_enabled("chat")

    admin = db.get_user_by_id(admin_user)
    assert admin["onboarding_required"] == 0
    assert admin["onboarding_completed_at"]
    assert admin["intro_required"] == 1


def test_onboarding_does_not_change_site_language(client, admin_user):
    db.update_site_settings(interface_language="it")
    db.update_user(admin_user, onboarding_required=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        "/onboarding",
        data={
            "mode": "easy",
            "site_name": "Knowledge Base",
            "interface_language": "en",
            "default_theme_mode": "light",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert db.get_site_settings()["interface_language"] == "it"


def test_advanced_onboarding_creates_users_with_intro(client, admin_user):
    db.update_user(admin_user, onboarding_required=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        "/onboarding",
        data={
            "mode": "advanced",
            "site_name": "Team Wiki",
            "interface_language": "en",
            "default_theme_mode": "dark",
            "plugins": ["kanban", "canvas", "chat"],
            "kanban_access": "all",
            "canvas_access": "editor",
            "new_user_intro_enabled": "1",
            "new_username": ["writer"],
            "new_password": ["writerpass123"],
            "new_role": ["editor"],
            "new_force_password_change": ["0"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    created = db.get_user_by_username("writer")
    assert created is not None
    assert created["role"] == "editor"
    assert created["intro_required"] == 1
    assert created["force_password_change"] == 1
    assert db.get_site_settings()["new_user_intro_enabled"] == 1
    assert db.is_plugin_enabled("chat")


def test_admin_created_user_gets_intro_when_setting_enabled(logged_in_admin):
    db.update_site_settings(new_user_intro_enabled=1)
    resp = logged_in_admin.post(
        "/admin/users/create",
        data={
            "username": "newreader",
            "password": "readerpass123",
            "confirm_password": "readerpass123",
            "role": "user",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    user = db.get_user_by_username("newreader")
    assert user["intro_required"] == 1


def test_admin_created_user_can_require_password_change(client, logged_in_admin):
    db.update_site_settings(new_user_intro_enabled=1)
    resp = logged_in_admin.post(
        "/admin/users/create",
        data={
            "username": "tempreader",
            "password": "readerpass123",
            "confirm_password": "readerpass123",
            "role": "user",
            "force_password_change": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    user = db.get_user_by_username("tempreader")
    assert user["force_password_change"] == 1
    assert user["intro_required"] == 1

    client.post("/logout")
    login = client.post(
        "/login",
        data={"username": "tempreader", "password": "readerpass123"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/force-change-password")


def test_force_password_change_blocks_url_and_post_bypass(client):
    db.update_site_settings(setup_done=1)
    user_id = db.create_user("mustchange", generate_password_hash("readerpass123"), role="user")
    db.update_user(user_id, force_password_change=1, intro_required=1)
    client.post("/login", data={"username": "mustchange", "password": "readerpass123"})

    get_resp = client.get("/", follow_redirects=False)
    assert get_resp.status_code == 302
    assert get_resp.headers["Location"].endswith("/force-change-password")

    post_resp = client.post("/tour/start", follow_redirects=False)
    assert post_resp.status_code == 403


def test_intro_required_controls_intro_page_access(client):
    db.update_site_settings(setup_done=1)
    user_id = db.create_user("needsintro", generate_password_hash("readerpass123"), role="user")
    db.update_user(user_id, intro_required=1)
    client.post("/login", data={"username": "needsintro", "password": "readerpass123"})

    intro_resp = client.get("/intro", follow_redirects=False)
    assert intro_resp.status_code == 200

    skip_resp = client.post("/intro", follow_redirects=False)
    assert skip_resp.status_code == 302
    assert db.get_user_by_id(user_id)["intro_required"] == 0

    home_resp = client.get("/", follow_redirects=False)
    assert home_resp.status_code == 200


def test_onboarding_required_blocks_url_and_post_bypass_until_setup(client, admin_user):
    db.update_user(admin_user, onboarding_required=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    get_resp = client.get("/admin/users", follow_redirects=False)
    assert get_resp.status_code == 302
    assert get_resp.headers["Location"].endswith("/onboarding")

    post_resp = client.post(
        "/admin/users/create",
        data={
            "username": "blocked",
            "password": "readerpass123",
            "confirm_password": "readerpass123",
            "role": "user",
        },
        follow_redirects=False,
    )
    assert post_resp.status_code == 403
    assert db.get_user_by_username("blocked") is None


def test_intro_completion_clears_required_flag(client, admin_user):
    user_id = db.create_user("reader", generate_password_hash("readerpass123"), role="user")
    db.update_user(user_id, intro_required=1)
    client.post("/login", data={"username": "reader", "password": "readerpass123"})

    resp = client.post("/intro", follow_redirects=False)
    assert resp.status_code == 302
    refreshed = db.get_user_by_id(user_id)
    assert refreshed["intro_required"] == 0
    assert refreshed["intro_completed_at"]


def test_intro_renders_live_tour_launcher_without_fake_preview(client, admin_user):
    db.update_user(admin_user, intro_required=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.get("/intro")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Start live guided tour" in html
    assert "Take the live tour" in html
    assert "Start from the wiki home" in html
    assert "Plugins control the feature surface" in html
    assert "intro-browser" not in html
    assert "data-tour-target" not in html
    assert "data-tour-overlay" not in html


def test_live_tour_start_sets_session_and_redirects(client, admin_user):
    db.update_user(admin_user, intro_required=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post("/tour/start", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess["guided_tour_active"] == 1
        assert sess["guided_tour_step"] == 0


def test_completed_user_can_restart_live_tour_by_default(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post("/tour/start", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess["guided_tour_active"] == 1
        assert sess["guided_tour_step"] == 0


def test_completed_user_intro_page_can_be_reopened_by_default(client):
    db.update_site_settings(setup_done=1)
    user_id = db.create_user("replayreader", generate_password_hash("readerpass123"), role="user")
    db.update_user(user_id, intro_required=0)
    client.post("/login", data={"username": "replayreader", "password": "readerpass123"})

    resp = client.get("/intro", follow_redirects=False)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Take the live tour" in html
    assert "Start live guided tour" in html


def test_onboarding_replay_setting_blocks_completed_user_restart(client):
    db.update_site_settings(setup_done=1, onboarding_replay_disabled=1)
    user_id = db.create_user("blockedreplay", generate_password_hash("readerpass123"), role="user")
    db.update_user(user_id, intro_required=0)
    client.post("/login", data={"username": "blockedreplay", "password": "readerpass123"})

    intro = client.get("/intro", follow_redirects=False)
    start = client.post("/tour/start", follow_redirects=False)

    assert intro.status_code == 302
    assert intro.headers["Location"].endswith("/")
    assert start.status_code == 302
    assert start.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert "guided_tour_active" not in sess
        assert "guided_tour_step" not in sess


def test_completed_admin_can_reopen_onboarding_by_default(client, admin_user):
    db.update_user(admin_user, onboarding_required=0, intro_required=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.get("/onboarding", follow_redirects=False)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Onboarding replay" in html
    assert "Update setup" in html
    assert 'name="interface_language"' not in html


def test_onboarding_replay_setting_blocks_completed_admin_setup(client, admin_user):
    db.update_site_settings(onboarding_replay_disabled=1)
    db.update_user(admin_user, onboarding_required=0, intro_required=0)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.get("/onboarding", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_live_tour_overlay_renders_on_real_pages(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    with client.session_transaction() as sess:
        sess["guided_tour_active"] = 1
        sess["guided_tour_step"] = 0

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'data-live-tour' in html
    assert 'data-live-tour-spotlight' in html
    assert 'window.BW_LIVE_TOUR' in html
    assert "Start from the wiki home" in html
    assert "Click the highlighted site name to continue." in html


def test_live_tour_pages_render_stable_tour_targets(logged_in_admin):
    db.set_plugins_enabled(["kanban", "canvas"], enabled=True)

    expected_targets = {
        "/": [
            'data-tour-id="site-logo"',
            'data-tour-id="sidebar-search"',
            'data-tour-id="sidebar-new-page"',
        ],
        "/settings": ['data-tour-id="personal-settings-panel"'],
        "/create-page": ['data-tour-id="create-page-title"'],
        "/admin/settings": ['data-tour-id="admin-site-name"'],
        "/admin/users": ['data-tour-id="admin-create-user"'],
        "/admin/plugins": ['data-tour-id="admin-plugins-list"'],
        "/kanban": ['data-tour-id="kanban-page"', 'data-tour-id="kanban-new-board"'],
        "/canvas": ['data-tour-id="canvas-page"', 'data-tour-id="canvas-new-canvas"'],
    }

    for path, targets in expected_targets.items():
        resp = logged_in_admin.get(path, follow_redirects=True)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        for target in targets:
            assert target in html


def test_live_tour_step_navigation_redirects_to_real_page(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    with client.session_transaction() as sess:
        sess["guided_tour_active"] = 1
        sess["guided_tour_step"] = 0

    first = client.post("/tour/step/1", follow_redirects=False)
    assert first.status_code == 302
    assert first.headers["Location"].endswith("/")

    resp = client.post("/tour/step/2", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/settings")
    with client.session_transaction() as sess:
        assert sess["guided_tour_step"] == 2


def test_live_tour_rejects_skipped_step_jump(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    with client.session_transaction() as sess:
        sess["guided_tour_active"] = 1
        sess["guided_tour_step"] = 0

    resp = client.post("/tour/step/4", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess["guided_tour_step"] == 0


def test_live_tour_finish_clears_intro_and_session(client, admin_user):
    db.update_user(admin_user, intro_required=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    with client.session_transaction() as sess:
        sess["guided_tour_active"] = 1
        sess["guided_tour_step"] = 1

    resp = client.post("/tour/finish", follow_redirects=False)

    assert resp.status_code == 302
    refreshed = db.get_user_by_id(admin_user)
    assert refreshed["intro_required"] == 0
    assert refreshed["intro_completed_at"]
    with client.session_transaction() as sess:
        assert "guided_tour_active" not in sess
        assert "guided_tour_step" not in sess


def test_user_can_preview_admin_tour_without_admin_permissions(client):
    db.update_site_settings(setup_done=1, intro_role_switching_enabled=1)
    user_id = db.create_user("curious", generate_password_hash("readerpass123"), role="user")
    db.update_user(user_id, intro_required=1)
    client.post("/login", data={"username": "curious", "password": "readerpass123"})

    resp = client.post(
        "/tour/start",
        data={"tour_role": "admin"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess["guided_tour_role"] == "admin"
        assert sess["guided_tour_preview_path"] == "/"

        sess["guided_tour_step"] = 7
        sess["guided_tour_preview_path"] = "/global-settings"

    preview = client.get("/global-settings")
    html = preview.get_data(as_text=True)
    assert preview.status_code == 200
    assert "Site Settings" in html
    assert "Showing the real Admin page as a read-only tour preview" in html
    assert "Your account remains User" in html

    off_step = client.get("/admin/users", follow_redirects=False)
    assert off_step.status_code == 302
    assert off_step.headers["Location"].endswith("/global-settings")

    blocked = client.post(
        "/admin/users/create",
        data={
            "username": "shouldnotexist",
            "password": "readerpass123",
            "confirm_password": "readerpass123",
            "role": "admin",
        },
        follow_redirects=False,
    )
    assert blocked.status_code == 403
    assert db.get_user_by_username("shouldnotexist") is None
    assert db.get_user_by_id(user_id)["role"] == "user"


def test_role_perspective_switch_can_be_disabled_for_users(client):
    db.update_site_settings(
        setup_done=1,
        intro_role_switching_enabled=1,
        intro_role_switching_roles="editor,admin",
    )
    user_id = db.create_user("noswitch", generate_password_hash("readerpass123"), role="user")
    db.update_user(user_id, intro_required=1)
    client.post("/login", data={"username": "noswitch", "password": "readerpass123"})

    intro = client.get("/intro")
    html = intro.get_data(as_text=True)
    assert 'name="tour_role" value="user"' in html
    assert 'value="admin"' not in html

    resp = client.post(
        "/tour/start",
        data={"tour_role": "admin"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess["guided_tour_role"] == "user"


def test_onboarding_only_offers_installable_plugins(client, admin_user):
    """Every plugin the setup form offers must actually be installable.

    Experimental built-ins are deliberately not seeded by
    ``seed_builtin_plugins``, so offering one here would render a checkbox
    that silently does nothing when ticked.
    """
    import db
    from routes.onboarding import OPTIONAL_PLUGIN_IDS, CORE_PLUGIN_IDS

    installed = {row["id"] for row in db.list_plugins()}
    offered = set(OPTIONAL_PLUGIN_IDS) | CORE_PLUGIN_IDS
    assert offered <= installed, (
        "onboarding offers plugins that are never installed: "
        f"{sorted(offered - installed)}"
    )
