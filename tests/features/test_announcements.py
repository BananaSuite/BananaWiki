"""Announcements: administration, the banner, audiences, expiry and the public page."""

from __future__ import annotations

import pytest

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.announcements import service


def _form(**overrides):
    data = {"content": "**Maintenance** tonight", "color": "blue", "text_size": "normal",
            "visibility": "both", "audience_mode": "all", "not_removable": "1", "show_countdown": "1"}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _insert(db, admin, **values):
    row = {"content": "Hello banner", "color": "orange", "text_size": "normal", "visibility": "both",
           "created_by": admin["id"], "created_at": sql_in(seconds=-10)}
    row.update(values)
    return db.insert("announcements", row)


def test_admin_crud(admin_client, admin, db):
    assert admin_client.get("/admin/announcements").status_code == 200
    response = admin_client.post("/admin/announcements/create", data=_form(expires_at="2099-01-01T10:00"))
    assert response.status_code == 302
    row = db.one("SELECT * FROM announcements")
    assert row["color"] == "blue" and row["created_by"] == admin["id"] and row["expires_at"] == "2099-01-01 10:00:00"
    assert row["revision"] == 1 and row["is_active"] == 1
    assert b"Maintenance" in admin_client.get(f"/admin/announcements/{row['id']}/edit").data
    response = admin_client.post(f"/admin/announcements/{row['id']}/edit",
                                 data=_form(content="Changed", color="red", not_removable=None))
    assert response.status_code == 302
    row = db.one("SELECT * FROM announcements")
    assert (row["content"], row["color"], row["revision"], row["is_active"], row["not_removable"]) == \
        ("Changed", "red", 2, 0, 0)
    assert admin_client.post(f"/admin/announcements/{row['id']}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM announcements") == 0
    assert admin_client.post("/admin/announcements/999/delete").status_code == 404


@pytest.mark.parametrize("overrides, message", [
    ({"content": ""}, b"Write the announcement."),
    ({"content": "x" * 2001}, b"at most 2000"),
    ({"color": "purple"}, b"valid colour"),
    ({"visibility": "robots"}, b"Choose who sees"),
    ({"custom_colors": "1", "custom_background": "red", "custom_text_color": "#ffffff"}, b"#1a2b3c"),
    ({"audience_mode": "allowlist"}, b"at least one person"),
    ({"audience_mode": "denylist", "audience_users": "nobody_here"}, b"nobody_here"),
    ({"expires_at": "tomorrow"}, b"expiry date is not valid"),
])
def test_validation(admin_client, db, overrides, message):
    response = admin_client.post("/admin/announcements/create", data=_form(**overrides))
    assert response.status_code == 400 and message in response.data
    assert db.scalar("SELECT COUNT(*) FROM announcements") == 0


def test_non_admins_cannot_manage(client, login, make_user, admin, db):
    ann = _insert(db, admin)
    login(client, make_user("editor9", role="editor"))
    assert client.get("/admin/announcements").status_code == 403
    assert client.post("/admin/announcements/create", data=_form()).status_code == 403
    assert client.post(f"/admin/announcements/{ann}/edit", data=_form()).status_code == 403
    assert client.post(f"/admin/announcements/{ann}/delete").status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM announcements") == 1


def test_bar_on_every_page_with_markdown_and_no_inline_script(client, admin, db):
    _insert(db, admin, content="**Bold** <script>alert(1)</script>", not_removable=0)
    page = client.get("/login")
    assert b'id="announcements-bar"' in page.data and b"<strong>Bold</strong>" in page.data
    assert b"<script>alert(1)" not in page.data
    assert b"data-announcement-dismiss" in page.data
    assert b"/static/announcements/announcements.js" in page.data


def test_not_removable_has_no_close_button(client, admin, db):
    _insert(db, admin, not_removable=1)
    assert b"data-announcement-dismiss" not in client.get("/login").data


