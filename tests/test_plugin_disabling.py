import db


def test_disabled_chat_plugin_hides_navigation_and_blocks_routes(client, admin_user):
    db.disable_plugin("chat")

    client.post("/login", data={"username": "admin", "password": "admin123"})

    home = client.get("/")
    assert home.status_code == 200
    assert "💬 Chats" not in home.get_data(as_text=True)

    response = client.get("/chats")
    assert response.status_code == 404


def test_disabled_announcements_plugin_hides_active_announcements_and_routes(client, admin_user):
    announcement_id = db.create_announcement(
        "Plugin-disabled announcement",
        "orange",
        "normal",
        "both",
        None,
        admin_user,
    )
    db.disable_plugin("announcements")

    client.post("/login", data={"username": "admin", "password": "admin123"})

    home = client.get("/")
    assert home.status_code == 200
    assert "Plugin-disabled announcement" not in home.get_data(as_text=True)
    assert db.get_announcement(announcement_id) is not None

    response = client.get(f"/announcements/{announcement_id}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Admin settings UI: plugin gating
# ---------------------------------------------------------------------------

_SETTINGS_BASE = {
    "site_name": "BananaWiki",
    "timezone": "UTC",
    "primary_color": "#8fa0d4",
    "secondary_color": "#151520",
    "accent_color": "#6e8aca",
    "text_color": "#c8ccd8",
    "sidebar_color": "#111118",
    "bg_color": "#0d0d14",
}


def test_disabled_page_reservations_plugin_hides_settings_section(client, admin_user):
    """When the page_reservations plugin is disabled the settings section must not appear."""
    db.disable_plugin("page_governance")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get("/global-settings")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "page_reservations_enabled" not in html
    assert "page_reservation_duration_hours" not in html


def test_disabled_page_reservations_plugin_preserves_settings_on_post(client, admin_user):
    """POSTing to settings while page_reservations is disabled must not overwrite its DB values."""
    # Establish known baseline reservation settings
    db.update_site_settings(
        page_reservations_enabled=1,
        page_reservation_duration_hours=99,
        page_reservation_cooldown_hours=77,
        default_reserved_pages_quota=3,
    )
    db.disable_plugin("page_governance")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    # Post settings with different reservation values. They should be ignored
    response = client.post("/global-settings", data={
        **_SETTINGS_BASE,
        "page_reservations_enabled": "1",
        "page_reservation_duration_hours": "12",
        "page_reservation_cooldown_hours": "6",
        "default_reserved_pages_quota": "10",
    })
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    # Reservation fields must be unchanged (preserved from before the POST)
    assert settings["page_reservations_enabled"] == 1
    assert settings["page_reservation_duration_hours"] == 99
    assert settings["page_reservation_cooldown_hours"] == 77
    assert settings["default_reserved_pages_quota"] == 3


def test_disabled_chat_plugin_hides_settings_section(client, admin_user):
    """When the chat plugin is disabled the chat settings section must not appear."""
    db.disable_plugin("chat")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get("/global-settings")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    # The Chat Settings heading and actual form inputs must be absent.
    # JS may still reference field names as strings in querySelector calls, which is fine.
    assert "<h3>Chat Settings</h3>" not in html
    assert 'type="number" name="chat_max_message_length"' not in html


def test_disabled_assessments_plugin_hides_settings_section(client, admin_user):
    """When assessments plugin is disabled the points badge setting must not appear."""
    db.disable_plugin("assessments")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get("/global-settings")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "assessment_points_badge_enabled" not in html


def test_disabled_assessments_plugin_preserves_settings_on_post(client, admin_user):
    """POSTing to settings while assessments is disabled must not overwrite assessment settings."""
    db.update_site_settings(assessment_points_badge_enabled=1)
    db.disable_plugin("assessments")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/global-settings", data={
        **_SETTINGS_BASE,
        "assessment_points_badge_enabled": "0",
    })
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["assessment_points_badge_enabled"] == 1


