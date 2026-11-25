"""A member who may not do something is told why, in plain words."""

from __future__ import annotations

import pytest

from bananawiki.wiki import permissions as perms
from bananawiki.wiki.db import connection_scope

from .access_support import members_see
from .pages_support import make_category, make_page


@pytest.fixture
def world(app, db):
    docs = make_category(app, "Docs")
    team = make_category(app, "Team")
    return {"docs": docs, "team": team,
            "guide": make_page(app, "Guide", "text", category_id=docs["id"]),
            "loose": make_page(app, "Loose", "text")}


def set_permissions(admin_client, user, keys, **form):
    response = admin_client.post(f"/admin/users/{user['id']}/permissions", data={"permissions": sorted(keys), **form})
    assert response.status_code == 302


def text(response) -> str:
    return response.get_data(as_text=True)


def test_editing_someone_elses_page(app, client, make_user, login, world):
    login(client, make_user("member"))
    response = client.get("/page/guide/edit")
    assert response.status_code == 403
    body = text(response)
    assert "You can read this page but not edit it." in body
    assert "You can edit the pages you created, but not other people&#39;s." in body
    assert "An administrator can give you access." in body and 'href="/page/guide"' in body
    # The page itself says so in its menu, where nothing else offers a way in.
    assert "You can read this page but not edit it." in text(client.get("/page/guide"))


def test_the_refusal_offers_the_request_when_it_is_on(app, client, make_user, login, db, world):
    db.execute("UPDATE site_settings SET access_requests_enabled = 1")
    login(client, make_user("member"))
    body = text(client.get("/page/guide/edit"))
    assert "Ask for edit access" in body and "An administrator can give you access." not in body
    assert "You can read this page but not edit it." not in text(client.get("/page/guide")).split("menu__panel")[1][:400]


def test_a_category_limit_is_named(admin_client, app, client, make_user, login, world):
    member = make_user("member", role="editor")
    set_permissions(admin_client, member, perms.builtin_defaults("editor"),
                    write_restricted="1", write_category_ids=[str(world["team"]["id"])])
    login(client, member)
    assert "Editing pages in “Docs” is limited to some people." in text(client.get("/page/guide/edit"))
    assert "Pages without a category can only be edited" in text(client.get("/page/loose/edit"))


def test_a_role_without_editing(admin_client, app, client, make_user, login, db, world):
    member = make_user("member")
    set_permissions(admin_client, member, {"page.view_all"})
    mine = make_page(app, "Mine", "x", author_id=member["id"])
    login(client, member)
    assert "Your role doesn&#39;t include editing pages." in text(client.get(f"/page/{mine['slug']}/edit"))
    assert "Your role doesn&#39;t include creating pages." in text(client.get("/create"))


def test_scripts_get_the_reason_too(app, client, make_user, login, world):
    login(client, make_user("member"))
    response = client.get("/page/guide/edit", headers={"Accept": "application/json"})
    assert response.status_code == 403
    assert response.get_json()["error"].startswith("You can read this page but not edit it.")


def test_creating_in_a_category_one_may_not_write_to(admin_client, app, client, make_user, login, world):
    member = make_user("member")
    set_permissions(admin_client, member, perms.builtin_defaults("user"),
                    write_restricted="1", write_category_ids=[str(world["team"]["id"])])
    login(client, member)
    response = client.post("/create", data={"title": "New", "content": "x", "category_id": str(world["docs"]["id"])})
    assert response.status_code == 403
    assert "You can&#39;t create pages here." in text(response)
    assert "Creating pages in “Docs” is limited to some people." in text(response)


def test_signed_out_visitors_are_asked_to_sign_in(app, client, db, world):
    db.execute("UPDATE site_settings SET public_mode = 1")
    response = client.get("/page/guide/edit")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_a_board_says_who_can_share_it(app, admin_client, admin, make_user, login, db):
    response = admin_client.post("/kanban/create", data={"title": "Roadmap"})
    board_id = int(response.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    members_see(db)
    other = app.test_client()
    login(other, make_user("member"))
    body = text(other.get(f"/kanban/{board_id}"))
    assert "You can view this board but not edit it. admin_user, who owns it, can share it with you" in body
    assert "You can view this board" not in text(admin_client.get(f"/kanban/{board_id}"))


def test_a_canvas_says_who_can_share_it(app, admin, make_user, login, db):
    from bananawiki.wiki.features.canvas import service

    with app.test_request_context(), connection_scope():
        layout = service.create("Plan", "", admin["id"])
        service.set_visibility(layout, "members")
    other = app.test_client()
    login(other, make_user("member"))
    message = "You can view this canvas but not edit it. admin_user, who owns it, can share it with you"
    assert message in text(other.get(f"/canvas/{layout['slug']}"))
    refused = other.get(f"/canvas/{layout['slug']}/settings")
    assert refused.status_code == 403 and message in text(refused)