def test_visibility_and_expiry(client, login, make_user, admin, db):
    _insert(db, admin, content="for-members", visibility="logged_in")
    _insert(db, admin, content="for-guests", visibility="logged_out")
    _insert(db, admin, content="long-gone", expires_at=sql_in(hours=-1))
    _insert(db, admin, content="broken-expiry", expires_at="not-a-date")
    _insert(db, admin, content="switched-off", is_active=0)
    anonymous = client.get("/login").data
    assert b"for-guests" in anonymous and b"for-members" not in anonymous
    login(client, make_user("member"))
    member = client.get("/admin/announcements", follow_redirects=True).data
    assert b"for-members" in member and b"for-guests" not in member
    for gone in (b"long-gone", b"broken-expiry", b"switched-off"):
        assert gone not in member


def test_audience_allowlist_and_denylist(app, make_user, admin, db):
    alice, bob = make_user("alice"), make_user("bob")
    allow = _insert(db, admin, audience_mode="allowlist")
    deny = _insert(db, admin, audience_mode="denylist")
    db.execute("INSERT INTO announcement_audience_users VALUES (?, ?)", (allow, alice["id"]))
    db.execute("INSERT INTO announcement_audience_users VALUES (?, ?)", (deny, alice["id"]))
    with app.test_request_context(), connection_scope():
        assert {r["id"] for r in service.visible_for(alice)} == {allow}
        assert {r["id"] for r in service.visible_for(bob)} == {deny}
        assert {r["id"] for r in service.visible_for(None)} == {deny}


def test_audience_saved_from_usernames(admin_client, make_user, db):
    alice = make_user("alice")
    admin_client.post("/admin/announcements/create", data=_form(audience_mode="allowlist", audience_users="ALICE, alice"))
    assert db.column("SELECT user_id FROM announcement_audience_users") == [alice["id"]]


def test_custom_colours_rendered_only_when_valid(client, admin, db):
    _insert(db, admin, custom_background="#112233", custom_text_color="#fefefe")
    _insert(db, admin, content="evil", custom_background="red;position:fixed", custom_text_color="#ffffff")
    page = client.get("/login").data
    assert b'style="background:#112233;color:#fefefe"' in page
    assert b"position:fixed" not in page


def test_public_page_respects_visibility_and_public_mode(client, login, make_user, admin, db):
    members = _insert(db, admin, content="members only", visibility="logged_in")
    everyone = _insert(db, admin, content="for all")
    assert client.get(f"/announcements/{everyone}").status_code == 302  # private wiki: sign in first
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    assert client.get(f"/announcements/{everyone}").status_code == 200
    assert client.get(f"/announcements/{members}").status_code == 404
    login(client, make_user("member"))
    assert client.get(f"/announcements/{members}").status_code == 200
    db.execute("UPDATE announcements SET is_active = 0 WHERE id = ?", (members,))
    assert client.get(f"/announcements/{members}").status_code == 404


def test_long_content_links_to_full_page(client, login, make_user, admin, db):
    _insert(db, admin, content="word " * 150 + "TAILMARK")
    assert b"TAILMARK" in client.get("/login").data  # anonymous in a private wiki: full text in the bar
    login(client, make_user("member"))
    page = client.get("/admin/announcements", follow_redirects=True).data
    assert b"TAILMARK" not in page and b"Read more" in page


def test_prune_removes_long_expired_only(app, admin, db):
    old = _insert(db, admin, expires_at=sql_in(days=-40))
    recent = _insert(db, admin, expires_at=sql_in(days=-2))
    forever = _insert(db, admin)
    with app.test_request_context(), connection_scope():
        assert service.prune_expired() == 1
    assert set(db.column("SELECT id FROM announcements")) == {recent, forever}
    assert old not in db.column("SELECT id FROM announcements")


def test_disabled_feature_hides_bar_and_routes(app, admin_client, admin, db):
    from bananawiki.wiki import registry

    _insert(db, admin, content="should-vanish")
    with app.test_request_context(), connection_scope():
        registry.set_enabled("announcements", False)
    assert admin_client.get("/admin/announcements").status_code == 404
    assert b"should-vanish" not in admin_client.get("/login", follow_redirects=True).data
