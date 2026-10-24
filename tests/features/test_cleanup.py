"""Settings and links that used to live in two places, or led nowhere."""

from __future__ import annotations

import json
import re

from bananawiki.wiki.db import connection_scope

from .people_support import edit_page, make_page, publish, set_feature


def _signed_in(app, login, user):
    client = app.test_client()
    login(client, user)
    return client


def _admin(app, login):
    from bananawiki.wiki import accounts

    with app.test_request_context(), connection_scope():
        admin = accounts.by_username("boss") or accounts.create("boss", "correct horse battery", role="admin",
                                                                emit_event=False)
    client = app.test_client()
    login(client, admin, "correct horse battery")
    return client


# The page builder is switched on under Plugins only


def test_a_page_builder_the_host_forbids_is_locked_in_plugins_too(app_factory, login):
    app = app_factory(environ={"BW_FORBID_PAGE_BUILDER": "1"})
    client = _admin(app, login)
    page = client.get("/admin/plugins").get_data(as_text=True)
    assert "has not approved this feature" in page and 'action="/admin/plugins/page_builder/enable"' not in page
    client.post("/admin/plugins/page_builder/enable")
    with app.test_request_context(), connection_scope() as session:
        assert session.scalar("SELECT page_builder_enabled FROM site_settings") == 0
        session.execute("UPDATE site_settings SET page_builder_enabled = 1")
    page = client.get("/admin/plugins").get_data(as_text=True)
    assert "Off (server setting)" in page
    assert 'href="/admin/page-builder"' not in client.get("/admin/dashboard").get_data(as_text=True), \
        "no menu entry leading to a page that answers 404"


# Deleting guide pages at once: one setting, on Pending deletions


def test_the_guide_bypass_is_set_on_pending_deletions_only(app, admin_client, db):
    set_feature(app, "deletion_slowdown", True)
    page = admin_client.get("/admin/documentation").get_data(as_text=True)
    assert 'name="docs_bypass_deletion_slowdown"' not in page
    assert 'href="/admin/pending-deletions#docs-bypass"' in page and "deleted at once" in page
    response = admin_client.post("/admin/pending-deletions/settings", data={})
    assert response.status_code == 302
    assert db.scalar("SELECT docs_bypass_deletion_slowdown FROM site_settings") == 0
    entry = db.one("SELECT details FROM audit_log WHERE action = 'docs.settings_changed'")
    assert json.loads(entry["details"]) == {"bypass_deletion_slowdown": 0}
    assert "wait out the deletion waiting period" in admin_client.get("/admin/documentation").get_data(as_text=True)


def test_an_old_documentation_form_still_saves_through_the_same_path(app, admin_client, db):
    set_feature(app, "deletion_slowdown", True)
    response = admin_client.post("/admin/docs-settings", data={})
    assert response.headers["Location"].endswith("/admin/pending-deletions")
    assert db.scalar("SELECT docs_bypass_deletion_slowdown FROM site_settings") == 0
    admin_client.post("/admin/docs-settings", data={})
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'docs.settings_changed'") == 1, \
        "saving the same value again records nothing"


# The administration menu offers non-administrators only what they may open


def test_invite_managers_get_no_menu_of_pages_they_cannot_open(app, make_user, login, db):
    from .test_admin_invites import grant

    editor = make_user("inviter", role="editor")
    grant(db, editor, "invite.view", "invite.generate")
    client = _signed_in(app, login, editor)
    page = client.get("/admin/codes").get_data(as_text=True)
    assert 'class="admin-nav"' not in page and "/admin/bulk" not in page
    assert client.get("/admin/bulk").status_code in (302, 403)


# "Last edited by" links only to profiles the reader may open


def test_the_author_is_linked_only_when_the_reader_may_see_the_profile(app, make_user, login, db):
    author, reader = make_user("writer", role="editor"), make_user("reader")
    page = make_page(app, "Notes", "first", author_id=author["id"])
    edit_page(app, page["id"], "second", author_id=author["id"])
    client = _signed_in(app, login, reader)
    html = client.get(f"/page/{page['slug']}").get_data(as_text=True)
    assert "<strong>writer</strong>" in html and 'href="/users/writer"' not in html
    publish(db, author)
    html = client.get(f"/page/{page['slug']}").get_data(as_text=True)
    assert 'href="/users/writer"' in html
    assert client.get("/users/writer").status_code == 200


