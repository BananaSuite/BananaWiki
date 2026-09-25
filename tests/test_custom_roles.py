"""Tests for the custom role system: CRUD, assignment, similarity, plugin awareness."""
import pytest
import db
from helpers._permissions import (
    get_default_permissions,
    get_all_permission_keys,
    PERMISSION_PLUGIN_MAP,
)


@pytest.fixture
def cr_admin(isolated_db):
    """Create an admin user and return (user_id, user_dict)."""
    from werkzeug.security import generate_password_hash
    uid = db.create_user("cr_admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid, db.get_user_by_id(uid)


@pytest.fixture
def cr_editor(isolated_db):
    """Create an editor user and return (user_id, user_dict)."""
    from werkzeug.security import generate_password_hash
    uid = db.create_user("cr_editor", generate_password_hash("editor123"), role="editor")
    defaults = get_default_permissions("editor")
    db.set_user_permissions(uid, defaults)
    return uid, db.get_user_by_id(uid)


@pytest.fixture
def cr_user(isolated_db):
    """Create a regular user and return (user_id, user_dict)."""
    from werkzeug.security import generate_password_hash
    uid = db.create_user("cr_user", generate_password_hash("user123"), role="user")
    defaults = get_default_permissions("user")
    db.set_user_permissions(uid, defaults)
    return uid, db.get_user_by_id(uid)


@pytest.fixture
def sample_category(isolated_db):
    """Create a category for testing category access."""
    return db.create_category("Test Category")


class TestCustomRoleCRUD:
    def test_create_role_basic(self, isolated_db, cr_admin):
        _, admin = cr_admin
        role_id = db.create_custom_role(
            name="Content Reviewer",
            description="Can review but not edit",
            base_role="user",
            permission_keys={"page.view_all", "history.view"},
            created_by=admin["id"],
        )
        assert role_id is not None
        role = db.get_custom_role(role_id)
        assert role["name"] == "Content Reviewer"
        assert role["description"] == "Can review but not edit"
        assert role["base_role"] == "user"
        assert "page.view_all" in role["permission_keys"]
        assert role["user_count"] == 0

    def test_create_role_editor_base(self, isolated_db):
        role_id = db.create_custom_role(
            name="Full Editor",
            base_role="editor",
            permission_keys=get_default_permissions("editor"),
        )
        role = db.get_custom_role(role_id)
        assert role["base_role"] == "editor"
        assert "page.create" in role["permission_keys"]

    def test_create_role_with_category_access(self, isolated_db, sample_category):
        role_id = db.create_custom_role(
            name="Restricted Reader",
            base_role="user",
            permission_keys={"page.view_all"},
            read_restricted=True,
            read_category_ids=[sample_category],
        )
        role = db.get_custom_role(role_id)
        assert role["read_restricted"] is True
        assert sample_category in role["read_category_ids"]

    def test_create_role_editor_with_write_access(self, isolated_db, sample_category):
        role_id = db.create_custom_role(
            name="Category Editor",
            base_role="editor",
            permission_keys=get_default_permissions("editor"),
            write_restricted=True,
            write_category_ids=[sample_category],
        )
        role = db.get_custom_role(role_id)
        assert role["write_restricted"] is True
        assert sample_category in role["write_category_ids"]

    def test_create_role_user_ignores_write_restriction(self, isolated_db, sample_category):
        """User-based roles cannot have write restrictions."""
        role_id = db.create_custom_role(
            name="User Role",
            base_role="user",
            write_restricted=True,
            write_category_ids=[sample_category],
        )
        role = db.get_custom_role(role_id)
        assert role["write_restricted"] is False
        assert role["write_category_ids"] == []

    def test_create_role_sanitizes_permissions(self, isolated_db):
        """User-based roles should not get editor-only permissions."""
        role_id = db.create_custom_role(
            name="Limited User",
            base_role="user",
            permission_keys={"page.view_all", "page.create"},
        )
        role = db.get_custom_role(role_id)
        assert "page.view_all" in role["permission_keys"]
        assert "page.create" not in role["permission_keys"]

    def test_create_duplicate_name_fails(self, isolated_db):
        db.create_custom_role(name="UniqueRole")
        with pytest.raises(db.IntegrityError):
            db.create_custom_role(name="UniqueRole")

    def test_list_custom_roles(self, isolated_db):
        db.create_custom_role(name="Role A")
        db.create_custom_role(name="Role B")
        roles = db.list_custom_roles()
        assert len(roles) >= 2
        names = {r["name"] for r in roles}
        assert "Role A" in names
        assert "Role B" in names

    def test_update_role_name(self, isolated_db):
        role_id = db.create_custom_role(name="Old Name")
        db.update_custom_role(role_id, name="New Name")
        role = db.get_custom_role(role_id)
        assert role["name"] == "New Name"

    def test_update_role_permissions(self, isolated_db):
        role_id = db.create_custom_role(
            name="Updatable",
            base_role="user",
            permission_keys={"page.view_all"},
        )
        db.update_custom_role(role_id, permission_keys={"page.view_all", "search.pages"})
        role = db.get_custom_role(role_id)
        assert "search.pages" in role["permission_keys"]

    def test_update_role_base_role(self, isolated_db, cr_user):
        """Changing base_role updates assigned users' role field."""
        uid, user = cr_user
        role_id = db.create_custom_role(name="Promoted", base_role="user")
        db.assign_custom_role(uid, role_id)
        db.update_custom_role(role_id, base_role="editor",
                              permission_keys=get_default_permissions("editor"))
        user = db.get_user_by_id(uid)
        assert user["role"] == "editor"

    def test_update_nonexistent_role_raises(self, isolated_db):
        with pytest.raises(ValueError):
            db.update_custom_role(99999, name="Nope")

    def test_delete_role_no_users(self, isolated_db):
        role_id = db.create_custom_role(name="Deletable")
        db.delete_custom_role(role_id)
        assert db.get_custom_role(role_id) is None

    def test_delete_nonexistent_role_raises(self, isolated_db):
        with pytest.raises(ValueError):
            db.delete_custom_role(99999)

    def test_get_nonexistent_role_returns_none(self, isolated_db):
        assert db.get_custom_role(99999) is None


class TestCustomRoleAssignment:
    def test_assign_role_to_user(self, isolated_db, cr_user):
        uid, user = cr_user
        role_id = db.create_custom_role(
            name="Assigned Role",
            base_role="user",
            permission_keys={"page.view_all", "search.pages"},
        )
        db.assign_custom_role(uid, role_id)
        user = db.get_user_by_id(uid)
        assert user["custom_role_id"] == role_id

    def test_assign_role_changes_base_role(self, isolated_db, cr_user):
        uid, user = cr_user
        role_id = db.create_custom_role(
            name="Editor Role",
            base_role="editor",
            permission_keys=get_default_permissions("editor"),
        )
        db.assign_custom_role(uid, role_id)
        user = db.get_user_by_id(uid)
        assert user["role"] == "editor"

    def test_permissions_come_from_role(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(
            name="Custom Perms",
            base_role="user",
            permission_keys={"page.view_all", "search.users"},
        )
        db.assign_custom_role(uid, role_id)
        perms = db.get_user_permissions(uid)
        assert "page.view_all" in perms["enabled_permissions"]
        assert "search.users" in perms["enabled_permissions"]

    def test_has_permission_with_custom_role(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(
            name="Perm Check",
            base_role="user",
            permission_keys={"page.view_all", "search.pages"},
        )
        db.assign_custom_role(uid, role_id)
        user = db.get_user_by_id(uid)
        assert db.has_permission(user, "page.view_all") is True
        assert db.has_permission(user, "search.pages") is True
        assert db.has_permission(user, "page.create") is False

    def test_category_read_access_from_role(self, isolated_db, cr_user, sample_category):
        uid, _ = cr_user
        role_id = db.create_custom_role(
            name="Restricted Read",
            base_role="user",
            permission_keys={"page.view_all"},
            read_restricted=True,
            read_category_ids=[sample_category],
        )
        db.assign_custom_role(uid, role_id)
        user = db.get_user_by_id(uid)
        assert db.has_category_read_access(user, sample_category) is True
        other_cat = db.create_category("Other Category")
        assert db.has_category_read_access(user, other_cat) is False

    def test_unassign_role_sets_defaults(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(name="Temporary Role", base_role="user")
        db.assign_custom_role(uid, role_id)
        db.unassign_custom_role(uid)
        user = db.get_user_by_id(uid)
        assert user["custom_role_id"] is None
        perms = db.get_user_permissions(uid)
        assert len(perms["enabled_permissions"]) > 0

    def test_unassign_with_replacement(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_a = db.create_custom_role(name="Role A", base_role="user")
        role_b = db.create_custom_role(name="Role B", base_role="user")
        db.assign_custom_role(uid, role_a)
        db.unassign_custom_role(uid, replacement_role_id=role_b)
        user = db.get_user_by_id(uid)
        assert user["custom_role_id"] == role_b

    def test_get_users_with_role(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(name="Group Role", base_role="user")
        db.assign_custom_role(uid, role_id)
        users = db.get_users_with_role(role_id)
        assert len(users) == 1
        assert users[0]["id"] == uid

    def test_get_user_custom_role(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(name="My Role", base_role="user")
        db.assign_custom_role(uid, role_id)
        role = db.get_user_custom_role(uid)
        assert role is not None
        assert role["name"] == "My Role"

    def test_get_user_custom_role_none(self, isolated_db, cr_user):
        uid, _ = cr_user
        role = db.get_user_custom_role(uid)
        assert role is None

    def test_assign_nonexistent_role_raises(self, isolated_db, cr_user):
        uid, _ = cr_user
        with pytest.raises(ValueError):
            db.assign_custom_role(uid, 99999)

    def test_assign_nonexistent_user_raises(self, isolated_db):
        role_id = db.create_custom_role(name="Orphan Role", base_role="user")
        with pytest.raises(ValueError):
            db.assign_custom_role("nonexistent", role_id)

    def test_user_count_updates(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(name="Count Role", base_role="user")
        role = db.get_custom_role(role_id)
        assert role["user_count"] == 0
        db.assign_custom_role(uid, role_id)
        role = db.get_custom_role(role_id)
        assert role["user_count"] == 1


class TestCustomRoleDeletion:
    def test_delete_role_with_replacement(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_a = db.create_custom_role(name="Old Role", base_role="user",
                                       permission_keys={"page.view_all"})
        role_b = db.create_custom_role(name="New Role", base_role="user",
                                       permission_keys={"page.view_all", "search.pages"})
        db.assign_custom_role(uid, role_a)
        db.delete_custom_role(role_a, replacement_role_id=role_b)
        user = db.get_user_by_id(uid)
        assert user["custom_role_id"] == role_b

    def test_delete_role_auto_similar(self, isolated_db, cr_user):
        uid, _ = cr_user
        perms = {"page.view_all", "search.pages", "search.users"}
        role_a = db.create_custom_role(name="Role A", base_role="user", permission_keys=perms)
        role_b = db.create_custom_role(name="Role B", base_role="user", permission_keys=perms)
        db.create_custom_role(name="Different", base_role="editor",
                              permission_keys=get_default_permissions("editor"))
        db.assign_custom_role(uid, role_a)
        db.delete_custom_role(role_a)
        user = db.get_user_by_id(uid)
        assert user["custom_role_id"] == role_b

    def test_delete_last_role_clears_assignment(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(name="Only Role", base_role="user",
                                        permission_keys={"page.view_all"})
        db.assign_custom_role(uid, role_id)
        db.delete_custom_role(role_id)
        user = db.get_user_by_id(uid)
        assert user["custom_role_id"] is None
        perms = db.get_user_permissions(uid)
        assert len(perms["enabled_permissions"]) > 0

    def test_delete_role_no_users(self, isolated_db):
        role_id = db.create_custom_role(name="Empty Role")
        db.delete_custom_role(role_id)
        assert db.get_custom_role(role_id) is None

    def test_similarity_prefers_same_base_role(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_a = db.create_custom_role(name="User Role", base_role="user",
                                       permission_keys={"page.view_all"})
        role_b_user = db.create_custom_role(name="Also User", base_role="user",
                                            permission_keys={"page.view_all"})
        db.create_custom_role(name="Editor", base_role="editor",
                                              permission_keys={"page.view_all"})
        db.assign_custom_role(uid, role_a)
        db.delete_custom_role(role_a)
        user = db.get_user_by_id(uid)
        assert user["custom_role_id"] == role_b_user


class TestCustomRoleCascading:
    def test_update_role_permissions_affects_users(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(
            name="Dynamic Role",
            base_role="user",
            permission_keys={"page.view_all"},
        )
        db.assign_custom_role(uid, role_id)

        user = db.get_user_by_id(uid)
        assert db.has_permission(user, "page.view_all") is True
        assert db.has_permission(user, "search.pages") is False

        db.update_custom_role(role_id, permission_keys={"page.view_all", "search.pages"})

        user = db.get_user_by_id(uid)
        assert db.has_permission(user, "search.pages") is True

    def test_update_role_category_access_affects_users(self, isolated_db, cr_user,
                                                       sample_category):
        uid, _ = cr_user
        role_id = db.create_custom_role(
            name="Cat Role",
            base_role="user",
            permission_keys={"page.view_all"},
            read_restricted=False,
        )
        db.assign_custom_role(uid, role_id)
        user = db.get_user_by_id(uid)
        assert db.has_category_read_access(user, sample_category) is True

        other_cat = db.create_category("Other")
        db.update_custom_role(role_id, read_restricted=True,
                              read_category_ids=[other_cat])

        user = db.get_user_by_id(uid)
        assert db.has_category_read_access(user, sample_category) is False
        assert db.has_category_read_access(user, other_cat) is True


class TestPluginAwarePermissions:
    def test_plugin_gated_permission_map_exists(self, isolated_db):
        assert len(PERMISSION_PLUGIN_MAP) > 0

    def test_non_plugin_permission_works(self, isolated_db, cr_user):
        uid, _ = cr_user
        role_id = db.create_custom_role(
            name="Basic Role",
            base_role="user",
            permission_keys={"page.view_all", "search.pages"},
        )
        db.assign_custom_role(uid, role_id)
        user = db.get_user_by_id(uid)
        assert db.has_permission(user, "page.view_all") is True
        assert db.has_permission(user, "search.pages") is True


class TestCustomRoleRoutes:
    def test_roles_list_page(self, client, logged_in_admin):
        resp = logged_in_admin.get("/admin/roles")
        assert resp.status_code == 200
        assert b"Custom Roles" in resp.data

    def test_create_role_page(self, client, logged_in_admin):
        resp = logged_in_admin.get("/admin/roles/create")
        assert resp.status_code == 200
        assert b"Create Custom Role" in resp.data

    def test_create_role_post(self, client, logged_in_admin):
        resp = logged_in_admin.post("/admin/roles/create", data={
            "name": "Test Role",
            "description": "A test role",
            "base_role": "user",
            "permissions": ["page.view_all", "search.pages"],
        }, follow_redirects=True)
        assert resp.status_code == 200
        roles = db.list_custom_roles()
        assert any(r["name"] == "Test Role" for r in roles)

    def test_create_role_empty_name(self, client, logged_in_admin):
        resp = logged_in_admin.post("/admin/roles/create", data={
            "name": "",
            "base_role": "user",
        }, follow_redirects=True)
        assert b"required" in resp.data

    def test_edit_role_page(self, client, logged_in_admin):
        role_id = db.create_custom_role(name="Edit Me")
        resp = logged_in_admin.get(f"/admin/roles/{role_id}")
        assert resp.status_code == 200
        assert b"Edit Me" in resp.data

    def test_edit_role_post(self, client, logged_in_admin):
        role_id = db.create_custom_role(name="Before Edit")
        resp = logged_in_admin.post(f"/admin/roles/{role_id}", data={
            "name": "After Edit",
            "description": "Updated",
            "base_role": "user",
            "permissions": ["page.view_all"],
        }, follow_redirects=True)
        assert resp.status_code == 200
        role = db.get_custom_role(role_id)
        assert role["name"] == "After Edit"

    def test_edit_nonexistent_role_404(self, client, logged_in_admin):
        resp = logged_in_admin.get("/admin/roles/99999")
        assert resp.status_code == 404

    def test_delete_role(self, client, logged_in_admin):
        role_id = db.create_custom_role(name="Delete Me")
        resp = logged_in_admin.post(f"/admin/roles/{role_id}/delete", data={},
                                    follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_custom_role(role_id) is None

    def test_assign_user_to_role(self, client, logged_in_admin, regular_user):
        role_id = db.create_custom_role(name="Assign Role", base_role="user")
        resp = logged_in_admin.post(f"/admin/roles/{role_id}/assign", data={
            "user_id": regular_user,
        }, follow_redirects=True)
        assert resp.status_code == 200
        user = db.get_user_by_id(regular_user)
        assert user["custom_role_id"] == role_id

    def test_unassign_user_from_role(self, client, logged_in_admin, regular_user):
        role_id = db.create_custom_role(name="Unassign Role", base_role="user")
        db.assign_custom_role(regular_user, role_id)
        resp = logged_in_admin.post(f"/admin/roles/{role_id}/unassign", data={
            "user_id": regular_user,
        }, follow_redirects=True)
        assert resp.status_code == 200
        user = db.get_user_by_id(regular_user)
        assert user["custom_role_id"] is None

    def test_assign_role_from_users_page(self, client, logged_in_admin, regular_user):
        role_id = db.create_custom_role(name="From Users", base_role="user")
        resp = logged_in_admin.post(f"/admin/users/{regular_user}/assign-role", data={
            "custom_role_id": str(role_id),
        }, follow_redirects=True)
        assert resp.status_code == 200
        user = db.get_user_by_id(regular_user)
        assert user["custom_role_id"] == role_id

    def test_remove_role_from_users_page(self, client, logged_in_admin, regular_user):
        role_id = db.create_custom_role(name="Remove Role", base_role="user")
        db.assign_custom_role(regular_user, role_id)
        resp = logged_in_admin.post(f"/admin/users/{regular_user}/assign-role", data={
            "custom_role_id": "none",
        }, follow_redirects=True)
        assert resp.status_code == 200
        user = db.get_user_by_id(regular_user)
        assert user["custom_role_id"] is None

    def test_cannot_assign_admin_to_custom_role(self, client, admin_user, logged_in_admin):
        from werkzeug.security import generate_password_hash
        uid2 = db.create_user("admin2", generate_password_hash("admin456"), role="admin")
        role_id = db.create_custom_role(name="Not For Admin", base_role="user")
        resp = logged_in_admin.post(f"/admin/roles/{role_id}/assign", data={
            "user_id": uid2,
        }, follow_redirects=True)
        assert resp.status_code == 200
        user = db.get_user_by_id(uid2)
        assert user["custom_role_id"] is None

    def test_users_page_shows_roles_link(self, client, logged_in_admin):
        resp = logged_in_admin.get("/admin/users")
        assert resp.status_code == 200
        assert b"Manage Custom Roles" in resp.data

    def test_users_page_shows_custom_role_badge(self, client, logged_in_admin, regular_user):
        role_id = db.create_custom_role(name="Visible Role", base_role="user")
        db.assign_custom_role(regular_user, role_id)
        resp = logged_in_admin.get("/admin/users")
        assert resp.status_code == 200
        assert b"Visible Role" in resp.data

    def test_non_admin_cannot_access_roles(self, client, admin_user, logged_in_editor):
        resp = logged_in_editor.get("/admin/roles")
        assert resp.status_code == 403 or resp.status_code == 302


# ---------------------------------------------------------------------------
#  get_role_permissions
# ---------------------------------------------------------------------------

class TestGetRolePermissions:
    def test_get_role_permissions(self, isolated_db):
        role_id = db.create_custom_role(
            name="Perm Role",
            base_role="user",
            permission_keys={"page.view_all", "search.pages"},
            read_restricted=True,
            read_category_ids=[],
        )
        result = db.get_role_permissions(role_id)
        assert "page.view_all" in result["enabled_permissions"]
        assert result["category_access"]["restricted"] is True

    def test_get_role_permissions_nonexistent(self, isolated_db):
        result = db.get_role_permissions(99999)
        assert result["enabled_permissions"] == set()
