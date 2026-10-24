"""BM mode: the easy mode of a wiki, which replaced the easy wiki switch, adjustable item by item."""

from __future__ import annotations

import json
import re

import pytest

from bananawiki.wiki import bm, registry
from bananawiki.wiki.db import connection_scope

EDITOR_RULE = '[data-bm-hide~="pages.simple_editor"]{display:none!important}'
ALL_SIMPLIFICATIONS = ("pages.simple_editor", "pages.simple_actions", "pages.simple_reading", "pages.simple_sidebar",
                       "admin.short_menu", "kanban.simple_board", "kanban.simple_tickets", "canvas.simple_tools",
                       "bm_mode.getting_started", "users.simple_settings", "difficulty_tags.hide_tags")


@pytest.fixture
def easy(app_factory):
    """A wiki the operator starts in BM mode (the hosting platform's easy mode)."""
    return app_factory(environ={"BW_EASY_WIKI": "1"})


def _in(app, check):
    with app.test_request_context(), connection_scope():
        return check()


def _signed_in(app, login, user):
    client = app.test_client()
    login(client, user)
    return client


def _save(client, *, mode, features=(), off=()):
    data = {"bm_mode": "1"} if mode else {}
    data.update({f"feature_{feature}": "1" for feature in features})
    data.update({f"simplify_{item}": "1" for item in ALL_SIMPLIFICATIONS if item not in off})
    return client.post("/admin/bm-mode", data=data)


def test_a_wiki_is_in_full_mode_unless_the_operator_or_an_administrator_says_otherwise(app, client, admin, login):
    assert not _in(app, bm.active)
    assert _in(app, lambda: registry.is_enabled("chat"))
    page = _signed_in(app, login, admin).get("/").get_data(as_text=True)
    assert "data-bm-hide~=" not in page


def test_bm_mode_hides_advanced_features_but_keeps_boards_and_canvases(easy):
    assert _in(easy, bm.active)
    assert not _in(easy, lambda: registry.is_enabled("chat"))
    assert not _in(easy, lambda: registry.is_enabled("assessments"))
    assert _in(easy, lambda: registry.is_enabled("kanban")) and _in(easy, lambda: registry.is_enabled("canvas"))
    assert set(_in(easy, bm.applied)) == set(ALL_SIMPLIFICATIONS)


def test_simplifications_reach_the_pages(easy, admin, login):
    client = _signed_in(easy, login, admin)
    editor = client.get("/create").get_data(as_text=True)
    assert EDITOR_RULE in editor and 'data-bm-hide="pages.simple_editor"' in editor
    dashboard = client.get("/admin/dashboard").get_data(as_text=True)
    assert "Getting started" in dashboard and 'href="/create-page"' in dashboard
    assert 'href="/admin/roles"' not in dashboard, "the short menu leaves roles out"
    assert client.get("/admin/roles").status_code == 200, "but the page stays reachable"
    assert client.get("/chats").status_code == 404, "a hidden feature answers 404 like a switched-off one"


def test_administrators_choose_features_and_simplifications(easy, admin, login):
    client = _signed_in(easy, login, admin)
    page = client.get("/admin/bm-mode").get_data(as_text=True)
    assert 'name="bm_mode" value="1" checked' in page and 'name="feature_kanban" value="1" checked' in page
    assert 'name="feature_chat" value="1">' in page, "hidden by default, one tick away"
    response = _save(client, mode=True, features=("chat", "kanban", "canvas"), off=("kanban.simple_board",))
    assert response.status_code == 302
    assert _in(easy, lambda: registry.is_enabled("chat"))
    assert not _in(easy, lambda: registry.is_enabled("drafts")), "everything left unticked is hidden"
    assert "kanban.simple_board" not in _in(easy, bm.applied)
    with easy.app_context(), connection_scope() as session:
        entry = session.one("SELECT details FROM audit_log WHERE action = 'bm_mode.changed'")
    assert json.loads(entry["details"])["simplifications_off"] == ["kanban.simple_board"]


def test_switching_bm_mode_off_restores_the_full_wiki(easy, admin, login):
    client = _signed_in(easy, login, admin)
    _save(client, mode=False)
    assert not _in(easy, bm.active)
    assert _in(easy, lambda: registry.is_enabled("chat")) and _in(easy, lambda: registry.is_enabled("drafts"))
    assert EDITOR_RULE not in client.get("/create").get_data(as_text=True)


