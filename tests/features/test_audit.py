"""The audit log: recorded events, the admin page and retention."""

from __future__ import annotations

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope


def actions(db):
    return db.column("SELECT action FROM audit_log ORDER BY id")


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


def test_retention_prunes_old_entries(app, admin_client, db):
    db.execute("INSERT INTO audit_log (created_at, action) VALUES (?, 'old')", (sql_in(days=-40),))
    admin_client.post("/admin/audit/retention", data={"audit_log_retention_days": "30"})
    assert db.scalar("SELECT audit_log_retention_days FROM site_settings") == 30
    admin_client.post("/admin/audit/retention", data={"audit_log_retention_days": "-1"})
    assert db.scalar("SELECT audit_log_retention_days FROM site_settings") == 30
    assert app.extensions["bananawiki.scheduler"].run_due(force=True, only="audit.prune") == ["audit.prune"]
    assert "old" not in actions(db)


def test_disabled_feature_records_nothing(app, client, make_user, login, db):
    with app.test_request_context(), connection_scope():
        registry.set_enabled("audit", False)
    login(client, make_user("dan"))
    assert actions(db) == []
    assert client.get("/admin/audit").status_code in (403, 404)
