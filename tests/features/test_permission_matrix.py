"""The permission matrix: each base role × each verb × pages, Kanban boards and canvases.

The same verbs apply to the three kinds of content (``bananawiki/wiki/permissions.py``):
view, create, edit own, delete own, share own (boards and canvases), edit all,
delete all. "Own" is what the person created; "all" is everything they can open.
"""

from __future__ import annotations

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import access as canvas_access
from bananawiki.wiki.features.canvas import service as canvases
from bananawiki.wiki.features.kanban import access as kanban_access
from bananawiki.wiki.features.kanban import service as boards
from bananawiki.wiki.features.kanban import store as board_store
from bananawiki.wiki.features.pages import service as pages

from .access_support import only_stored_permissions

VERBS = ("view", "create", "edit_own", "delete_own", "share_own", "edit_all", "delete_all", "share_all")
EXPECTED = {
    "user": {"view": True, "create": True, "edit_own": True, "delete_own": True, "share_own": True,
             "edit_all": False, "delete_all": False, "share_all": False},
    "editor": {"view": True, "create": True, "edit_own": True, "delete_own": True, "share_own": True,
               "edit_all": True, "delete_all": False, "share_all": False},
    "admin": dict.fromkeys(VERBS, True),
}


def _in_app(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


@pytest.fixture
def world(app, make_user):
    """Someone else's page, board and canvas visible to every member."""
    other = make_user("someone")

    def build():
        board = boards.create_board(other, "Theirs")
        boards.set_visibility(board, "members")
        layout = canvases.create("Theirs", "", other["id"])
        canvases.set_visibility(layout, "members")
        return {"page": pages.create("Theirs", "x", author_id=other["id"]),
                "board": board_store.get_board(board["id"]), "canvas": canvases.get(layout["id"])}

    return _in_app(app, build)


def _own(app, user):
    def build():
        board = boards.create_board(user, "Mine")
        layout = canvases.create("Mine", "", user["id"])
        return {"page": pages.create("Mine", "x", author_id=user["id"]),
                "board": board_store.get_board(board["id"]), "canvas": canvases.get(layout["id"])}

    return _in_app(app, build)


def _matrix(app, user, own, theirs):
    def check():
        return {
            "page": {
                "view": pages.can_view(theirs["page"], user), "create": pages.can_create(None, user),
                "edit_own": pages.can_edit(own["page"], user), "delete_own": pages.can_delete(own["page"], user),
                "edit_all": pages.can_edit(theirs["page"], user),
                "delete_all": pages.can_delete(theirs["page"], user),
            },
            "board": {
                "view": kanban_access.can_view(user, theirs["board"]), "create": kanban_access.can_create(user),
                "edit_own": kanban_access.can_write(user, own["board"]),
                "delete_own": kanban_access.can_delete(user, own["board"]),
                "share_own": kanban_access.is_owner(user, own["board"]),
                "edit_all": kanban_access.can_write(user, theirs["board"]),
                "delete_all": kanban_access.can_delete(user, theirs["board"]),
                "share_all": kanban_access.is_owner(user, theirs["board"]),
            },
            "canvas": {
                "view": canvas_access.can_view(user, theirs["canvas"]), "create": canvas_access.can_create(user),
                "edit_own": canvas_access.can_edit(user, own["canvas"]),
                "delete_own": canvas_access.can_delete(user, own["canvas"]),
                "share_own": canvas_access.is_owner(user, own["canvas"]),
                "edit_all": canvas_access.can_edit(user, theirs["canvas"]),
                "delete_all": canvas_access.can_delete(user, theirs["canvas"]),
                "share_all": canvas_access.is_owner(user, theirs["canvas"]),
            },
        }

    return _in_app(app, check)


@pytest.mark.parametrize("role", ["user", "editor", "admin"])
def test_the_matrix_of_the_built_in_defaults(app, make_user, world, role):
    person = make_user(f"person_{role}", role=role)
    found = _matrix(app, person, _own(app, person), world)
    expected = EXPECTED[role]
    assert found["page"] == {verb: expected[verb] for verb in found["page"]}
    assert found["board"] == expected
    assert found["canvas"] == expected


@pytest.mark.parametrize("kind", ["page", "kanban", "canvas"])
@pytest.mark.parametrize("verb", ["create", "edit_own", "delete_own", "edit_all", "delete_all"])
def test_each_verb_follows_its_key(app, db, make_user, world, kind, verb):
    """Granting or taking away one key (here through the base role's defaults) changes that verb only."""
    person = make_user("person_x")
    own = _own(app, person)
    before = _matrix(app, person, own, world)["board" if kind == "kanban" else kind]
    granted = not EXPECTED["user"][verb]
    db.execute("INSERT INTO base_role_permissions (role, permission_key, granted) VALUES ('user', ?, ?)",
               (f"{kind}.{verb}", int(granted)))
    after = _matrix(app, person, own, world)["board" if kind == "kanban" else kind]
    changed = {name for name in after if after[name] != before[name]}
    if kind == "page" and verb == "edit_own":  # deleting one's page needs editing it too
        assert changed == {"edit_own", "delete_own"} and after["edit_own"] is granted
    elif kind == "page" and verb == "delete_all":  # among the pages they can edit
        assert changed == set()
    else:
        assert changed == {verb} and after[verb] is granted


def test_share_own_is_its_own_key(app, db, make_user, world):
    person = make_user("person_s")
    own = _own(app, person)
    db.executemany("INSERT INTO base_role_permissions (role, permission_key, granted) VALUES ('user', ?, 0)",
                   [("kanban.share_own",), ("canvas.share_own",)])
    found = _matrix(app, person, own, world)
    assert found["board"]["share_own"] is False and found["canvas"]["share_own"] is False
    assert found["board"]["edit_own"] and found["canvas"]["delete_own"]


def test_without_view_nothing_works(app, db, make_user, world):
    person = make_user("person_v")
    own = _own(app, person)
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (person["id"],))  # individual permissions: none at all
    only_stored_permissions(db, person["id"])
    found = _matrix(app, person, own, world)
    for kind in ("page", "board", "canvas"):
        assert not any(found[kind].values()), kind


def test_category_limits_apply_to_own_pages(app, db, make_user):
    from .pages_support import make_category, restrict

    open_cat, closed = make_category(app, "Open"), make_category(app, "Closed")
    person = make_user("person_c")
    restrict(db, person, read=[open_cat["id"], closed["id"]], write=[open_cat["id"]])
    own = _in_app(app, lambda: pages.create("Mine", "x", category_id=closed["id"], author_id=person["id"]))
    current = db.one("SELECT * FROM users WHERE id = ?", (person["id"],))
    assert _in_app(app, lambda: pages.can_view(own, current)) is True
    assert _in_app(app, lambda: pages.can_edit(own, current)) is False  # not writable there
    assert _in_app(app, lambda: pages.can_create(open_cat["id"], current)) is True
    assert _in_app(app, lambda: pages.can_create(closed["id"], current)) is False
