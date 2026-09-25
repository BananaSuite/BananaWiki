"""The REST API applies the same page permissions as the web editor.

The API used to check only that the caller's role was editor or above. A
restricted editor, or anyone with a bearer token, could read pages outside
their categories, edit, create, move and delete pages in categories they had
no write access to, and overwrite pages that were protected or checked out
by someone else. The web interface refused all of that. These tests keep the
two in step by asserting the API refuses exactly what the editor refuses.
"""

import pytest
from werkzeug.security import generate_password_hash

import db


def _editor(name, *, read=None, write=None, permissions=("page.edit_all",)):
    user_id = db.create_user(name, generate_password_hash("pw-123456!"), role="editor")
    db.update_user(user_id, api_access_enabled=1)
    db.set_user_permissions(
        user_id, set(permissions),
        read_restricted=read is not None, read_category_ids=read or [],
        write_restricted=write is not None, write_category_ids=write or [],
    )
    return user_id


def _headers(user_id, *, write=True):
    token = db.create_api_token(user_id, name="authorization test", permissions={
        "read": True, "write": write, "scopes": ["pages"],
    })
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


@pytest.fixture
def wiki(client, admin_user):
    db.update_site_settings(api_service_enabled=1)
    allowed = db.create_category("Allowed")
    forbidden = db.create_category("Forbidden")
    db.create_page("Open", "open", "open text", category_id=allowed, user_id=admin_user)
    db.create_page("Secret", "secret", "classified text", category_id=forbidden, user_id=admin_user)
    return {"client": client, "admin": admin_user, "allowed": allowed, "forbidden": forbidden}


# Reading

def test_a_page_outside_the_readers_categories_is_not_returned(wiki):
    reader = _editor("reader", read=[wiki["allowed"]], write=[wiki["allowed"]])
    response = wiki["client"].get("/api/v1/pages/secret", headers=_headers(reader))
    # 404 rather than 403, so the API does not confirm that the page exists.
    assert response.status_code == 404
    assert b"classified" not in response.data


def test_listing_pages_works_once_pages_exist(wiki):
    """It used to raise on the first page it serialised, for every caller."""
    admin_headers = _headers(wiki["admin"])
    db.update_user(wiki["admin"], api_access_enabled=1)
    response = wiki["client"].get("/api/v1/pages", headers=admin_headers)
    assert response.status_code == 200
    slugs = {page["slug"] for page in response.get_json()["pages"]}
    assert {"open", "secret"} <= slugs


def test_listing_pages_leaves_out_what_the_caller_cannot_read(wiki):
    reader = _editor("lister", read=[wiki["allowed"]], write=[wiki["allowed"]])
    response = wiki["client"].get("/api/v1/pages", headers=_headers(reader))
    assert response.status_code == 200
    slugs = {page["slug"] for page in response.get_json()["pages"]}
    assert "open" in slugs
    assert "secret" not in slugs


# Writing

@pytest.mark.parametrize("attempt", [
    "edit", "create", "delete", "move_out", "move_in",
])
def test_a_write_restricted_editor_cannot_touch_another_category(wiki, attempt):
    editor = _editor("writer", write=[wiki["allowed"]])
    client, headers, forbidden, allowed = wiki["client"], _headers(editor), wiki["forbidden"], wiki["allowed"]
    response = {
        "edit": lambda: client.put("/api/v1/pages/secret", json={"content": "overwritten"}, headers=headers),
        "create": lambda: client.post("/api/v1/pages", json={
            "title": "Planted", "content": "x", "category_id": forbidden}, headers=headers),
        "delete": lambda: client.delete("/api/v1/pages/secret", headers=headers),
        "move_out": lambda: client.put("/api/v1/pages/secret", json={"category_id": allowed}, headers=headers),
        "move_in": lambda: client.put("/api/v1/pages/open", json={"category_id": forbidden}, headers=headers),
    }[attempt]()
    assert response.status_code == 403

    secret = db.get_page_by_slug("secret")
    assert secret["content"] == "classified text"
    assert secret["category_id"] == forbidden
    assert not secret["pending_deletion"]
    assert db.get_page_by_slug("open")["category_id"] == allowed
    assert db.get_page_by_slug("planted") is None


def test_the_same_editor_can_still_work_inside_their_own_category(wiki):
    editor = _editor("insider", write=[wiki["allowed"]])
    headers = _headers(editor)
    client = wiki["client"]
    assert client.put("/api/v1/pages/open", json={"content": "edited"}, headers=headers).status_code == 200
    assert db.get_page_by_slug("open")["content"] == "edited"
    created = client.post("/api/v1/pages", json={
        "title": "Fresh", "content": "x", "category_id": wiki["allowed"]}, headers=headers)
    assert created.status_code == 201


