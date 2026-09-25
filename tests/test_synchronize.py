"""
Tests for the BananaWiki synchronization changes.

Validates:
- The deduplicated build_user_export_zip function works correctly
- Both user self-export and admin export routes use the shared function
- The shared function produces correct ZIP contents
- All route modules import cleanly and have no circular imports
- All db functions referenced in route modules are properly exported
- All helper functions referenced in route modules are properly exported
- All templates referenced in render_template calls exist on disk
"""

import ast
import io
import json
import os
import sys
import types
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a fresh temporary SQLite database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(tmp_path / "attachments"))
    monkeypatch.setattr(config, "CHAT_ATTACHMENT_FOLDER", str(tmp_path / "chat_attachments"))
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "test.log"))
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate-limit store before and after each test."""
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def client():
    """Flask test client with CSRF disabled."""
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_user():
    """Create an admin user and mark setup as done."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def editor_user(admin_user):
    """Create an editor user (setup must already be done)."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor1", generate_password_hash("editor123"), role="editor")
    return uid


@pytest.fixture
def regular_user(admin_user):
    """Create a regular user (setup must already be done)."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("regular1", generate_password_hash("regular123"), role="user")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a test client already authenticated as the admin user."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, editor_user):
    """Return a test client already authenticated as the editor user."""
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    return client


@pytest.fixture
def logged_in_regular(client, regular_user):
    """Return a test client already authenticated as the regular user."""
    client.post("/login", data={"username": "regular1", "password": "regular123"})
    return client


# ===========================================================================
# 1. build_user_export_zip deduplication tests
# ===========================================================================

class TestBuildUserExportZipDeduplication:
    """Verify the shared build_user_export_zip function works correctly."""

    def test_shared_function_is_importable_from_routes_users(self):
        """build_user_export_zip can be imported from routes.users."""
        from routes.users import build_user_export_zip
        assert callable(build_user_export_zip)

    def test_shared_function_is_importable_from_routes_admin(self):
        """routes.admin imports build_user_export_zip from routes.users."""
        from routes.admin import build_user_export_zip
        assert callable(build_user_export_zip)

    def test_admin_and_users_share_same_function(self):
        """routes.admin and routes.users reference the exact same function object."""
        from routes.users import build_user_export_zip as users_fn
        from routes.admin import build_user_export_zip as admin_fn
        assert users_fn is admin_fn

    def test_function_is_module_level_not_nested(self):
        """build_user_export_zip is defined at module level in routes/users.py."""
        import routes.users as users_mod
        assert hasattr(users_mod, "build_user_export_zip")
        assert callable(users_mod.build_user_export_zip)

    def test_function_has_docstring(self):
        """build_user_export_zip carries a meaningful docstring."""
        from routes.users import build_user_export_zip
        assert build_user_export_zip.__doc__ is not None
        assert len(build_user_export_zip.__doc__.strip()) > 10

    def test_function_returns_bytesio(self, admin_user):
        """build_user_export_zip returns a BytesIO object."""
        import db
        from routes.users import build_user_export_zip
        user = db.get_user_by_id(admin_user)
        result = build_user_export_zip(user)
        assert isinstance(result, io.BytesIO)

    def test_function_returns_valid_zip(self, admin_user):
        """build_user_export_zip returns a valid ZIP archive."""
        import db
        from routes.users import build_user_export_zip
        user = db.get_user_by_id(admin_user)
        result = build_user_export_zip(user)
        assert result.read(2) == b"PK"  # ZIP magic bytes
        result.seek(0)
        assert zipfile.is_zipfile(result)

    def test_zip_contains_all_expected_files(self, admin_user):
        """The generated ZIP contains all expected JSON files."""
        import db
        from routes.users import build_user_export_zip
        user = db.get_user_by_id(admin_user)
        result = build_user_export_zip(user)
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
        assert "account.json" in names
        assert "username_history.json" in names
        assert "contributions.json" in names
        assert "drafts.json" in names
        assert "accessibility.json" in names

    def test_account_json_excludes_password(self, admin_user):
        """account.json must not contain the password hash."""
        import db
        from routes.users import build_user_export_zip
        user = db.get_user_by_id(admin_user)
        result = build_user_export_zip(user)
        with zipfile.ZipFile(result) as zf:
            account = json.loads(zf.read("account.json"))
        assert "password" not in account

    def test_account_json_contains_correct_username(self, admin_user):
        """account.json should reflect the correct username."""
        import db
        from routes.users import build_user_export_zip
        user = db.get_user_by_id(admin_user)
        result = build_user_export_zip(user)
        with zipfile.ZipFile(result) as zf:
            account = json.loads(zf.read("account.json"))
        assert account["username"] == "admin"

    def test_account_json_contains_all_fields(self, admin_user):
        """account.json should include all required fields."""
        import db
        from routes.users import build_user_export_zip
        user = db.get_user_by_id(admin_user)
        result = build_user_export_zip(user)
        with zipfile.ZipFile(result) as zf:
            account = json.loads(zf.read("account.json"))
        expected_fields = {"id", "username", "role", "suspended", "invite_code",
                           "created_at", "last_login_at", "is_superuser"}
        assert expected_fields == set(account.keys())

    def test_zip_for_different_users_differ(self, admin_user, regular_user):
        """ZIPs built for different users contain different usernames."""
        import db
        from routes.users import build_user_export_zip
        admin = db.get_user_by_id(admin_user)
        regular = db.get_user_by_id(regular_user)
        admin_zip = build_user_export_zip(admin)
        regular_zip = build_user_export_zip(regular)
        with zipfile.ZipFile(admin_zip) as zf:
            admin_account = json.loads(zf.read("account.json"))
        with zipfile.ZipFile(regular_zip) as zf:
            regular_account = json.loads(zf.read("account.json"))
        assert admin_account["username"] == "admin"
        assert regular_account["username"] == "regular1"
        assert admin_account["role"] == "admin"
        assert regular_account["role"] == "user"


