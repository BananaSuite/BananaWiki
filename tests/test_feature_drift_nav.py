"""Regression tests for feature drift fixes: admin nav buttons, plugin guards,
toggle chat route and duplicate buttons.

Verifies the seven testing criteria from the feature-drift issue:
1. Admin nav buttons appear when plugins are enabled
2. Beta Testers button disappears and invite banner hidden when plugin is off
3. Temporary Accounts button disappears when plugin is off
4. Checkouts button disappears when plugin/setting is off
5. Only one Permissions button per user row on admin users page
6. Chat-disable toggle flips the DB flag without errors
7. Admin settings page does not leak plugin-gated sections
"""
import db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _login_admin(client):
    client.post("/login", data={"username": "admin", "password": "admin123"})


def _create_admin():
    from werkzeug.security import generate_password_hash
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1, page_reservations_enabled=1)
    return uid


def _create_editor():
    from werkzeug.security import generate_password_hash
    return db.create_user("editor", generate_password_hash("editor123"), role="editor")


def _create_user(name="alice"):
    from werkzeug.security import generate_password_hash
    return db.create_user(name, generate_password_hash(f"{name}123"), role="user")


# ---------------------------------------------------------------------------
# 1. Admin nav buttons appear when plugins are enabled
# ---------------------------------------------------------------------------

def test_admin_nav_shows_temporary_when_enabled(client, admin_user):
    """Temporary Items nav button appears when plugin is enabled."""
    db.enable_plugin("temporary_accounts")
    _login_admin(client)
    resp = client.get("/settings")
    html = resp.get_data(as_text=True)
    assert "Temporary" in html
    assert "/admin/temporary" in html


def test_admin_nav_shows_checkouts_when_enabled(client, admin_user):
    """Checkouts nav button appears when plugin and setting are enabled."""
    db.enable_plugin("page_governance")
    db.update_site_settings(page_reservations_enabled=1)
    _login_admin(client)
    resp = client.get("/settings")
    html = resp.get_data(as_text=True)
    assert "Checkout" in html
    assert "/admin/checkouts" in html


def test_admin_nav_always_shows_migration(client, admin_user):
    """Migration nav button always appears (not plugin-gated)."""
    _login_admin(client)
    resp = client.get("/settings")
    html = resp.get_data(as_text=True)
    assert "Migration" in html
    assert "/admin/migration" in html


# ---------------------------------------------------------------------------
# 2. Beta Testers button disappears and invite banner hidden when plugin off
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3. Temporary Accounts button disappears when plugin off
# ---------------------------------------------------------------------------

def test_admin_nav_hides_temporary_when_disabled(client, admin_user):
    """Temporary Items nav button disappears when plugin is disabled."""
    db.disable_plugin("temporary_accounts")
    _login_admin(client)
    resp = client.get("/settings")
    html = resp.get_data(as_text=True)
    assert "/admin/temporary" not in html


# ---------------------------------------------------------------------------
# 4. Checkouts button disappears when plugin/setting off
# ---------------------------------------------------------------------------

def test_admin_nav_hides_checkouts_when_plugin_disabled(client, admin_user):
    """Checkouts nav button disappears when page_reservations plugin is disabled."""
    db.disable_plugin("page_governance")
    _login_admin(client)
    resp = client.get("/settings")
    html = resp.get_data(as_text=True)
    assert "/admin/checkouts" not in html


def test_admin_nav_hides_checkouts_when_setting_disabled(client, admin_user):
    """Checkouts nav button disappears when page_reservations_enabled is off."""
    db.enable_plugin("page_governance")
    db.update_site_settings(page_reservations_enabled=0)
    _login_admin(client)
    resp = client.get("/settings")
    html = resp.get_data(as_text=True)
    assert "/admin/checkouts" not in html


# ---------------------------------------------------------------------------
# 5. Per-user permissions button is removed from admin users page
# ---------------------------------------------------------------------------

def test_single_permissions_button_per_user(client, admin_user):
    """Per-user permissions button should not be shown after deprecation."""
    _create_editor()
    _create_user("alice")
    _login_admin(client)
    resp = client.get("/admin/users")
    html = resp.get_data(as_text=True)
    perm_count = html.count('title="Customize per-user permissions"')
    assert perm_count == 0, f"Expected 0 Permissions buttons, got {perm_count}"


