"""Tests for the API Service plugin."""

import json
import pytest
from flask import url_for


@pytest.fixture(autouse=True)
def enable_api_service():
    """Enable API Service settings before each test."""
    import db
    db.update_site_settings(api_service_enabled=1)


def _enable_test_logging(monkeypatch, tmp_path):
    import config
    import wiki_logger

    if wiki_logger._logger is not None:
        for handler in list(wiki_logger._logger.handlers):
            handler.close()
            wiki_logger._logger.removeHandler(handler)
    wiki_logger._logger = None
    wiki_logger._log_level = None

    log_file = str(tmp_path / "logs" / "bananawiki.log")
    monkeypatch.setattr(config, "LOGGING_LEVEL", "medium")
    monkeypatch.setattr(config, "LOG_FILE", log_file)
    return log_file


def _read_test_log(log_file):
    import wiki_logger

    for handler in wiki_logger.get_logger().handlers:
        handler.flush()
    with open(log_file, encoding="utf-8") as fh:
        return fh.read()


class TestAPIStatus:
    """Test the public status endpoint."""

    def test_status_returns_ok(self, client, admin_user):
        resp = client.get("/api/v1/status")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["api_enabled"] is True
        assert data["service"] == "BananaWiki API Service"


class TestAPIServiceNavigation:
    """Test account/admin navigation links for API service surfaces."""

    def test_admin_settings_shows_api_service_link(self, logged_in_admin):
        resp = logged_in_admin.get("/settings")
        assert resp.status_code == 200
        assert b"/admin/api-service" in resp.data
        assert b"/settings/api-tokens" in resp.data

    def test_settings_hide_api_service_when_plugin_disabled(self, logged_in_admin):
        import db

        db.disable_plugin("api_service")
        resp = logged_in_admin.get("/settings")
        assert resp.status_code == 200
        assert b"/admin/api-service" not in resp.data
        assert b"/settings/api-tokens" not in resp.data

    def test_api_service_surfaces_404_when_plugin_disabled(self, logged_in_admin):
        import db

        db.disable_plugin("api_service")
        for path in ("/admin/api-service", "/settings/api-tokens", "/api-docs"):
            resp = logged_in_admin.get(path)
            assert resp.status_code == 404
        resp = logged_in_admin.get("/api/v1/status")
        assert resp.status_code == 404

    def test_user_settings_shows_token_link(self, logged_in_user):
        import db
        from werkzeug.security import generate_password_hash

        db.create_user("api_nav_user", generate_password_hash("pass12345"), role="user")
        db.update_site_settings(setup_done=1)
        client = logged_in_user
        client.post("/login", data={"username": "api_nav_user", "password": "pass12345"})

        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"/settings/api-tokens" in resp.data


