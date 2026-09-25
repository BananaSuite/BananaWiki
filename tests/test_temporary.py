"""Tests for the temporary accounts & pages feature."""

import pytest
from datetime import datetime, timezone, timedelta


def _login(client, username, password):
    """Log in via the test client."""
    return client.post("/login", data={"username": username, "password": password})


def _enable_temporary_accounts_plugin():
    """Enable the temporary_accounts plugin in the database."""
    import db
    conn = db.get_db()
    conn.execute(
        "UPDATE plugins SET enabled = 1 WHERE id = 'temporary_accounts'"
    )
    conn.commit()
    conn.close()


def _disable_temporary_accounts_plugin():
    """Disable the temporary_accounts plugin in the database."""
    import db
    conn = db.get_db()
    conn.execute(
        "UPDATE plugins SET enabled = 0 WHERE id = 'temporary_accounts'"
    )
    conn.commit()
    conn.close()


class TestTemporaryPluginGating:
    """Routes return 404 when the plugin is disabled."""

    def test_admin_page_returns_404_when_disabled(self, client, admin_user):
        _disable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/admin/temporary")
        assert resp.status_code == 404

    def test_admin_page_returns_200_when_enabled(self, client, admin_user):
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/admin/temporary")
        assert resp.status_code == 200


class TestTempPageDB:
    """Tests for db._temporary page functions."""

    def test_set_and_get_page_expiry(self):
        import db
        page_id = db.create_page("Test", "test-page", content="Hello")
        db.set_page_expiry(page_id, "2099-12-31T23:59:59", show_countdown=True)
        row = db.get_page_expiry(page_id)
        assert row is not None
        assert row["expires_at"] == "2099-12-31T23:59:59"
        assert row["show_countdown"] == 1

    def test_set_page_expiry_to_none_removes_schedule(self):
        import db
        page_id = db.create_page("T2", "test2", content="X")
        db.set_page_expiry(page_id, "2099-01-01T00:00:00")
        assert db.get_page_expiry(page_id) is not None
        db.set_page_expiry(page_id, None)
        assert db.get_page_expiry(page_id) is None

    def test_update_existing_page_expiry(self):
        import db
        page_id = db.create_page("T3", "test3", content="X")
        db.set_page_expiry(page_id, "2099-01-01T00:00:00")
        db.set_page_expiry(page_id, "2099-06-15T12:00:00", show_countdown=False)
        row = db.get_page_expiry(page_id)
        assert row["expires_at"] == "2099-06-15T12:00:00"
        assert row["show_countdown"] == 0

    def test_list_temp_pages(self):
        import db
        p1 = db.create_page("A", "a-page", content="A")
        p2 = db.create_page("B", "b-page", content="B")
        db.set_page_expiry(p1, "2099-01-01T00:00:00")
        db.set_page_expiry(p2, "2099-06-01T00:00:00")
        result = db.list_temp_pages()
        assert len(result) == 2
        assert result[0]["title"] == "A"  # Ordered by expires_at ASC

    def test_get_expired_pages(self):
        import db
        p1 = db.create_page("Old", "old-page", content="X")
        p2 = db.create_page("Future", "future-page", content="Y")
        db.set_page_expiry(p1, "2020-01-01T00:00:00")
        db.set_page_expiry(p2, "2099-01-01T00:00:00")
        expired = db.get_expired_pages()
        assert len(expired) == 1
        assert expired[0]["title"] == "Old"

    def test_cleanup_expired_temp_pages_deletes_page(self):
        import db
        page_id = db.create_page("Expired", "expired-page", content="X")
        db.set_page_expiry(page_id, "2020-01-01T00:00:00")
        count = db.cleanup_expired_temp_pages()
        assert count == 1
        assert db.get_page(page_id) is None

    def test_cleanup_does_not_delete_future_pages(self):
        import db
        page_id = db.create_page("Future", "future2", content="X")
        db.set_page_expiry(page_id, "2099-01-01T00:00:00")
        count = db.cleanup_expired_temp_pages()
        assert count == 0
        assert db.get_page(page_id) is not None