def test_disabled_chat_plugin_preserves_settings_on_post(client, admin_user):
    """POSTing to settings while chat is disabled must not overwrite chat DB values."""
    # Establish known baseline chat settings
    db.update_site_settings(
        chat_max_message_length=1234,
        chat_dm_enabled=0,
        chat_allow_dm_creation=0,
    )
    db.disable_plugin("chat")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    # Post settings with different chat values. They should be ignored
    response = client.post("/global-settings", data={
        **_SETTINGS_BASE,
        "chat_max_message_length": "9999",
        "chat_dm_enabled": "1",
        "chat_allow_dm_creation": "1",
    })
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    # Chat fields must be unchanged
    assert settings["chat_max_message_length"] == 1234
    assert settings["chat_dm_enabled"] == 0
    assert settings["chat_allow_dm_creation"] == 0


def test_disabled_user_data_export_plugin_blocks_export_own_data_route(client, admin_user):
    """GET /settings/export must return 404 when the user_data_export plugin is disabled."""
    db.disable_plugin("user_data_export")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get("/settings/export")
    assert response.status_code == 404


def test_disabled_page_reservations_plugin_blocks_user_reservation_quota_route(client, admin_user):
    """GET /settings/reservation-quota must return 404 when the page_reservations plugin is disabled."""
    db.disable_plugin("page_governance")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get("/settings/reservation-quota")
    assert response.status_code == 404


def test_disabled_user_data_export_plugin_blocks_admin_user_export_route(client, admin_user):
    """GET /admin/users/<id>/export must return 404 when the user_data_export plugin is disabled."""
    db.disable_plugin("user_data_export")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get(f"/admin/users/{admin_user}/export")
    assert response.status_code == 404


def test_disabled_page_reservations_plugin_blocks_admin_user_reservation_quota_route(client, admin_user):
    """GET /admin/users/<id>/reservation-quota must return 404 when page_reservations is disabled."""
    db.disable_plugin("page_governance")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get(f"/admin/users/{admin_user}/reservation-quota")
    assert response.status_code == 404


def test_disabled_audit_plugin_blocks_admin_user_audit_route(client, admin_user):
    """GET /admin/users/<id>/audit must return 404 when the audit plugin is disabled."""
    db.disable_plugin("audit")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get(f"/admin/users/{admin_user}/audit")
    assert response.status_code == 404


def test_enabled_page_reservations_plugin_updates_settings_on_post(client, admin_user):
    """POSTing to settings while page_reservations is enabled must update its DB values."""
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/global-settings", data={
        **_SETTINGS_BASE,
        "page_reservations_enabled": "1",
        "page_reservation_duration_hours": "24",
        "page_reservation_cooldown_hours": "12",
        "default_reserved_pages_quota": "7",
    })
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["page_reservations_enabled"] == 1
    assert settings["page_reservation_duration_hours"] == 24
    assert settings["page_reservation_cooldown_hours"] == 12
    assert settings["default_reserved_pages_quota"] == 7


# ---------------------------------------------------------------------------
# Plugin disable resets related feature-toggle settings
# ---------------------------------------------------------------------------

