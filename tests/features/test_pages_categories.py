"""Category management and ordering (including the 1.4 M1 fix)."""

from flask import redirect

from bananawiki.wiki.features.pages import categories

from .pages_support import add_interceptor, get_page, in_app, make_category, make_page, restrict


def _category(app, category_id):
    return in_app(app, lambda: categories.get(category_id))


def _editor_client(app, make_user, login, db, name, **grants):
    user = make_user(name, role="editor")
    if grants:
        restrict(db, user, **grants)
    client = app.test_client()
    login(client, user)
    return client


def test_editor_creates_renames_moves_category(app, make_user, login, db):
    client = _editor_client(app, make_user, login, db, "catuser")
    client.post("/category/create", data={"name": "Parent"})
    client.post("/category/create", data={"name": "Child"})
    parent, child = in_app(app, categories.all_categories)
    client.post(f"/category/{child['id']}/edit", data={"name": "Renamed"})
    client.post(f"/category/{child['id']}/move", data={"parent_id": parent["id"]})
    moved = _category(app, child["id"])
    assert moved["name"] == "Renamed" and moved["parent_id"] == parent["id"]


def test_move_into_descendant_refused(app, admin_client):
    top = make_category(app, "Top")
    sub = make_category(app, "Sub", top["id"])
    admin_client.post(f"/category/{top['id']}/move", data={"parent_id": sub["id"]})
    assert _category(app, top["id"])["parent_id"] is None


def test_restricted_editor_cannot_touch_other_categories(app, make_user, login, db):
    mine = make_category(app, "Mine")
    theirs = make_category(app, "Theirs")
    client = _editor_client(app, make_user, login, db, "limited", write=[mine["id"]])
    assert client.post(f"/category/{theirs['id']}/edit", data={"name": "Pwned"}).status_code == 403
    assert client.post(f"/category/{theirs['id']}/move", data={"parent_id": mine["id"]}).status_code == 403
    assert client.post(f"/category/{mine['id']}/move", data={"parent_id": ""}).status_code == 403
    assert client.post("/category/create", data={"name": "Top level"}).status_code == 403
    assert client.post("/category/create", data={"name": "Sub", "parent_id": theirs["id"]}).status_code == 403
    assert client.post("/category/create", data={"name": "Sub", "parent_id": mine["id"]}).status_code == 302
    assert client.post(f"/category/{theirs['id']}/delete").status_code == 403
    assert _category(app, theirs["id"])["name"] == "Theirs"


def test_readable_but_not_writable_category_refused(app, make_user, login, db):
    mine = make_category(app, "Mine2")
    seen = make_category(app, "Seen")
    client = _editor_client(app, make_user, login, db, "reader_ed", read=[seen["id"]], write=[mine["id"]])
    assert client.post(f"/category/{seen['id']}/edit", data={"name": "X"}).status_code == 403
    assert client.post(f"/category/{seen['id']}/sequential-nav", data={"sequential_nav": "1"}).status_code == 403


def test_category_permissions_enforced(app, make_user, login, db):
    cat = make_category(app, "Perm")
    client = _editor_client(app, make_user, login, db, "noperms", keys={"page.edit_all"})
    assert client.post("/category/create", data={"name": "X"}).status_code == 403
    assert client.post(f"/category/{cat['id']}/edit", data={"name": "X"}).status_code == 403
    assert client.post(f"/category/{cat['id']}/delete").status_code == 403
    assert client.post(f"/category/{cat['id']}/sequential-nav", data={"sequential_nav": "1"}).status_code == 403
    plain = app.test_client()
    login(plain, make_user("plain_c"))
    assert plain.post("/category/create", data={"name": "X"}).status_code == 403


def test_sequential_toggle(app, admin_client):
    cat = make_category(app, "Seq")
    admin_client.post(f"/category/{cat['id']}/sequential-nav", data={"sequential_nav": ["0", "1"]})
    assert _category(app, cat["id"])["sequential_nav"] == 1
    admin_client.post(f"/category/{cat['id']}/sequential-nav", data={"sequential_nav": "0"})
    assert _category(app, cat["id"])["sequential_nav"] == 0


