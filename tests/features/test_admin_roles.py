"""Custom roles and per-user permission overrides."""

from __future__ import annotations

from bananawiki.wiki import permissions as perms


def category(db, name):
    return db.insert("categories", {"name": name})


def grants_of(app, user_id):
    from bananawiki.wiki.db import connection_scope

    with app.app_context(), connection_scope() as session:
        user = session.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return perms.load_grants(session, user)


def create_role(client, **data):
    payload = {"name": "Reviewers", "description": "", "base_role": "editor",
               "permissions": ["page.view_all", "page.edit_all"]}
    payload.update(data)
    return client.post("/admin/roles/create", data=payload)


# Custom roles


def test_create_role_sanitizes_permissions(admin_client, db):
    response = create_role(admin_client, base_role="user",
                           permissions=["page.edit_all", "category.create", "page.view_deindexed", "custom_page.manage",
                                        "page.delete", "bogus"])
    assert response.status_code == 302
    role_id = db.scalar("SELECT id FROM custom_roles")
    keys = set(db.column("SELECT permission_key FROM custom_role_permissions WHERE role_id = ?", (role_id,)))
    # Editing content is open to members; editor-only (categories) and admin-only keys are dropped, implied
    # keys added, the 1.4 name page.delete read as page.delete_all.
    assert keys == {"page.edit_all", "page.view_deindexed", "page.view_all", "page.delete_all"}


def test_role_validation(admin_client, db):
    assert create_role(admin_client, name="").status_code == 400
    assert create_role(admin_client, name="x" * 101).status_code == 400
    assert create_role(admin_client, base_role="admin").status_code == 400
    assert create_role(admin_client).status_code == 302
    assert create_role(admin_client, name="reviewers").status_code == 400  # case-insensitive duplicate
    assert db.scalar("SELECT COUNT(*) FROM custom_roles") == 1


def test_write_restriction_implies_read(admin_client, db):
    docs = category(db, "Docs")
    create_role(admin_client, write_restricted="1", write_category_ids=[str(docs), "99999"])
    role = db.one("SELECT * FROM custom_roles")
    assert role["write_restricted"] == 1 and role["read_restricted"] == 1
    rows = db.all("SELECT category_id, access_type FROM custom_role_categories ORDER BY access_type")
    assert rows == [{"category_id": docs, "access_type": "read"}, {"category_id": docs, "access_type": "write"}]


def test_assign_and_unassign_role(app, admin_client, make_user, db):
    create_role(admin_client)
    role_id = db.scalar("SELECT id FROM custom_roles")
    bob = make_user("bob")
    admin_client.post(f"/admin/roles/{role_id}/assign", data={"user_id": bob["id"]})
    row = db.one("SELECT role, custom_role_id FROM users WHERE id = ?", (bob["id"],))
    assert row == {"role": "editor", "custom_role_id": role_id}
    assert "page.edit_all" in grants_of(app, bob["id"]).keys
    admin_client.post(f"/admin/roles/{role_id}/unassign", data={"user_id": bob["id"]})
    assert db.scalar("SELECT custom_role_id FROM users WHERE id = ?", (bob["id"],)) is None


def test_admins_cannot_hold_custom_roles(admin_client, make_user, db):
    create_role(admin_client)
    role_id = db.scalar("SELECT id FROM custom_roles")
    peer = make_user("other_admin", role="admin")
    admin_client.post(f"/admin/roles/{role_id}/assign", data={"user_id": peer["id"]})
    admin_client.post(f"/admin/users/{peer['id']}/assign-role", data={"identity": f"custom:{role_id}"})
    assert db.scalar("SELECT custom_role_id FROM users WHERE id = ?", (peer["id"],)) is None


def test_base_role_change_moves_holders(admin_client, make_user, db):
    create_role(admin_client)
    role_id = db.scalar("SELECT id FROM custom_roles")
    bob = make_user("bob")
    admin_client.post(f"/admin/users/{bob['id']}/assign-role", data={"identity": f"custom:{role_id}"})
    admin_client.post(f"/admin/roles/{role_id}", data={"name": "Reviewers", "base_role": "user",
                                                        "permissions": ["page.view_all"]})
    assert db.scalar("SELECT role FROM users WHERE id = ?", (bob["id"],)) == "user"
    assert db.scalar("SELECT COUNT(*) FROM role_history WHERE user_id = ?", (bob["id"],)) == 2


