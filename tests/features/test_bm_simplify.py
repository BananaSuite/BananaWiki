"""BM mode's shorter settings, sidebar and page view, for a whole wiki in BM mode and for one account in it."""

from __future__ import annotations

import pytest
from flask import g

from bananawiki.wiki.db import connection_scope

SETTINGS_RULE = '[data-bm-hide~="users.simple_settings"]{display:none!important}'
READING_RULE = '[data-bm-hide~="pages.simple_reading"]{display:none!important}'
TAGS_RULE = '[data-bm-hide~="difficulty_tags.hide_tags"]{display:none!important}'


@pytest.fixture
def easy(app_factory):
    return app_factory(environ={"BW_EASY_WIKI": "1"})


@pytest.fixture(params=["wiki", "account"])
def setup(request, app, easy, make_user, login):
    """A signed-in editor in BM mode: the whole wiki's, or their account's only (BM mode is off for the wiki)."""
    wiki = easy if request.param == "wiki" else app
    with wiki.app_context(), connection_scope() as session:
        from bananawiki.wiki import accounts

        person = accounts.create(f"bm{request.param}", "a long passphrase 41", role="editor",
                                 extra={"bm_mode": 1 if request.param == "account" else 0}, emit_event=False)
        session.execute("INSERT INTO pages (title, slug, content, difficulty_tag) VALUES "
                        "('Notes', 'notes', 'hello', 'expert')")
    client = wiki.test_client()
    client.post("/login", data={"username": person["username"], "password": "a long passphrase 41"})
    return wiki, client, person


def _full_client(app, make_user, login, name="fullview"):
    client = app.test_client()
    login(client, make_user(name, role="editor"))
    return client


def test_settings_show_the_essentials_and_keep_the_rest_one_click_away(setup):
    _wiki, client, _person = setup
    page = client.get("/settings").get_data(as_text=True)
    assert SETTINGS_RULE in page
    for kept in ('action="/settings/username"', 'action="/settings/password"', 'id="f-language"',
                 'id="notifications"', 'href="/settings/display"'):
        assert kept in page, kept
    assert 'href="/settings/sessions" data-bm-hide="users.simple_settings"' in page
    assert 'aria-labelledby="delete-heading" data-bm-hide="users.simple_settings"' in page
    two_step = page.index('id="two-step"')
    assert page.rfind('data-bm-hide="users.simple_settings"', 0, two_step) > page.rfind("</div>", 0, two_step), \
        "two-step verification is inside a hidden section"
    assert 'href="/settings?all=1"' in page and "All settings" in page
    everything = client.get("/settings?all=1").get_data(as_text=True)
    assert 'data-bm-hide="users.simple_settings"' not in everything and 'id="two-step"' in everything
    assert "All settings" not in everything
    assert client.get("/settings/sessions").status_code == 200, "a hidden section stays at its address"


def test_customise_and_profile_stay_whole(setup):
    _wiki, client, _person = setup
    display = client.get("/settings/display").get_data(as_text=True)
    assert 'id="bm-mode"' in display, "the BM mode switch (or who decides it) stays reachable"
    assert client.get("/settings/profile").status_code == 200


def test_whoever_must_act_first_gets_the_whole_settings_page(setup):
    wiki, _client, person = setup
    from bananawiki.wiki.features.users import routes

    with wiki.test_request_context("/settings"), connection_scope() as session:
        g.user = g.real_user = {**session.one("SELECT * FROM users WHERE id = ?", (person["id"],))}
        assert routes.short_settings(g.user)
        g.user["role"] = "admin"
        session.execute("UPDATE site_settings SET two_step_required_for_admins = 1")
        from bananawiki.wiki import settings

        settings.invalidate()
        assert not routes.short_settings(g.user), "two-step verification is required of them and not set up"


def test_the_sidebar_keeps_pages_boards_and_canvases(setup):
    wiki, client, person = setup
    with wiki.app_context(), connection_scope() as session:  # an administrator opens every board and canvas
        session.execute("UPDATE users SET role = 'admin' WHERE id = ?", (person["id"],))
    page = client.get("/page/notes").get_data(as_text=True)
    sidebar = page[page.index('id="sidebar"'):]
    assert 'href="/kanban"' in sidebar and 'href="/canvas"' in sidebar
    assert 'href="/users"' not in sidebar, "People leaves the sidebar"
    assert client.get("/users").status_code == 200, "and stays at its address"


