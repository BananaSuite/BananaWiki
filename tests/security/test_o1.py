"""Regression tests for the o1 findings of the full re-audit pass 9 (OW1-1, OW1-2, OW2-2, OW3-2)."""

from __future__ import annotations

import pytest

from tests.hosting.conftest import login, make_account, make_wiki, portal, query, runtime, web  # noqa: F401
from tests.hosting.test_cross_wiki_sign_in import platform  # noqa: F401
from tests.security.test_fw2 import _platform_sign_in, _transfer, speech_server  # noqa: F401
from tests.security.test_gw2 import _connect

# OW1-1: a platform sign-in waiting at the two-step code when the owner of the wiki changes


def _enable_two_step(platform, user_id: str) -> str:  # noqa: F811
    from bananawiki.core import totp
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.auth import two_step

    secret = totp.generate_secret()
    with platform["wiki"].app_context(), connection_scope():
        assert two_step.enable(user_id, secret, 0)
    return secret


@pytest.mark.parametrize("job_ran", [True, False])
def test_ow1_1_pending_code_after_change_of_owner_does_not_sign_in(platform, make_account, login, query,  # noqa: F811
                                                                    job_ran):
    from bananawiki.core import totp
    from tests.hosting.test_cross_wiki_sign_in import _run_signouts, _signed_in

    browser = _connect(platform, login, "admin_team")
    secret = _enable_two_step(platform, "adm")
    wiki, location = _platform_sign_in(platform, browser)
    assert location.endswith("/login/two-step")
    _transfer(platform, make_account, login, query, "none")
    if job_ran:
        _run_signouts(platform)  # the link is gone here too
    answer = wiki.post("/login/two-step", data={"code": totp.totp_code(secret)}, base_url=platform["url"])
    assert answer.status_code == 302 and "/login" in answer.headers["Location"]
    assert not _signed_in(platform, wiki)
    assert wiki.get("/admin/users", base_url=platform["url"]).status_code != 200


def test_ow1_1_pending_code_completes_while_the_link_holds(platform, login):  # noqa: F811
    from bananawiki.core import totp
    from tests.hosting.test_cross_wiki_sign_in import _signed_in

    browser = _connect(platform, login, "admin_team")
    secret = _enable_two_step(platform, "adm")
    wiki, location = _platform_sign_in(platform, browser)
    assert location.endswith("/login/two-step")
    answer = wiki.post("/login/two-step", data={"code": totp.totp_code(secret)}, base_url=platform["url"])
    assert answer.status_code == 302
    assert _signed_in(platform, wiki)


# OW1-2: the REST API administration leaves site audit entries


def _wiki_login(client, user):
    """The wiki's sign-in (this module's ``login`` fixture is the hosting portal's)."""
    from tests.conftest import PASSWORD

    response = client.post("/login", data={"username": user["username"], "password": PASSWORD})
    assert response.status_code == 302, response.data[:500]



def _audit_since(db, mark: int) -> list[tuple[str, dict]]:
    import json

    return [(row["action"], json.loads(row["details"] or "{}"))
            for row in db.all("SELECT action, details FROM audit_log WHERE id > ? ORDER BY id", (mark,))]


def _audit_mark(db) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log", default=0))


def test_ow1_2_api_settings_form_is_recorded(app, client, make_user, db):
    from tests.features.api_support import enable_api

    enable_api(app, db)
    _wiki_login(client, make_user("plainadm", role="admin"))
    mark = _audit_mark(db)
    answer = client.post("/admin/api-service/settings", data={
        "enabled": "1", "rate_limit": "9999", "admin_rate_limit": "120", "max_tokens_per_user": "99",
        "webhook_max_failures": "0"})
    assert answer.status_code == 302
    entries = _audit_since(db, mark)
    assert [action for action, _ in entries] == ["settings.updated"]
    changed = entries[0][1]["changed"].split(", ")
    assert "api_service_rate_limit" in changed and "api_service_max_tokens_per_user" in changed
    assert entries[0][1]["via"] == "api_service"


def test_ow1_2_api_access_changes_are_recorded(app, client, make_user, db):
    from tests.features.api_support import call, enable_api, issue

    enable_api(app, db)
    admin, member = make_user("plainadm", role="admin"), make_user("memb")
    _wiki_login(client, admin)
    token = issue(app, admin, ["admin", "users"])
    db.execute("UPDATE users SET api_access_enabled = 0 WHERE id = ?", (member["id"],))
    mark = _audit_mark(db)
    assert client.post(f"/admin/api-service/users/{member['id']}/toggle-access").status_code == 302
    assert _audit_since(db, mark) == [("user.api_access_changed", {"username": "memb", "enabled": True})]
    mark = _audit_mark(db)
    answer = call(client, "PUT", f"/admin/users/{member['id']}/api-access", token, json={"enabled": False})
    assert answer.status_code == 200
    assert _audit_since(db, mark) == [("user.api_access_changed", {"username": "memb", "enabled": False})]
    mark = _audit_mark(db)
    call(client, "PUT", f"/admin/users/{member['id']}/api-access", token, json={"enabled": False})
    assert _audit_since(db, mark) == []  # nothing changed, nothing to record


