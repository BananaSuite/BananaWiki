"""Canvas: access rules, list, creation, sharing and management."""

from __future__ import annotations

import pytest

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import service


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


def make_canvas(app, owner, title="Plan", **fields):
    layout = run(app, lambda: service.create(title, "", owner["id"]))
    if fields:
        run(app, lambda: service.set_visibility(layout, fields["visibility"]))
    return layout


def share(db, layout, permission, *, user=None, role=None):
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, role, permission) VALUES (?, ?, ?, ?)",
               (layout["id"], user["id"] if user else None, role, permission))


def settings(db, **values):
    for key, value in values.items():
        db.execute(f"UPDATE site_settings SET {key} = ? WHERE id = 1", (value,))


@pytest.fixture
def people(make_user):
    return {
        "owner": make_user("owner1", role="editor"),
        "editor": make_user("editor2", role="editor"),
        "user": make_user("plain1"),
        "admin": make_user("boss1", role="admin"),
    }


def as_(client, login, user):
    client.get("/logout")
    login(client, user)
    return client


def test_default_access_is_admin_only(client, login, people, app, db):
    layout = make_canvas(app, people["admin"], visibility="public")
    as_(client, login, people["user"])
    assert client.get("/canvas").status_code == 403
    assert client.get(f"/canvas/{layout['slug']}").status_code == 404
    as_(client, login, people["admin"])
    page = client.get("/canvas")
    assert page.status_code == 200 and b"Plan" in page.data
    assert client.get(f"/canvas/{layout['slug']}").status_code == 200


def test_single_share_does_not_open_public_canvases(client, login, people, app, db):
    """1.4 bug: a user with one individual share could read every public canvas."""
    shared = make_canvas(app, people["admin"], "Shared with me")
    public = make_canvas(app, people["admin"], "Public one", visibility="public")
    share(db, shared, "view", user=people["user"])
    as_(client, login, people["user"])
    listing = client.get("/canvas")
    assert listing.status_code == 200
    assert b"Shared with me" in listing.data and b"Public one" not in listing.data
    assert client.get(f"/canvas/{shared['slug']}").status_code == 200
    assert client.get(f"/canvas/{public['slug']}").status_code == 404
    assert client.get(f"/canvas/{public['slug']}/data").status_code == 404


def test_global_access_sees_public_and_role_shares_and_none_denies(client, login, people, app, db):
    settings(db, canvas_access="all")
    public = make_canvas(app, people["admin"], "Everyone board", visibility="public")
    private = make_canvas(app, people["admin"], "Secret board")
    by_role = make_canvas(app, people["admin"], "Role board")
    denied = make_canvas(app, people["admin"], "Denied board", visibility="public")
    share(db, by_role, "view", role="user")
    share(db, denied, "none", user=people["user"])
    as_(client, login, people["user"])
    body = client.get("/canvas").data
    assert b"Everyone board" in body and b"Role board" in body
    assert b"Secret board" not in body and b"Denied board" not in body
    assert client.get(f"/canvas/{private['slug']}").status_code == 404
    assert client.get(f"/canvas/{denied['slug']}").status_code == 404
    assert client.get(f"/canvas/{public['slug']}/data").get_json()["can_edit"] is False


def test_edit_permission_and_view_only(client, login, people, app, db):
    layout = make_canvas(app, people["owner"])
    share(db, layout, "view", user=people["editor"])
    as_(client, login, people["editor"])
    ops = {"ops": [{"op": "upsert_node", "node": {"id": "n1", "type": "text", "content": "hi"}}]}
    assert client.post(f"/canvas/{layout['slug']}/ops", json=ops).status_code == 403
    db.execute("UPDATE canvas__permissions SET permission = 'edit' WHERE layout_id = ?", (layout["id"],))
    assert client.post(f"/canvas/{layout['slug']}/ops", json=ops).status_code == 200
    # Editing does not give management rights.
    assert client.post(f"/canvas/{layout['slug']}/delete").status_code == 403
    assert client.post(f"/canvas/{layout['slug']}/share", data={"action": "set_visibility",
                                                               "visibility": "public"}).status_code == 403


