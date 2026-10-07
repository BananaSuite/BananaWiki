import pytest

from bananawiki.wiki import permissions
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import categories, service


@pytest.fixture
def ctx(app):
    with app.test_request_context(), connection_scope():
        yield


def as_user(user):
    from flask import g

    g.user = g.real_user = user
    g.pop("_grants", None)


def test_create_update_conflict_and_history(ctx, make_user):
    editor = make_user("editor1", role="editor")
    as_user(editor)
    page = service.create("Hello World", "one", author_id=editor["id"])
    assert page["slug"] == "hello-world" and page["revision"] == 1
    again = service.create("Hello World", "two", author_id=editor["id"])
    assert again["slug"] == "hello-world-2"
    updated = service.update(page, author_id=editor["id"], content="uno", expected_revision=1)
    assert updated["revision"] == 2
    with pytest.raises(service.EditConflict):
        service.update(page, author_id=editor["id"], content="stale", expected_revision=1)
    assert [h["edit_message"] for h in service.history(page["id"])][-1] == "Created"
    reverted = service.revert(updated, service.history(page["id"])[-1]["id"], author_id=editor["id"])
    assert reverted["content"] == "one"


def test_visibility_and_restricted_categories(ctx, make_user, db):
    cat_a = categories.create("A")
    cat_b = categories.create("B")
    p_a = service.create("In A", category_id=cat_a["id"], author_id=None)
    p_b = service.create("In B", category_id=cat_b["id"], author_id=None)
    hidden = service.create("Hidden", author_id=None)
    service.set_fields(hidden["id"], is_deindexed=1)
    reader = make_user("reader")
    for key in permissions.defaults("user"):  # individual settings replace the defaults: keep them
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (reader["id"], key))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 1)",
               (reader["id"],))
    db.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'read')",
               (reader["id"], cat_a["id"]))
    as_user(reader)
    titles = {p["title"] for p in service.list_visible()}
    assert "In A" in titles and "In B" not in titles and "Hidden" not in titles
    assert service.can_view(service.get(p_a["id"])) and not service.can_view(service.get(p_b["id"]))
    tree = categories.tree()
    assert [node["name"] for node in tree["categories"]] == ["A"]


def test_search(ctx, make_user):
    as_user(make_user("admin1", role="admin"))
    service.create("Photosynthesis", "Plants convert light into energy", author_id=None)
    service.create("Other", "nothing here", author_id=None)
    results = service.search("light energy")
    assert [r["title"] for r in results] == ["Photosynthesis"]
    assert [r["title"] for r in service.search("photo", titles_only=True)] == ["Photosynthesis"]


def test_category_delete_moves_children(ctx):
    parent = categories.create("Parent")
    child = categories.create("Child", parent["id"])
    page = service.create("P", category_id=parent["id"], author_id=None)
    categories.delete(parent)
    assert categories.get(child["id"])["parent_id"] is None
    assert service.get(page["id"])["category_id"] is None


def test_cycle_refused(ctx):
    a = categories.create("A")
    b = categories.create("B", a["id"])
    with pytest.raises(categories.CategoryError):
        categories.set_parent(a, b["id"])
