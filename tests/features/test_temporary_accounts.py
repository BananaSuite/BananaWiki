"""Temporary pages, accounts, roles and timed visibility."""

from __future__ import annotations

from datetime import timedelta

import pytest

from bananawiki.core.timeutil import sql_in, utcnow
from bananawiki.wiki import accounts, registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.temporary_accounts import service
from bananawiki.wiki.features.temporary_accounts.service import TemporaryError

from .governance_support import as_user, build_app

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


def test_last_admin_is_never_deleted(ctx, db, people):
    service.schedule_account_deletion(people["admin2"], SOON, show_countdown=True, actor=people["admin"])
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