def test_open_access_lets_everyone_edit(client, login, people, app, db):
    settings(db, canvas_open_access=1)
    layout = make_canvas(app, people["owner"])
    as_(client, login, people["user"])
    assert client.get(f"/canvas/{layout['slug']}/data").get_json()["can_edit"] is True


def test_feature_switch_and_permission(client, login, people, app, db):
    layout = make_canvas(app, people["owner"])
    as_(client, login, people["owner"])
    assert client.get(f"/canvas/{layout['slug']}").status_code == 200
    db.execute("UPDATE plugins SET enabled = 0 WHERE id = 'canvas'")
    assert client.get(f"/canvas/{layout['slug']}").status_code == 404
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'canvas'")
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (people["owner"]["id"],))  # explicit permission set without canvas.view
    assert client.get(f"/canvas/{layout['slug']}").status_code == 404


def test_anonymous_public_access(app, people, db, client):
    public = make_canvas(app, people["admin"], "Open", visibility="public")
    private = make_canvas(app, people["admin"], "Closed")
    assert client.get(f"/canvas/{public['slug']}").status_code == 302
    settings(db, public_mode=1, canvas_public_access_enabled=1)
    assert client.get(f"/canvas/{public['slug']}").status_code == 200
    assert client.get(f"/canvas/{public['slug']}/data").status_code == 200
    assert client.get(f"/api/embed/canvas/{public['slug']}").status_code == 200
    assert client.get(f"/canvas/{private['slug']}").status_code == 302
    assert client.post(f"/canvas/{public['slug']}/ops", json={"ops": []}).status_code in (302, 401)
    body = client.get("/canvas").data
    assert b"Open" in body and b"Closed" not in body


def test_create_requires_write_access_and_permission(client, login, people, app, db):
    as_(client, login, people["owner"])
    assert client.post("/canvas/create", data={"title": "Nope"}).status_code == 403
    settings(db, canvas_access="editor", canvas_write_access="editor")
    response = client.post("/canvas/create", data={"title": "My board", "description": "d"})
    assert response.status_code == 302 and "/canvas/my-board" in response.headers["Location"]
    row = db.one("SELECT * FROM canvas__layouts WHERE slug = 'my-board'")
    assert row["creator_id"] == people["owner"]["id"] and row["visibility"] == "private"
    assert db.scalar("SELECT COUNT(*) FROM canvas__history WHERE layout_id = ?", (row["id"],)) == 1
    # Plain users lack canvas.create by default even when the setting allows everyone.
    settings(db, canvas_access="all", canvas_write_access="all")
    as_(client, login, people["user"])
    assert client.post("/canvas/create", data={"title": "x"}).status_code == 403
    # Validation.
    as_(client, login, people["owner"])
    client.post("/canvas/create", data={"title": "  "})
    client.post("/canvas/create", data={"title": "t" * 201})
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 1


def test_rename_keeps_slug(client, login, people, app, db):
    layout = make_canvas(app, people["owner"], "First name")
    as_(client, login, people["owner"])
    client.post(f"/canvas/{layout['slug']}/edit", data={"title": "Second", "description": "about"})
    row = db.one("SELECT * FROM canvas__layouts WHERE id = ?", (layout["id"],))
    assert row["title"] == "Second" and row["slug"] == "first-name" and row["description"] == "about"


def test_sharing_management(client, login, people, app, db):
    settings(db, canvas_access="editor")
    layout = make_canvas(app, people["owner"])
    as_(client, login, people["owner"])
    url = f"/canvas/{layout['slug']}/share"
    client.post(url, data={"action": "add_user", "username": "plain1", "permission": "edit"})
    client.post(url, data={"action": "add_user", "username": "plain1", "permission": "view"})
    rows = db.all("SELECT * FROM canvas__permissions WHERE layout_id = ?", (layout["id"],))
    assert [(r["user_id"], r["permission"]) for r in rows] == [(people["user"]["id"], "view")]
    # Roles without global access cannot be shared with.
    client.post(url, data={"action": "add_role", "role": "user", "permission": "view"})
    client.post(url, data={"action": "add_role", "role": "editor", "permission": "edit"})
    roles = db.column("SELECT role FROM canvas__permissions WHERE role IS NOT NULL AND layout_id = ?",
                      (layout["id"],))
    assert roles == ["editor"]
    client.post(url, data={"action": "add_user", "username": "ghost", "permission": "view"})
    client.post(url, data={"action": "set_visibility", "visibility": "bogus"})
    assert db.scalar("SELECT visibility FROM canvas__layouts WHERE id = ?", (layout["id"],)) == "private"
    client.post(url, data={"action": "remove_user", "user_id": people["user"]["id"]})
    assert db.scalar("SELECT COUNT(*) FROM canvas__permissions WHERE user_id IS NOT NULL") == 0
    page = client.get(f"/canvas/{layout['slug']}/settings")
    assert page.status_code == 200


