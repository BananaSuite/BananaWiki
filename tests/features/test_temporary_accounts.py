"""Temporary pages, accounts, roles and timed visibility."""

from __future__ import annotations

from datetime import timedelta

import pytest

from bananawiki.core.timeutil import sql_in, utcnow
from bananawiki.wiki import accounts, registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.page_governance import protection
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.temporary_accounts import service
from bananawiki.wiki.features.temporary_accounts.service import TemporaryError

from .governance_support import as_user, build_app, set_settings

SOON = utcnow() + timedelta(hours=2)


@pytest.fixture
def app(app_factory):
    return build_app(app_factory, "temporary_accounts")


@pytest.fixture
def ctx(app):
    with app.test_request_context(), connection_scope():
        yield


@pytest.fixture
def people(make_user):
    return {"admin": make_user("boss", role="admin"), "admin2": make_user("boss_two", role="admin"),
            "editor": make_user("ed_one", role="editor"), "reader": make_user("reader"),
            "owner": make_user("big_owner", role="owner")}


def _expire(db, table):
    db.execute(f"UPDATE {table} SET expires_at = ?", (sql_in(minutes=-1),))


def test_page_deletion_expires_and_emits(app, ctx, db, people):
    deleted = []
    app.extensions["bananawiki.registry"]._handlers.setdefault("page.deleted", []).append(
        ("temporary_accounts", lambda **kw: deleted.append(kw["page"]["id"])))
    page = pages.create("Short lived", author_id=None)
    with pytest.raises(TemporaryError):
        service.schedule_page_deletion(page, utcnow() - timedelta(hours=1), show_countdown=True, actor=people["admin"])
    with pytest.raises(TemporaryError):
        service.schedule_page_deletion(pages.home(), SOON, show_countdown=True, actor=people["admin"])
    service.schedule_page_deletion(page, SOON, show_countdown=True, actor=people["admin"])
    service.run_expiry()
    assert pages.get(page["id"]) is not None
    _expire(db, "temp_pages")
    service.run_expiry()
    service.run_expiry()
    assert pages.get(page["id"]) is None and deleted == [page["id"]]


def test_scheduled_deletion_respects_page_protection(app_factory, db, people):
    app = build_app(app_factory, "temporary_accounts", "page_governance")
    set_settings(app, page_protection_enabled=1)
    with app.test_request_context(), connection_scope():
        page = pages.create("Guarded", author_id=people["editor"]["id"])
        service.schedule_page_deletion(page, SOON, show_countdown=True, actor=people["admin"])
        protection.protect(page, people["editor"])
        with pytest.raises(TemporaryError) as refused:
            service.schedule_page_deletion(page, SOON, show_countdown=True, actor=people["admin"])
        assert refused.value.key == "page_governance.blocked.protected"
    _expire(db, "temp_pages")
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="temporary_accounts.expire")
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (page["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM temp_pages WHERE page_id = ?", (page["id"],)) == 1
    with app.test_request_context(), connection_scope():
        protection.unprotect(pages.get(page["id"]), people["editor"])
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="temporary_accounts.expire")
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (page["id"],)) == 0


def test_scheduled_deletion_goes_through_deletion_slowdown(app_factory, db, people):
    app = build_app(app_factory, "temporary_accounts", "deletion_slowdown")
    with app.test_request_context(), connection_scope():
        page = pages.create("Slow goodbye", author_id=None)
        service.schedule_page_deletion(page, SOON, show_countdown=True, actor=people["admin"])
    _expire(db, "temp_pages")
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="temporary_accounts.expire")
    assert db.scalar("SELECT pending_deletion FROM pages WHERE id = ?", (page["id"],)) == 1
    assert db.scalar("SELECT pending_deletion_by FROM pages WHERE id = ?", (page["id"],)) == people["admin"]["id"]
    assert db.scalar("SELECT COUNT(*) FROM temp_pages WHERE page_id = ?", (page["id"],)) == 0


def test_visibility_schedule_restores(ctx, db, people):
    page = pages.create("Secret", author_id=None)
    service.schedule_visibility(page, hide=True, expires=SOON, show_countdown=False, actor=people["admin"])
    assert pages.get(page["id"])["is_deindexed"] == 1
    with pytest.raises(TemporaryError):
        service.schedule_visibility(page, hide=False, expires=SOON, show_countdown=False, actor=people["admin"])
    _expire(db, "temp_page_index_state")
    service.run_expiry()
    assert pages.get(page["id"])["is_deindexed"] == 0
    assert service.visibility_schedule(page["id"]) is None