# ===========================================================================
# 2. Route integration tests for user export
# ===========================================================================

class TestUserExportRoute:
    """Test the user self-export route uses the shared function correctly."""

    def test_self_export_returns_200_zip(self, logged_in_admin):
        """GET /settings/export returns 200 with ZIP content type."""
        resp = logged_in_admin.get("/settings/export")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"

    def test_self_export_zip_is_valid(self, logged_in_admin):
        """GET /settings/export returns a valid ZIP file."""
        resp = logged_in_admin.get("/settings/export")
        buf = io.BytesIO(resp.data)
        assert zipfile.is_zipfile(buf)

    def test_self_export_contains_expected_files(self, logged_in_admin):
        """Self-export ZIP contains all expected files."""
        resp = logged_in_admin.get("/settings/export")
        buf = io.BytesIO(resp.data)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        assert "account.json" in names
        assert "username_history.json" in names
        assert "contributions.json" in names
        assert "drafts.json" in names
        assert "accessibility.json" in names

    def test_self_export_has_correct_username(self, logged_in_admin):
        """Self-export account.json has the correct username."""
        resp = logged_in_admin.get("/settings/export")
        buf = io.BytesIO(resp.data)
        with zipfile.ZipFile(buf) as zf:
            account = json.loads(zf.read("account.json"))
        assert account["username"] == "admin"

    def test_self_export_requires_login(self, client, admin_user):
        """GET /settings/export redirects to login when not authenticated."""
        resp = client.get("/settings/export")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# ===========================================================================
# 3. Route integration tests for admin export
# ===========================================================================