def test_ow1_2_webhook_lifecycle_is_recorded_without_the_address(app, client, make_user, db, monkeypatch):
    import socket

    from tests.features.api_support import call, enable_api, issue

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    enable_api(app, db)
    admin = make_user("plainadm", role="admin")
    _wiki_login(client, admin)
    token = issue(app, admin, ["admin"])
    mark = _audit_mark(db)
    assert client.post("/admin/api-service/webhooks", data={
        "url": "https://hooks.example.org/services/T000/SECRETPATH?token=abc", "events": ["page.created"]},
    ).status_code == 201
    web_hook = db.scalar("SELECT MAX(id) FROM api_service__webhooks")
    assert client.post(f"/admin/api-service/webhooks/{web_hook}/rotate").status_code == 200
    assert client.post(f"/admin/api-service/webhooks/{web_hook}/toggle").status_code == 302
    answer = call(client, "POST", "/admin/webhooks", token,
                  json={"url": "https://other.example.org/in", "events": ["page.created"]})
    assert answer.status_code == 201
    api_hook = answer.json["webhook"]["id"]
    assert call(client, "POST", f"/admin/webhooks/{api_hook}/rotate-secret", token).status_code == 200
    assert call(client, "DELETE", f"/admin/webhooks/{api_hook}", token).status_code == 200
    entries = _audit_since(db, mark)
    assert [action for action, _ in entries] == [
        "api_service.webhook_created", "api_service.webhook_secret_rotated", "api_service.webhook_updated",
        "api_service.webhook_created", "api_service.webhook_secret_rotated", "api_service.webhook_deleted"]
    assert entries[0][1] == {"host": "hooks.example.org"}
    assert entries[2][1]["changed"].startswith("active")
    assert "SECRETPATH" not in str(entries) and "token=abc" not in str(entries)


# OW2-2: the documentation bypass of the deletion grace period


def _docs_setup(app, db, make_user):
    from bananawiki.wiki import permissions
    from bananawiki.wiki.features.auth import docs
    from tests.features.pages_support import make_page, restrict, set_feature

    set_feature(app, "deletion_slowdown", True)
    set_feature(app, "page_history", True)
    with app.app_context():
        category = docs.spawn()
    db.execute("UPDATE site_settings SET docs_bypass_deletion_slowdown = 1")
    builtin = db.one("SELECT id, slug FROM pages WHERE category_id = ? ORDER BY id LIMIT 1", (category["id"],))
    page = make_page(app, "Important", "keep me")
    eddie = make_user("eddie", role="editor")
    restrict(db, eddie, keys=permissions.defaults("editor")
             | {"page.delete", "history.view", "history.delete", "history.transfer"})
    return category, builtin, eddie, page


def test_ow2_2_a_page_with_a_forged_installer_history_waits(app, client, db, make_user):
    from tests.features.pages_support import get_page

    category, builtin, eddie, page = _docs_setup(app, db, make_user)
    _wiki_login(client, eddie)
    revision = get_page(app, page["id"])["revision"]
    assert client.post(f"/page/{page['slug']}/edit", data={
        "content": "keep me v2", "edit_message": "Built-in documentation", "revision": revision}).status_code == 302
    rows = db.all("SELECT id FROM page_history WHERE page_id = ? ORDER BY id", (page["id"],))
    assert client.post(f"/page/{page['slug']}/history/{rows[-1]['id']}/deattribute").status_code == 302
    for row in rows[:-1]:
        assert client.post(f"/page/{page['slug']}/history/{row['id']}/delete").status_code == 302
    first = db.one("SELECT edited_by, edit_message FROM page_history WHERE page_id = ? ORDER BY id LIMIT 1",
                   (page["id"],))
    assert first["edited_by"] is None and first["edit_message"] == "Built-in documentation"
    assert client.post(f"/page/{page['slug']}/move", data={"category_id": category["id"]}).status_code == 302
    client.post(f"/page/{page['slug']}/delete")
    after = get_page(app, page["id"])
    assert after is not None and after["pending_deletion"] == 1
    client.post(f"/page/{builtin['slug']}/delete")  # the guide's own pages still go at once
    assert get_page(app, builtin["id"]) is None


