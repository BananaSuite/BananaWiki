"""Deletion slowdown: scheduling, restore, purge job and behaviour while switched off."""

from __future__ import annotations

import pytest

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.deletion_slowdown import service
from bananawiki.wiki.features.pages import categories
from bananawiki.wiki.features.pages import service as pages

from .governance_support import as_user, build_app, set_settings


@pytest.fixture
def app(app_factory):
    return build_app(app_factory, "deletion_slowdown")


@pytest.fixture
def people(make_user):
    return {"admin": make_user("boss", role="admin"), "editor": make_user("ed_one", role="editor"),
            "reader": make_user("reader")}


@pytest.fixture
def page(app):
    with app.test_request_context(), connection_scope():
        return pages.create("Doomed", "x", author_id=None)


def _intercept(app, page, user):
    with app.test_request_context(f"/page/{page['slug']}/delete", method="POST"), connection_scope():
        as_user(user)
        response = registry.intercept("page.delete", page=pages.get(page["id"]), user=user)
        return response.status_code if response is not None else None, pages.get(page["id"])


def test_delete_is_scheduled_and_hidden(app, people, page):
    status, current = _intercept(app, page, people["admin"])
    assert status == 302 and current["pending_deletion"] == 1
    assert current["pending_deletion_by"] == people["admin"]["id"]
    with app.test_request_context(), connection_scope():
        assert not pages.can_view(current, people["reader"])
    # deleting again does not reset the clock
    assert _intercept(app, page, people["admin"])[0] == 302


def test_docs_bypass(app, people, page):
    with app.test_request_context(), connection_scope():
        docs = categories.create("Docs")
        pages.move(page, docs["id"], actor_id=None)
    set_settings(app, docs_category_id=docs["id"], docs_bypass_deletion_slowdown=1)
    assert _intercept(app, page, people["admin"])[0] is None  # the pages feature deletes at once


def test_purge_job_is_idempotent_and_emits(app, db, people, page):
    deleted = []
    with app.test_request_context(), connection_scope():
        service.schedule(page, people["admin"]["id"])
        assert service.purge_due() == 0
    db.execute("UPDATE pages SET pending_deletion_at = ? WHERE id = ?", (sql_in(hours=-49), page["id"]))
    reg = app.extensions["bananawiki.registry"]
    reg._handlers.setdefault("page.deleted", []).append(("deletion_slowdown", lambda **kw: deleted.append(kw)))
    with app.test_request_context(), connection_scope():
        assert service.purge_due() == 1
        assert service.purge_due() == 0
        assert pages.get(page["id"]) is None
    assert len(deleted) == 1


def test_purge_job_does_not_run_when_disabled(app, db, people, page):
    with app.test_request_context(), connection_scope():
        service.schedule(page, people["admin"]["id"])
        registry.set_enabled("deletion_slowdown", False)
    db.execute("UPDATE pages SET pending_deletion_at = ? WHERE id = ?", (sql_in(hours=-49), page["id"]))
    scheduler = app.extensions["bananawiki.scheduler"]
    assert "deletion_slowdown.purge" not in scheduler.run_due(force=True, only="deletion_slowdown.purge")
    assert db.scalar("SELECT pending_deletion FROM pages WHERE id = ?", (page["id"],)) == 1
    # while off, deleting a waiting page deletes it immediately
    assert _intercept(app, page, people["admin"])[0] is None
    with app.test_request_context(), connection_scope():
        registry.set_enabled("deletion_slowdown", True)
    assert scheduler.run_due(force=True, only="deletion_slowdown.purge") == ["deletion_slowdown.purge"]
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (page["id"],)) == 0


def test_restore_route_permissions(app, client, login, people, page, db):
    with app.test_request_context(), connection_scope():
        service.schedule(page, people["admin"]["id"])
    login(client, people["editor"])  # no page.delete by default
    assert client.post(f"/admin/pending-deletions/{page['id']}/restore").status_code == 403
    assert client.get("/admin/pending-deletions").status_code == 403
    client.post("/logout")
    login(client, people["admin"])
    assert client.get("/admin/pending-deletions").status_code == 200
    assert client.post(f"/admin/pending-deletions/{page['id']}/restore").status_code == 302
    assert db.scalar("SELECT pending_deletion FROM pages WHERE id = ?", (page["id"],)) == 0
    assert client.post(f"/admin/pending-deletions/{page['id']}/restore").status_code == 404


def test_admin_purge_and_settings(app, client, login, people, page, db):
    with app.test_request_context(), connection_scope():
        service.schedule(page, people["admin"]["id"])
    login(client, people["admin"])
    client.post(f"/admin/pending-deletions/{page['id']}/purge")
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (page["id"],)) == 0
    client.post("/admin/pending-deletions/settings", data={"docs_bypass_deletion_slowdown": "1"})
    assert db.scalar("SELECT docs_bypass_deletion_slowdown FROM site_settings") == 1


def test_notice_slot(app, people, page):
    with app.test_request_context(), connection_scope():
        service.schedule(page, people["admin"]["id"])
        as_user(people["admin"])
        html = registry.render_slot("page.above_content", page=pages.get(page["id"]))
        assert "deletion_slowdown/" not in html and "restore" in html