class TestAdminExportRoute:
    """Test the admin export route uses the shared function correctly."""

    def test_admin_export_returns_200_zip(self, logged_in_admin, regular_user):
        """Admin export of another user returns 200 with ZIP content type."""
        resp = logged_in_admin.get(f"/admin/users/{regular_user}/export")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"

    def test_admin_export_zip_is_valid(self, logged_in_admin, regular_user):
        """Admin export returns a valid ZIP file."""
        resp = logged_in_admin.get(f"/admin/users/{regular_user}/export")
        buf = io.BytesIO(resp.data)
        assert zipfile.is_zipfile(buf)

    def test_admin_export_contains_target_user_data(self, logged_in_admin, regular_user):
        """Admin export ZIP contains the target user's data, not the admin's."""
        resp = logged_in_admin.get(f"/admin/users/{regular_user}/export")
        buf = io.BytesIO(resp.data)
        with zipfile.ZipFile(buf) as zf:
            account = json.loads(zf.read("account.json"))
        assert account["username"] == "regular1"
        assert account["role"] == "user"

    def test_admin_export_nonexistent_user_404(self, logged_in_admin):
        """Admin export for non-existent user returns 404."""
        resp = logged_in_admin.get("/admin/users/nonexistent_id_xyz/export")
        assert resp.status_code == 404

    def test_admin_export_requires_admin_role(self, logged_in_regular, regular_user):
        """Regular users cannot use the admin export route."""
        resp = logged_in_regular.get(f"/admin/users/{regular_user}/export")
        assert resp.status_code in (302, 403)

    def test_admin_export_editor_cannot_access(self, logged_in_editor, regular_user):
        """Editors cannot use the admin export route."""
        resp = logged_in_editor.get(f"/admin/users/{regular_user}/export")
        assert resp.status_code in (302, 403)

    def test_admin_export_contains_all_files(self, logged_in_admin, editor_user):
        """Admin export ZIP contains all expected files."""
        resp = logged_in_admin.get(f"/admin/users/{editor_user}/export")
        buf = io.BytesIO(resp.data)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        assert "account.json" in names
        assert "username_history.json" in names
        assert "contributions.json" in names
        assert "drafts.json" in names
        assert "accessibility.json" in names

    def test_admin_self_export_matches_user_export(self, logged_in_admin, admin_user):
        """Admin exporting own data via admin route matches self-export route data."""
        self_resp = logged_in_admin.get("/settings/export")
        admin_resp = logged_in_admin.get(f"/admin/users/{admin_user}/export")
        assert self_resp.status_code == 200
        assert admin_resp.status_code == 200

        with zipfile.ZipFile(io.BytesIO(self_resp.data)) as zf:
            self_account = json.loads(zf.read("account.json"))
        with zipfile.ZipFile(io.BytesIO(admin_resp.data)) as zf:
            admin_account = json.loads(zf.read("account.json"))

        # Should contain same data since same user
        assert self_account["username"] == admin_account["username"]
        assert self_account["role"] == admin_account["role"]
        assert self_account["id"] == admin_account["id"]


# ===========================================================================
# 4. Module-level import and structure verification
# ===========================================================================

class TestModuleStructure:
    """Verify module imports, route registration, and structure are intact."""

    def test_all_route_modules_import_cleanly(self):
        """Every route module imports without errors."""
        from routes.auth import register_auth_routes
        from routes.wiki import register_wiki_routes
        from routes.users import register_user_routes
        from routes.admin import register_admin_routes
        from routes.chat import register_chat_routes
        from routes.groups import register_group_routes
        from routes.api import register_api_routes
        from routes.uploads import register_upload_routes
        from routes.errors import register_error_handlers
        assert all(callable(f) for f in [
            register_auth_routes, register_wiki_routes, register_user_routes,
            register_admin_routes, register_chat_routes, register_group_routes,
            register_api_routes, register_upload_routes, register_error_handlers,
        ])

    def test_all_helper_modules_import_cleanly(self):
        """Every helper module imports without errors."""
        from helpers import _constants, _rate_limiting, _markdown, _diff
        from helpers import _text, _validation, _auth, _time
        assert all(isinstance(m, types.ModuleType) for m in [
            _constants, _rate_limiting, _markdown, _diff,
            _text, _validation, _auth, _time,
        ])

    def test_all_db_modules_import_cleanly(self):
        """Every db module imports without errors."""
        from db import _connection, _schema, _users, _invites, _categories
        from db import _pages, _drafts, _settings, _announcements, _migration
        from db import _profiles, _chats, _groups, _audit
        assert all(isinstance(m, types.ModuleType) for m in [
            _connection, _schema, _users, _invites, _categories,
            _pages, _drafts, _settings, _announcements, _migration,
            _profiles, _chats, _groups, _audit,
        ])

    def test_no_duplicate_build_user_export_zip_in_admin(self):
        """routes/admin.py must not contain its own definition of build_user_export_zip."""
        admin_path = os.path.join(os.path.dirname(__file__), "..", "routes", "admin.py")
        with open(admin_path, "r") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name != "_build_user_export_zip", \
                    "routes/admin.py still has a local _build_user_export_zip definition"
                if node.name == "build_user_export_zip":
                    # Should not be defined here; only imported
                    pytest.fail("routes/admin.py defines build_user_export_zip locally; it should import it")

    def test_admin_imports_from_routes_users(self):
        """routes/admin.py imports build_user_export_zip from routes.users."""
        admin_path = os.path.join(os.path.dirname(__file__), "..", "routes", "admin.py")
        with open(admin_path, "r") as f:
            source = f.read()
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "routes.users":
                    for alias in node.names:
                        if alias.name == "build_user_export_zip":
                            found = True
        assert found, "routes/admin.py does not import build_user_export_zip from routes.users"

    def test_flask_app_has_expected_route_count(self):
        """The Flask app registers a reasonable number of URL rules."""
        from app import app
        rules = list(app.url_map.iter_rules())
        # Should have 100+ rules (auth, wiki, admin, chat, groups, api, uploads, errors)
        assert len(rules) >= 80, f"Only {len(rules)} routes registered, expected 80+"

    def test_register_all_routes_function_exists(self):
        """routes/__init__.py exposes register_all_routes."""
        from routes import register_all_routes
        assert callable(register_all_routes)


