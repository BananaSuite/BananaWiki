from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import categories, service

CONFIRM = "DELETE"


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
    admin_client.post("/admin/bulk/pages/delete", data={"ids": [str(b["id"]), str(home_id), "x"],
                                                       "confirm_text": CONFIRM})
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (b["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (home_id,)) == 1
    admin_client.post("/admin/bulk/categories/delete", data={"ids": [str(cat["id"])], "page_action": "uncategorize",
                                                            "confirm_text": CONFIRM})
    assert db.scalar("SELECT category_id FROM pages WHERE id = ?", (a["id"],)) is None


def test_bulk_delete_needs_the_typed_confirmation(app, admin_client, db):
    cat, a, b = _seed(app)
    for typed in (None, "", "yes", "delet"):
        data = {"ids": [str(a["id"]), str(b["id"])]}
        if typed is not None:
            data["confirm_text"] = typed
        response = admin_client.post("/admin/bulk/pages/delete", data=data)
        assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id IN (?, ?)", (a["id"], b["id"])) == 2
    admin_client.post("/admin/bulk/categories/delete", data={"ids": [str(cat["id"])], "page_action": "delete"})
    assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (cat["id"],)) == 1
    page = admin_client.get("/admin/bulk").get_data(as_text=True)
    assert 'name="confirm_text"' in page
    admin_client.post("/admin/bulk/pages/delete", data={"ids": [str(a["id"]), str(b["id"])], "confirm_text": " delete "})
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id IN (?, ?)", (a["id"], b["id"])) == 0


def test_italian_confirmation_word_is_accepted(app, admin_client, db):
    cat, a, b = _seed(app)
    admin_client.post("/admin/bulk/pages/delete?lang=it", data={"ids": [str(b["id"])], "confirm_text": "ELIMINA"})
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (b["id"],)) == 0


def test_bulk_deletions_are_audited(app, admin_client, admin, db):
    cat, a, b = _seed(app)
    admin_client.post("/admin/bulk/pages/delete", data={"ids": [str(b["id"])], "confirm_text": CONFIRM})
    entry = db.one("SELECT * FROM audit_log WHERE action = 'bulk.deleted'")
    assert entry is not None and entry["actor_id"] == admin["id"]
    assert '"kind": "pages"' in entry["details"] and "Beta" in entry["details"]