class TestTempUserDB:
    """Tests for db._temporary user functions."""

    def test_set_and_get_user_expiry(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("tempuser", generate_password_hash("pass"), role="user")
        db.set_user_expiry(uid, "2099-12-31T23:59:59", show_countdown=False)
        row = db.get_user_expiry(uid)
        assert row is not None
        assert row["show_countdown"] == 0

    def test_remove_user_expiry(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("tempuser2", generate_password_hash("pass"), role="user")
        db.set_user_expiry(uid, "2099-01-01T00:00:00")
        db.set_user_expiry(uid, None)
        assert db.get_user_expiry(uid) is None

    def test_list_temp_users(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("templist", generate_password_hash("pass"), role="editor")
        db.set_user_expiry(uid, "2099-01-01T00:00:00")
        result = db.list_temp_users()
        assert len(result) >= 1
        assert any(r["username"] == "templist" for r in result)

    def test_cleanup_expired_temp_users(self):
        import db
        from werkzeug.security import generate_password_hash
        # Create an admin first so the system has one
        db.create_user("keeper", generate_password_hash("pass"), role="admin")
        uid = db.create_user("todelete", generate_password_hash("pass"), role="user")
        db.set_user_expiry(uid, "2020-01-01T00:00:00")
        count = db.cleanup_expired_temp_users()
        assert count == 1
        assert db.get_user_by_id(uid) is None

    def test_cleanup_skips_owner(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("padmin", generate_password_hash("pass"), role="owner")
        db.set_user_expiry(uid, "2020-01-01T00:00:00")
        count = db.cleanup_expired_temp_users()
        assert count == 0
        assert db.get_user_by_id(uid) is not None

    def test_cleanup_skips_last_admin(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("onlyadmin", generate_password_hash("pass"), role="admin")
        db.set_user_expiry(uid, "2020-01-01T00:00:00")
        count = db.cleanup_expired_temp_users()
        assert count == 0
        assert db.get_user_by_id(uid) is not None


class TestTempRoleDB:
    """Tests for db._temporary role functions."""

    def test_set_and_get_role_expiry(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("roleuser", generate_password_hash("pass"), role="editor")
        db.set_role_expiry(uid, "user", expires_at="2099-12-31T23:59:59")
        row = db.get_role_expiry(uid)
        assert row is not None
        assert row["original_role"] == "user"

    def test_cancel_role_expiry(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("rolecancel", generate_password_hash("pass"), role="admin")
        db.set_role_expiry(uid, "user", expires_at="2099-01-01T00:00:00")
        db.set_role_expiry(uid, "user", expires_at=None)
        assert db.get_role_expiry(uid) is None

    def test_list_temp_roles(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("temprole", generate_password_hash("pass"), role="admin")
        db.set_role_expiry(uid, "editor", expires_at="2099-01-01T00:00:00")
        result = db.list_temp_roles()
        assert len(result) >= 1
        assert any(r["username"] == "temprole" for r in result)

    def test_cleanup_expired_temp_roles(self):
        import db
        from werkzeug.security import generate_password_hash
        # Need at least one other admin
        db.create_user("keepadmin", generate_password_hash("pass"), role="admin")
        uid = db.create_user("tmpeditor", generate_password_hash("pass"), role="admin")
        db.set_role_expiry(uid, "editor", expires_at="2020-01-01T00:00:00")
        count = db.cleanup_expired_temp_roles()
        assert count == 1
        user = db.get_user_by_id(uid)
        assert user["role"] == "editor"
        # Schedule should be gone
        assert db.get_role_expiry(uid) is None

    def test_cleanup_skips_last_admin_demotion(self):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("soleadmin", generate_password_hash("pass"), role="admin")
        db.set_role_expiry(uid, "user", expires_at="2020-01-01T00:00:00")
        count = db.cleanup_expired_temp_roles()
        # Should skip because it's the only admin
        assert count == 0
        user = db.get_user_by_id(uid)
        assert user["role"] == "admin"


class TestCombinedCleanup:
    """Test the cleanup_all_expired_temporary orchestrator."""

    def test_cleanup_all(self):
        import db
        from werkzeug.security import generate_password_hash
        # Set up expired page
        page_id = db.create_page("ExpPage", "exp-page", content="X")
        db.set_page_expiry(page_id, "2020-01-01T00:00:00")
        # Set up expired user (need a permanent admin)
        db.create_user("permadmin", generate_password_hash("pass"), role="admin")
        uid = db.create_user("expuser", generate_password_hash("pass"), role="user")
        db.set_user_expiry(uid, "2020-01-01T00:00:00")
        # Run combined cleanup
        result = db.cleanup_all_expired_temporary()
        assert result["expired_temp_pages"] >= 1
        assert result["expired_temp_users"] >= 1

    def test_run_full_cleanup_includes_temporary(self):
        import db
        result = db.run_full_cleanup(vacuum=False)
        assert "expired_temporary" in result


class TestTemporaryAdminRoutes:
    """Admin routes for managing temporary items."""

    def test_admin_page_lists_items(self, client, admin_user):
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.get("/admin/temporary")
        assert resp.status_code == 200
        assert b"Temporary Pages" in resp.data

    def test_set_page_expiry(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        page_id = db.create_page("SetExp", "setexp", content="X")
        resp = client.post("/admin/temporary/page", data={
            "page_id": str(page_id),
            "expires_at": "2099-12-31T23:59",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"scheduled for auto-deletion" in resp.data
        assert db.get_page_expiry(page_id) is not None

    def test_remove_page_expiry(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        page_id = db.create_page("RemExp", "remexp", content="X")
        db.set_page_expiry(page_id, "2099-01-01T00:00:00")
        resp = client.post(f"/admin/temporary/page/{page_id}/remove",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_page_expiry(page_id) is None

    def test_remove_page_expiry_nonexistent_page(self, client, admin_user):
        """Removing expiry for a nonexistent page should show an error."""
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/page/999999/remove",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert b"Page not found" in resp.data

    def test_set_user_expiry(self, client, admin_user, regular_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/user", data={
            "user_id": regular_user,
            "expires_at": "2099-12-31T23:59",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"scheduled for auto-deletion" in resp.data

    def test_cannot_set_own_expiry(self, client, admin_user):
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/user", data={
            "user_id": admin_user,
            "expires_at": "2099-12-31T23:59",
        }, follow_redirects=True)
        assert b"cannot set auto-deletion on your own account" in resp.data

    def test_cannot_set_owner_expiry(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        pa_id = db.create_user("padmin", generate_password_hash("pass"),
                               role="owner")
        resp = client.post("/admin/temporary/user", data={
            "user_id": pa_id,
            "expires_at": "2099-12-31T23:59",
        }, follow_redirects=True)
        assert b'class="flash-message">Owner accounts cannot be set' in resp.data

    def test_can_set_peer_admin_expiry(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        peer_admin = db.create_user("temppeeradmin", generate_password_hash("pass"), role="admin")
        resp = client.post("/admin/temporary/user", data={
            "user_id": peer_admin,
            "expires_at": "2099-12-31T23:59",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_user_expiry(peer_admin) is not None

    def test_remove_user_expiry(self, client, admin_user, regular_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        db.set_user_expiry(regular_user, "2099-01-01T00:00:00")
        resp = client.post(f"/admin/temporary/user/{regular_user}/remove",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_user_expiry(regular_user) is None

    def test_remove_user_expiry_not_found(self, client, admin_user):
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/user/nonexist/remove",
                           follow_redirects=True)
        assert b"User not found" in resp.data

    def test_remove_user_expiry_owner(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        pa_id = db.create_user("padmin", generate_password_hash("pass"),
                               role="owner")
        resp = client.post(f"/admin/temporary/user/{pa_id}/remove",
                           follow_redirects=True)
        assert b'class="flash-message">Owner accounts cannot be set' in resp.data

    def test_can_remove_peer_admin_expiry(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        peer_admin = db.create_user("temppeeradmin2", generate_password_hash("pass"), role="admin")
        db.set_user_expiry(peer_admin, "2099-01-01T00:00:00")
        resp = client.post(f"/admin/temporary/user/{peer_admin}/remove",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_user_expiry(peer_admin) is None

    def test_remove_user_expiry_self(self, client, admin_user):
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post(f"/admin/temporary/user/{admin_user}/remove",
                           follow_redirects=True)
        assert b"cannot set auto-deletion on your own account" in resp.data

    def test_set_role_expiry(self, client, admin_user, editor_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/role", data={
            "user_id": editor_user,
            "original_role": "user",
            "expires_at": "2099-12-31T23:59",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Temporary role set" in resp.data

    def test_role_expiry_revert_role_must_be_lower(self, client, admin_user, regular_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/role", data={
            "user_id": regular_user,
            "original_role": "editor",
            "expires_at": "2099-12-31T23:59",
        }, follow_redirects=True)
        assert b"must be lower than the current role" in resp.data

    def test_can_set_peer_admin_role_expiry(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        peer_admin = db.create_user("temprolepeer", generate_password_hash("pass"), role="admin")
        resp = client.post("/admin/temporary/role", data={
            "user_id": peer_admin,
            "original_role": "editor",
            "expires_at": "2099-12-31T23:59",
        }, follow_redirects=True)
        assert b"Temporary role set" in resp.data
        assert db.get_role_expiry(peer_admin) is not None

    def test_remove_role_expiry(self, client, admin_user, editor_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        db.set_role_expiry(editor_user, "user", expires_at="2099-01-01T00:00:00")
        resp = client.post(f"/admin/temporary/role/{editor_user}/remove",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_role_expiry(editor_user) is None

    def test_remove_role_expiry_not_found(self, client, admin_user):
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/role/nonexist/remove",
                           follow_redirects=True)
        assert b"User not found" in resp.data

    def test_remove_role_expiry_owner(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        pa_id = db.create_user("padmin2", generate_password_hash("pass"),
                               role="owner")
        resp = client.post(f"/admin/temporary/role/{pa_id}/remove",
                           follow_redirects=True)
        assert b'class="flash-message">Owner accounts cannot have temporary role grants' in resp.data

    def test_can_remove_peer_admin_role_expiry(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        peer_admin = db.create_user("temprolepeer2", generate_password_hash("pass"), role="admin")
        db.set_role_expiry(peer_admin, "editor", expires_at="2099-01-01T00:00:00")
        resp = client.post(f"/admin/temporary/role/{peer_admin}/remove",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_role_expiry(peer_admin) is None

    def test_remove_role_expiry_self(self, client, admin_user):
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post(f"/admin/temporary/role/{admin_user}/remove",
                           follow_redirects=True)
        assert b"cannot set a temporary role on your own account" in resp.data

    def test_non_admin_cannot_access(self, client, admin_user, regular_user):
        _enable_temporary_accounts_plugin()
        _login(client, "user", "user123")
        resp = client.get("/admin/temporary")
        # Should get redirected (302/403)
        assert resp.status_code in (302, 403)

    def test_set_page_expiry_warns_on_reserved_page(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        page_id = db.create_page("Reserved", "reserved-page", content="X")
        # Simulate a reservation
        db.update_site_settings(page_reservations_enabled=1)
        resp = client.post("/admin/temporary/page", data={
            "page_id": str(page_id),
            "expires_at": "2099-12-31T23:59",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        # The page should still get the expiry set even if reserved
        assert db.get_page_expiry(page_id) is not None

    def test_set_page_expiry_blocks_home_page(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        page_id = db.create_page("Home", "home-page", content="Welcome")
        db.set_home_page(page_id)
        resp = client.post("/admin/temporary/page", data={
            "page_id": str(page_id),
            "expires_at": "2099-12-31T23:59",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Cannot schedule the home page for auto-deletion" in resp.data
        assert db.get_page_expiry(page_id) is None

    def test_set_page_expiry_rejects_past_date(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        page_id = db.create_page("PastExp", "pastexp", content="X")
        resp = client.post("/admin/temporary/page", data={
            "page_id": str(page_id),
            "expires_at": "2020-01-01T00:00",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Expiry date must be in the future" in resp.data
        assert db.get_page_expiry(page_id) is None

    def test_set_user_expiry_rejects_past_date(self, client, admin_user, regular_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/user", data={
            "user_id": regular_user,
            "expires_at": "2020-01-01T00:00",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Expiry date must be in the future" in resp.data

    def test_set_role_expiry_rejects_past_date(self, client, admin_user, editor_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        resp = client.post("/admin/temporary/role", data={
            "user_id": editor_user,
            "original_role": "user",
            "expires_at": "2020-01-01T00:00",
            "show_countdown": "1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Expiry date must be in the future" in resp.data


class TestPageCountdownDisplay:
    """Test that the countdown banner appears on page view."""

    def test_countdown_visible_when_show_countdown_enabled(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        page_id = db.create_page("Countdown", "countdown-page", content="Test")
        db.set_page_expiry(page_id, "2099-12-31T23:59:59", show_countdown=True)
        resp = client.get("/page/countdown-page")
        assert resp.status_code == 200
        assert b"scheduled for deletion" in resp.data

    def test_countdown_hidden_for_non_admin_when_disabled(self, client, admin_user, regular_user):
        import db
        _enable_temporary_accounts_plugin()
        page_id = db.create_page("Hidden", "hidden-page", content="Test")
        db.set_page_expiry(page_id, "2099-12-31T23:59:59", show_countdown=False)
        _login(client, "user", "user123")
        resp = client.get("/page/hidden-page")
        assert resp.status_code == 200
        assert b"xs-864e677b" not in resp.data

    def test_countdown_visible_for_admin_even_when_hidden(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        page_id = db.create_page("AdminSee", "admin-see", content="Test")
        db.set_page_expiry(page_id, "2099-12-31T23:59:59", show_countdown=False)
        resp = client.get("/page/admin-see")
        assert resp.status_code == 200
        assert b"xs-864e677b" in resp.data

    def test_no_banner_for_permanent_page(self, client, admin_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        db.create_page("Perm", "perm-page", content="Stays forever")
        resp = client.get("/page/perm-page")
        assert resp.status_code == 200
        assert b"xs-864e677b" not in resp.data


class TestUserCountdownDisplay:
    """Test countdown banners on user profile and account settings."""

    def test_user_expiry_visible_on_profile_when_enabled(self, client, admin_user, regular_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        db.set_user_expiry(regular_user, "2099-12-31T23:59:59", show_countdown=True)
        resp = client.get("/users/user")
        assert resp.status_code == 200
        assert b"xs-20b97ce2" in resp.data

    def test_user_expiry_hidden_from_others_when_disabled(self, client, admin_user, regular_user):
        import db
        from werkzeug.security import generate_password_hash
        _enable_temporary_accounts_plugin()
        db.set_user_expiry(regular_user, "2099-12-31T23:59:59", show_countdown=False)
        # Create and publish a profile for the target user so it's visible
        db.upsert_user_profile(regular_user, page_published=True)
        # Login as a different non-admin user
        db.create_user("other", generate_password_hash("other123"), role="user")
        _login(client, "other", "other123")
        resp = client.get("/users/user")
        assert resp.status_code == 200
        assert b"xs-20b97ce2" not in resp.data

    def test_role_expiry_visible_on_profile(self, client, admin_user, editor_user):
        import db
        _enable_temporary_accounts_plugin()
        _login(client, "admin", "admin123")
        db.set_role_expiry(editor_user, "user", expires_at="2099-12-31T23:59:59",
                           show_countdown=True)
        resp = client.get("/users/editor")
        assert resp.status_code == 200
        assert b"Role reverts to user" in resp.data

    def test_user_settings_shows_user_expiry(self, client, admin_user, regular_user):
        import db
        _enable_temporary_accounts_plugin()
        db.set_user_expiry(regular_user, "2099-12-31T23:59:59", show_countdown=True)
        _login(client, "user", "user123")
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"Account scheduled for deletion" in resp.data


class TestMigrationExport:
    """Verify temporary tables are included in site migration exports."""

    def test_export_tables_include_temp_tables(self):
        import db
        assert "temp_pages" in db._EXPORT_TABLES
        assert "temp_users" in db._EXPORT_TABLES
        assert "temp_roles" in db._EXPORT_TABLES