class TestAPIAuth:
    """Test API authentication."""

    def test_no_auth_returns_401(self, client, admin_user):
        resp = client.get("/api/v1/pages")
        assert resp.status_code == 401
        data = resp.get_json()
        assert "error" in data

    def test_invalid_token_returns_401(self, client, admin_user):
        resp = client.get(
            "/api/v1/pages",
            headers={"Authorization": "Bearer invalid_token"},
        )
        assert resp.status_code == 401
        data = resp.get_json()
        assert "error" in data

    def test_bearer_token_auth_works(self, client, admin_user, logged_in_admin):
        import db
        # Create an API token for the admin user
        raw_token = db.create_api_token(admin_user, name="Test Token")
        resp = client.get(
            "/api/v1/pages",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_revoked_token_rejected(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Revocable")
        # Get the token id
        tokens = db.list_user_tokens(admin_user)
        assert len(tokens) == 1
        db.revoke_api_service_token(tokens[0]["id"])
        resp = client.get(
            "/api/v1/pages",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 401

    def test_cross_origin_rejected(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user)
        resp = client.get(
            "/api/v1/pages",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Origin": "https://evil.com",
            },
        )
        assert resp.status_code == 403


class TestUsersAPI:
    """Test /api/v1/users endpoints."""

    def test_list_users_admin(self, client, admin_user, logged_in_admin):
        import db
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["users"]})
        resp = client.get(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["users"]) >= 1

    def test_create_user(self, client, admin_user, logged_in_admin):
        import db
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["users"]})
        resp = client.post(
            "/api/v1/users",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "username": "newuser",
                "password": "securepass",
                "role": "editor",
            }),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        assert data["user"]["username"] == "newuser"
        assert data["user"]["role"] == "editor"

    def test_bulk_create_users(self, client, admin_user, logged_in_admin):
        import db
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["users"]})
        resp = client.post(
            "/api/v1/users/bulk",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "users": [
                    {"username": "bulk1", "password": "bulk-pass-1", "role": "user"},
                    {"username": "bulk2", "password": "bulk-pass-2", "role": "editor"},
                ],
            }),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["created"]) == 2

    def test_delete_user(self, client, admin_user, logged_in_admin, regular_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["users"]})
        resp = client.delete(
            f"/api/v1/users/{regular_user}",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "deleted" in data["message"]

    def test_admin_token_can_modify_peer_admin(self, client, admin_user, logged_in_admin):
        import db
        from werkzeug.security import generate_password_hash, check_password_hash

        peer_admin = db.create_user("apipeer", generate_password_hash("peer123"), role="admin")
        raw_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["users"]},
        )

        resp = client.put(
            f"/api/v1/users/{peer_admin}",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"suspended": True, "password": "newpass123", "role": "user"}),
        )
        assert resp.status_code == 200

        user = db.get_user_by_id(peer_admin)
        assert user["role"] == "user"
        assert user["suspended"] == 1
        assert check_password_hash(user["password"], "newpass123")

    def test_admin_token_can_delete_peer_admin(self, client, admin_user, logged_in_admin):
        import db
        from werkzeug.security import generate_password_hash

        peer_admin = db.create_user("apideletepeer", generate_password_hash("peer123"), role="admin")
        raw_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["users"]},
        )

        resp = client.delete(
            f"/api/v1/users/{peer_admin}",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        assert db.get_user_by_id(peer_admin) is None

    def test_api_cannot_assign_owner(self, client, admin_user, logged_in_admin, regular_user):
        import db

        raw_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["users"]},
        )

        resp = client.put(
            f"/api/v1/users/{regular_user}",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"role": "owner"}),
        )
        assert resp.status_code == 403
        assert "Cannot assign protected admin via API" in resp.get_json()["error"]
        assert db.get_user_by_id(regular_user)["role"] == "user"

    def test_admin_token_can_grant_admin_role(self, client, admin_user, logged_in_admin, regular_user):
        import db

        raw_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["users"]},
        )

        resp = client.post(
            "/api/v1/users",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "username": "apiadmin",
                "password": "securepass",
                "role": "admin",
            }),
        )
        assert resp.status_code == 201
        assert db.get_user_by_username("apiadmin")["role"] == "admin"

        resp = client.put(
            f"/api/v1/users/{regular_user}",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"role": "admin"}),
        )
        assert resp.status_code == 200
        assert db.get_user_by_id(regular_user)["role"] == "admin"

    def test_bulk_create_allows_admin_role_for_admin_token(self, client, admin_user, logged_in_admin):
        import db

        raw_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["users"]},
        )

        resp = client.post(
            "/api/v1/users/bulk",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "users": [
                    {"username": "bulkadmin", "password": "bulk-pass-1", "role": "admin"},
                    {"username": "bulkeditor", "password": "bulk-pass-2", "role": "editor"},
                ],
            }),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert len(data["created"]) == 2
        assert {u["username"] for u in data["created"]} == {"bulkadmin", "bulkeditor"}
        assert data["errors"] == []
        assert db.get_user_by_username("bulkadmin")["role"] == "admin"


