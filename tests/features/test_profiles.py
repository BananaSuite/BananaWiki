"""Custom profile fields, custom tags and profile publication (user_profiles feature)."""

from __future__ import annotations

from .people_support import publish, set_feature


def _field(admin_client, db, label: str, field_type: str = "text") -> int:
    admin_client.post("/admin/profile-fields", data={"action": "create", "label": label, "field_type": field_type})
    return db.scalar("SELECT id FROM user_profile_fields__definitions WHERE label = ?", (label,))


def test_tables_exist_on_new_databases(db):
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__definitions") == 0


def test_admin_manages_field_definitions(admin_client, db):
    first = _field(admin_client, db, "Favourite colour")
    second = _field(admin_client, db, "Website", "url")
    assert db.scalar("SELECT key FROM user_profile_fields__definitions WHERE id = ?", (first,)) == "favourite_colour"
    admin_client.post("/admin/profile-fields", data={"action": "move_up", "field_id": second})
    order = db.column("SELECT id FROM user_profile_fields__definitions ORDER BY sort_order")
    assert order == [second, first]
    admin_client.post("/admin/profile-fields", data={"action": "update", "field_id": first, "label": "Colour",
                                                     "field_type": "longtext"})
    assert db.scalar("SELECT field_type FROM user_profile_fields__definitions WHERE id = ?", (first,)) == "longtext"
    admin_client.post("/admin/profile-fields", data={"action": "delete", "field_id": first})
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__definitions") == 1
    assert admin_client.post("/admin/profile-fields", data={"action": "delete", "field_id": 999}).status_code == 404


def test_field_definition_validation(admin_client, db):
    admin_client.post("/admin/profile-fields", data={"action": "create", "label": " ", "field_type": "text"})
    admin_client.post("/admin/profile-fields", data={"action": "create", "label": "X", "field_type": "script"})
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__definitions") == 0