# ===========================================================================
# 5. Cross-route feature consistency tests
# ===========================================================================

class TestCrossRouteConsistency:
    """Verify features work consistently across related routes."""

    def test_login_and_access_admin_panel(self, logged_in_admin):
        """Admin can access the admin users page."""
        resp = logged_in_admin.get("/admin/users")
        assert resp.status_code == 200

    def test_login_and_access_user_settings(self, logged_in_admin):
        """Admin can access account settings."""
        resp = logged_in_admin.get("/settings")
        assert resp.status_code == 200

    def test_chat_list_accessible(self, logged_in_admin):
        """Chat list page is accessible to logged-in users."""
        resp = logged_in_admin.get("/chats")
        assert resp.status_code == 200

    def test_group_list_accessible(self, logged_in_admin):
        """Group list page is accessible to logged-in users."""
        resp = logged_in_admin.get("/groups")
        assert resp.status_code == 200

    def test_users_list_accessible(self, logged_in_admin):
        """Users/People list page is accessible to logged-in users."""
        resp = logged_in_admin.get("/users")
        assert resp.status_code == 200

    def test_home_page_accessible(self, logged_in_admin):
        """Home page is accessible after login."""
        resp = logged_in_admin.get("/")
        assert resp.status_code == 200

    def test_admin_settings_accessible(self, logged_in_admin):
        """Admin settings page is accessible."""
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200

    def test_admin_codes_accessible(self, logged_in_admin):
        """Admin invite codes page is accessible."""
        resp = logged_in_admin.get("/admin/codes")
        assert resp.status_code == 200

    def test_admin_announcements_accessible(self, logged_in_admin):
        """Admin announcements page is accessible."""
        resp = logged_in_admin.get("/admin/announcements")
        assert resp.status_code == 200

    def test_admin_chats_accessible(self, logged_in_admin):
        """Admin chats monitoring page is accessible."""
        resp = logged_in_admin.get("/admin/chats")
        assert resp.status_code == 200

    def test_admin_groups_accessible(self, logged_in_admin):
        """Admin groups monitoring page is accessible."""
        resp = logged_in_admin.get("/admin/groups")
        assert resp.status_code == 200

    def test_admin_migration_accessible(self, logged_in_admin):
        """Admin migration page is accessible."""
        resp = logged_in_admin.get("/admin/migration")
        assert resp.status_code == 200

    def test_create_page_accessible(self, logged_in_admin):
        """Create page form is accessible to admin/editor."""
        resp = logged_in_admin.get("/create-page")
        assert resp.status_code == 200

    def test_api_preview_endpoint(self, logged_in_admin):
        """API preview endpoint works."""
        resp = logged_in_admin.post("/api/preview",
                                    data=json.dumps({"content": "**bold**"}),
                                    content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "html" in data

    def test_api_search_endpoint(self, logged_in_admin):
        """API search endpoint works."""
        resp = logged_in_admin.get("/api/pages/search?q=test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_404_page_renders_correctly(self, logged_in_admin):
        """404 error page renders without crashing."""
        resp = logged_in_admin.get("/page/nonexistent-slug-xyz")
        assert resp.status_code == 404

    def test_page_creation_and_viewing(self, logged_in_admin):
        """Full page creation and viewing flow works."""
        resp = logged_in_admin.post("/create-page", data={
            "title": "Sync Test Page",
            "content": "# Test\nHello world",
            "category_id": "",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Sync Test Page" in resp.data

    def test_page_edit_flow(self, logged_in_admin):
        """Page edit flow works end-to-end."""
        import db
        db.create_page("Edit Test", "edit-test", "v1", None, None)
        resp = logged_in_admin.get("/page/edit-test/edit")
        assert resp.status_code == 200
        resp = logged_in_admin.post("/page/edit-test/edit", data={
            "title": "Edit Test Updated",
            "content": "v2 content",
            "edit_message": "test update",
        }, follow_redirects=True)
        assert resp.status_code == 200


# ===========================================================================
# 6. Template existence verification
# ===========================================================================

class TestTemplateExistence:
    """Verify all templates referenced in route modules exist."""

    @pytest.fixture(autouse=True)
    def templates_dir(self):
        """Locate the templates directory."""
        self.tpl_dir = os.path.join(os.path.dirname(__file__), "..", "app", "templates")

    def test_auth_templates_exist(self):
        """All authentication templates exist."""
        for tpl in ["login.html", "signup.html", "setup.html",
                     "maintenance.html", "admin_login.html",
                     "session_conflict.html"]:
            path = os.path.join(self.tpl_dir, "auth", tpl)
            assert os.path.isfile(path), f"Missing template: auth/{tpl}"

    def test_wiki_templates_exist(self):
        """All wiki templates exist."""
        for tpl in ["page.html", "edit.html", "create_page.html",
                     "history.html", "history_entry.html", "announcement.html",
                     "403.html", "404.html", "429.html", "500.html"]:
            path = os.path.join(self.tpl_dir, "wiki", tpl)
            assert os.path.isfile(path), f"Missing template: wiki/{tpl}"

    def test_admin_templates_exist(self):
        """All admin templates exist."""
        for tpl in ["users.html", "codes.html", "codes_expired.html",
                     "settings.html", "announcements.html", "audit.html",
                     "editor_access.html", "migration.html",
                     "chats.html", "chat_view.html", "groups.html", "group_view.html"]:
            path = os.path.join(self.tpl_dir, "admin", tpl)
            assert os.path.isfile(path), f"Missing template: admin/{tpl}"

    def test_chat_templates_exist(self):
        """All chat templates exist."""
        for tpl in ["list.html", "new.html", "chat.html"]:
            path = os.path.join(self.tpl_dir, "chats", tpl)
            assert os.path.isfile(path), f"Missing template: chats/{tpl}"

    def test_group_templates_exist(self):
        """All group chat templates exist."""
        for tpl in ["list.html", "new.html", "join.html", "chat.html"]:
            path = os.path.join(self.tpl_dir, "groups", tpl)
            assert os.path.isfile(path), f"Missing template: groups/{tpl}"

    def test_user_templates_exist(self):
        """All user templates exist."""
        for tpl in ["list.html", "profile.html"]:
            path = os.path.join(self.tpl_dir, "users", tpl)
            assert os.path.isfile(path), f"Missing template: users/{tpl}"

    def test_account_templates_exist(self):
        """Account settings template exists."""
        path = os.path.join(self.tpl_dir, "account", "settings.html")
        assert os.path.isfile(path), "Missing template: account/settings.html"

    def test_base_template_exists(self):
        """Base layout template exists."""
        path = os.path.join(self.tpl_dir, "base.html")
        assert os.path.isfile(path), "Missing template: base.html"

    def test_announcements_bar_exists(self):
        """Announcements bar partial exists."""
        path = os.path.join(self.tpl_dir, "_announcements_bar.html")
        assert os.path.isfile(path), "Missing template: _announcements_bar.html"


# ===========================================================================
# 7. DB function availability tests
# ===========================================================================

class TestDbFunctionAvailability:
    """Verify all db functions used in route modules are properly exported."""

    def test_user_related_functions(self):
        """All user-related db functions are available."""
        import db
        fns = ["create_user", "get_user_by_id", "get_user_by_username",
               "update_user", "delete_user", "list_users", "count_admins"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))

    def test_page_related_functions(self):
        """All page-related db functions are available."""
        import db
        fns = ["create_page", "get_page", "get_page_by_slug", "update_page",
               "delete_page", "get_home_page", "get_page_history",
               "get_history_entry", "search_pages", "get_category_tree"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))

    def test_chat_related_functions(self):
        """All chat-related db functions are available."""
        import db
        fns = ["get_user_chats", "get_chat_by_id", "is_chat_participant",
               "send_chat_message", "get_chat_messages",
               "get_or_create_chat", "is_user_chat_disabled"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))

    def test_group_related_functions(self):
        """All group-related db functions are available."""
        import db
        fns = ["get_user_groups", "create_group_chat", "get_group_chat",
               "is_group_member", "send_group_message", "get_group_messages",
               "add_group_member", "remove_group_member",
               "get_group_member_role", "is_group_member_banned"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))

    def test_export_related_functions(self):
        """All functions used by build_user_export_zip are available."""
        import db
        fns = ["get_username_history", "get_user_contributions",
               "list_user_drafts", "get_user_accessibility"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))

    def test_settings_functions(self):
        """Site settings db functions are available."""
        import db
        fns = ["get_site_settings", "update_site_settings"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))

    def test_profile_functions(self):
        """User profile db functions are available."""
        import db
        fns = ["get_user_profile", "upsert_user_profile",
               "list_published_profiles", "list_all_users_with_profiles"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))

    def test_announcement_functions(self):
        """Announcement db functions are available."""
        import db
        fns = ["get_active_announcements", "list_announcements",
               "create_announcement", "get_announcement",
               "update_announcement", "delete_announcement"]
        for fn_name in fns:
            assert hasattr(db, fn_name), f"db.{fn_name} not found"
            assert callable(getattr(db, fn_name))


