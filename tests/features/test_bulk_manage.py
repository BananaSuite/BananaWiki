from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import categories, service


def _seed(app):
    with app.test_request_context(), connection_scope():
        cat = categories.create("Old")
        a = service.create("Alpha", category_id=cat["id"], author_id=None)
        b = service.create("Beta", author_id=None)
        return cat, a, b


def test_admin_only(client, make_user, login):
    login(client, make_user("plain"))
    assert client.get("/admin/bulk").status_code == 403
    assert client.post("/admin/bulk/pages/delete", data={"ids": ["1"]}).status_code == 403


def test_bulk_delete_pages_and_categories(app, admin_client, db):
    cat, a, b = _seed(app)
    assert b"Alpha" in admin_client.get("/admin/bulk").data
    home_id = db.scalar("SELECT id FROM pages WHERE is_home = 1")
    admin_client.post("/admin/bulk/pages/delete", data={"ids": [str(b["id"]), str(home_id), "x"]})
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (b["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (home_id,)) == 1
    admin_client.post("/admin/bulk/categories/delete", data={"ids": [str(cat["id"])], "page_action": "uncategorize"})
    assert db.scalar("SELECT category_id FROM pages WHERE id = ?", (a["id"],)) is None