def test_transfer_and_delete(client, login, people, app, db):
    layout = make_canvas(app, people["owner"])
    as_(client, login, people["owner"])
    response = client.post(f"/canvas/{layout['slug']}/share",
                           data={"action": "transfer_ownership", "new_owner": "editor2"})
    assert response.status_code == 302
    assert db.scalar("SELECT creator_id FROM canvas__layouts WHERE id = ?", (layout["id"],)) == people["editor"]["id"]
    assert client.post(f"/canvas/{layout['slug']}/delete").status_code == 404
    as_(client, login, people["editor"])
    assert client.post(f"/canvas/{layout['slug']}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 0


def test_list_order_is_per_user_and_validated(client, login, people, app, db):
    settings(db, canvas_access="editor")
    first = make_canvas(app, people["owner"], "Alpha")
    second = make_canvas(app, people["owner"], "Beta")
    hidden = make_canvas(app, people["admin"], "Hidden")
    as_(client, login, people["owner"])
    response = client.post("/api/canvas/layout-order", json={"layout_ids": [second["id"], hidden["id"], "x",
                                                                            first["id"]]})
    assert response.status_code == 200
    assert db.column("SELECT layout_id FROM canvas_user_layout_order WHERE user_id = ? ORDER BY sort_order",
                     (people["owner"]["id"],)) == [second["id"], first["id"]]
    body = client.get("/canvas").data.decode()
    assert body.index("Beta") < body.index("Alpha")
    # The shared counter is canvas-only: the kanban/site setting is not touched.
    assert db.scalar("SELECT list_order_version FROM site_settings") == 0


def test_shared_order_version_under_open_access(client, login, people, app, db):
    settings(db, canvas_open_access=1)
    first = make_canvas(app, people["owner"], "Alpha")
    second = make_canvas(app, people["owner"], "Beta")
    as_(client, login, people["user"])
    before = client.get("/api/canvas/list-order-version").get_json()["list_order_version"]
    saved = client.post("/api/canvas/layout-order", json={"layout_ids": [second["id"], first["id"]]}).get_json()
    assert saved["list_order_version"] != before
    assert db.column("SELECT layout_id FROM canvas_user_layout_order WHERE user_id IS NULL ORDER BY sort_order") == [
        second["id"], first["id"]]


def test_admin_settings_page(client, login, people, db):
    as_(client, login, people["owner"])
    assert client.get("/admin/canvas").status_code == 403
    as_(client, login, people["admin"])
    assert client.get("/admin/canvas").status_code == 200
    client.post("/admin/canvas", data={"canvas_access": "all", "canvas_write_access": "bogus",
                                      "canvas_open_access": "1"})
    row = db.one("SELECT canvas_access, canvas_write_access, canvas_open_access, canvas_public_access_enabled "
                 "FROM site_settings")
    assert dict(row) == {"canvas_access": "all", "canvas_write_access": "admin", "canvas_open_access": 1,
                         "canvas_public_access_enabled": 0}


def test_nav_link_and_embed_script_slot(client, login, people, app, db):
    make_canvas(app, people["owner"])
    as_(client, login, people["owner"])
    body = client.get("/canvas").data
    assert b"canvas-embed.js" in body
    with app.test_request_context(), connection_scope():
        assert registry.is_enabled("canvas")


def test_easy_wiki_hides_canvas(app_factory):
    easy = app_factory(environ={"BW_EASY_WIKI": "1"})
    with easy.test_request_context(), connection_scope():
        assert not registry.is_enabled("canvas")