def test_account_rules_and_expiry(ctx, db, people):
    with pytest.raises(TemporaryError):
        service.schedule_account_deletion(people["admin"], SOON, show_countdown=True, actor=people["admin"])
    with pytest.raises(TemporaryError):
        service.schedule_account_deletion(people["owner"], SOON, show_countdown=True, actor=people["admin"])
    service.schedule_account_deletion(people["reader"], SOON, show_countdown=True, actor=people["admin"])
    _expire(db, "temp_users")
    service.run_expiry()
    assert accounts.by_id(people["reader"]["id"]) is None


def test_administrators_are_scheduled_only_by_owners(ctx, db, people):
    with pytest.raises(TemporaryError):
        service.schedule_account_deletion(people["admin2"], SOON, show_countdown=True, actor=people["admin"])
    with pytest.raises(TemporaryError):
        service.schedule_role_revert(people["admin2"], "user", SOON, show_countdown=True, actor=people["admin"])
    service.schedule_account_deletion(people["admin2"], SOON, show_countdown=True, actor=people["owner"])
    db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (people["owner"]["id"],))
    _expire(db, "temp_users")
    service.run_expiry()
    assert accounts.by_id(people["admin2"]["id"]) is not None, "the scheduler no longer outranks the target"
    assert service.account_schedule(people["admin2"]["id"]) is None


def test_last_admin_is_never_deleted(ctx, db, people):
    service.schedule_account_deletion(people["admin2"], SOON, show_countdown=True, actor=people["owner"])
    db.execute("UPDATE users SET role = 'user' WHERE id IN (?, ?)", (people["admin"]["id"], people["owner"]["id"]))
    _expire(db, "temp_users")
    service.run_expiry()
    assert accounts.by_id(people["admin2"]["id"]) is not None
    assert service.account_schedule(people["admin2"]["id"]) is not None


def test_role_revert(ctx, db, people):
    with pytest.raises(TemporaryError):
        service.schedule_role_revert(people["editor"], "admin", SOON, show_countdown=True, actor=people["admin"])
    service.schedule_role_revert(people["editor"], "user", SOON, show_countdown=True, actor=people["admin"])
    _expire(db, "temp_roles")
    service.run_expiry()
    assert accounts.by_id(people["editor"]["id"])["role"] == "user"
    assert db.scalar("SELECT new_role FROM role_history WHERE user_id = ?", (people["editor"]["id"],)) == "user"
    assert service.role_schedule(people["editor"]["id"]) is None


def test_job_does_not_run_when_disabled(app, db, people):
    with app.test_request_context(), connection_scope():
        page = pages.create("Kept", author_id=None)
        service.schedule_page_deletion(page, SOON, show_countdown=True, actor=people["admin"])
        registry.set_enabled("temporary_accounts", False)
    _expire(db, "temp_pages")
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="temporary_accounts.expire")
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (page["id"],)) == 1


def test_delete_interceptor_and_countdown(app, people):
    with app.test_request_context("/page/x/delete", method="POST"), connection_scope():
        as_user(people["admin"])
        page = pages.create("Timed", author_id=None)
        assert registry.intercept("page.delete", page=page, user=people["admin"]) is None
        service.schedule_page_deletion(page, SOON, show_countdown=True, actor=people["admin"])
        assert registry.intercept("page.delete", page=page, user=people["admin"]).status_code == 302
        assert "alert" in registry.render_slot("page.above_content", page=page)


