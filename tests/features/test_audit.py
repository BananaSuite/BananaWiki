"""The audit log: recorded events, the admin page and retention."""

from __future__ import annotations

import pytest

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope


def actions(db):
    return db.column("SELECT action FROM audit_log ORDER BY id")


@pytest.fixture
def owner(make_user):
    return make_user("site_owner", role="owner", is_superuser=1)


@pytest.fixture
def owner_client(app, owner, login):
    client = app.test_client()
    login(client, owner)
    return client


def test_sign_in_and_account_events_are_recorded(app, client, make_user, login, db):
    user = make_user("carol")
    login(client, user)
    entry = db.one("SELECT * FROM audit_log WHERE action = 'user.login'")
    assert entry["actor_id"] == user["id"] and entry["target_id"] == user["id"] and entry["ip"]
    with app.test_request_context(), connection_scope():
        registry.emit("user.role_changed", user=user, old_role="user", new_role="editor", changed_by="admin-id")
        registry.emit("category.deleted", category={"id": 7, "name": "Old"}, actor_id=None)
    assert actions(db)[-2:] == ["user.role_changed", "category.deleted"]
    role = db.one("SELECT * FROM audit_log WHERE action = 'user.role_changed'")
    assert role["actor_id"] == "admin-id" and '"new_role": "editor"' in role["details"]


def test_page_lists_and_filters_entries(admin_client, admin, db):
    db.execute("INSERT INTO audit_log (created_at, actor_id, action, details) VALUES (?, 'ghost', 'page.deleted', "
               "'{\"title\": \"<b>x</b>\"}')", (sql_in(),))
    page = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "Page deleted" in page and "&lt;b&gt;x&lt;/b&gt;" in page and "Signed in" in page
    filtered = admin_client.get("/admin/audit?action=page.deleted").get_data(as_text=True)
    assert "Signed in" not in filtered.split("<tbody>")[1]
    by_actor = admin_client.get(f"/admin/audit?actor={admin['username']}").get_data(as_text=True)
    assert "Page deleted" not in by_actor.split("<tbody>")[1]


def test_audit_is_admin_only(client, make_user, login):
    login(client, make_user("eve", role="editor"))
    assert client.get("/admin/audit").status_code == 403
    assert client.post("/admin/audit/retention", data={"audit_log_retention_days": "0"}).status_code == 403


def test_retention_prunes_old_entries(app, owner_client, db):
    db.execute("INSERT INTO audit_log (created_at, action) VALUES (?, 'old')", (sql_in(days=-40),))
    owner_client.post("/admin/audit/retention", data={"audit_log_retention_days": "30"})
    assert db.scalar("SELECT audit_log_retention_days FROM site_settings") == 30
    owner_client.post("/admin/audit/retention", data={"audit_log_retention_days": "-1"})
    assert db.scalar("SELECT audit_log_retention_days FROM site_settings") == 30
    assert app.extensions["bananawiki.scheduler"].run_due(force=True, only="audit.prune") == ["audit.prune"]
    assert "old" not in actions(db)
    pruned = db.one("SELECT * FROM audit_log WHERE action = 'audit.pruned'")
    assert pruned is not None and '"deleted": 1' in pruned["details"], "pruning leaves a trace"


def test_only_owners_and_superusers_shorten_the_retention(admin_client, db):
    page = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "audit_log_retention_days" not in page
    assert admin_client.post("/admin/audit/retention", data={"audit_log_retention_days": "1"}).status_code == 403
    assert db.scalar("SELECT audit_log_retention_days FROM site_settings") == 365


def test_disabled_feature_records_nothing(app, client, make_user, login, db):
    with app.test_request_context(), connection_scope():
        registry.set_enabled("audit", False)
    assert actions(db) == ["plugin.disabled"], "switching it off is its last entry"
    login(client, make_user("dan"))
    assert actions(db) == ["plugin.disabled"]
    assert client.get("/admin/audit").status_code in (403, 404)


# Account administration, plugins and the audit log itself


