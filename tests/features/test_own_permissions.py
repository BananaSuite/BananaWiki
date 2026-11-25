"""A person's own permissions are changes to what their role gives, and the summary of what they can do."""

from __future__ import annotations

from bananawiki.wiki import permissions as perms

from .access_support import role_default


def category(db, name):
    return db.insert("categories", {"name": name})


def grants_of(app, user_id):
    from bananawiki.wiki.db import connection_scope

    with app.app_context(), connection_scope() as session:
        user = session.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return perms.load_grants(session, user)


def save_permissions(client, user, keys, **form):
    return client.post(f"/admin/users/{user['id']}/permissions", data={"permissions": sorted(keys), **form})


def test_saving_stores_only_the_differences_and_the_rest_follows_the_defaults(app, admin_client, make_user, db):
    bob = make_user("bob")
    keys = (perms.builtin_defaults("user") | {"page.edit_all"}) - {"chat.upload"}
    assert save_permissions(admin_client, bob, keys).status_code == 302
    stored = {row["permission_key"]: row["granted"] for row in db.all(
        "SELECT permission_key, granted FROM user_permissions WHERE user_id = ?", (bob["id"],))}
    assert stored == {"page.edit_all": 1, "chat.upload": 0}
    assert db.scalar("SELECT COUNT(*) FROM user_category_access WHERE user_id = ?", (bob["id"],)) == 0
    # The wiki changes the member defaults later: Bob follows them, except for his own changes.
    role_default(db, "user", "page.export_pdf", True)
    role_default(db, "user", "page.edit_all", False)
    found = grants_of(app, bob["id"])
    assert "page.export_pdf" in found.keys and "page.edit_all" in found.keys and "chat.upload" not in found.keys
    assert found.personal and found.source == "defaults"


def test_saving_the_defaults_unchanged_keeps_following_them(app, admin_client, make_user, db):
    bob = make_user("bob")
    save_permissions(admin_client, bob, perms.builtin_defaults("user"))
    assert db.scalar("SELECT COUNT(*) FROM user_permissions WHERE user_id = ?", (bob["id"],)) == 0
    assert not grants_of(app, bob["id"]).personal
    page = admin_client.get("/admin/roles").get_data(as_text=True)
    assert "People using these defaults" in page


def test_custom_role_holders_get_own_changes_on_top_of_their_role(app, admin_client, make_user, db):
    docs, team, extra = category(db, "Docs"), category(db, "Team"), category(db, "Extra")
    admin_client.post("/admin/roles/create", data={
        "name": "Writers", "description": "", "base_role": "user", "permissions": ["page.view_all", "page.create"],
        "write_restricted": "1", "write_category_ids": [str(docs)]})
    role_id = db.scalar("SELECT id FROM custom_roles WHERE name = 'Writers'")
    carol = make_user("carol")
    admin_client.post(f"/admin/roles/{role_id}/assign", data={"user_id": carol["id"]})
    page = admin_client.get(f"/admin/users/{carol['id']}/permissions").get_data(as_text=True)
    assert "has the custom role Writers" in page and "Their role allows: Docs." in page
    response = save_permissions(admin_client, carol, {"page.view_all", "page.create", "page.edit_all"},
                                write_restricted="1", write_category_ids=[str(extra)])
    assert response.status_code == 302
    found = grants_of(app, carol["id"])
    assert found.source == "custom_role" and "page.edit_all" in found.keys
    assert [perms.can_write_category(found, c) for c in (docs, team, extra)] == [True, False, True]
    assert [perms.can_read_category(found, c) for c in (docs, team, extra)] == [True, False, True]
    # The role changes: Carol keeps her own addition.
    admin_client.post(f"/admin/roles/{role_id}", data={
        "name": "Writers", "description": "", "base_role": "user", "permissions": ["page.view_all"],
        "write_restricted": "1", "write_category_ids": [str(team)]})
    found = grants_of(app, carol["id"])
    assert "page.edit_all" in found.keys and "page.create" not in found.keys
    assert [perms.can_write_category(found, c) for c in (docs, team, extra)] == [False, True, True]


def test_combining_category_limits():
    def access(restricted, ids=()):
        return perms.CategoryAccess(restricted, frozenset(ids))

    # No role limit: the person's own settings, as stored.
    read, write = perms.combine_access(access(False), access(False), {"write": True}, [], [2])
    assert (read, write) == (access(False, {2}), access(True, {2}))
    # The role limits writing to 1 (and so reading): the person's list adds 3.
    read, write = perms.combine_access(access(True, {1}), access(True, {1}), {"write": True}, [], [3])
    assert (read, write) == (access(True, {1, 3}), access(True, {1, 3}))
    # A role limit with no own list of that kind: the role's alone.
    read, write = perms.combine_access(access(True, {1}), access(False), {"write": True}, [5], [4])
    assert (read, write) == (access(True, {1, 4}), access(True, {4}))


def test_reset_removes_own_changes_only(app, admin_client, make_user, db):
    bob = make_user("bob", role="editor")
    docs = category(db, "Docs")
    save_permissions(admin_client, bob, perms.builtin_defaults("editor") - {"page.edit_all"},
                     read_restricted="1", read_category_ids=[str(docs)])
    assert grants_of(app, bob["id"]).personal
    admin_client.post(f"/admin/users/{bob['id']}/permissions", data={"action": "reset"})
    found = grants_of(app, bob["id"])
    assert not found.personal and found.keys == perms.builtin_defaults("editor")
    entry = db.one("SELECT details FROM audit_log WHERE action = 'user.permissions_changed'")
    assert "-page.edit_all" in entry["details"]


def test_summary_says_what_a_person_can_do(app, admin_client, make_user, db):
    docs, team = category(db, "Docs"), category(db, "Team")
    dana = make_user("dana")
    save_permissions(admin_client, dana, (perms.builtin_defaults("user") | {"page.edit_all"}) - {"chat.upload"},
                     write_restricted="1", write_category_ids=[str(team)])
    page = admin_client.get(f"/admin/users/{dana['id']}").get_data(as_text=True)
    assert "What dana can do" in page
    assert "User role defaults" in page and "with their own changes" in page
    assert "Every category" in page and "Only Team" in page
    assert "Edit all pages" in page and "Added for them" in page and "Taken away" in page
    assert docs
    boss = make_user("boss", role="admin")
    assert "Administrators can do everything" in admin_client.get(f"/admin/users/{boss['id']}").get_data(as_text=True)


def test_summary_of_someone_who_reads_nothing(app, admin_client, make_user, db):
    eve = make_user("eve")
    save_permissions(admin_client, eve, set())
    page = admin_client.get(f"/admin/users/{eve['id']}").get_data(as_text=True)
    assert "No pages" in page and "Nothing" in page
