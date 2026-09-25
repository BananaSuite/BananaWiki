"""
Tests for BananaWiki modular structure.

Verifies that:
- All packages (helpers, db, routes) import cleanly with no errors.
- Each package correctly re-exports its public API.
- Every public function and route handler carries a docstring.
- The core logic in each module works correctly end-to-end via the Flask test
  client (module boundaries are not broken by the refactoring).
"""

import ast
import os
import sys
import types

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
def editor_user():
    """Create an editor user (setup must already be done by another fixture)."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor1", generate_password_hash("editor123"), role="editor")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a test client already authenticated as the admin user."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


# ===========================================================================
# 1. Package-level import tests
# ===========================================================================

class TestHelperPackageImports:
    """helpers/ package imports cleanly and re-exports all public names."""

    def test_helpers_package_imports(self):
        """``import helpers`` succeeds without errors."""
        import helpers  # noqa: F401
        assert isinstance(helpers, types.ModuleType)

    def test_helpers_submodules_importable(self):
        """Every helpers sub-module is importable individually."""
        from helpers import _constants, _rate_limiting, _markdown, _diff, _text, _validation, _auth, _time
        for mod in [_constants, _rate_limiting, _markdown, _diff, _text, _validation, _auth, _time]:
            assert isinstance(mod, types.ModuleType)

    def test_helpers_reexports_constants(self):
        """helpers package re-exports ALLOWED_TAGS, ROLE_LABELS, _DUMMY_HASH."""
        import helpers
        assert hasattr(helpers, "ALLOWED_TAGS")
        assert hasattr(helpers, "ROLE_LABELS")
        assert hasattr(helpers, "_DUMMY_HASH")

    def test_helpers_reexports_rate_limiting(self):
        """helpers package re-exports _rl_check, rate_limit, _RL_LOCK, _RL_STORE."""
        import helpers
        assert hasattr(helpers, "_rl_check")
        assert hasattr(helpers, "rate_limit")
        assert hasattr(helpers, "_RL_LOCK")
        assert hasattr(helpers, "_RL_STORE")

    def test_helpers_reexports_markdown(self):
        """helpers package re-exports render_markdown."""
        import helpers
        assert hasattr(helpers, "render_markdown")

    def test_helpers_reexports_diff(self):
        """helpers package re-exports compute_char_diff, compute_diff_html, compute_formatted_diff_html."""
        import helpers
        assert hasattr(helpers, "compute_char_diff")
        assert hasattr(helpers, "compute_diff_html")
        assert hasattr(helpers, "compute_formatted_diff_html")

    def test_helpers_reexports_text(self):
        """helpers package re-exports slugify."""
        import helpers
        assert hasattr(helpers, "slugify")

    def test_helpers_reexports_validation(self):
        """helpers package re-exports allowed_file, allowed_attachment, _is_valid_username."""
        import helpers
        assert hasattr(helpers, "allowed_file")
        assert hasattr(helpers, "allowed_attachment")
        assert hasattr(helpers, "_is_valid_username")

    def test_helpers_reexports_auth(self):
        """helpers package re-exports login_required, editor_required, admin_required, get_current_user."""
        import helpers
        assert hasattr(helpers, "login_required")
        assert hasattr(helpers, "editor_required")
        assert hasattr(helpers, "admin_required")
        assert hasattr(helpers, "get_current_user")

    def test_helpers_reexports_time(self):
        """helpers package re-exports time_ago, format_datetime, get_site_timezone."""
        import helpers
        assert hasattr(helpers, "time_ago")
        assert hasattr(helpers, "format_datetime")
        assert hasattr(helpers, "get_site_timezone")


class TestDbPackageImports:
    """db/ package imports cleanly and re-exports all public names."""

    def test_db_package_imports(self):
        """``import db`` succeeds without errors."""
        import db
        assert isinstance(db, types.ModuleType)

    def test_db_reexports_connection(self):
        """db package re-exports get_db."""
        import db
        assert hasattr(db, "get_db")

    def test_db_reexports_schema(self):
        """db package re-exports init_db."""
        import db
        assert hasattr(db, "init_db")

    def test_db_reexports_users(self):
        """db package re-exports user CRUD functions."""
        import db
        for name in ("create_user", "get_user_by_id", "get_user_by_username",
                     "update_user", "delete_user", "list_users", "count_admins"):
            assert hasattr(db, name), f"db.{name} is missing"

    def test_db_reexports_settings(self):
        """db package re-exports get_site_settings and update_site_settings."""
        import db
        assert hasattr(db, "get_site_settings")
        assert hasattr(db, "update_site_settings")

    def test_db_reexports_categories(self):
        """db package re-exports category functions."""
        import db
        for name in ("create_category", "get_category", "update_category",
                     "list_categories", "get_category_tree", "delete_category"):
            assert hasattr(db, name), f"db.{name} is missing"

    def test_db_reexports_pages(self):
        """db package re-exports page CRUD functions."""
        import db
        for name in ("create_page", "get_page", "get_page_by_slug", "get_home_page",
                     "update_page", "update_page_title", "update_page_category",
                     "update_page_tag", "delete_page", "get_page_history", "get_history_entry"):
            assert hasattr(db, name), f"db.{name} is missing"

    def test_db_reexports_drafts(self):
        """db package re-exports draft management functions."""
        import db
        for name in ("save_draft", "get_draft", "get_drafts_for_page",
                     "delete_draft", "transfer_draft"):
            assert hasattr(db, name), f"db.{name} is missing"

    def test_db_reexports_invites(self):
        """db package re-exports invite code functions."""
        import db
        for name in ("generate_invite_code", "validate_invite_code",
                     "use_invite_code", "delete_invite_code", "list_invite_codes"):
            assert hasattr(db, name), f"db.{name} is missing"

    def test_db_reexports_announcements(self):
        """db package re-exports announcement functions."""
        import db
        for name in ("create_announcement", "get_announcement",
                     "list_announcements", "update_announcement", "delete_announcement"):
            assert hasattr(db, name), f"db.{name} is missing"


class TestRoutesPackageImports:
    """routes/ package imports cleanly and exposes register_all_routes."""

    def test_routes_package_imports(self):
        """``import routes`` succeeds without errors."""
        import routes
        assert isinstance(routes, types.ModuleType)

    def test_routes_register_all_routes_callable(self):
        """routes.register_all_routes is callable."""
        import routes
        assert callable(routes.register_all_routes)

    def test_routes_submodule_register_functions_callable(self):
        """Every routes sub-module exposes a callable register_*_routes function."""
        from routes.auth import register_auth_routes
        from routes.wiki import register_wiki_routes
        from routes.admin import register_admin_routes
        from routes.api import register_api_routes
        from routes.chat import register_chat_routes
        from routes.groups import register_group_routes
        from routes.uploads import register_upload_routes
        from routes.users import register_user_routes
        from routes.errors import register_error_handlers
        for fn in (register_auth_routes, register_wiki_routes, register_admin_routes,
                   register_api_routes, register_chat_routes, register_group_routes,
                   register_upload_routes, register_user_routes, register_error_handlers):
            assert callable(fn)


# ===========================================================================
# 2. Docstring completeness tests
# ===========================================================================

def _collect_missing_docstrings(filepath):
    """Return a list of (lineno, kind, name) for functions/classes missing docstrings."""
    with open(filepath) as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return []
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            has_doc = (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            )
            if not has_doc:
                missing.append((node.lineno, type(node).__name__, node.name))
    return missing


def _python_files_in(directory):
    """Yield all .py file paths in *directory* (non-recursive)."""
    for fname in sorted(os.listdir(directory)):
        if fname.endswith(".py"):
            yield os.path.join(directory, fname)


PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")

DOCSTRING_DIRS = ["helpers", "db", "routes"]


@pytest.mark.parametrize("dirpath", DOCSTRING_DIRS)
def test_no_missing_docstrings_in_package(dirpath):
    """Every function and class in the named package has a docstring."""
    full_dir = os.path.join(PROJECT_ROOT, dirpath)
    all_missing = []
    for filepath in _python_files_in(full_dir):
        missing = _collect_missing_docstrings(filepath)
        for lineno, kind, name in missing:
            rel = os.path.relpath(filepath, PROJECT_ROOT)
            all_missing.append(f"{rel}:{lineno}: {kind} '{name}' has no docstring")
    assert not all_missing, "Missing docstrings:\n" + "\n".join(all_missing)


# ===========================================================================
# 3. Module-level functionality tests (helpers logic)
# ===========================================================================

class TestHelpersLogic:
    """Core helper functions behave correctly after modular extraction."""

    def test_slugify_basic(self):
        """slugify converts a title to a lowercase hyphenated slug."""
        from helpers._text import slugify
        assert slugify("Hello World") == "hello-world"

    def test_slugify_special_chars(self):
        """slugify strips punctuation and symbols, preserving Unicode word chars."""
        from helpers._text import slugify
        # Ampersand and dot are stripped; accented letters are kept (Python \w is Unicode-aware)
        result = slugify("Café & Co.")
        assert "&" not in result
        assert "." not in result
        assert "café" in result or "caf" in result

    def test_slugify_empty_falls_back_to_page(self):
        """slugify returns 'page' when the result would otherwise be empty."""
        from helpers._text import slugify
        assert slugify("!!!") == "page"

    def test_is_valid_username_valid(self):
        """_is_valid_username accepts letters, digits, underscores, hyphens."""
        from helpers._validation import _is_valid_username
        assert _is_valid_username("User_Name-123")

    def test_is_valid_username_rejects_spaces(self):
        """_is_valid_username rejects usernames containing spaces."""
        from helpers._validation import _is_valid_username
        assert not _is_valid_username("user name")

    def test_is_valid_username_rejects_special_chars(self):
        """_is_valid_username rejects usernames with special characters."""
        from helpers._validation import _is_valid_username
        assert not _is_valid_username("user@name")

    def test_is_valid_hex_color_valid(self):
        """_is_valid_hex_color accepts a 6-digit hex color."""
        from helpers._validation import _is_valid_hex_color
        assert _is_valid_hex_color("#aabbcc")
        assert _is_valid_hex_color("#AABBCC")

    def test_is_valid_hex_color_invalid(self):
        """_is_valid_hex_color rejects invalid color strings."""
        from helpers._validation import _is_valid_hex_color
        assert not _is_valid_hex_color("aabbcc")  # missing #
        assert not _is_valid_hex_color("#abc")     # 3-digit

    def test_compute_char_diff_insert(self):
        """compute_char_diff counts added/deleted characters correctly."""
        from helpers._diff import compute_char_diff
        added, deleted = compute_char_diff("hello", "hello world")
        assert added == 6
        assert deleted == 0

    def test_compute_char_diff_delete(self):
        """compute_char_diff reports deleted characters."""
        from helpers._diff import compute_char_diff
        added, deleted = compute_char_diff("hello world", "hello")
        assert added == 0
        assert deleted == 6

    def test_compute_char_diff_replace(self):
        """compute_char_diff handles replacements."""
        from helpers._diff import compute_char_diff
        added, deleted = compute_char_diff("cat", "dog")
        assert added == 3
        assert deleted == 3

    def test_render_markdown_basic(self):
        """render_markdown converts markdown to sanitised HTML."""
        from helpers._markdown import render_markdown
        html = render_markdown("**bold**")
        assert "<strong>bold</strong>" in html

    def test_render_markdown_sanitises_scripts(self):
        """render_markdown strips <script> tags (Bleach sanitisation)."""
        from helpers._markdown import render_markdown
        html = render_markdown('<script>alert("xss")</script>')
        assert "<script>" not in html

    def test_time_ago_recent(self):
        """time_ago returns 'just now' for a very recent timestamp."""
        from helpers._time import time_ago
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        assert time_ago(recent) == "just now"

    def test_time_ago_minutes(self):
        """time_ago returns a minutes-ago string for a 2-minute-old timestamp."""
        from helpers._time import time_ago
        from datetime import datetime, timezone, timedelta
        two_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        result = time_ago(two_min_ago)
        assert "minute" in result

    def test_time_ago_never_for_none(self):
        """time_ago returns 'never' when passed None."""
        from helpers._time import time_ago
        assert time_ago(None) == "never"

    def test_rate_limit_rl_check_allows(self):
        """_rl_check allows requests within the configured limit."""
        from helpers._rate_limiting import _rl_check, _RL_STORE, _RL_LOCK
        with _RL_LOCK:
            _RL_STORE.clear()
        for _ in range(5):
            assert _rl_check("10.0.0.1", "test_allow", 5, 60) is True

    def test_rate_limit_rl_check_blocks(self):
        """_rl_check blocks once the limit is exceeded."""
        from helpers._rate_limiting import _rl_check, _RL_STORE, _RL_LOCK
        with _RL_LOCK:
            _RL_STORE.clear()
        for _ in range(5):
            _rl_check("10.0.0.2", "test_block", 5, 60)
        assert _rl_check("10.0.0.2", "test_block", 5, 60) is False


# ===========================================================================
# 4. DB module-level functionality tests
# ===========================================================================

class TestDbModuleLogic:
    """Core DB functions work correctly after modular extraction."""

    def test_create_and_retrieve_user(self):
        """create_user persists a user that can be retrieved by id and username."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("testuser", generate_password_hash("pw"), role="user")
        assert uid
        row_by_id = db.get_user_by_id(uid)
        assert row_by_id is not None
        assert row_by_id["username"] == "testuser"
        row_by_name = db.get_user_by_username("testuser")
        assert row_by_name is not None
        assert row_by_name["id"] == uid

    def test_update_user(self):
        """update_user modifies only the specified column."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("u2", generate_password_hash("pw"), role="user")
        db.update_user(uid, suspended=1)
        row = db.get_user_by_id(uid)
        assert row["suspended"] == 1

    def test_delete_user(self):
        """delete_user removes the user from the database."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("u3", generate_password_hash("pw"), role="user")
        db.delete_user(uid)
        assert db.get_user_by_id(uid) is None

    def test_list_users_and_count_admins(self):
        """list_users returns all users; count_admins counts active admin accounts."""
        from werkzeug.security import generate_password_hash
        import db
        db.create_user("u4", generate_password_hash("pw"), role="user")
        db.create_user("adm1", generate_password_hash("pw"), role="admin")
        users = db.list_users()
        assert len(users) == 2
        assert db.count_admins() == 1

    def test_list_users_role_filter(self):
        """list_users(role_filter=...) returns only users with the given role."""
        from werkzeug.security import generate_password_hash
        import db
        db.create_user("u5", generate_password_hash("pw"), role="user")
        db.create_user("e1", generate_password_hash("pw"), role="editor")
        editors = db.list_users(role_filter="editor")
        assert all(u["role"] == "editor" for u in editors)
        assert len(editors) == 1

    def test_site_settings_default_and_update(self):
        """get_site_settings returns a row; update_site_settings persists changes."""
        import db
        settings = db.get_site_settings()
        assert settings is not None
        db.update_site_settings(site_name="TestWiki")
        updated = db.get_site_settings()
        assert updated["site_name"] == "TestWiki"

    def test_update_site_settings_rejects_invalid_column(self):
        """update_site_settings raises ValueError for unknown column names."""
        import db
        with pytest.raises(ValueError, match="Invalid column"):
            db.update_site_settings(nonexistent_column="value")

    def test_create_and_get_category(self):
        """create_category persists a category; get_category retrieves it."""
        import db
        cat_id = db.create_category("Docs")
        cat = db.get_category(cat_id)
        assert cat is not None
        assert cat["name"] == "Docs"

    def test_list_categories(self):
        """list_categories returns all categories in order."""
        import db
        db.create_category("Alpha")
        db.create_category("Beta")
        cats = db.list_categories()
        names = [c["name"] for c in cats]
        assert "Alpha" in names
        assert "Beta" in names

    def test_update_category(self):
        """update_category renames an existing category."""
        import db
        cat_id = db.create_category("Old Name")
        db.update_category(cat_id, "New Name")
        cat = db.get_category(cat_id)
        assert cat["name"] == "New Name"

    def test_create_and_get_page(self):
        """create_page persists a page; get_page and get_page_by_slug retrieve it."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("author", generate_password_hash("pw"), role="editor")
        page_id = db.create_page("Test Page", "test-page", "## Hello", user_id=uid)
        page_by_id = db.get_page(page_id)
        assert page_by_id is not None
        assert page_by_id["title"] == "Test Page"
        page_by_slug = db.get_page_by_slug("test-page")
        assert page_by_slug is not None
        assert page_by_slug["id"] == page_id

    def test_update_page(self):
        """update_page changes content and records a history entry."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("ed", generate_password_hash("pw"), role="editor")
        page_id = db.create_page("P", "p-slug", "Old content", user_id=uid)
        db.update_page(page_id, "P", "New content", uid, "Updated")
        page = db.get_page(page_id)
        assert page["content"] == "New content"
        history = db.get_page_history(page_id)
        assert len(history) >= 2  # creation + update

    def test_delete_page(self):
        """delete_page removes the page by ID."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("deleter", generate_password_hash("pw"), role="editor")
        page_id = db.create_page("To Delete", "to-delete", user_id=uid)
        db.delete_page(page_id)
        assert db.get_page(page_id) is None

    def test_save_and_get_draft(self):
        """save_draft persists a draft; get_draft retrieves it."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("drafter", generate_password_hash("pw"), role="editor")
        page_id = db.create_page("Draft Page", "draft-page", user_id=uid)
        db.save_draft(page_id, uid, "Draft Title", "Draft content")
        draft = db.get_draft(page_id, uid)
        assert draft is not None
        assert draft["content"] == "Draft content"

    def test_delete_draft(self):
        """delete_draft removes a saved draft."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("dd", generate_password_hash("pw"), role="editor")
        page_id = db.create_page("DP", "dp", user_id=uid)
        db.save_draft(page_id, uid, "T", "C")
        db.delete_draft(page_id, uid)
        assert db.get_draft(page_id, uid) is None

    def test_generate_and_validate_invite_code(self):
        """generate_invite_code produces a code that passes validate_invite_code."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("inv_creator", generate_password_hash("pw"), role="admin")
        code = db.generate_invite_code(uid)
        assert code and "-" in code
        row = db.validate_invite_code(code)
        assert row is not None
        assert row["code"] == code

    def test_use_invite_code_marks_as_used(self):
        """use_invite_code marks the code as used and validate_invite_code returns None."""
        from werkzeug.security import generate_password_hash
        import db
        adm = db.create_user("adm_inv", generate_password_hash("pw"), role="admin")
        usr = db.create_user("usr_inv", generate_password_hash("pw"), role="user")
        code = db.generate_invite_code(adm)
        result = db.use_invite_code(code, usr)
        assert result is True
        assert db.validate_invite_code(code) is None

    def test_create_and_get_announcement(self):
        """create_announcement persists; get_announcement retrieves it."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("ann_creator", generate_password_hash("pw"), role="admin")
        ann_id = db.create_announcement("Hello!", "orange", "normal", "both", None, uid)
        ann = db.get_announcement(ann_id)
        assert ann is not None
        assert ann["content"] == "Hello!"

    def test_delete_announcement(self):
        """delete_announcement removes the announcement."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("ann_del", generate_password_hash("pw"), role="admin")
        ann_id = db.create_announcement("Bye!", "blue", "normal", "both", None, uid)
        db.delete_announcement(ann_id)
        assert db.get_announcement(ann_id) is None

    def test_update_page_tag_valid(self):
        """update_page_tag sets a valid difficulty tag on a page."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("tagger", generate_password_hash("pw"), role="editor")
        page_id = db.create_page("Tag Page", "tag-page", user_id=uid)
        db.update_page_tag(page_id, "beginner")
        page = db.get_page(page_id)
        assert page["difficulty_tag"] == "beginner"

    def test_update_page_tag_invalid_raises(self):
        """update_page_tag raises ValueError for an unknown tag value."""
        from werkzeug.security import generate_password_hash
        import db
        uid = db.create_user("tagger2", generate_password_hash("pw"), role="editor")
        page_id = db.create_page("Tag Page 2", "tag-page-2", user_id=uid)
        with pytest.raises(ValueError):
            db.update_page_tag(page_id, "invalid_level")

    def test_update_site_settings_multiple_fields(self):
        """update_site_settings can update multiple columns in one call."""
        import db
        db.update_site_settings(site_name="Multi", timezone="US/Eastern")
        s = db.get_site_settings()
        assert s["site_name"] == "Multi"
        assert s["timezone"] == "US/Eastern"


# ===========================================================================
# 5. Integration / route tests that cross module boundaries
# ===========================================================================

class TestRouteIntegration:
    """Route handlers registered by the routes/ package work end-to-end."""

    def test_setup_route_accessible(self, client):
        """GET /setup returns 200 before setup is complete."""
        resp = client.get("/setup")
        assert resp.status_code == 200

    def test_setup_redirects_after_done(self, client):
        """GET /setup redirects to home once setup_done=1."""
        import db
        from werkzeug.security import generate_password_hash
        db.create_user("a", generate_password_hash("pw"), role="admin")
        db.update_site_settings(setup_done=1)
        resp = client.get("/setup")
        assert resp.status_code == 302

    def test_login_page_loads(self, client, admin_user):
        """GET /login returns 200."""
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_success(self, client, admin_user):
        """POST /login with valid credentials redirects to home."""
        resp = client.post("/login", data={"username": "admin", "password": "admin123"})
        assert resp.status_code == 302

    def test_login_failure_returns_200_with_error(self, client, admin_user):
        """POST /login with wrong password returns 200 and shows error message."""
        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert resp.status_code == 200
        assert b"Invalid" in resp.data

    def test_home_requires_login(self, client, admin_user):
        """GET / redirects to login when not authenticated."""
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_home_accessible_after_login(self, logged_in_admin):
        """GET / returns 200 for a logged-in admin."""
        resp = logged_in_admin.get("/")
        assert resp.status_code == 200

    def test_404_error_handler(self, logged_in_admin):
        """Requesting a nonexistent route returns a 404 response."""
        resp = logged_in_admin.get("/this/does/not/exist")
        assert resp.status_code == 404

    def test_create_page_route(self, logged_in_admin):
        """POST /create-page creates a new page and redirects."""
        resp = logged_in_admin.post("/create-page", data={
            "title": "My New Page",
            "content": "## Hello",
            "category_id": "",
        })
        assert resp.status_code == 302
        import db
        page = db.get_page_by_slug("my-new-page")
        assert page is not None
        assert page["title"] == "My New Page"

    def test_view_page_route(self, logged_in_admin):
        """GET /page/<slug> returns 200 for an existing page."""
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("author2", generate_password_hash("pw"), role="editor")
        db.create_page("View Me", "view-me", "Content", user_id=uid)
        resp = logged_in_admin.get("/page/view-me")
        assert resp.status_code == 200
        assert b"View Me" in resp.data

    def test_view_nonexistent_page_returns_404(self, logged_in_admin):
        """GET /page/no-such-page returns 404."""
        resp = logged_in_admin.get("/page/no-such-page")
        assert resp.status_code == 404

    def test_api_preview_endpoint(self, logged_in_admin):
        """POST /api/preview converts markdown and returns HTML JSON."""
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": "**bold text**"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None
        assert "<strong>bold text</strong>" in data["html"]

    def test_api_pages_search(self, logged_in_admin):
        """GET /api/pages/search returns matching pages as JSON."""
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("searcher", generate_password_hash("pw"), role="editor")
        db.create_page("Searchable Page", "searchable-page", user_id=uid)
        resp = logged_in_admin.get("/api/pages/search?q=searchable")
        assert resp.status_code == 200
        results = resp.get_json()
        assert any(r["slug"] == "searchable-page" for r in results)

    def test_admin_users_route(self, logged_in_admin):
        """GET /admin/users returns 200 for an admin."""
        resp = logged_in_admin.get("/admin/users")
        assert resp.status_code == 200

    def test_admin_codes_route(self, logged_in_admin):
        """GET /admin/codes returns 200 for an admin."""
        resp = logged_in_admin.get("/admin/codes")
        assert resp.status_code == 200

    def test_admin_settings_route(self, logged_in_admin):
        """GET /global-settings returns 200 for an admin."""
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200

    def test_admin_announcements_route(self, logged_in_admin):
        """GET /admin/announcements returns 200."""
        resp = logged_in_admin.get("/admin/announcements")
        assert resp.status_code == 200

    def test_create_category_route(self, logged_in_admin):
        """POST /category/create creates a new category."""
        import db
        resp = logged_in_admin.post("/category/create", data={"name": "NewCat", "parent_id": ""})
        assert resp.status_code == 302
        cats = db.list_categories()
        assert any(c["name"] == "NewCat" for c in cats)

    def test_logout_route(self, logged_in_admin):
        """POST /logout clears session and redirects to login."""
        resp = logged_in_admin.post("/logout")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        # After logout, home should redirect to login
        resp2 = logged_in_admin.get("/")
        assert resp2.status_code == 302

    def test_users_list_route(self, logged_in_admin):
        """GET /users returns 200 for a logged-in admin."""
        resp = logged_in_admin.get("/users")
        assert resp.status_code == 200

    def test_user_settings_route(self, logged_in_admin):
        """GET /settings returns 200 for a logged-in user."""
        resp = logged_in_admin.get("/settings")
        assert resp.status_code == 200