def test_account_administration_is_recorded(app, owner_client, owner, make_user, login, db):
    from bananawiki.core.timeutil import now_sql

    admin = make_user("plain_admin", role="admin")
    editor = make_user("an_editor", role="editor")
    pending = make_user("newcomer", approval_status="pending")
    suspended = make_user("benched", suspended=1)
    bob_client = app.test_client()
    login(bob_client, editor)
    db.execute("INSERT INTO role_history (user_id, old_role, new_role, changed_at) VALUES (?, 'user', 'editor', ?)",
               (editor["id"], now_sql()))
    owner_client.post(f"/admin/users/{admin['id']}/edit", data={"action": "toggle_superuser"})
    owner_client.post(f"/admin/users/{editor['id']}/permissions", data={"permissions": ["page.view_all"]})
    owner_client.post(f"/admin/users/{editor['id']}/permissions", data={"action": "reset"})
    owner_client.post(f"/admin/users/{suspended['id']}/edit", data={"action": "unsuspend"})
    owner_client.post(f"/admin/users/{pending['id']}/edit", data={"action": "approve_user"})
    owner_client.post(f"/admin/users/{editor['id']}/sessions/revoke")
    owner_client.post(f"/admin/users/{editor['id']}/attributions", data={"action": "deattribute_all"})
    owner_client.post(f"/admin/users/{editor['id']}/attributions",
                      data={"action": "mass_reattribute", "to_username": make_user("helper")["username"]})
    owner_client.post(f"/admin/users/{editor['id']}/attributions", data={"action": "delete_all_role_history"})
    owner_client.post("/admin/roles/create", data={"name": "Helpers", "base_role": "editor",
                                                   "permissions": ["page.view_all"]})
    role_id = db.scalar("SELECT id FROM custom_roles WHERE name = 'Helpers'")
    owner_client.post(f"/admin/roles/{role_id}", data={"name": "Helpers", "base_role": "editor",
                                                       "permissions": ["page.view_all", "page.edit_all"]})
    owner_client.post(f"/admin/roles/{role_id}/assign", data={"user_id": editor["id"]})
    owner_client.post(f"/admin/roles/{role_id}/unassign", data={"user_id": editor["id"]})
    owner_client.post(f"/admin/roles/{role_id}/delete", data={"replacement_role_id": "none"})
    owner_client.post("/admin/codes/generate", data={"max_uses": "1", "expiry_mode": "48h"})
    code_id = db.scalar("SELECT id FROM invite_codes")
    owner_client.post(f"/admin/codes/{code_id}/delete")
    owner_client.post(f"/admin/codes/expired/{code_id}/delete")
    owner_client.post("/admin/sessions/auto-logout", data={"auto_logout_enabled": "1", "auto_logout_hour": "4"})
    owner_client.post(f"/admin/users/{editor['id']}/impersonate")
    owner_client.post("/impersonation/stop")
    owner_client.post("/admin/mass-logout")
    recorded = set(actions(db))
    expected = {
        "user.superuser_granted", "user.permissions_changed", "user.permissions_reset", "user.unsuspended",
        "user.approved", "user.sessions_revoked", "user.attributions_removed", "user.attributions_moved",
        "user.role_history_deleted", "role.created", "role.updated", "user.custom_role_assigned",
        "user.custom_role_removed", "role.deleted", "invite.created", "invite.deactivated", "invite.deleted",
        "sessions.auto_logout_changed", "user.impersonation_started", "user.impersonation_ended",
        "sessions.mass_logout",
    }
    assert expected <= recorded, expected - recorded
    grant = db.one("SELECT * FROM audit_log WHERE action = 'user.superuser_granted'")
    assert grant["actor_id"] == owner["id"] and grant["target_id"] == admin["id"]
    started = db.one("SELECT * FROM audit_log WHERE action = 'user.impersonation_started'")
    assert started["actor_id"] == owner["id"] and started["target_id"] == editor["id"]
    assert '"code"' not in db.scalar("SELECT details FROM audit_log WHERE action = 'invite.created'"), \
        "the code itself is a credential"


def test_only_owners_and_superusers_switch_the_audit_log_off(app, admin_client, owner_client, db):
    response = admin_client.post("/admin/plugins/audit/disable")
    assert response.status_code == 302
    with app.test_request_context(), connection_scope():
        assert registry.is_enabled("audit")
    owner_client.post("/admin/plugins/audit/disable")
    with app.test_request_context(), connection_scope():
        assert not registry.is_enabled("audit")
    last = db.one("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1")
    assert last["action"] == "plugin.disabled" and last["target_id"] == "audit"
    owner_client.post("/admin/plugins/audit/enable")
    assert actions(db)[-1] == "plugin.enabled"


def test_plugin_switches_are_recorded(admin_client, db):
    admin_client.post("/admin/plugins/kanban/disable")
    entry = db.one("SELECT * FROM audit_log WHERE action = 'plugin.disabled'")
    assert entry is not None and entry["target_type"] == "plugin" and entry["target_id"] == "kanban"


def test_addresses_of_accounts_an_administrator_may_not_manage_are_hidden(app, admin_client, owner, owner_client,
                                                                         make_user, login, db):
    app.test_client().post("/login", data={"username": owner["username"], "password": "correct horse battery"},
                           environ_base={"REMOTE_ADDR": "198.51.100.61"})
    login(app.test_client(), make_user("bob"))
    db.execute("UPDATE audit_log SET ip = '203.0.113.62' WHERE action = 'user.login' AND actor_id = "
               "(SELECT id FROM users WHERE username = 'bob')")
    page = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "203.0.113.62" in page and "198.51.100.61" not in page
    assert "198.51.100.61" in owner_client.get("/admin/audit").get_data(as_text=True)
