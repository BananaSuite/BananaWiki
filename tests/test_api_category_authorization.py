"""The category API applies the same rules as the category controls in the wiki.

It used to accept any editor for creating, renaming and moving categories,
without the category.create, category.edit and category.reorder permissions
the web interface checks. Moving never worked at all: the handler passed
parent_id to a function that only renames, so any request carrying it raised.
Nothing validated a new parent either, so the web interface's guards against
putting a category inside itself or one of its own descendants had no
counterpart here.
"""

import pytest
from werkzeug.security import generate_password_hash

import db


def _editor(permissions):
    user_id = db.create_user("cat_editor", generate_password_hash("pw-123456!"), role="editor")
    db.update_user(user_id, api_access_enabled=1)
    db.set_user_permissions(user_id, set(permissions))
    token = db.create_api_token(user_id, name="categories", permissions={
        "read": True, "write": True, "scopes": ["categories"]})
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


@pytest.fixture(autouse=True)
def api_on(admin_user):
    # admin_user also marks setup complete; without it every request is
    # redirected to the setup page and never reaches the API.
    db.update_site_settings(api_service_enabled=1)


# Permissions

def test_creating_needs_category_create(client):
    refused = client.post("/api/v1/categories", json={"name": "Nope"}, headers=_editor({"page.edit_all"}))
    assert refused.status_code == 403


def test_renaming_needs_category_edit(client):
    category = db.create_category("Original")
    response = client.put(f"/api/v1/categories/{category}", json={"name": "Renamed"},
                          headers=_editor({"page.edit_all"}))
    assert response.status_code == 403
    assert db.get_category(category)["name"] == "Original"


def test_moving_needs_category_reorder(client):
    parent = db.create_category("Parent")
    child = db.create_category("Child")
    response = client.put(f"/api/v1/categories/{child}", json={"parent_id": parent},
                          headers=_editor({"category.edit"}))
    assert response.status_code == 403
    assert db.get_category(child)["parent_id"] is None


def test_with_the_permissions_all_three_work(client):
    headers = _editor({"category.create", "category.edit", "category.reorder"})
    created = client.post("/api/v1/categories", json={"name": "Made"}, headers=headers)
    assert created.status_code == 201
    made = created.get_json()["category"]["id"]
    parent = db.create_category("Home for it")
    assert client.put(f"/api/v1/categories/{made}", json={"name": "Renamed"}, headers=headers).status_code == 200
    moved = client.put(f"/api/v1/categories/{made}", json={"parent_id": parent}, headers=headers)
    assert moved.status_code == 200
    assert db.get_category(made)["name"] == "Renamed"
    assert db.get_category(made)["parent_id"] == parent


# Moving

def test_moving_to_the_top_level(client):
    parent = db.create_category("Parent")
    child = db.create_category("Child", parent_id=parent)
    response = client.put(f"/api/v1/categories/{child}", json={"parent_id": None},
                          headers=_editor({"category.reorder"}))
    assert response.status_code == 200
    assert db.get_category(child)["parent_id"] is None


@pytest.mark.parametrize("target", ["itself", "descendant", "missing", "garbage"])
def test_a_move_the_wiki_would_refuse_is_refused(client, target):
    root = db.create_category("Root")
    child = db.create_category("Child", parent_id=root)
    parent_id = {"itself": root, "descendant": child, "missing": 999999, "garbage": "x"}[target]
    response = client.put(f"/api/v1/categories/{root}", json={"parent_id": parent_id},
                          headers=_editor({"category.reorder"}))
    assert response.status_code == 400
    assert db.get_category(root)["parent_id"] is None


# Creating

@pytest.mark.parametrize("body", [
    {"name": "Orphan", "parent_id": 999999},
    {"name": "Bad parent", "parent_id": "x"},
    {"name": "x" * 101},
    {"name": "   "},
])
def test_creating_is_validated_like_the_wiki(client, body):
    before = len(db.list_categories())
    response = client.post("/api/v1/categories", json=body, headers=_editor({"category.create"}))
    assert response.status_code == 400
    assert len(db.list_categories()) == before