def test_admin_routes(client, login, people, db):
    login(client, people["reader"])
    assert client.get("/admin/temporary").status_code == 403
    client.post("/logout")
    login(client, people["admin"])
    assert client.get("/admin/temporary").status_code == 200
    when = (utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    client.post("/admin/temporary/user", data={"username": "reader", "expires_at": when, "show_countdown": "1"})
    assert db.scalar("SELECT COUNT(*) FROM temp_users WHERE user_id = ?", (people["reader"]["id"],)) == 1
    client.post("/admin/temporary/role", data={"username": "ed_one", "original_role": "user", "expires_at": when})
    assert db.scalar("SELECT original_role FROM temp_roles WHERE user_id = ?", (people["editor"]["id"],)) == "user"
    assert client.post(f"/admin/temporary/user/{people['reader']['id']}/remove").status_code == 302
    assert client.post(f"/admin/temporary/user/{people['reader']['id']}/remove").status_code == 404
    assert client.get("/admin/temporary").status_code == 200


def test_account_schedules_are_removed_only_within_the_hierarchy(app, client, login, people, db):
    admin, admin2, owner = people["admin"], people["admin2"], people["owner"]
    with app.test_request_context(), connection_scope():
        for target in (admin, admin2):
            service.schedule_role_revert(target, "user", SOON, show_countdown=True, actor=owner)
            service.schedule_account_deletion(target, SOON, show_countdown=True, actor=owner)
        with pytest.raises(TemporaryError) as refused:
            service.remove("role", admin["id"], actor=admin)
        assert refused.value.key == "temporary_accounts.error.self_remove"
    login(client, admin)
    assert "/remove" not in client.get("/admin/temporary").get_data(as_text=True)
    for kind in ("role", "user"):
        for target in (admin, admin2):
            assert client.post(f"/admin/temporary/{kind}/{target['id']}/remove").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM temp_roles") == 2
    assert db.scalar("SELECT COUNT(*) FROM temp_users") == 2
    _expire(db, "temp_roles")
    with app.test_request_context(), connection_scope():
        service.run_expiry()
    assert db.scalar("SELECT role FROM users WHERE id = ?", (admin["id"],)) == "user"
    client.post("/logout")
    login(client, owner)
    assert client.post(f"/admin/temporary/user/{admin['id']}/remove").status_code == 302
    assert client.post(f"/admin/temporary/user/{admin2['id']}/remove").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM temp_users") == 0


def test_manual_role_change_drops_revert(ctx, db, people):
    service.schedule_role_revert(people["editor"], "user", SOON, show_countdown=True, actor=people["admin"])
    registry.emit("user.role_changed", user=people["editor"], old_role="editor", new_role="admin",
                  changed_by=people["admin"]["id"])
    assert service.role_schedule(people["editor"]["id"]) is None


def test_revert_emits_role_changed(app, ctx, db, people):
    seen = []
    app.extensions["bananawiki.registry"]._handlers.setdefault("user.role_changed", []).append(
        ("temporary_accounts", lambda **kw: seen.append((kw["new_role"], kw["changed_by"]))))
    service.schedule_role_revert(people["editor"], "user", SOON, show_countdown=True, actor=people["admin"])
    _expire(db, "temp_roles")
    service.run_expiry()
    assert seen == [("user", None)]


def test_revert_scheduled_by_a_plain_admin_skips_a_since_promoted_admin(ctx, db, people):
    """A promotion while the feature was paused must not be undone by a scheduler who no longer outranks."""
    editor = people["editor"]
    service.schedule_role_revert(editor, "user", SOON, show_countdown=True, actor=people["admin"])
    registry.set_enabled("temporary_accounts", False)
    db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (editor["id"],))  # no event: the feature is off
    registry.set_enabled("temporary_accounts", True)
    _expire(db, "temp_roles")
    service.run_expiry()
    assert accounts.by_id(editor["id"])["role"] == "admin"
    assert service.role_schedule(editor["id"]) is None


def test_manual_role_change_while_paused_drops_the_revert(ctx, db, people):
    from bananawiki.wiki.features.admin import service as admin_service

    editor = people["editor"]
    service.schedule_role_revert(editor, "user", SOON, show_countdown=True, actor=people["owner"])
    db.execute("UPDATE temp_roles SET updated_at = ?", (sql_in(minutes=-5),))
    registry.set_enabled("temporary_accounts", False)
    admin_service.change_role(people["owner"], accounts.by_id(editor["id"]), "admin")
    registry.set_enabled("temporary_accounts", True)
    _expire(db, "temp_roles")
    service.run_expiry()
    assert accounts.by_id(editor["id"])["role"] == "admin"
    assert service.role_schedule(editor["id"]) is None


def test_revert_drops_a_custom_role_of_the_higher_base_role(ctx, db, people):
    editor = people["editor"]
    role_id = db.insert("custom_roles", {"name": "Helpers", "base_role": "editor", "created_at": sql_in(),
                                         "updated_at": sql_in()})
    db.execute("UPDATE users SET custom_role_id = ? WHERE id = ?", (role_id, editor["id"]))
    service.schedule_role_revert(accounts.by_id(editor["id"]), "user", SOON, show_countdown=True,
                                 actor=people["admin"])
    _expire(db, "temp_roles")
    service.run_expiry()
    reverted = accounts.by_id(editor["id"])
    assert reverted["role"] == "user" and reverted["custom_role_id"] is None