def test_an_unrestricted_editor_and_an_admin_are_unaffected(wiki):
    editor = _editor("free")
    client = wiki["client"]
    assert client.put("/api/v1/pages/secret", json={"content": "by editor"},
                      headers=_headers(editor)).status_code == 200
    db.update_user(wiki["admin"], api_access_enabled=1)
    assert client.put("/api/v1/pages/secret", json={"content": "by admin"},
                      headers=_headers(wiki["admin"])).status_code == 200


# Page governance

def test_a_protected_page_cannot_be_overwritten_through_the_api(wiki):
    db.enable_plugin("page_governance")
    db.update_site_settings(page_protection_enabled=1)
    protector = _editor("protector")
    db.set_page_protection(db.get_page_by_slug("secret")["id"], protector)
    # Allowed to delete pages in general, so the 409 comes from the lock.
    other = _editor("other", permissions=("page.edit_all", "page.delete"))
    headers = _headers(other)
    client = wiki["client"]
    assert client.put("/api/v1/pages/secret", json={"content": "overwritten"}, headers=headers).status_code == 409
    assert client.delete("/api/v1/pages/secret", headers=headers).status_code == 409
    assert db.get_page_by_slug("secret")["content"] == "classified text"


def test_a_page_checked_out_by_someone_else_cannot_be_overwritten(wiki):
    db.enable_plugin("page_governance")
    db.update_site_settings(page_reservations_enabled=1)
    holder = _editor("holder")
    db.reserve_page(db.get_page_by_slug("secret")["id"], holder)
    other = _editor("latecomer")
    response = wiki["client"].put("/api/v1/pages/secret", json={"content": "overwritten"},
                                  headers=_headers(other))
    assert response.status_code == 409
    assert db.get_page_by_slug("secret")["content"] == "classified text"


# Deletion

def test_deletion_slowdown_applies_to_api_deletes_too(wiki):
    """The plugin promises a 48-hour grace period. A bearer token used to
    delete permanently and immediately, which is the case it exists for."""
    db.enable_plugin("deletion_slowdown")
    editor = _editor("deleter", permissions=("page.edit_all", "page.delete"))
    response = wiki["client"].delete("/api/v1/pages/secret", headers=_headers(editor))
    assert response.status_code == 202
    assert response.get_json()["pending_deletion"] is True
    page = db.get_page_by_slug("secret")
    assert page is not None and page["pending_deletion"]


def test_a_page_already_pending_deletion_is_not_deleted_again(wiki):
    db.enable_plugin("deletion_slowdown")
    editor = _editor("second_deleter", permissions=("page.edit_all", "page.delete"))
    headers = _headers(editor)
    assert wiki["client"].delete("/api/v1/pages/secret", headers=headers).status_code == 202
    assert wiki["client"].delete("/api/v1/pages/secret", headers=headers).status_code == 409
    assert db.get_page_by_slug("secret") is not None


def test_without_the_slowdown_plugin_delete_still_removes_the_page(wiki):
    # The shared fixture turns every built-in plugin on, so switch this one off.
    db.disable_plugin("deletion_slowdown")
    editor = _editor("plain_deleter", permissions=("page.edit_all", "page.delete"))
    response = wiki["client"].delete("/api/v1/pages/secret", headers=_headers(editor))
    assert response.status_code == 200
    assert db.get_page_by_slug("secret") is None


def test_deleting_needs_the_page_delete_permission(wiki):
    """Editors do not get page.delete by default, and the web delete asks for
    it. Editing every page is not the same as being allowed to delete them."""
    db.disable_plugin("deletion_slowdown")
    editor = _editor("editor_without_delete")
    response = wiki["client"].delete("/api/v1/pages/secret", headers=_headers(editor))
    assert response.status_code == 403
    assert db.get_page_by_slug("secret") is not None


def test_creating_a_page_in_a_category_that_does_not_exist_is_refused(wiki):
    """The editor refuses this; the API used to store the dangling id."""
    editor = _editor("creator")
    response = wiki["client"].post("/api/v1/pages", json={
        "title": "Orphan", "content": "x", "category_id": 999999}, headers=_headers(editor))
    assert response.status_code == 400
    assert db.get_page_by_slug("orphan") is None