def test_delete_role_moves_holders_to_most_similar(admin_client, make_user, db):
    create_role(admin_client, name="Writers", permissions=["page.view_all", "page.edit_all", "page.create"])
    create_role(admin_client, name="Readers", base_role="user", permissions=["page.view_all"])
    create_role(admin_client, name="Authors", permissions=["page.view_all", "page.edit_all", "page.create",
                                                            "page.delete"])
    ids = {r["name"]: r["id"] for r in db.all("SELECT id, name FROM custom_roles")}
    bob = make_user("bob")
    admin_client.post(f"/admin/roles/{ids['Writers']}/assign", data={"user_id": bob["id"]})
    admin_client.post(f"/admin/roles/{ids['Writers']}/delete", data={"replacement_role_id": "auto"})
    assert db.scalar("SELECT custom_role_id FROM users WHERE id = ?", (bob["id"],)) == ids["Authors"]


def test_delete_role_with_explicit_or_no_replacement(admin_client, make_user, db):
    create_role(admin_client, name="One")
    create_role(admin_client, name="Two", base_role="user", permissions=["page.view_all"])
    ids = {r["name"]: r["id"] for r in db.all("SELECT id, name FROM custom_roles")}
    bob, carol = make_user("bob"), make_user("carol")
    admin_client.post(f"/admin/roles/{ids['One']}/assign", data={"user_id": bob["id"]})
    admin_client.post(f"/admin/roles/{ids['Two']}/assign", data={"user_id": carol["id"]})
    admin_client.post(f"/admin/roles/{ids['One']}/delete", data={"replacement_role_id": str(ids["Two"])})
    assert db.one("SELECT role, custom_role_id FROM users WHERE id = ?", (bob["id"],)) == {
        "role": "user", "custom_role_id": ids["Two"]}
    admin_client.post(f"/admin/roles/{ids['Two']}/delete", data={"replacement_role_id": "none"})
    assert db.scalar("SELECT COUNT(*) FROM users WHERE custom_role_id IS NOT NULL") == 0
    assert db.scalar("SELECT COUNT(*) FROM custom_roles") == 0


def test_unknown_role_is_404(admin_client):
    assert admin_client.get("/admin/roles/999").status_code == 404
    assert admin_client.post("/admin/roles/999/delete").status_code == 404


# Per-user overrides


def test_user_permission_overrides(app, admin_client, make_user, db):
    docs, secret = category(db, "Docs"), category(db, "Secret")
    bob = make_user("bob", role="editor")
    admin_client.post(f"/admin/users/{bob['id']}/permissions", data={
        "permissions": ["page.edit_all", "custom_page.manage"],
        "read_restricted": "1", "read_category_ids": [str(docs)],
        "write_restricted": "1", "write_category_ids": [str(docs)],
    })
    grants = grants_of(app, bob["id"])
    assert grants.keys == {"page.edit_all", "page.view_all"}
    assert perms.can_read_category(grants, docs) and not perms.can_read_category(grants, secret)
    assert perms.can_write_category(grants, docs) and not perms.can_write_category(grants, secret)
    admin_client.post(f"/admin/users/{bob['id']}/permissions", data={"action": "reset"})
    assert grants_of(app, bob["id"]).keys == perms.defaults("editor")


def test_grants_remember_where_the_permissions_come_from(app, admin_client, admin, make_user, db):
    people = {"admin": admin, "defaults": make_user("plain", role="editor"),
              "custom_role": make_user("roled", role="editor"), "own": make_user("solo")}
    create_role(admin_client, permissions=["canvas.create"])
    role_id = db.scalar("SELECT id FROM custom_roles")
    admin_client.post(f"/admin/roles/{role_id}/assign", data={"user_id": people["custom_role"]["id"]})
    admin_client.post(f"/admin/users/{people['own']['id']}/permissions",
                      data={"permissions": ["canvas.create"]})
    grants = {name: grants_of(app, user["id"]) for name, user in people.items()}
    # A person's own changes apply on top of their base role's defaults.
    assert {name: found.source for name, found in grants.items()} == {
        "admin": "admin", "defaults": "defaults", "custom_role": "custom_role", "own": "defaults"}
    assert {name for name, found in grants.items() if found.personal} == {"own"}
    assert all(perms.grants_permission(found, "canvas.create", lambda feature: True) for found in grants.values())
    assert not perms.grants_permission(grants["custom_role"], "canvas.create", lambda feature: False)