def test_the_administrators_choice_holds_until_the_operator_changes_theirs(app_factory, admin, login):
    easy = app_factory(environ={"BW_EASY_WIKI": "1"})
    _save(_signed_in(easy, login, admin), mode=False)
    assert not _in(app_factory(environ={"BW_EASY_WIKI": "1"}), bm.active), "the same operator setting: still off"
    full = app_factory()
    assert not _in(full, bm.active)
    _save(_signed_in(full, login, admin), mode=True)
    assert _in(app_factory(), bm.active)
    assert _in(app_factory(environ={"BW_EASY_WIKI": "1"}), bm.active)
    # The platform switched the wiki to full mode after the administrator chose: the platform's switch applies.
    _save(_signed_in(app_factory(environ={"BW_EASY_WIKI": "1"}), login, admin), mode=True)
    assert not _in(app_factory(), bm.active)


def _bm_column(app, user, column="bm_mode"):
    with app.app_context(), connection_scope() as session:
        return session.scalar(f"SELECT {column} FROM users WHERE id = ?", (user["id"],))


def _named(page):
    """The simplifications a page names on its root element (``<html data-bm>``), for scripts and stylesheets."""
    root = re.search(r"<html[^>]*>", page).group(0)
    found = re.search(r'\sdata-bm="([^"]*)"', root)
    return set(found.group(1).split()) if found else set()


def test_pages_name_the_simplifications_that_apply_on_their_root(app, easy, admin, make_user, login):
    assert _named(_signed_in(easy, login, admin).get("/").get_data(as_text=True)) == set(ALL_SIMPLIFICATIONS)
    member, own = make_user("plainview"), make_user("ownview", bm_mode=1)
    full = _signed_in(app, login, member).get("/").get_data(as_text=True)
    assert _named(full) == set() and "data-bm=" not in re.search(r"<html[^>]*>", full).group(0)
    assert _named(_signed_in(app, login, own).get("/").get_data(as_text=True)) == set(ALL_SIMPLIFICATIONS)
    chief = _signed_in(easy, login, admin)
    assert _save(chief, mode=True, features=("canvas",), off=("pages.simple_editor",)).status_code == 302
    left = set(_in(easy, bm.applied))
    assert "pages.simple_editor" not in left and "canvas.simple_tools" in left
    assert _named(chief.get("/").get_data(as_text=True)) == left


def test_one_person_can_use_bm_mode_while_the_wiki_does_not(app, make_user, login):
    member, other = make_user("member"), make_user("other")
    client = _signed_in(app, login, member)
    page = client.get("/settings/display").get_data(as_text=True)
    assert 'action="/settings/bm-mode"' in page and EDITOR_RULE not in page
    assert client.post("/settings/bm-mode", data={"bm_mode": "1"}).status_code == 302
    assert _bm_column(app, member) == 1
    assert EDITOR_RULE in client.get("/create").get_data(as_text=True), "the simpler editor, for them"
    assert client.get("/chats").status_code == 404, "a feature BM mode hides is hidden from them"
    assert 'href="/chats"' not in client.get("/").get_data(as_text=True)
    others = _signed_in(app, login, other)
    assert others.get("/chats").status_code == 200 and EDITOR_RULE not in others.get("/create").get_data(as_text=True)
    with app.test_request_context("/"), connection_scope():
        from flask import g

        g.user = {**member, "bm_mode": 1}
        assert registry.is_enabled("chat") and not registry.shown("chat"), "it keeps working for the wiki"
    with app.test_request_context("/api/v1/chats"), connection_scope():
        from flask import g

        g.user = {**member, "bm_mode": 1}
        assert registry.shown("chat"), "the REST API is never hidden"
    client.post("/settings/bm-mode", data={})
    assert _bm_column(app, member) == 0 and client.get("/chats").status_code == 200


def test_when_the_whole_wiki_is_in_bm_mode_everyone_is(easy, make_user, login):
    member = make_user("member")
    client = _signed_in(easy, login, member)
    page = client.get("/settings/display").get_data(as_text=True)
    assert "This wiki uses BM mode for everyone." in page and 'action="/settings/bm-mode"' not in page
    assert client.post("/settings/bm-mode", data={}).status_code == 403
    assert EDITOR_RULE in client.get("/create").get_data(as_text=True)


