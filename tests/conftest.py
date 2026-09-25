"""
Shared pytest fixtures for all test modules.

Provides isolated database, rate limit clearing, and authenticated client fixtures.
"""

import os
import sys

import sqlite3
import pytest

# Add parent directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


@pytest.fixture(autouse=True)
def isolated_hosting_health_watch(request, monkeypatch):
    """Provisioning unit tests must not leave recovery threads behind."""
    if request.node.path.name == "test_hosting_post_restart_recovery.py":
        return
    from hosting import instance_manager
    monkeypatch.setattr(instance_manager, "_spawn_post_restart_health_watch", lambda *args, **kwargs: None)


def _enable_all_builtin_plugins():
    """Enable all first-party plugins so feature-level tests work."""
    import db as db_mod
    from plugin_loader import discover_plugins
    for _dir, manifest, is_builtin in discover_plugins():
        if is_builtin:
            db_mod.register_plugin(
                manifest["id"],
                name=manifest.get("name", manifest["id"]),
                version=manifest.get("version", "0.0.0"),
                author=manifest.get("author", "BananaWiki"),
                description=manifest.get("description", ""),
                builtin=True,
                enabled=True,
            )


# Workaround: macOS Python 3.9 build lacks hashlib.scrypt, which werkzeug's
# generate_password_hash uses by default.  Fall back to pbkdf2:sha256 so
# tests don't fail at collection time on Apple hardware.
try:
    import hashlib
    hashlib.scrypt(b"t", salt=b"t", n=2, r=1, p=1, maxmem=132)
except (AttributeError, TypeError, ValueError):
    import werkzeug.security as _ws_scrypt
    _orig_generate_password_hash = _ws_scrypt.generate_password_hash

    def _portable_generate_password_hash(password, method="scrypt", salt_length=16):
        if method == "scrypt":
            method = "pbkdf2:sha256"
        return _orig_generate_password_hash(
            password,
            method=method,
            salt_length=salt_length,
        )

    _ws_scrypt.generate_password_hash = _portable_generate_password_hash


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a fresh temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
    # Enable all first-party plugins so existing tests continue to work
    _enable_all_builtin_plugins()
    # Ensure plugin routes are registered for the fresh database:
    # _enable_all_builtin_plugins only creates DB rows; the actual route
    # registration (trigger_plugin_enable) must be called separately so
    # templates that url_for() plugin endpoints (e.g. admin_announcements) work.
    import sys as _sys
    if 'app' in _sys.modules:
        import app as _app_mod
        from plugin_loader import get_loaded_plugins, trigger_plugin_enable
        _loaded_ids = set(get_loaded_plugins().keys())
        for _row in db_mod.list_plugins():
            if _row["enabled"] and _row["id"] not in _loaded_ids:
                trigger_plugin_enable(_row["id"], _app_mod.app)
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate limit store before and after each test."""
    # Import app module to access rate limit store
    try:
        import app as app_mod
        with app_mod._RL_LOCK:
            app_mod._RL_STORE.clear()
    except (ImportError, AttributeError):
        pass
    except sqlite3.OperationalError as exc:
        if "no such table: rate_limit_hits" not in str(exc):
            raise

    yield

    try:
        import app as app_mod
        with app_mod._RL_LOCK:
            app_mod._RL_STORE.clear()
    except (ImportError, AttributeError):
        pass
    except sqlite3.OperationalError as exc:
        # Recovery and data-reset tests intentionally leave an empty database.
        if "no such table: rate_limit_hits" not in str(exc):
            raise


@pytest.fixture
def client():
    """Flask test client with CSRF disabled for testing."""
    # Try routes-based app first, then fall back to monolithic app
    try:
        from app import app
    except ImportError:
        import app as app_mod
        app = app_mod.app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_user():
    """Create an admin user and mark setup as complete."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def editor_user():
    """Create an editor user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor", generate_password_hash("editor123"), role="editor")
    return uid


@pytest.fixture
def regular_user():
    """Create a regular (non-editor, non-admin) user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("user", generate_password_hash("user123"), role="user")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Flask client logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, editor_user):
    """Flask client logged in as editor."""
    client.post("/login", data={"username": "editor", "password": "editor123"})
    return client


@pytest.fixture
def logged_in_user(client, regular_user):
    """Flask client logged in as regular user."""
    client.post("/login", data={"username": "user", "password": "user123"})
    return client


@pytest.fixture(autouse=True)
def preserve_plugin_subscriptions(isolated_db):
    from bananawiki_sdk._hooks import _hook_registry, _hook_lock
    from bananawiki_sdk._slots import _slot_registry, _slot_lock
    with _hook_lock:
        hooks = {key: list(value) for key, value in _hook_registry.items()}
    with _slot_lock:
        slots = {key: list(value) for key, value in _slot_registry.items()}
    yield
    with _hook_lock:
        _hook_registry.clear()
        _hook_registry.update(hooks)
    with _slot_lock:
        _slot_registry.clear()
        _slot_registry.update(slots)