def test_ow2_2_migration_lists_the_installed_guide_only(tmp_path):
    import sqlite3

    from bananawiki.wiki.migrations import v17_builtin_doc_pages

    conn = sqlite3.connect(tmp_path / "w.db")
    conn.executescript(
        "CREATE TABLE site_settings (id INTEGER PRIMARY KEY, docs_category_id INTEGER);"
        "INSERT INTO site_settings VALUES (1, 7);"
        "CREATE TABLE pages (id INTEGER PRIMARY KEY, category_id INTEGER);"
        "INSERT INTO pages VALUES (1, 7), (2, 7), (3, NULL);"
        "CREATE TABLE page_history (id INTEGER PRIMARY KEY, page_id INTEGER, edited_by TEXT, edit_message TEXT);"
        "INSERT INTO page_history VALUES (1, 1, NULL, 'Built-in documentation'), (2, 2, 'u', 'Created'),"
        " (3, 3, NULL, 'Built-in documentation');"
    )
    v17_builtin_doc_pages.upgrade(conn)
    assert conn.execute("SELECT page_id FROM builtin_doc_pages").fetchall() == [(1,)]


# OW3-2: the settings pages of the collaboration features, and chat retention runs

CHAT_FORM = {
    "chat_dm_enabled": "1", "chat_group_enabled": "1", "chat_cleanup_enabled": "1",
    "chat_dm_auto_clear_messages": "1", "chat_dm_message_retention_days": "1",
    "chat_dm_attachment_retention_days": "0", "chat_group_message_retention_days": "0",
    "chat_group_attachment_retention_days": "0", "chat_max_message_length": "5000",
    "chat_max_attachment_size_mb": "5", "chat_attachments_per_day_limit": "10",
    "chat_cleanup_frequency_days": "1", "chat_cleanup_hour": "3"}


@pytest.mark.parametrize("url,form,via,key", [
    ("/admin/chats/settings", CHAT_FORM, "chat", "chat_dm_message_retention_days"),
    ("/kanban/settings", {"kanban_access": "all", "kanban_write_access": "all",
                          "kanban_public_access_enabled": "1", "kanban_open_access": "1"}, "kanban",
     "kanban_public_access_enabled"),
    ("/admin/canvas", {"canvas_access": "all", "canvas_write_access": "all",
                       "canvas_public_access_enabled": "1", "canvas_open_access": "1"}, "canvas", "canvas_open_access"),
    ("/admin/assessments", {"assessment_points_badge_enabled": "1"}, "assessments",
     "assessment_points_badge_enabled"),
])
def test_ow3_2_feature_settings_forms_are_recorded(app, client, db, make_user, url, form, via, key):
    from tests.features.pages_support import set_feature

    for feature in ("chat", "kanban", "canvas", "assessments"):
        set_feature(app, feature, True)
    _wiki_login(client, make_user("plainadm", role="admin"))
    mark = _audit_mark(db)
    assert client.post(url, data=form).status_code == 302
    entries = [details for action, details in _audit_since(db, mark) if action == "settings.updated"]
    assert len(entries) == 1 and entries[0]["via"] == via and key in entries[0]["changed"].split(", ")
    mark = _audit_mark(db)
    client.post(url, data=form)  # the same values again: nothing changed, nothing recorded
    assert not [action for action, _ in _audit_since(db, mark) if action == "settings.updated"]


def test_ow3_2_a_retention_run_that_deletes_messages_is_recorded(app, db, make_user):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.chat import retention

    alice = make_user("alice")
    make_user("bob")
    sender = app.test_client()
    _wiki_login(sender, alice)
    chat = sender.post("/chats/new", data={"username": "bob"}, headers={"Accept": "application/json"}).json["chat_id"]
    sender.post(f"/chats/{chat}/send", data={"content": "private"}, headers={"Accept": "application/json"})
    db.execute("UPDATE chat_messages SET created_at = datetime('now', '-3 days')")
    admin = app.test_client()
    _wiki_login(admin, make_user("plainadm", role="admin"))
    assert admin.post("/admin/chats/settings", data=CHAT_FORM).status_code == 302
    mark = _audit_mark(db)
    with app.app_context(), connection_scope():
        assert retention.run_retention(force=True)["messages"] == 1
        assert retention.run_retention(force=True)["messages"] == 0  # nothing deleted: no entry
    entries = _audit_since(db, mark)
    assert [action for action, _ in entries] == ["chat.retention_applied"]
    assert entries[0][1]["messages"] == 1 and entries[0][1]["rules_days"]["dm"] == {"messages": 1}