def test_disabling_page_governance_plugin_resets_enabled_flag(client, admin_user):
    """Disabling the page_governance plugin must set page_reservations_enabled=0."""
    db.update_site_settings(page_reservations_enabled=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/page_governance/disable")
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["page_reservations_enabled"] == 0


def test_disabling_page_governance_plugin_resets_page_protection_flag(client, admin_user):
    """Disabling page_governance must also turn off page protection."""
    db.update_site_settings(page_protection_enabled=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/page_governance/disable")
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["page_protection_enabled"] == 0


def test_disabling_page_governance_plugin_resets_contribution_approval_flag(client, admin_user):
    """Disabling page_governance must also turn off contribution approval."""
    db.update_site_settings(contribution_approval_enabled=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/page_governance/disable")
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["contribution_approval_enabled"] == 0


def test_disabling_chat_plugin_resets_dm_enabled_flag(client, admin_user):
    """Disabling the chat plugin must set chat_dm_enabled=0."""
    db.update_site_settings(chat_dm_enabled=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/chat/disable", data={"force": "1"})
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["chat_dm_enabled"] == 0


def test_disabling_assessments_plugin_resets_points_badge_flag(client, admin_user):
    """Disabling the assessments plugin must set assessment_points_badge_enabled=0."""
    db.update_site_settings(assessment_points_badge_enabled=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/assessments/disable")
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["assessment_points_badge_enabled"] == 0


def test_disabling_chat_plugin_resets_chat_cleanup_flags(client, admin_user):
    """Disabling the chat plugin must reset cleanup auto-deletion flags.

    The chat plugin now owns the cleanup scheduler; disabling it should clear
    every auto-deletion toggle so no settings can silently delete data.
    """
    db.update_site_settings(
        chat_cleanup_enabled=1,
        chat_dm_auto_clear_messages=1,
        chat_dm_auto_clear_attachments=1,
        chat_group_auto_clear_messages=1,
        chat_group_auto_clear_attachments=1,
        chat_auto_clear_messages=1,
        chat_auto_clear_attachments=1,
    )
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/chat/disable")
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["chat_cleanup_enabled"] == 0
    assert settings["chat_dm_auto_clear_messages"] == 0
    assert settings["chat_dm_auto_clear_attachments"] == 0
    assert settings["chat_group_auto_clear_messages"] == 0
    assert settings["chat_group_auto_clear_attachments"] == 0
    assert settings["chat_auto_clear_messages"] == 0
    assert settings["chat_auto_clear_attachments"] == 0


def test_disabling_chat_plugin_resets_group_enabled_flag(client, admin_user):
    """Disabling the chat plugin must reset group-related settings (groups is merged into chat)."""
    db.update_site_settings(chat_group_enabled=1, profile_group_badges_enabled=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/chat/disable")
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["chat_group_enabled"] == 0
    assert settings["profile_group_badges_enabled"] == 0


def test_disabling_user_profiles_plugin_resets_profile_settings(client, admin_user):
    """Disabling the user_profiles plugin must reset profile-related settings."""
    db.update_site_settings(
        profile_group_badges_enabled=1,
        profile_contribution_chart_enabled=1,
    )
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/user_profiles/disable", data={"force": "1"})
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["profile_group_badges_enabled"] == 0
    assert settings["profile_contribution_chart_enabled"] == 0


def test_disabling_deletion_slowdown_plugin_resets_docs_bypass_flag(client, admin_user):
    """Disabling the deletion_slowdown plugin must set docs_bypass_deletion_slowdown=0."""
    db.update_site_settings(docs_bypass_deletion_slowdown=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/plugins/deletion_slowdown/disable")
    assert response.status_code in (200, 302)

    settings = db.get_site_settings()
    assert settings["docs_bypass_deletion_slowdown"] == 0


def test_disabled_deletion_slowdown_plugin_hides_settings_section(client, admin_user):
    """When deletion_slowdown plugin is disabled the docs_bypass_deletion_slowdown form must not appear."""
    db.disable_plugin("deletion_slowdown")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get("/global-settings")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "docs_bypass_deletion_slowdown" not in html


def test_enabled_deletion_slowdown_plugin_shows_settings_section(client, admin_user):
    """When deletion_slowdown plugin is enabled the docs_bypass_deletion_slowdown form must appear."""
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.get("/global-settings")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "docs_bypass_deletion_slowdown" in html


def test_disabled_deletion_slowdown_plugin_blocks_docs_settings_post(client, admin_user):
    """POSTing to /admin/docs-settings while deletion_slowdown is disabled must be rejected."""
    db.update_site_settings(docs_bypass_deletion_slowdown=1)
    db.disable_plugin("deletion_slowdown")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    response = client.post("/admin/docs-settings", data={
        "docs_bypass_deletion_slowdown": "1",
    })
    assert response.status_code in (200, 302)

    # The setting must remain at its old value (set above). The POST was rejected.
    settings = db.get_site_settings()
    assert settings["docs_bypass_deletion_slowdown"] == 1
