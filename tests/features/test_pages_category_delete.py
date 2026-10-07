"""Deleting a category with its pages: the web interface, the API and bulk deletion apply the same rules."""

from __future__ import annotations

import pytest

from bananawiki.wiki import permissions
from bananawiki.wiki.features.pages import categories, service

from .api_support import call, enable_api, issue
from .pages_support import get_page, in_app, make_category, make_page, restrict, set_feature

EDITOR_WITH_DELETE = permissions.defaults("editor") | {"page.delete"}


@pytest.fixture
def api_app(app, db):
    enable_api(app, db)
    return app


def _category(app, category_id):
    return in_app(app, lambda: categories.get(category_id))


def _protect(app, db, slug):
    """Page protection on, *slug* controlled by another editor (blocks everyone else, administrators too)."""
    from bananawiki.wiki import accounts

    controller = db.one("SELECT id FROM users WHERE username = 'controller'") or in_app(
        app, lambda: accounts.create("controller", "correct horse battery", role="editor", emit_event=False))
    set_feature(app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1")
    db.execute("UPDATE pages SET protected_by = ?, protected_at = '2025-01-01 00:00:00' WHERE slug = ?",
               (controller["id"], slug))


def _forbidden(app, db, editor, category):
    restrict(db, editor, keys={"category.delete", "page.delete"})  # no page.edit_all
    make_page(app, f"Plain {category['name']}", category_id=category["id"])


def _hidden(app, db, editor, category):
    restrict(db, editor, keys={"category.delete", "page.delete", "page.edit_all"})  # no page.view_deindexed
    make_page(app, f"Seen {category['name']}", category_id=category["id"])
    hidden = make_page(app, f"Unseen {category['name']}", category_id=category["id"])
    db.execute("UPDATE pages SET is_deindexed = 1 WHERE id = ?", (hidden["id"],))


def _blocked(app, db, editor, category):
    restrict(db, editor, keys=EDITOR_WITH_DELETE)
    make_page(app, f"Free {category['name']}", category_id=category["id"])
    locked = make_page(app, f"Locked {category['name']}", category_id=category["id"])
    _protect(app, db, locked["slug"])


@pytest.mark.parametrize(("arrange", "status", "code"), [
    (_forbidden, 403, "category_pages_forbidden"),
    (_hidden, 403, "category_pages_hidden"),
    (_blocked, 409, "category_pages_blocked"),
])
def test_web_and_api_refuse_alike_and_change_nothing(api_app, client, login, make_user, db, arrange, status, code):
    editor = make_user("deleter", role="editor", api_access_enabled=1)
    by_web, by_api = make_category(api_app, "Web"), make_category(api_app, "Api")
    arrange(api_app, db, editor, by_web)
    arrange(api_app, db, editor, by_api)
    before = db.all("SELECT id, category_id, pending_deletion FROM pages ORDER BY id")
    web = api_app.test_client()
    login(web, editor)
    assert web.post(f"/category/{by_web['id']}/delete", data={"page_action": "delete"}).status_code == 302
    response = call(client, "DELETE", f"/categories/{by_api['id']}?page_action=delete",
                    issue(api_app, editor, ["categories", "pages"]))
    assert (response.status_code, response.json["code"]) == (status, code)
    visible_refused = {"category_pages_hidden": [], "category_pages_forbidden": ["plain-api"],
                       "category_pages_blocked": ["locked-api"]}[code]
    assert response.json["pages"] == visible_refused
    assert _category(api_app, by_web["id"]) is not None and _category(api_app, by_api["id"]) is not None
    assert db.all("SELECT id, category_id, pending_deletion FROM pages ORDER BY id") == before


def test_deletion_slowdown_applies_to_web_and_api(api_app, client, login, make_user, db):
    set_feature(api_app, "deletion_slowdown", True)
    editor = make_user("slow", role="editor", api_access_enabled=1)
    restrict(db, editor, keys=EDITOR_WITH_DELETE)
    by_web, by_api = make_category(api_app, "Web"), make_category(api_app, "Api")
    web_page = make_page(api_app, "Web page", category_id=by_web["id"])
    api_page = make_page(api_app, "Api page", category_id=by_api["id"])
    web = api_app.test_client()
    login(web, editor)
    web.post(f"/category/{by_web['id']}/delete", data={"page_action": "delete"})
    response = call(client, "DELETE", f"/categories/{by_api['id']}?page_action=delete",
                    issue(api_app, editor, ["categories", "pages"]))
    assert response.status_code == 202
    assert response.json["pending_deletion"] == ["api-page"] and response.json["kept"] == []
    for category, page in ((by_web, web_page), (by_api, api_page)):
        assert _category(api_app, category["id"]) is None
        current = get_page(api_app, page["id"])
        assert current["pending_deletion"] == 1 and current["category_id"] is None


def test_api_deletes_pages_when_every_one_may_go(api_app, client, make_user, db):
    editor = make_user("cleaner", role="editor", api_access_enabled=1)
    restrict(db, editor, keys=EDITOR_WITH_DELETE)
    doomed = make_category(api_app, "Doomed")
    page = make_page(api_app, "Doomed page", category_id=doomed["id"])
    path = f"/categories/{doomed['id']}?page_action=delete"
    # Deleting pages needs the pages scope, as DELETE /pages/<slug> does.
    response = call(client, "DELETE", path, issue(api_app, editor, ["categories"]))
    assert (response.status_code, response.json["code"]) == (403, "scope_missing")
    assert _category(api_app, doomed["id"]) is not None and get_page(api_app, page["id"]) is not None
    response = call(client, "DELETE", path, issue(api_app, editor, ["categories", "pages"]))
    assert response.status_code == 200 and response.json["pending_deletion"] == []
    assert _category(api_app, doomed["id"]) is None and get_page(api_app, page["id"]) is None


def test_bulk_deletion_skips_categories_with_protected_pages(app, admin_client, db):
    kept, gone = make_category(app, "Kept"), make_category(app, "Gone")
    locked = make_page(app, "Locked page", category_id=kept["id"])
    free = make_page(app, "Free page", category_id=gone["id"])
    _protect(app, db, locked["slug"])
    response = admin_client.post("/admin/bulk/categories/delete", follow_redirects=True,
                                 data={"ids": [str(kept["id"]), str(gone["id"])], "page_action": "delete"})
    assert "1 categories deleted, 1 skipped" in response.get_data(as_text=True)
    assert _category(app, kept["id"]) is not None and get_page(app, locked["id"])["category_id"] == kept["id"]
    assert _category(app, gone["id"]) is None and get_page(app, free["id"]) is None
    other = make_category(app, "Other")
    response = admin_client.post("/admin/bulk/categories/delete", follow_redirects=True,
                                 data={"ids": [str(other["id"])], "page_action": "delete"})
    assert "1 categories deleted." in response.get_data(as_text=True)


def test_refusal_message_names_the_first_pages_only():
    refused = categories.PagesRefused("forbidden", [{"title": f"Page {n}"} for n in range(25)])
    titles = refused.values["titles"]
    assert titles.startswith("Page 0, ") and "Page 9" in titles and "Page 10" not in titles
    assert titles.endswith("… (+15)") and len(refused.pages) == 25


def test_system_deletion_removes_pages_before_the_category(app, monkeypatch):
    category = make_category(app, "Guide")
    make_page(app, "First", category_id=category["id"])
    make_page(app, "Second", category_id=category["id"])
    real_delete = service.delete

    def failing(page, *, actor_id):
        if page["title"] == "Second":
            raise RuntimeError("disk full")
        real_delete(page, actor_id=actor_id)

    monkeypatch.setattr(service, "delete", failing)
    with pytest.raises(RuntimeError):
        in_app(app, lambda: categories.delete(category, page_action="delete"))
    assert _category(app, category["id"]) is not None
