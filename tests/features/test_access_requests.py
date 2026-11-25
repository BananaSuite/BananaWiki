"""Asking for edit access: the setting, the request, the decision and what it gives."""

from __future__ import annotations

import pytest

from bananawiki.wiki import permissions as perms
from bananawiki.wiki.db import connection_scope

from .pages_support import make_category, make_page


@pytest.fixture
def world(app, db):
    docs = make_category(app, "Docs")
    team = make_category(app, "Team")
    return {
        "docs": docs, "team": team,
        "guide": make_page(app, "Guide", "text", category_id=docs["id"]),
        "plan": make_page(app, "Plan", "text", category_id=team["id"]),
        "loose": make_page(app, "Loose", "text"),
    }


def switch(db, on: bool = True) -> None:
    db.execute("UPDATE site_settings SET access_requests_enabled = ?", (int(on),))


def grants_of(app, user_id):
    with app.app_context(), connection_scope() as session:
        return perms.load_grants(session, session.one("SELECT * FROM users WHERE id = ?", (user_id,)))


def limit_writing(admin_client, user, *category_ids):
    """Through the admin form: the person writes only in *category_ids*."""
    keys = perms.builtin_defaults(user["role"])
    response = admin_client.post(f"/admin/users/{user['id']}/permissions", data={
        "permissions": sorted(keys), "write_restricted": "1", "write_category_ids": [str(c) for c in category_ids]})
    assert response.status_code == 302


def ask(client, page, message=""):
    return client.post(f"/page/{page['slug']}/ask-edit-access", data={"message": message})


def requests(db):
    return db.all("SELECT * FROM access_requests ORDER BY id")


# The setting


def test_switched_off_by_default_and_then_nothing_shows(app, client, make_user, login, db, world):
    member = make_user("member")
    login(client, member)
    assert db.scalar("SELECT access_requests_enabled FROM site_settings") == 0
    assert "Ask for edit access" not in client.get("/page/guide").get_data(as_text=True)
    assert ask(client, world["guide"]).status_code == 302
    assert requests(db) == []