def test_delete_options(app, admin_client):
    a = make_category(app, "A")
    b = make_category(app, "B")
    c = make_category(app, "C")
    p1 = make_page(app, "P1", category_id=a["id"])
    p2 = make_page(app, "P2", category_id=b["id"])
    p3 = make_page(app, "P3", category_id=c["id"])
    admin_client.post(f"/category/{a['id']}/delete", data={"page_action": "uncategorize"})
    assert get_page(app, p1["id"])["category_id"] is None
    admin_client.post(f"/category/{b['id']}/delete", data={"page_action": "move", "target_category_id": c["id"]})
    assert get_page(app, p2["id"])["category_id"] == c["id"]
    admin_client.post(f"/category/{c['id']}/delete", data={"page_action": "delete"})
    assert get_page(app, p2["id"]) is None and get_page(app, p3["id"]) is None
    assert in_app(app, categories.all_categories) == []


def test_delete_with_pages_needs_page_delete_and_respects_interceptor(app, make_user, login, db, admin_client):
    cat = make_category(app, "Pages")
    keep = make_page(app, "Keep", category_id=cat["id"])
    client = _editor_client(app, make_user, login, db, "delcat")
    client.post(f"/category/{cat['id']}/delete", data={"page_action": "delete"})
    assert _category(app, cat["id"]) is not None and get_page(app, keep["id"]) is not None
    add_interceptor(app, "page.delete", lambda page, user: redirect("/later"))
    admin_client.post(f"/category/{cat['id']}/delete", data={"page_action": "delete"})
    assert _category(app, cat["id"]) is None
    assert get_page(app, keep["id"])["category_id"] is None


def test_delete_move_target_must_be_writable(app, make_user, login, db):
    mine = make_category(app, "M")
    other = make_category(app, "O")
    make_page(app, "Stay", category_id=mine["id"])
    client = _editor_client(app, make_user, login, db, "mover", write=[mine["id"]], read=[other["id"]])
    response = client.post(f"/category/{mine['id']}/delete", data={"page_action": "move", "target_category_id": other["id"]})
    assert response.status_code == 403


def test_reorder_pages_keeps_unlisted_slots(app, admin_client, db):
    cat = make_category(app, "Order")
    pages = [make_page(app, f"Page {n}", category_id=cat["id"]) for n in range(4)]
    response = admin_client.post("/api/reorder/pages", json={"ids": [pages[2]["id"], pages[0]["id"]]})
    assert response.status_code == 200
    ordered = db.column("SELECT title FROM pages WHERE category_id = ? ORDER BY sort_order", (cat["id"],))
    assert ordered == ["Page 2", "Page 1", "Page 0", "Page 3"]


def test_reorder_pages_checks_edit_access(app, make_user, login, db):
    locked = make_category(app, "Locked")
    page = make_page(app, "L1", category_id=locked["id"])
    client = _editor_client(app, make_user, login, db, "orderer", read=[locked["id"]], write=[])
    assert client.post("/api/reorder/pages", json={"ids": [page["id"]]}).status_code == 403
    assert client.post("/api/reorder/pages", json={"ids": "nope"}).status_code == 400
    assert client.post("/api/reorder/pages", json={"ids": [1, 1]}).status_code == 400


def test_reorder_categories(app, admin_client, make_user, login, db):
    x, y, z = (make_category(app, name) for name in ("X", "Y", "Z"))
    assert admin_client.post("/api/reorder/categories", json={"ids": [z["id"], x["id"], y["id"]]}).status_code == 200
    assert [c["name"] for c in in_app(app, categories.all_categories)] == ["Z", "X", "Y"]
    child = make_category(app, "Child", x["id"])
    assert admin_client.post("/api/reorder/categories", json={"ids": [child["id"], y["id"]]}).status_code == 400
    client = _editor_client(app, make_user, login, db, "catorder", write=[x["id"]])
    assert client.post("/api/reorder/categories", json={"ids": [x["id"], y["id"]]}).status_code in (403, 404)


def test_category_page_and_management_fragment(app, admin_client, make_user, login):
    cat = make_category(app, "Showcase")
    make_page(app, "Shown", category_id=cat["id"])
    body = admin_client.get(f"/category/{cat['id']}").get_data(as_text=True)
    assert "Shown" in body and "Manage category" in body
    fragment = admin_client.get(f"/api/category/{cat['id']}/management").get_json()["html"]
    assert "Rename" in fragment
    plain = app.test_client()
    login(plain, make_user("viewer"))
    assert "Manage category" not in plain.get(f"/category/{cat['id']}").get_data(as_text=True)