def test_administrators_decide_who_may_switch(app, admin, make_user, login):
    member, other = make_user("member"), make_user("other")
    admin_client = _signed_in(app, login, admin)
    admin_client.post("/admin/bm-mode", data={"feature_kanban": "1"})  # "everyone may switch" left unticked
    client = _signed_in(app, login, member)
    assert client.post("/settings/bm-mode", data={"bm_mode": "1"}).status_code == 403
    assert "Your administrators decide" in client.get("/settings/display").get_data(as_text=True)
    admin_client.post(f"/admin/users/{member['id']}/bm-mode", data={"bm_self": "allow"})
    assert client.post("/settings/bm-mode", data={"bm_mode": "1"}).status_code == 302, "allowed for them alone"
    assert _signed_in(app, login, other).post("/settings/bm-mode", data={"bm_mode": "1"}).status_code == 403
    admin_client.post("/admin/bm-mode", data={"feature_kanban": "1", "bm_self_service": "1"})
    admin_client.post(f"/admin/users/{member['id']}/bm-mode", data={"bm_mode": "1", "bm_self": "deny"})
    assert client.post("/settings/bm-mode", data={}).status_code == 403, "denied for them, whatever the wiki says"
    assert _bm_column(app, member) == 1


def test_administrators_switch_accounts_within_their_reach(app, admin, make_user, login, db):
    member, boss = make_user("member"), make_user("boss", role="owner")
    admin_client = _signed_in(app, login, admin)
    page = admin_client.get(f"/admin/users/{member['id']}").get_data(as_text=True)
    assert f'action="/admin/users/{member["id"]}/bm-mode"' in page
    admin_client.post(f"/admin/users/{member['id']}/bm-mode", data={"bm_mode": "1", "bm_self": ""})
    assert _bm_column(app, member) == 1
    entry = db.one("SELECT target_id, details FROM audit_log WHERE action = 'bm_mode.account_changed'")
    assert entry["target_id"] == member["id"] and json.loads(entry["details"])["bm_mode"] is True
    admin_client.post(f"/admin/users/{boss['id']}/bm-mode", data={"bm_mode": "1", "bm_self": "deny"})
    assert _bm_column(app, boss) == 0 and _bm_column(app, boss, "bm_self") == "", "a plain administrator cannot"
    assert admin_client.post(f"/admin/users/{member['id']}/bm-mode", data={"bm_self": "maybe"}).status_code == 400
    response = _everyone(admin_client, "on", "user", "editor", "admin")
    assert response.status_code == 302
    assert _bm_column(app, admin) == 1 and _bm_column(app, boss) == 0, "every account the administrator manages"
    _everyone(admin_client, "off", "user", "editor", "admin")
    assert _bm_column(app, member) == 0 and _bm_column(app, admin) == 0


def _everyone(client, action, *roles):
    return client.post("/admin/bm-mode/everyone", data={"action": action, "role": list(roles)})


def _states(app, *users):
    return [_bm_column(app, user) for user in users]


def test_every_account_at_once_takes_only_the_ticked_roles(app, admin, make_user, login, db):
    member, writer = make_user("member"), make_user("writer", role="editor")
    client = _signed_in(app, login, admin)
    page = client.get("/admin/bm-mode").get_data(as_text=True)
    assert 'name="role" value="user" checked' in page, "users are ticked to begin with"
    assert 'name="role" value="editor">' in page and 'name="role" value="admin">' in page
    _everyone(client, "on", "user")
    assert _states(app, member, writer, admin) == [1, 0, 0]
    entry = db.one("SELECT details FROM audit_log WHERE action = 'bm_mode.everyone_changed'")
    assert json.loads(entry["details"]) == {"bm_mode": True, "roles": ["user"], "accounts": 1}
    _everyone(client, "on", "editor", "user")
    log = " ".join(client.get("/admin/audit?action=bm_mode.everyone_changed").get_data(as_text=True).split())
    assert '<strong title="roles">Roles</strong>: User, Editor</span>' in log, "in the reader's language"
    assert _states(app, member, writer, admin) == [1, 1, 0], "the administrator's own account only with administrators"
    _everyone(client, "on", "admin")
    assert _states(app, member, writer, admin) == [1, 1, 1]