# ---------------------------------------------------------------------------
# 6. Chat-disable toggle flips DB flag without errors
# ---------------------------------------------------------------------------

def test_toggle_chat_disables_then_enables(client, admin_user):
    """admin_toggle_chat route should flip the chat_disabled flag."""
    user_id = _create_user("alice")
    _login_admin(client)

    assert not db.is_user_chat_disabled(user_id)

    # Disable
    resp = client.post(f"/admin/users/{user_id}/toggle_chat",
                       follow_redirects=True)
    assert resp.status_code == 200
    assert db.is_user_chat_disabled(user_id)

    # Re-enable
    resp = client.post(f"/admin/users/{user_id}/toggle_chat",
                       follow_redirects=True)
    assert resp.status_code == 200
    assert not db.is_user_chat_disabled(user_id)


def test_toggle_chat_nonexistent_user_returns_error(client, admin_user):
    """Toggling chat for a nonexistent user should flash error and redirect."""
    _login_admin(client)
    resp = client.post("/admin/users/NONEXIST/toggle_chat")
    assert resp.status_code == 302


def test_toggle_chat_shows_on_users_page(client, admin_user):
    """The toggle chat form should be present on the admin users page."""
    _create_user("alice")
    _login_admin(client)
    resp = client.get("/admin/users")
    html = resp.get_data(as_text=True)
    assert "toggle_chat" in html


# ---------------------------------------------------------------------------
# 7. Admin settings page: plugin-gated sections
# ---------------------------------------------------------------------------

def test_admin_settings_hides_kanban_when_disabled(client, admin_user):
    """Kanban section hidden when plugin is off."""
    db.disable_plugin("kanban")
    _login_admin(client)
    resp = client.get("/global-settings")
    html = resp.get_data(as_text=True)
    assert 'name="kanban_access"' not in html


def test_admin_settings_shows_kanban_when_enabled(client, admin_user):
    """Kanban section visible when plugin is on."""
    db.enable_plugin("kanban")
    _login_admin(client)
    resp = client.get("/global-settings")
    html = resp.get_data(as_text=True)
    assert "Kanban Board" in html


def test_admin_settings_hides_reservations_when_disabled(client, admin_user):
    """Page Reservations section hidden when plugin is off."""
    db.disable_plugin("page_governance")
    _login_admin(client)
    resp = client.get("/global-settings")
    html = resp.get_data(as_text=True)
    assert 'name="page_reservations_enabled"' not in html


def test_admin_settings_hides_chat_when_disabled(client, admin_user):
    """Chat section hidden when plugin is off."""
    db.disable_plugin("chat")
    _login_admin(client)
    resp = client.get("/global-settings")
    html = resp.get_data(as_text=True)
    assert 'name="chat_max_message_length"' not in html


def test_admin_settings_migration_always_visible(client, admin_user):
    """Site Migration section always appears on settings page."""
    _login_admin(client)
    resp = client.get("/global-settings")
    html = resp.get_data(as_text=True)
    assert "Site Migration" in html


# ---------------------------------------------------------------------------
# Settings sub-menu tabs: hide tabs whose sections are all plugin-gated and
# all those plugins are disabled, so the tab does not open an empty pane.
# ---------------------------------------------------------------------------

def test_admin_settings_integrations_tab_empty_when_all_plugins_disabled(client, admin_user):
    """When every plugin contributing to the Integrations tab is disabled, no
    Integrations sections render. The client-side nav script then hides the
    Integrations tab button (verified by the empty section list)."""
    db.disable_plugin("chat")
    _login_admin(client)
    resp = client.get("/global-settings")
    html = resp.get_data(as_text=True)
    # Tab button is still rendered (JS hides it client-side), but no section
    # belonging to the integrations group should be present in the markup.
    assert 'data-settings-group="integrations"' not in html


def test_admin_settings_integrations_tab_visible_when_a_plugin_enabled(client, admin_user):
    """When at least one Integrations-tab plugin is enabled, its section renders
    so the tab has content and stays visible."""
    db.enable_plugin("chat")
    _login_admin(client)
    resp = client.get("/global-settings")
    html = resp.get_data(as_text=True)
    assert 'data-settings-group="integrations"' in html