def test_a_page_shows_no_history_link_or_difficulty_tag(setup):
    _wiki, client, _person = setup
    page = client.get("/page/notes").get_data(as_text=True)
    assert READING_RULE in page and TAGS_RULE in page
    assert 'href="/page/notes/history" data-bm-hide="pages.simple_reading"' in page
    assert 'class="difficulty-tag-line" data-bm-hide="difficulty_tags.hide_tags"' in page
    assert 'data-dialog-open="dlg-difficulty-tag"' not in page, "no Tag action in the menu"
    editor = client.get("/page/notes/edit").get_data(as_text=True)
    assert 'data-tag-fields data-bm-hide="difficulty_tags.hide_tags"' in editor
    assert client.get("/page/notes/history").status_code == 200


def test_people_outside_bm_mode_see_everything(app, make_user, login):
    with app.app_context(), connection_scope() as session:
        session.execute("INSERT INTO pages (title, slug, content, difficulty_tag) VALUES ('Notes', 'notes', 'hi', 'expert')")
    client = _full_client(app, make_user, login)
    page = client.get("/page/notes").get_data(as_text=True)
    assert READING_RULE not in page and TAGS_RULE not in page and 'data-dialog-open="dlg-difficulty-tag"' in page
    assert 'href="/users"' in page[page.index('id="sidebar"'):]
    settings_page = client.get("/settings").get_data(as_text=True)
    assert SETTINGS_RULE not in settings_page and 'data-bm-hide="users.simple_settings"' not in settings_page
    assert "All settings" not in settings_page


def test_each_new_simplification_can_be_switched_off(easy, make_user, login):
    from .test_bm_mode import ALL_SIMPLIFICATIONS

    admin = easy.test_client()
    login(admin, make_user("chief", role="admin"))
    off = ("users.simple_settings", "pages.simple_sidebar", "pages.simple_reading", "difficulty_tags.hide_tags")
    data = {"bm_mode": "1", **{f"simplify_{item}": "1" for item in ALL_SIMPLIFICATIONS if item not in off},
            "feature_kanban": "1", "feature_canvas": "1", "feature_difficulty_tags": "1", "feature_page_history": "1"}
    assert admin.post("/admin/bm-mode", data=data).status_code == 302
    with easy.app_context(), connection_scope() as session:
        session.execute("INSERT INTO pages (title, slug, content, difficulty_tag) VALUES ('Notes', 'notes', 'hi', 'expert')")
    client = _full_client(easy, make_user, login)
    page = client.get("/page/notes").get_data(as_text=True)
    assert READING_RULE not in page and TAGS_RULE not in page and 'data-dialog-open="dlg-difficulty-tag"' in page
    assert 'href="/users"' in page[page.index('id="sidebar"'):]
    assert SETTINGS_RULE not in client.get("/settings").get_data(as_text=True)


def test_a_merge_waiting_for_someone_shows_the_whole_settings_page(app, make_user, login, db):
    """Their only way to confirm is the button the short page hides."""
    member, other = make_user("member", bm_mode=1), make_user("other")
    client = app.test_client()
    login(client, member)
    assert "/settings?all=1" in client.get("/settings").get_data(as_text=True), "short while nothing waits"
    db.execute("INSERT INTO account_merge_requests (source_user_id, target_user_id, created_by, status, "
               "source_approved, target_approved, created_at) VALUES (?, ?, ?, 'pending', 1, 0, "
               "'2026-01-01 00:00:00')", (other["id"], member["id"], other["id"]))
    assert "/settings?all=1" not in client.get("/settings").get_data(as_text=True)


def test_an_app_the_administrators_chose_to_show_stays_in_the_short_sidebar(app, admin, make_user, login):
    member = make_user("member", bm_mode=1)
    client = app.test_client()
    login(client, member)
    assert 'href="/chats"' not in client.get("/").get_data(as_text=True), "Chat is hidden in BM mode by default"
    admin_client = app.test_client()
    login(admin_client, admin)
    admin_client.post("/admin/bm-mode", data={"feature_chat": "1", "feature_kanban": "1", "feature_canvas": "1",
                                              "bm_self_service": "1"} | {
        f"simplify_{item}": "1" for item in ("pages.simple_sidebar", "users.simple_settings")})
    page = client.get("/").get_data(as_text=True)
    assert 'href="/chats"' in page, "shown in BM mode on purpose: its link stays, or it could not be reached"