# ===========================================================================
# 8. Helper function availability tests
# ===========================================================================

class TestHelperFunctionAvailability:
    """Verify all helper functions used across route modules are exported."""

    def test_auth_helpers(self):
        """Authentication helpers are available."""
        from helpers import (login_required, editor_required, admin_required,
                             get_current_user, editor_has_category_access)
        assert all(callable(f) for f in [
            login_required, editor_required, admin_required,
            get_current_user, editor_has_category_access,
        ])

    def test_validation_helpers(self):
        """Validation helpers are available."""
        from helpers import (allowed_file, allowed_attachment,
                             _is_valid_hex_color, _is_valid_username, _safe_referrer)
        assert all(callable(f) for f in [
            allowed_file, allowed_attachment,
            _is_valid_hex_color, _is_valid_username, _safe_referrer,
        ])

    def test_markdown_helpers(self):
        """Markdown helpers are available."""
        from helpers import render_markdown
        assert callable(render_markdown)

    def test_diff_helpers(self):
        """Diff helpers are available."""
        from helpers import (compute_char_diff, compute_diff_html,
                             compute_formatted_diff_html)
        assert all(callable(f) for f in [
            compute_char_diff, compute_diff_html, compute_formatted_diff_html,
        ])

    def test_text_helpers(self):
        """Text processing helpers are available."""
        from helpers import slugify
        assert callable(slugify)

    def test_time_helpers(self):
        """Time/timezone helpers are available."""
        from helpers import (get_site_timezone, time_ago,
                             format_datetime, format_datetime_local_input)
        assert all(callable(f) for f in [
            get_site_timezone, time_ago, format_datetime, format_datetime_local_input,
        ])

    def test_rate_limit_helpers(self):
        """Rate limiting helpers are available."""
        from helpers import rate_limit, _rl_check
        assert callable(rate_limit)
        assert callable(_rl_check)

    def test_constants(self):
        """Shared constants are available."""
        from helpers import ALLOWED_TAGS, ALLOWED_ATTRS, ROLE_LABELS
        assert isinstance(ALLOWED_TAGS, (list, set, tuple, frozenset))
        assert isinstance(ALLOWED_ATTRS, dict)
        assert isinstance(ROLE_LABELS, dict)