def test_taking_accounts_out_follows_the_ticked_roles_too(app, admin, make_user, login, db):
    member, writer = make_user("member"), make_user("writer", role="editor")
    db.execute("UPDATE users SET bm_mode = 1")
    client = _signed_in(app, login, admin)
    _everyone(client, "off", "editor")
    assert _states(app, member, writer, admin) == [1, 0, 1]
    _everyone(client, "off", "user")
    assert _states(app, member, writer, admin) == [0, 0, 1]
    _everyone(client, "off", "admin")
    assert _states(app, member, writer, admin) == [0, 0, 0]


def test_a_custom_role_counts_under_its_base_role(app, admin, make_user, login, db):
    reviewer = make_user("reviewer")
    client = _signed_in(app, login, admin)
    client.post("/admin/roles/create", data={"name": "Reviewers", "description": "", "base_role": "editor",
                                             "permissions": ["page.view_all", "page.edit_all"]})
    role_id = db.scalar("SELECT id FROM custom_roles")
    client.post(f"/admin/roles/{role_id}/assign", data={"user_id": reviewer["id"]})
    assert db.scalar("SELECT role FROM users WHERE id = ?", (reviewer["id"],)) == "editor"
    _everyone(client, "on", "user")
    assert _bm_column(app, reviewer) == 0
    _everyone(client, "on", "editor")
    assert _bm_column(app, reviewer) == 1


def test_a_plain_administrator_ticking_administrators_changes_only_their_own_account(app, admin, make_user, login):
    colleague, boss = make_user("colleague", role="admin"), make_user("boss", role="owner")
    keeper, member = make_user("keeper", is_superuser=1), make_user("member")
    client = _signed_in(app, login, admin)
    assert "Just your own account" in client.get("/admin/bm-mode").get_data(as_text=True)
    _everyone(client, "on", "admin")
    assert _states(app, admin, colleague, boss, keeper, member) == [1, 0, 0, 0, 0]
    _everyone(client, "on", "user", "editor", "admin")
    assert _states(app, admin, colleague, boss, keeper, member) == [1, 0, 0, 0, 1], "a superuser is never a user here"


def test_an_owner_ticking_administrators_changes_the_other_administrators(app, admin, make_user, login):
    boss, other_owner = make_user("boss", role="owner"), make_user("other_owner", role="owner")
    root_admin, member = make_user("root_admin", role="admin", is_superuser=1), make_user("member")
    client = _signed_in(app, login, boss)
    assert "Other administrators and your own account" in client.get("/admin/bm-mode").get_data(as_text=True)
    _everyone(client, "on", "user")
    assert _states(app, boss, admin, member) == [0, 0, 1]
    _everyone(client, "on", "admin")
    assert _states(app, boss, admin, other_owner, root_admin) == [1, 1, 0, 0]


def test_no_ticked_role_changes_nobody(app, admin, make_user, login, db):
    member = make_user("member")
    client = _signed_in(app, login, admin)
    response = client.post("/admin/bm-mode/everyone", data={"action": "on"}, follow_redirects=True)
    assert "Tick at least one role." in response.get_data(as_text=True)
    assert _everyone(client, "on", "owner", "superuser").status_code == 302, "not roles the card offers"
    assert _everyone(client, "maybe", "user").status_code == 400
    assert _states(app, member, admin) == [0, 0]
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'bm_mode.everyone_changed'") == 0


def test_new_accounts_start_in_bm_mode_when_the_administrators_say_so(app, admin, make_user, login):
    assert _bm_column(app, make_user("before")) == 0, "off by default"
    _signed_in(app, login, admin).post("/admin/bm-mode", data={"bm_new_accounts": "1", "bm_self_service": "1"})
    assert _bm_column(app, make_user("after")) == 1


def test_switching_is_refused_while_impersonating(app, admin, make_user, login):
    member = make_user("member")
    client = _signed_in(app, login, admin)
    assert client.post(f"/admin/users/{member['id']}/impersonate").status_code == 302
    assert client.post("/settings/bm-mode", data={"bm_mode": "1"}).status_code == 403
    assert _bm_column(app, member) == 0


def test_only_administrators_change_bm_mode(easy, make_user, login, admin):
    member = _signed_in(easy, login, make_user("member"))
    assert member.get("/admin/bm-mode").status_code in (302, 403)
    assert member.post("/admin/bm-mode", data={}).status_code in (302, 403)
    assert _in(easy, bm.active)


