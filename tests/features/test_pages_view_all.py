"""``page.view_all`` and ``category.view_all`` are enforced, not just shown in the admin forms."""

from __future__ import annotations

import pytest

from bananawiki.wiki import permissions
from bananawiki.wiki.features.pages import service

from .api_support import call, enable_api, issue
from .pages_support import in_app, make_category, make_page, restrict


@pytest.fixture
def world(app, db):
    enable_api(app, db)
    archive = make_category(app, "Zebra Archive")
    make_page(app, "Filed Okapi", "quasar inside", category_id=archive["id"])
    make_page(app, "Loose Okapi", "quasar outside")
    return archive


def _signed_in(app, login, user):
    client = app.test_client()
    login(client, user)
    return client


def _text(response) -> str:
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.mark.parametrize("role", ["user", "editor"])
def test_default_roles_read_and_list_everything(app, make_user, login, world, role):
    client = _signed_in(app, login, make_user(f"default_{role}", role=role))
    assert client.get("/page/filed-okapi").status_code == 200
    navigation = _text(client.get("/navigation"))
    assert "Zebra Archive" in navigation and "Filed Okapi" in navigation and "Loose Okapi" in navigation
    found = client.get("/api/sidebar/search?q=Zebra").json
    assert [c["name"] for c in found["categories"]] == ["Zebra Archive"]
    results = _text(client.get("/search?q=quasar"))
    assert "Filed Okapi" in results and "Loose Okapi" in results
    assert "Zebra Archive" in _text(client.get("/search?q=Zebra&type=category"))


def test_without_page_view_all_no_page_is_readable(app, make_user, login, db, world):
    reader = make_user("no_pages", api_access_enabled=1)
    restrict(db, reader, keys={"category.view_all", "search.pages", "history.view"})
    client = _signed_in(app, login, reader)
    for slug in ("filed-okapi", "loose-okapi", "home"):
        assert client.get(f"/page/{slug}").status_code == 404
        assert client.get(f"/api/pages/preview-by-slug?slug={slug}").status_code == 404
    navigation = _text(client.get("/navigation"))
    assert "Filed Okapi" not in navigation and "Loose Okapi" not in navigation
    assert "Okapi" not in _text(client.get("/search?q=quasar"))
    assert client.get("/api/sidebar/search?q=Okapi").json["pages"] == []
    token = issue(app, reader)
    assert call(client, "GET", "/pages", token).json["pages"] == []
    assert call(client, "GET", "/pages/loose-okapi", token).status_code == 404
    assert in_app(app, lambda: service.visible_filter(reader)) == ("0", [])


def test_custom_role_without_page_view_all_reads_nothing(app, admin_client, make_user, login, db, world):
    response = admin_client.post("/admin/roles/create", data={
        "name": "Listeners", "description": "", "base_role": "user",
        "permissions": ["category.view_all", "search.pages"]})
    assert response.status_code == 302
    role_id = db.scalar("SELECT id FROM custom_roles WHERE name = 'Listeners'")
    member = make_user("listener")
    db.execute("UPDATE users SET custom_role_id = ? WHERE id = ?", (role_id, member["id"]))
    client = _signed_in(app, login, member)
    assert client.get("/page/filed-okapi").status_code == 404
    assert "Okapi" not in _text(client.get("/search?q=quasar"))


def test_without_category_view_all_categories_are_not_listed(app, make_user, login, db, world):
    reader = make_user("no_listing", api_access_enabled=1)
    restrict(db, reader, keys={"page.view_all", "search.pages"})
    client = _signed_in(app, login, reader)
    navigation = _text(client.get("/navigation"))
    assert "Zebra Archive" not in navigation and "Loose Okapi" in navigation
    found = client.get("/api/sidebar/search?q=Okapi").json
    assert found["categories"] == [] and {p["title"] for p in found["pages"]} == {"Filed Okapi", "Loose Okapi"}
    assert client.get("/api/sidebar/search?q=Zebra").json["categories"] == []
    assert "Zebra Archive" not in _text(client.get("/search?q=Zebra&type=category"))
    # Read access is unchanged: the category and its pages still open from a link.
    assert "Filed Okapi" in _text(client.get(f"/category/{world['id']}"))
    assert client.get("/page/filed-okapi").status_code == 200
    token = issue(app, reader, ["categories"])
    assert call(client, "GET", "/categories", token).json["categories"] == []
    assert call(client, "GET", f"/categories/{world['id']}", token).status_code == 200


def test_administrators_and_public_readers_are_unchanged(app, admin_client, db, world):
    assert "Zebra Archive" in _text(admin_client.get("/navigation"))
    assert admin_client.get("/page/filed-okapi").status_code == 200
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    anonymous = app.test_client()
    assert anonymous.get("/page/filed-okapi").status_code == 200
    navigation = _text(anonymous.get("/navigation"))
    assert "Zebra Archive" in navigation and "Filed Okapi" in navigation


def test_stored_permissions_get_their_implied_keys(make_user, db):
    """Rows saved without the admin forms (1.4 data) keep reading what they may edit."""
    editor = make_user("legacy_editor", role="editor")
    db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, 'page.edit_all')", (editor["id"],))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (editor["id"],))
    assert permissions.load_grants(db, editor).keys == {"page.edit_all", "page.view_all"}