# Base roles


def test_base_role_defaults_are_changed_for_the_whole_wiki(app, admin_client, make_user, db):
    member, editor = make_user("plain"), make_user("editor7", role="editor")
    page = admin_client.get("/admin/roles").get_data(as_text=True)
    assert "/admin/roles/base/user" in page and "/admin/roles/base/editor" in page
    form = admin_client.get("/admin/roles/base/user").get_data(as_text=True)
    assert 'value="page.create" checked' in form and 'value="category.create"' not in form  # editor-only
    keys = sorted(perms.builtin_defaults("user") - {"page.create", "kanban.create"} | {"page.export_pdf"})
    assert admin_client.post("/admin/roles/base/user", data={"permissions": keys}).status_code == 302
    rows = {row["permission_key"]: row["granted"] for row in db.all("SELECT * FROM base_role_permissions")}
    assert rows == {"page.create": 0, "kanban.create": 0, "page.export_pdf": 1}  # only the differences
    found = grants_of(app, member["id"])
    assert "page.create" not in found.keys and "page.export_pdf" in found.keys and found.source == "defaults"
    assert grants_of(app, editor["id"]).keys == perms.builtin_defaults("editor")  # the other role is untouched
    admin_client.post("/admin/roles/base/user", data={"action": "reset"})
    assert db.scalar("SELECT COUNT(*) FROM base_role_permissions") == 0
    assert grants_of(app, member["id"]).keys == perms.builtin_defaults("user")
    assert admin_client.get("/admin/roles/base/admin").status_code == 404


def test_only_administrators_change_base_roles(client, make_user, login, db):
    login(client, make_user("mallory", role="editor"))
    assert client.post("/admin/roles/base/user", data={"permissions": ["page.edit_all"]}).status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM base_role_permissions") == 0


def test_overrides_refused_for_admins_and_custom_roles(admin_client, make_user, db):
    peer = make_user("other_admin", role="admin")
    admin_client.post(f"/admin/users/{peer['id']}/permissions", data={"permissions": ["page.view_all"]})
    assert db.scalar("SELECT COUNT(*) FROM user_category_access") == 0
    assert b"Only an owner or a superuser" in admin_client.get(f"/admin/users/{peer['id']}/permissions").data


def test_write_limits_apply_to_members_too(app, admin_client, make_user, db):
    docs, other = category(db, "Docs"), category(db, "Other")
    bob = make_user("bob")
    admin_client.post(f"/admin/users/{bob['id']}/permissions", data={
        "permissions": sorted(perms.builtin_defaults("user")),
        "write_restricted": "1", "write_category_ids": [str(docs)]})
    grants = grants_of(app, bob["id"])
    assert perms.can_write_category(grants, docs) and not perms.can_write_category(grants, other)
    assert perms.can_read_category(grants, other)  # reading stays unlimited


def test_unrestricted_writing_stays_within_what_one_reads(app, admin_client, make_user, db):
    docs, other = category(db, "Docs"), category(db, "Other")
    bob = make_user("bob", role="editor")
    admin_client.post(f"/admin/users/{bob['id']}/permissions", data={
        "permissions": sorted(perms.builtin_defaults("editor")), "read_restricted": "1",
        "read_category_ids": [str(docs)]})
    grants = grants_of(app, bob["id"])
    assert perms.can_write_category(grants, docs) and not perms.can_write_category(grants, other)
    assert not perms.can_read_category(grants, other)


def test_there_is_no_editor_access_page(admin_client, make_user):
    bob = make_user("bob", role="editor")
    assert admin_client.get(f"/admin/users/{bob['id']}/editor-access").status_code == 404


def test_non_admin_cannot_edit_permissions(client, make_user, login, db):
    bob = make_user("bob", role="editor")
    login(client, make_user("mallory", role="editor"))
    assert client.post(f"/admin/users/{bob['id']}/permissions",
                       data={"permissions": ["page.delete"]}).status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM user_permissions") == 0