def test_field_admin_is_admin_only(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/admin/profile-fields").status_code == 403


def test_member_fills_in_fields_with_visibility(app, admin_client, make_user, login, db):
    site = _field(admin_client, db, "Website", "url")
    motto = _field(admin_client, db, "Motto")
    bob = make_user("bob")
    publish(db, bob)
    client = app.test_client()
    login(client, bob)
    client.post("/settings/profile-fields", data={f"value_{site}": "https://example.org", f"visible_{site}": "1",
                                                  f"value_{motto}": "Keep calm"})
    assert b"Keep calm" in client.get("/users/bob").data
    viewer = app.test_client()
    login(viewer, make_user("eve"))
    html = viewer.get("/users/bob").get_data(as_text=True)
    assert 'href="https://example.org"' in html and "Keep calm" not in html


def test_url_fields_link_only_to_http_even_if_the_type_changed_later(app, admin_client, make_user, login, db):
    """A value saved in a text field stays text when an administrator turns the field into a URL field."""
    field = _field(admin_client, db, "Homepage")
    bob = make_user("bob")
    publish(db, bob)
    client = app.test_client()
    login(client, bob)
    client.post("/settings/profile-fields", data={f"value_{field}": "javascript:alert(document.cookie)",
                                                  f"visible_{field}": "1"})
    admin_client.post("/admin/profile-fields", data={"action": "update", "field_id": field, "label": "Homepage",
                                                     "field_type": "url"})
    assert db.scalar("SELECT field_type FROM user_profile_fields__definitions WHERE id = ?", (field,)) == "url"
    html = client.get("/users/bob").get_data(as_text=True)
    assert "javascript:alert(document.cookie)" in html
    assert 'href="javascript:' not in html


def test_field_values_are_validated(app, admin_client, make_user, login, db):
    site = _field(admin_client, db, "Website", "url")
    born = _field(admin_client, db, "Anniversary", "date")
    bob = make_user("bob")
    client = app.test_client()
    login(client, bob)
    client.post("/settings/profile-fields", data={f"value_{site}": "javascript:alert(1)"})
    client.post("/settings/profile-fields", data={f"value_{born}": "not a date"})
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__values") == 0


def test_field_values_go_with_the_account(app, admin_client, make_user, login, db):
    motto = _field(admin_client, db, "Motto")
    bob = make_user("bob")
    client = app.test_client()
    login(client, bob)
    client.post("/settings/profile-fields", data={f"value_{motto}": "Hi"})
    db.execute("DELETE FROM users WHERE id = ?", (bob["id"],))
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__values") == 0


def test_publish_and_hide_own_profile(client, make_user, login, db):
    bob = make_user("bob")
    login(client, bob)
    client.post("/settings/profile/publish", data={"published": "1"})
    assert db.scalar("SELECT page_published FROM user_profiles WHERE user_id = ?", (bob["id"],)) == 1
    client.post("/settings/profile/publish", data={"published": "0"})
    assert db.scalar("SELECT page_published FROM user_profiles WHERE user_id = ?", (bob["id"],)) == 0


def test_admin_disable_blocks_publishing(app, admin_client, make_user, login, db):
    bob = make_user("bob")
    publish(db, bob)
    admin_client.post(f"/admin/users/{bob['id']}/profile", data={"action": "disable_profile"})
    row = db.one("SELECT * FROM user_profiles WHERE user_id = ?", (bob["id"],))
    assert row["page_disabled_by_admin"] == 1 and row["page_published"] == 0
    client = app.test_client()
    login(client, bob)
    client.post("/settings/profile/publish", data={"published": "1"})
    assert db.scalar("SELECT page_published FROM user_profiles WHERE user_id = ?", (bob["id"],)) == 0
    admin_client.post(f"/admin/users/{bob['id']}/profile", data={"action": "enable_profile"})
    assert db.scalar("SELECT page_disabled_by_admin FROM user_profiles WHERE user_id = ?", (bob["id"],)) == 0


def test_admin_edits_profile_basics(admin_client, make_user, db):
    bob = make_user("bob")
    assert admin_client.get(f"/admin/users/{bob['id']}/profile").status_code == 200
    admin_client.post(f"/admin/users/{bob['id']}/profile", data={"action": "edit_profile", "real_name": "Robert",
                                                                 "bio": "", "birth_date": ""})
    assert db.scalar("SELECT real_name FROM user_profiles WHERE user_id = ?", (bob["id"],)) == "Robert"
    admin_client.post(f"/admin/users/{bob['id']}/profile", data={"action": "delete_profile"})
    assert db.scalar("SELECT 1 FROM user_profiles WHERE user_id = ?", (bob["id"],)) is None


def test_owner_profiles_are_protected_from_other_admins(admin_client, make_user, db):
    owner = make_user("the_owner", role="owner")
    assert admin_client.get(f"/admin/users/{owner['id']}/profile").status_code == 403
    admin_client.post(f"/admin/users/{owner['id']}/tags", data={"action": "add_tag", "tag_label": "x",
                                                                "tag_color": "#112233"})
    assert db.scalar("SELECT COUNT(*) FROM user_custom_tags") == 0


def test_moderation_is_admin_only(client, make_user, login, db):
    bob = make_user("bob")
    login(client, make_user("eve"))
    assert client.post(f"/admin/users/{bob['id']}/profile", data={"action": "delete_profile"}).status_code == 403
    assert client.post(f"/admin/users/{bob['id']}/tags", data={"action": "add_tag"}).status_code == 403


def test_custom_tags(app, admin_client, make_user, login, db):
    bob = make_user("bob")
    url = f"/admin/users/{bob['id']}/tags"
    admin_client.post(url, data={"action": "add_tag", "tag_label": "Mentor", "tag_color": "#AABBCC"})
    admin_client.post(url, data={"action": "add_tag", "tag_label": "Bad", "tag_color": "red;}"})
    admin_client.post(url, data={"action": "add_tag", "tag_label": "Second", "tag_color": "#000000"})
    tags = db.all("SELECT id, label, color FROM user_custom_tags WHERE user_id = ? ORDER BY sort_order", (bob["id"],))
    assert [t["label"] for t in tags] == ["Mentor", "Second"] and tags[0]["color"] == "#aabbcc"
    admin_client.post(url, data={"action": "move_down", "tag_id": tags[0]["id"]})
    assert db.column("SELECT label FROM user_custom_tags ORDER BY sort_order") == ["Second", "Mentor"]
    other = make_user("other")
    admin_client.post(f"/admin/users/{other['id']}/tags", data={"action": "delete_tag", "tag_id": tags[0]["id"]})
    assert db.scalar("SELECT COUNT(*) FROM user_custom_tags") == 2
    publish(db, bob)
    viewer = app.test_client()
    login(viewer, make_user("eve"))
    assert b"Mentor" not in viewer.get("/users/bob").data
    assert b"Mentor" in admin_client.get("/users/bob").data


def test_feature_off_hides_routes(app, client, make_user, login):
    set_feature(app, "user_profiles", False)
    login(client, make_user("bob"))
    assert client.get("/settings/profile-fields").status_code == 404