def test_a_hidden_feature_turned_on_in_plugins_is_shown_in_bm_mode(easy, admin, login):
    client = _signed_in(easy, login, admin)
    client.post("/admin/plugins/chat/enable")
    assert _in(easy, lambda: registry.is_enabled("chat"))
    page = client.get("/admin/bm-mode").get_data(as_text=True)
    assert 'name="feature_chat" value="1" checked' in page


def test_the_getting_started_list_disappears_when_done(easy, admin, login, db):
    client = _signed_in(easy, login, admin)
    db.execute("INSERT INTO pages (title, slug, content) VALUES ('Notes', 'notes', 'hello')")
    db.execute("UPDATE site_settings SET site_name = 'Class 3B', open_signup = 1")
    assert "Getting started" not in client.get("/admin/dashboard").get_data(as_text=True)


def test_unknown_choices_and_broken_settings_fall_back_to_the_defaults(easy, db):
    db.execute("UPDATE site_settings SET bm_choices = 'not json'")
    assert _in(easy, lambda: registry.is_enabled("kanban")) and not _in(easy, lambda: registry.is_enabled("chat"))
    db.execute("UPDATE site_settings SET bm_choices = '[1, 2]'")
    assert _in(easy, bm.choices) == {}


def test_hiding_follows_the_same_rules_as_switching_off(easy, admin, make_user, login):
    """A plain administrator may not switch the audit log off, so BM mode does not hide it for them either."""
    client = _signed_in(easy, login, admin)
    response = _save(client, mode=True, features=("kanban",))
    assert response.status_code == 302
    assert _in(easy, lambda: registry.is_enabled("audit"))
    assert "Only owners and superusers can switch the audit log off." in client.get("/admin/bm-mode").get_data(
        as_text=True)
    owner = _signed_in(easy, login, make_user("boss", role="owner"))
    _save(owner, mode=True, features=("kanban",))
    assert not _in(easy, lambda: registry.is_enabled("audit"))
    with easy.app_context(), connection_scope() as session:
        last = session.one("SELECT details FROM audit_log WHERE action = 'bm_mode.changed' ORDER BY id DESC LIMIT 1")
    assert "audit" in json.loads(last["details"])["hidden_features"], "hiding the audit log is itself recorded"


def test_bm_mode_leaves_third_party_plugins_to_the_plugins_page(app_factory, tmp_path, admin, login):
    """Showing a plugin again would run its code, which only owners and superusers may switch on."""
    from .plugins_support import HELLO_PLUGIN, install_folder, start_enabled

    install_folder(tmp_path, HELLO_PLUGIN)
    app = start_enabled(app_factory, tmp_path, "hello_plugin", BW_EASY_WIKI="1")
    assert _in(app, bm.active) and _in(app, lambda: registry.is_enabled("hello_plugin"))
    client = _signed_in(app, login, admin)
    assert "feature_hello_plugin" not in client.get("/admin/bm-mode").get_data(as_text=True)
    client.post("/admin/bm-mode", data={"bm_mode": "1", "feature_kanban": "1"})  # hello_plugin left unticked
    assert _in(app, lambda: registry.is_enabled("hello_plugin")), "only Admin → Plugins switches it"
    assert "feature:hello_plugin" not in _in(app, bm.choices)


def test_one_persons_bm_mode_keeps_custom_page_content_and_hides_only_reachable_queues(app, make_user):
    from flask import g
    from werkzeug.exceptions import NotFound

    member = make_user("member")
    with app.test_request_context(), connection_scope():
        registry.set_enabled("custom_pages", True)
    for path, endpoint_hidden in (("/_cpd/1", False), ("/_cpf/1/a.png", False), ("/admin/custom-pages", True)):
        with app.test_request_context(path), connection_scope():
            g.user = {**member, "bm_mode": 1}
            check = registry.require_feature("custom_pages")
            if endpoint_hidden:
                with pytest.raises(NotFound):
                    check()
            else:
                assert check() is None, "a published page's document and files stay visible: the page does"
    from bananawiki.wiki import attention

    with app.test_request_context("/"), connection_scope():
        g.user = {**member, "bm_mode": 1}
        assert "chat" not in {source.id.split(".")[0] for source in attention.sources()}, "its pages 404 for them"
    with app.app_context(), connection_scope():
        assert "chat" in {source.id.split(".")[0] for source in attention.sources()}, "emails still cover it"