class TestPagesAPI:
    """Test /api/v1/pages endpoints."""

    def test_list_pages(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Test Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.get(
            "/api/v1/pages",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_create_page(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Test Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.post(
            "/api/v1/pages",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "title": "API Test Page",
                "content": "# Hello from API",
                "slug": "api-test-page",
            }),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        assert data["page"]["slug"] == "api-test-page"

    def test_get_page(self, client, admin_user):
        import db
        db.create_page("Test Page", "test-page", "Content")
        raw_token = db.create_api_token(admin_user, name="Test Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.get(
            "/api/v1/pages/test-page",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["page"]["title"] == "Test Page"

    def test_update_page(self, client, admin_user):
        import db
        db.create_page("Old Title", "updatable", "Old content")
        raw_token = db.create_api_token(admin_user, name="Test Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.put(
            "/api/v1/pages/updatable",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"title": "New Title", "content": "New content"}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["page"]["title"] == "New Title"

    def test_delete_page(self, client, admin_user):
        import db
        db.create_page("Delete Me", "delete-me", "Content")
        raw_token = db.create_api_token(admin_user, name="Test Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.delete(
            "/api/v1/pages/delete-me",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        # The fixture enables deletion_slowdown, so the page enters its grace
        # period rather than disappearing. test_api_page_authorization covers
        # the immediate delete with the plugin off.
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["ok"] is True
        assert data["pending_deletion"] is True
        assert db.get_page_by_slug("delete-me")["pending_deletion"]

    def test_bulk_create_pages(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.post(
            "/api/v1/pages/bulk",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "pages": [
                    {"title": "Bulk Page 1", "slug": "bulk-1"},
                    {"title": "Bulk Page 2", "slug": "bulk-2"},
                ],
            }),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert len(data["created"]) == 2

    def test_bulk_edit_pages(self, client, admin_user):
        import db
        db.create_page("Page 1", "edit-1", "Content")
        db.create_page("Page 2", "edit-2", "Content")
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.post(
            "/api/v1/pages/bulk-edit",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "edits": [
                    {"slug": "edit-1", "title": "Edited 1"},
                    {"slug": "edit-2", "title": "Edited 2"},
                ],
            }),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["updated"] == 2

    def test_bulk_delete_pages(self, client, admin_user):
        import db
        db.create_page("Del 1", "del-1", "Content")
        db.create_page("Del 2", "del-2", "Content")
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.post(
            "/api/v1/pages/bulk-delete",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"slugs": ["del-1", "del-2"]}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted"] == 2


class TestCategoriesAPI:
    """Test /api/v1/categories endpoints."""

    def test_create_category(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Test Token",
                                         permissions={"read": True, "write": True, "scopes": ["categories"]})
        resp = client.post(
            "/api/v1/categories",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"name": "API Category"}),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["category"]["name"] == "API Category"

    def test_list_categories(self, client, admin_user):
        import db
        db.create_category("Test Cat")
        raw_token = db.create_api_token(admin_user, name="Test Token",
                                         permissions={"read": True, "write": True, "scopes": ["categories"]})
        resp = client.get(
            "/api/v1/categories",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["categories"]) >= 1

    def test_delete_category(self, client, admin_user):
        import db
        cat_id = db.create_category("Delete Me")
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["categories"]})
        resp = client.delete(
            f"/api/v1/categories/{cat_id}",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200


class TestTokensAPI:
    """Test /api/v1/tokens endpoints."""

    def test_create_and_list_tokens(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Parent",
                                         permissions={"read": True, "write": True, "scopes": ["tokens", "pages"]})
        # Create a sub-token
        resp = client.post(
            "/api/v1/tokens",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "name": "Child Token",
                "permissions": {"read": True, "write": False, "scopes": ["pages"]},
            }),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "token" in data

        # List tokens
        resp = client.get(
            "/api/v1/tokens",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["tokens"]) >= 1

    def test_token_limit(self, client, admin_user):
        import db
        settings = db.get_api_service_settings()
        max_tokens = settings["max_tokens_per_user"]
        # Create max tokens
        parent_token = None
        for i in range(max_tokens):
            parent_token = db.create_api_token(
                admin_user, name=f"Token {i}",
                permissions={"read": True, "write": True, "scopes": ["tokens"]},
            )
        # Try to create one more via API
        resp = client.post(
            "/api/v1/tokens",
            headers={
                "Authorization": f"Bearer {parent_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"name": "Extra Token"}),
        )
        assert resp.status_code == 400


class TestAdminAPI:
    """Test /api/v1/admin/* endpoints."""

    def test_audit_log(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["admin"]})
        resp = client.get(
            "/api/v1/admin/audit-log",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_non_superuser_cannot_clear_audit_log(self, client, admin_user):
        import db

        raw_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        resp = client.delete(
            "/api/v1/admin/audit-log?before_days=0",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403
        assert "Only superusers can clear API audit logs" in resp.get_json()["error"]

    def test_superuser_can_clear_audit_log(self, client, admin_user):
        import db

        db.update_user(admin_user, is_superuser=1)
        raw_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        resp = client.delete(
            "/api/v1/admin/audit-log?before_days=0",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_admin_token_can_revoke_peer_admin_token(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash

        peer_admin = db.create_user("apitokenpeer", generate_password_hash("peer123"), role="admin")
        caller_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        db.create_api_token(
            peer_admin,
            name="Peer Token",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        peer_token_id = db.list_user_tokens(peer_admin)[0]["id"]

        resp = client.post(
            f"/api/v1/admin/tokens/{peer_token_id}/revoke",
            headers={"Authorization": f"Bearer {caller_token}"},
        )
        assert resp.status_code == 200
        assert db.get_token_by_id(peer_token_id)["active"] == 0

    def test_superuser_can_revoke_peer_admin_token(self, client, admin_user):
        import db
        from werkzeug.security import generate_password_hash

        db.update_user(admin_user, is_superuser=1)
        peer_admin = db.create_user("apitokensuperpeer", generate_password_hash("peer123"), role="admin")
        caller_token = db.create_api_token(
            admin_user,
            name="Admin Token",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        db.create_api_token(
            peer_admin,
            name="Peer Token",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        peer_token_id = db.list_user_tokens(peer_admin)[0]["id"]

        resp = client.post(
            f"/api/v1/admin/tokens/{peer_token_id}/revoke",
            headers={"Authorization": f"Bearer {caller_token}"},
        )
        assert resp.status_code == 200
        assert db.get_token_by_id(peer_token_id)["active"] == 0


class TestSettingsAPI:
    """Test /api/v1/settings endpoints."""

    def test_get_settings(self, client, admin_user):
        import db
        raw_token = db.create_api_token(admin_user, name="Admin Token",
                                         permissions={"read": True, "write": True, "scopes": ["settings"]})
        resp = client.get(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True


class TestUiPages:
    """Test UI management pages."""

    def test_admin_page_requires_admin(self, client, logged_in_user):
        resp = client.get("/admin/api-service")
        assert resp.status_code == 302

    def test_admin_page_accessible_by_admin(self, client, logged_in_admin):
        resp = client.get("/admin/api-service")
        assert resp.status_code == 200

    def test_user_token_page_requires_login(self, client):
        resp = client.get("/settings/api-tokens")
        assert resp.status_code == 302

    def test_user_token_page_accessible(self, client, logged_in_admin):
        resp = client.get("/settings/api-tokens")
        assert resp.status_code == 200

    def test_docs_page_public(self, client, admin_user):
        resp = client.get("/api-docs")
        assert resp.status_code == 200

    def test_docs_page_content(self, client, admin_user):
        resp = client.get("/api-docs")
        html = resp.data.decode("utf-8")
        assert "API Service Documentation" in html
        assert "/api/v1/users" in html
        assert "/api/v1/pages" in html


class TestPermissionScopes:
    """Test that permission scopes are enforced."""

    def test_read_only_token_cannot_write(self, client, admin_user, editor_user):
        import db
        raw_token = db.create_api_token(editor_user, name="Read Only",
                                         permissions={"read": True, "write": False, "scopes": ["pages"]})
        resp = client.post(
            "/api/v1/pages",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"title": "Should Fail"}),
        )
        assert resp.status_code == 403

    def test_wrong_scope_rejected(self, client, admin_user, editor_user):
        import db
        raw_token = db.create_api_token(editor_user, name="Only Pages",
                                         permissions={"read": True, "write": True, "scopes": ["pages"]})
        resp = client.get(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403


class TestAdminUiActions:
    """Test admin UI endpoints."""

    def test_update_settings(self, client, logged_in_admin, monkeypatch, tmp_path):
        log_file = _enable_test_logging(monkeypatch, tmp_path)
        resp = client.post(
            "/admin/api-service/settings",
            data={
                "enabled": "1",
                "rate_limit": "100",
                "admin_rate_limit": "200",
                "max_tokens_per_user": "10",
            },
        )
        assert resp.status_code == 302
        import db
        settings = db.get_api_service_settings()
        assert settings["rate_limit"] == 100
        assert settings["admin_rate_limit"] == 200
        assert settings["max_tokens_per_user"] == 10
        contents = _read_test_log(log_file)
        assert "admin_update_api_service_settings" in contents
        assert "user=admin" in contents

    def test_toggle_user_api_access(self, client, logged_in_admin, regular_user, monkeypatch, tmp_path):
        log_file = _enable_test_logging(monkeypatch, tmp_path)
        resp = client.post(f"/admin/api-service/users/{regular_user}/toggle-access")
        assert resp.status_code == 302
        import db
        user = db.get_user_by_id(regular_user)
        assert user["api_access_enabled"] == 1
        contents = _read_test_log(log_file)
        assert "admin_toggle_api_access" in contents
        assert "user=admin" in contents
        assert "target_user=user" in contents

    def test_admin_ui_cannot_clear_api_audit_log(self, client, logged_in_admin):
        resp = client.post(
            "/admin/api-service/clear-audit",
            data={"before_days": "0"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Only superusers can clear API audit logs" in resp.data

    def test_admin_ui_can_revoke_peer_admin_tokens(self, client, logged_in_admin):
        import db
        from werkzeug.security import generate_password_hash

        peer_admin = db.create_user("uipeeradmin", generate_password_hash("peer123"), role="admin")
        db.create_api_token(
            peer_admin,
            name="Peer Token",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        token_id = db.list_user_tokens(peer_admin)[0]["id"]

        resp = client.post(
            f"/admin/api-service/tokens/{token_id}/revoke",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert db.get_token_by_id(token_id)["active"] == 0

        db.create_api_token(
            peer_admin,
            name="Peer Token 2",
            permissions={"read": True, "write": True, "scopes": ["admin"]},
        )
        token_id = db.list_user_tokens(peer_admin)[0]["id"]

        resp = client.post(
            "/admin/api-service/tokens/revoke-all",
            data={"user_id": peer_admin},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert db.get_token_by_id(token_id)["active"] == 0