def test_page_history_follows_the_same_rule(app, make_user, login, db):
    author, reader = make_user("writer", role="editor"), make_user("reader", role="editor")
    page = make_page(app, "Notes", "first", author_id=author["id"])
    edit_page(app, page["id"], "second", author_id=author["id"])
    html = _signed_in(app, login, reader).get(f"/page/{page['slug']}/history").get_data(as_text=True)
    assert "writer" in html and 'href="/users/writer"' not in html


# Profile fields: a card only when there is something to show or fill in


def test_no_empty_profile_fields_card_on_a_wiki_without_fields(app, admin_client, make_user, login, db):
    member = make_user("member")
    client = _signed_in(app, login, member)
    page = client.get("/users/member").get_data(as_text=True)
    assert "profile-about-heading" not in page and "/settings/profile-fields" not in page
    admin_client.post("/admin/profile-fields", data={"action": "create", "label": "Motto", "field_type": "text"})
    page = client.get("/users/member").get_data(as_text=True)
    assert "Profile fields" in page and "Edit profile fields" in page
    assert "You have not filled in any profile fields." in page


# The built-in guide is offered in the wiki's own language


def test_documentation_suggests_the_default_language(admin_client, db):
    db.execute("UPDATE site_settings SET interface_language = 'it', interface_language_fallback = 'de'")
    page = admin_client.get("/admin/documentation").get_data(as_text=True)
    assert '<option value="it" selected>' in page
    db.execute("UPDATE site_settings SET interface_language = 'fr'")
    page = admin_client.get("/admin/documentation").get_data(as_text=True)
    assert '<option value="de" selected>' in page, "no French guide: the fallback chosen under Languages"


# Language: chosen on the Account tab; the display page keeps it


def test_people_with_an_account_choose_their_language_on_the_account_tab(app, make_user, login, db):
    member = make_user("member")
    client = _signed_in(app, login, member)
    client.post("/account/language", data={"language": "it"})
    page = client.get("/settings/display").get_data(as_text=True)
    assert 'name="interface_language"' not in page
    client.post("/settings/display", data={"theme_mode": "dark"})
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (member["id"],)))
    assert saved["interface_language"] == "it" and saved["theme_mode"] == "dark"
    client.post("/settings/display", data={"theme_mode": "dark", "interface_language": "de"})
    saved = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (member["id"],)))
    assert saved["interface_language"] == "de", "an older form that still sends the language is honoured"


def test_visitors_still_choose_their_language_on_the_display_page(app, db):
    db.execute("UPDATE site_settings SET public_mode = 1")
    page = app.test_client().get("/settings/display").get_data(as_text=True)
    assert 'name="interface_language"' in page


def test_the_display_page_has_one_name(app, make_user, login):
    page = _signed_in(app, login, make_user("member")).get("/settings/display").get_data(as_text=True)
    assert "Display preferences</title>" not in page
    assert page.count(">Customise<") >= 2, "the top bar, the account menu and the tab agree"


# Plugins: what the server and the feature's own switches say


def test_federation_switched_off_on_the_server_is_shown_as_off(app_factory, login):
    app = app_factory()
    client = _admin(app, login)
    page = client.get("/admin/plugins").get_data(as_text=True)
    assert "Off (server setting)" in page
    assert 'data-id="federation"' not in page, "not offered in the sidebar order while it can never appear"
    page = _admin(app_factory(environ={"BW_FEDERATION_ENABLED": "1"}), login).get("/admin/plugins").get_data(
        as_text=True)
    assert "Off (server setting)" not in page and 'data-id="federation"' in page


def test_the_rest_api_row_says_when_the_api_itself_is_off(app, admin_client, db):
    set_feature(app, "api_service", True)
    page = admin_client.get("/admin/plugins").get_data(as_text=True)
    assert 'href="/admin/api-service"' in page and "The API itself is switched off" in page
    db.execute("UPDATE site_settings SET api_service_enabled = 1")
    assert "The API itself is switched off" not in admin_client.get("/admin/plugins").get_data(as_text=True)


# The sidebar keeps related apps together


def test_messages_and_groups_sit_together_before_people(admin_client):
    page = admin_client.get("/").get_data(as_text=True)
    apps = page[page.index('class="sidebar__apps"'):]
    apps = apps[:apps.index("</ul>")]
    links = re.findall(r'<a href="([^"]+)"', apps)
    messages = links.index("/chats")
    assert links[messages + 1] == "/groups" and links.index("/users") > messages + 1