def test_members_who_cannot_edit_see_the_button(app, client, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    login(client, member)
    page = client.get("/page/guide").get_data(as_text=True)
    assert "Ask for edit access" in page and "not only your own" in page
    # Signed-out visitors and people who can edit see nothing of it.
    client.post("/logout")
    db.execute("UPDATE site_settings SET public_mode = 1")
    assert "Ask for edit access" not in client.get("/page/guide").get_data(as_text=True)
    login(client, make_user("editor_one", role="editor"))
    assert "Ask for edit access" not in client.get("/page/guide").get_data(as_text=True)


def test_admin_page_switches_the_setting_with_its_one_save(admin_client, db):
    page = admin_client.get("/admin/access-requests").get_data(as_text=True)
    assert "Let people ask for edit access" in page and page.count('type="submit" form="access-requests-form"') == 1
    admin_client.post("/admin/access-requests", data={"access_requests_enabled": "1"})
    assert db.scalar("SELECT access_requests_enabled FROM site_settings") == 1
    admin_client.post("/admin/access-requests", data={})
    assert db.scalar("SELECT access_requests_enabled FROM site_settings") == 0
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'settings.updated'") == 2


# Asking


def test_a_request_for_the_wiki_when_categories_are_not_limited(client, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    login(client, member)
    assert ask(client, world["guide"], "  I fix typos  ").status_code == 302
    (row,) = requests(db)
    assert (row["scope"], row["category_id"], row["message"], row["status"]) == ("wiki", None, "I fix typos", "pending")
    assert row["page_id"] == world["guide"]["id"]
    assert "Edit access requested" in client.get("/page/guide").get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'access_request.created'") == 1
    assert db.scalar("SELECT COUNT(*) FROM attention_events WHERE source_id = 'access_requests.pending'") == 1


def test_a_request_for_the_category_when_writing_is_limited(admin_client, client, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    limit_writing(admin_client, member, world["team"]["id"])
    login(client, member)
    assert "“Docs”" in client.get("/page/guide").get_data(as_text=True)
    ask(client, world["guide"])
    (row,) = requests(db)
    assert (row["scope"], row["category_id"]) == ("category", world["docs"]["id"])


def test_no_request_where_none_can_help(admin_client, client, make_user, login, db, world):
    """A page without a category needs writing everywhere, which no request gives."""
    switch(db)
    member = make_user("member")
    limit_writing(admin_client, member, world["team"]["id"])
    login(client, member)
    assert "Ask for edit access" not in client.get("/page/loose").get_data(as_text=True)
    ask(client, world["loose"])
    assert requests(db) == []


def test_one_open_request_per_scope(client, make_user, login, db, world):
    switch(db)
    login(client, make_user("member"))
    ask(client, world["guide"])
    ask(client, world["plan"])  # the same scope (the wiki) from another page
    assert len(requests(db)) == 1
    assert "Edit access requested" in client.get("/page/plan").get_data(as_text=True)


def test_messages_are_limited(client, make_user, login, db, world):
    switch(db)
    login(client, make_user("member"))
    ask(client, world["guide"], "x" * 501)
    assert requests(db) == []


def test_a_declined_request_waits_before_asking_again(admin_client, app, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    other = app.test_client()
    login(other, member)
    ask(other, world["guide"])
    request_id = requests(db)[0]["id"]
    admin_client.post(f"/admin/access-requests/{request_id}/decline")
    assert "Request declined" in other.get("/page/guide").get_data(as_text=True)
    ask(other, world["guide"])
    assert len(requests(db)) == 1
    db.execute("UPDATE access_requests SET decided_at = datetime('now', '-8 days')")
    ask(other, world["guide"])
    assert [row["status"] for row in requests(db)] == ["declined", "pending"]


def test_requests_a_day_are_limited(admin_client, client, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    login(client, member)
    db.executemany("INSERT INTO access_requests (user_id, scope, status, created_at, decided_at) "
                   "VALUES (?, 'wiki', 'approved', datetime('now'), datetime('now'))", [(member["id"],)] * 5)
    ask(client, world["guide"])
    assert db.scalar("SELECT COUNT(*) FROM access_requests WHERE status = 'pending'") == 0


# Deciding


def test_approving_the_wiki_scope_gives_exactly_edit_all(admin_client, app, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    other = app.test_client()
    login(other, member)
    ask(other, world["guide"])
    before = grants_of(app, member["id"])
    listing = admin_client.get("/admin/access-requests").get_data(as_text=True)
    assert "member" in listing and "Approving gives them “Edit all pages”" in listing
    request_id = requests(db)[0]["id"]
    assert admin_client.post(f"/admin/access-requests/{request_id}/approve").status_code == 302
    after = grants_of(app, member["id"])
    assert after.keys - before.keys == {"page.edit_all"} and before.keys <= after.keys
    assert (after.read, after.write) == (before.read, before.write) and after.source == "defaults"
    assert other.get("/page/guide/edit").status_code == 200
    assert requests(db)[0]["status"] == "approved"
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'access_request.approved'") == 1
    notice = db.one("SELECT * FROM user_notices WHERE user_id = ?", (member["id"],))
    assert (notice["source_id"], notice["outcome"]) == ("access_requests.pending", "approved")
    assert "Your request for edit access was approved." in other.get("/").get_data(as_text=True)


def test_approving_a_category_adds_only_that_category(admin_client, app, make_user, login, db, world):
    switch(db)
    member = make_user("member", role="editor")
    limit_writing(admin_client, member, world["team"]["id"])
    other = app.test_client()
    login(other, member)
    ask(other, world["guide"])
    before = grants_of(app, member["id"])
    listing = admin_client.get("/admin/access-requests").get_data(as_text=True)
    assert "Approving adds “Docs” to the categories they can edit in." in listing
    admin_client.post(f"/admin/access-requests/{requests(db)[0]['id']}/approve")
    after = grants_of(app, member["id"])
    assert after.keys == before.keys
    assert after.write == perms.CategoryAccess(True, frozenset({world["team"]["id"], world["docs"]["id"]}))
    assert other.get("/page/guide/edit").status_code == 200
    assert not perms.can_write_category(after, None)


def test_approving_for_a_custom_role_holder_keeps_the_role(admin_client, app, make_user, login, db, world):
    switch(db)
    admin_client.post("/admin/roles/create", data={
        "name": "Team writers", "description": "", "base_role": "user",
        "permissions": ["page.view_all", "page.edit_all", "category.view_all"],
        "write_restricted": "1", "write_category_ids": [str(world["team"]["id"])],
        "read_restricted": "1", "read_category_ids": [str(world["docs"]["id"])]})
    role_id = db.scalar("SELECT id FROM custom_roles")
    member = make_user("member")
    admin_client.post(f"/admin/roles/{role_id}/assign", data={"user_id": member["id"]})
    other = app.test_client()
    login(other, member)
    ask(other, world["guide"])
    (row,) = requests(db)
    assert row["scope"] == "category" and row["category_id"] == world["docs"]["id"]
    admin_client.post(f"/admin/access-requests/{row['id']}/approve")
    found = grants_of(app, member["id"])
    assert db.scalar("SELECT custom_role_id FROM users WHERE id = ?", (member["id"],)) == role_id
    assert found.source == "custom_role" and found.keys == found.start
    assert perms.can_write_category(found, world["docs"]["id"])
    # Their own addition stays when the role changes, and the role's other holders are untouched.
    admin_client.post(f"/admin/roles/{role_id}", data={
        "name": "Team writers", "description": "", "base_role": "user",
        "permissions": ["page.view_all", "page.edit_all"],
        "write_restricted": "1", "write_category_ids": [str(world["team"]["id"])]})
    assert perms.can_write_category(grants_of(app, member["id"]), world["docs"]["id"])
    colleague = make_user("colleague")
    admin_client.post(f"/admin/roles/{role_id}/assign", data={"user_id": colleague["id"]})
    assert not perms.can_write_category(grants_of(app, colleague["id"]), world["docs"]["id"])


def test_declining_changes_nothing_and_tells_the_person(admin_client, app, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    other = app.test_client()
    login(other, member)
    ask(other, world["guide"])
    before = grants_of(app, member["id"])
    admin_client.post(f"/admin/access-requests/{requests(db)[0]['id']}/decline")
    assert grants_of(app, member["id"]).keys == before.keys
    notice = db.one("SELECT * FROM user_notices WHERE user_id = ?", (member["id"],))
    assert notice["outcome"] == "denied"
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'access_request.declined'") == 1
    # A decided request cannot be decided again.
    admin_client.post(f"/admin/access-requests/{requests(db)[0]['id']}/approve")
    assert grants_of(app, member["id"]).keys == before.keys


def test_only_administrators_decide(app, client, make_user, login, db, world):
    switch(db)
    member = make_user("member")
    login(client, member)
    ask(client, world["guide"])
    request_id = requests(db)[0]["id"]
    for person in (member, make_user("editor_one", role="editor")):
        login(client, person)
        assert client.get("/admin/access-requests").status_code == 403
        assert client.post(f"/admin/access-requests/{request_id}/approve").status_code == 403
        assert client.post("/admin/access-requests", data={"access_requests_enabled": "0"}).status_code == 403
    assert requests(db)[0]["status"] == "pending"
    assert db.scalar("SELECT access_requests_enabled FROM site_settings") == 1


def test_open_requests_count_in_the_attention_list(admin_client, app, make_user, login, db, world):
    switch(db)
    other = app.test_client()
    login(other, make_user("member"))
    ask(other, world["guide"])
    assert "Requests for edit access" in admin_client.get("/attention").get_data(as_text=True)
    dashboard = admin_client.get("/admin/access-requests").get_data(as_text=True)
    assert "Access requests" in dashboard


def test_the_decision_is_emailed_when_the_wiki_sends_emails(admin_client, app, make_user, login, db, world):
    from bananawiki.core import mail
    from bananawiki.wiki.features.attention import mailer

    from .governance_support import set_settings

    switch(db)
    set_settings(app, decision_email_enabled=1, public_base_url="https://wiki.example.org")
    member = make_user("member")
    db.execute("UPDATE users SET email = 'member@example.org' WHERE id = ?", (member["id"],))
    other = app.test_client()
    login(other, member)
    ask(other, world["guide"])
    admin_client.post(f"/admin/access-requests/{requests(db)[0]['id']}/approve")
    with mail.capture_outbox() as outbox, app.test_request_context(), connection_scope():
        assert mailer.send_decisions() == 1
    assert outbox[0].to == "member@example.org" and "request for edit access was approved" in outbox[0].text


def test_the_api_lists_approves_and_declines(app, client, db, make_user, login, world):
    from .api_support import call, enable_api, issue

    enable_api(app, db)
    switch(db)
    boss = make_user("api_boss", role="admin", api_access_enabled=1)
    first, second = make_user("first", api_access_enabled=1), make_user("second", api_access_enabled=1)
    for person in (first, second):
        browser = app.test_client()
        login(browser, person)
        ask(browser, world["guide"], f"from {person['username']}")
    token = issue(app, boss, ["users"])
    listing = call(client, "GET", "/access-requests", token).json
    assert [(r["username"], r["scope"], r["status"]) for r in listing["access_requests"]] == [
        ("second", "wiki", "pending"), ("first", "wiki", "pending")]
    first_id, second_id = (r["id"] for r in reversed(listing["access_requests"]))
    # Tokens of members, and read-only tokens, decide nothing.
    assert call(client, "POST", f"/access-requests/{first_id}/approve", issue(app, first, ["users"])).status_code == 403
    read_only = issue(app, boss, ["users"], write=False)
    assert call(client, "POST", f"/access-requests/{first_id}/approve", read_only).status_code == 403
    approved = call(client, "POST", f"/access-requests/{first_id}/approve", token)
    assert approved.status_code == 200
    assert approved.json["granted"] == {"permissions": ["page.edit_all"], "write_categories": []}
    assert approved.json["access_request"]["status"] == "approved"
    assert "page.edit_all" in grants_of(app, first["id"]).keys
    assert call(client, "POST", f"/access-requests/{first_id}/decline", token).json["code"] == "already_decided"
    declined = call(client, "POST", f"/access-requests/{second_id}/decline", token)
    assert declined.json["access_request"]["status"] == "declined"
    assert "page.edit_all" not in grants_of(app, second["id"]).keys
    assert call(client, "POST", "/access-requests/999/approve", token).status_code == 404
    assert len(call(client, "GET", "/access-requests?status=all", token).json["access_requests"]) == 2
    assert call(client, "GET", "/access-requests", token).json["access_requests"] == []
    assert call(client, "GET", "/access-requests?status=maybe", token).status_code == 400
