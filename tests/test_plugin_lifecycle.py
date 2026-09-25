"""
Tests for plugin auto-refresh lifecycle:

- Enabling/disabling a plugin calls its on_enable / on_disable hooks
  immediately (no server restart required).
- Enabling a plugin that was not loaded at startup dynamically loads it.
- Built-in plugins can be uninstalled via the admin UI (DB row removed,
  plugin files preserved).
- After uninstalling a built-in plugin its routes return 404.
- Zip Slip / path traversal in external plugin import is rejected.
"""

import io
import json
import os
import tempfile
import threading
import zipfile

import db
import plugin_loader
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracked_plugin(plugin_id="test_lifecycle"):
    """Return a Plugin instance whose on_enable/on_disable calls are tracked."""
    from bananawiki_sdk import Plugin

    p = Plugin(plugin_id)
    p._calls = []

    @p.on_enable
    def _enable():
        p._calls.append("enable")

    @p.on_disable
    def _disable():
        p._calls.append("disable")

    @p.on_load
    def _load(app):
        p._calls.append("load")

    return p


# ---------------------------------------------------------------------------
# trigger_plugin_enable / trigger_plugin_disable unit tests
# ---------------------------------------------------------------------------

def test_trigger_enable_calls_on_enable_when_already_loaded():
    """on_enable is fired for a plugin that is already in _loaded_plugins."""
    plugin_id = "already_loaded_plugin"
    tracked = _make_tracked_plugin(plugin_id)
    plugin_loader._loaded_plugins[plugin_id] = {
        "module": None,
        "manifest": {"id": plugin_id},
        "plugin_obj": tracked,
        "dir": "/fake",
    }
    try:
        from app import app
        plugin_loader.trigger_plugin_enable(plugin_id, app)
        assert "enable" in tracked._calls
        # on_load must NOT be called again (plugin was already loaded)
        assert "load" not in tracked._calls
    finally:
        plugin_loader._loaded_plugins.pop(plugin_id, None)


def test_trigger_disable_calls_on_disable_and_removes_from_loaded():
    """on_disable is fired and the plugin is removed from _loaded_plugins."""
    plugin_id = "disable_test_plugin"
    tracked = _make_tracked_plugin(plugin_id)
    plugin_loader._loaded_plugins[plugin_id] = {
        "module": None,
        "manifest": {"id": plugin_id},
        "plugin_obj": tracked,
        "dir": "/fake",
    }
    result = plugin_loader.trigger_plugin_disable(plugin_id)
    assert result is True
    assert "disable" in tracked._calls
    assert plugin_id not in plugin_loader._loaded_plugins


def test_trigger_disable_returns_false_when_not_loaded():
    """trigger_plugin_disable returns False for a plugin that was never loaded."""
    result = plugin_loader.trigger_plugin_disable("nonexistent_xyz")
    assert result is False


def test_trigger_enable_returns_false_for_unknown_plugin():
    """trigger_plugin_enable returns False when the plugin cannot be found on disk."""
    from app import app
    result = plugin_loader.trigger_plugin_enable("totally_nonexistent_plugin_xyz", app)
    assert result is False


def test_trigger_enable_discovers_and_loads_builtin_plugin():
    """A built-in plugin that was not loaded at startup is loaded dynamically."""
    # Use the "badges" plugin which is always present in plugins/builtin/
    plugin_id = "badges"
    # Ensure it is not already in _loaded_plugins for this test
    was_loaded = plugin_loader._loaded_plugins.pop(plugin_id, None)
    try:
        from app import app
        result = plugin_loader.trigger_plugin_enable(plugin_id, app)
        assert result is True
        assert plugin_id in plugin_loader._loaded_plugins
    finally:
        # Restore previous state to avoid interfering with other tests
        if was_loaded is not None:
            plugin_loader._loaded_plugins[plugin_id] = was_loaded
        else:
            plugin_loader._loaded_plugins.pop(plugin_id, None)


def test_trigger_enable_succeeds_when_got_first_request_is_true():
    """trigger_plugin_enable must succeed even when Flask has already served a request.

    Flask 3.x raises AssertionError inside register_blueprint() if
    _got_first_request is True.  trigger_plugin_enable must temporarily clear
    that flag so plugins with Blueprint routes can be enabled at runtime via
    the admin UI without requiring a server restart.
    """
    plugin_id = "badges"
    was_loaded = plugin_loader._loaded_plugins.pop(plugin_id, None)
    from app import app
    prev = app._got_first_request
    app._got_first_request = True  # simulate a running server that has served ≥1 request
    try:
        result = plugin_loader.trigger_plugin_enable(plugin_id, app)
        assert result is True, "trigger_plugin_enable must return True even after first request"
        assert plugin_id in plugin_loader._loaded_plugins
    finally:
        app._got_first_request = prev
        if was_loaded is not None:
            plugin_loader._loaded_plugins[plugin_id] = was_loaded
        else:
            plugin_loader._loaded_plugins.pop(plugin_id, None)


def test_load_plugin_module_does_not_mutate_sys_path():
    """_load_plugin_module must not add plugin directories to sys.path (CWE-426)."""
    import sys
    plugin_id = "badges"
    builtin_dir = plugin_loader.BUILTIN_DIR
    plugin_dir = os.path.join(builtin_dir, plugin_id)
    parent = os.path.dirname(plugin_dir)
    manifest = {"id": plugin_id, "name": "Badges", "version": "1.0.0"}

    # Snapshot sys.path before loading
    path_before = list(sys.path)

    # Remove plugin from sys.modules so loading actually runs
    mod_name = f"_bw_plugin_{plugin_id}"
    old_mod = sys.modules.pop(mod_name, None)
    try:
        plugin_loader._load_plugin_module(plugin_dir, manifest)
        # sys.path must not have gained any new entries
        assert sys.path == path_before, (
            f"sys.path was mutated by _load_plugin_module; "
            f"new entries: {set(sys.path) - set(path_before)}"
        )
        # Specifically ensure the plugin parent dir was not inserted
        assert sys.path.count(parent) == path_before.count(parent)
    finally:
        sys.modules.pop(mod_name, None)
        if old_mod is not None:
            sys.modules[mod_name] = old_mod


# ---------------------------------------------------------------------------
# Admin UI: enable/disable hooks via HTTP
# ---------------------------------------------------------------------------

def test_admin_enable_plugin_no_restart_message(client, admin_user):
    """Enabling a plugin through the admin UI no longer says 'restart the server'."""
    db.disable_plugin("chat")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/admin/plugins/chat/enable",
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "successfully enabled" in html
    # The flash message itself must not mention restarting
    assert "Restart the server to activate" not in html


def test_admin_disable_plugin_no_restart_message(client, admin_user):
    """Disabling a plugin through the admin UI no longer says 'restart the server'."""
    db.enable_plugin("chat")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/admin/plugins/chat/disable",
        data={"force": "1"},
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "successfully disabled" in html
    # The flash message itself must not mention restarting
    assert "Restart the server to" not in html


def test_enable_plugin_effect_is_immediate(client, admin_user):
    """After enabling a plugin via the admin UI, its routes work in the same session."""
    db.disable_plugin("chat")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    # Before enable: /chats should 404
    assert client.get("/chats").status_code == 404

    # Enable the plugin
    client.post("/admin/plugins/chat/enable", follow_redirects=True)

    # After enable: /chats should work (200 or redirect, not 404)
    resp = client.get("/chats")
    assert resp.status_code != 404


def test_disable_plugin_effect_is_immediate(client, admin_user):
    """After disabling a plugin via the admin UI, its routes return 404 immediately."""
    db.enable_plugin("chat")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    # Before disable: /chats should work
    assert client.get("/chats").status_code != 404

    # Disable the plugin
    client.post("/admin/plugins/chat/disable", data={"force": "1"}, follow_redirects=True)

    # After disable: /chats should 404
    assert client.get("/chats").status_code == 404


# ---------------------------------------------------------------------------
# Admin UI: uninstalling built-in plugins
# ---------------------------------------------------------------------------

def test_admin_plugins_page_shows_uninstall_for_builtin(client, admin_user):
    """The admin plugins page shows an Uninstall button for built-in plugins."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/admin/plugins")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Uninstall" in html


def test_managed_plugins_page_explains_container_requirement(client, admin_user, monkeypatch):
    import config
    monkeypatch.setattr(config, "MANAGED_HOSTING", True)
    monkeypatch.setattr(config, "ALLOW_EXTERNAL_PLUGINS", False)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    response = client.get("/admin/plugins")
    assert response.status_code == 200
    assert b"Custom plugin protection" in response.data
    assert b'name="plugin_file"' not in response.data


def test_managed_denylisted_builtin_cannot_be_enabled(client, admin_user, monkeypatch):
    import config
    monkeypatch.setattr(config, "MANAGED_HOSTING", True)
    monkeypatch.setattr(config, "MANAGED_PLUGIN_DENYLIST", ("tts",))
    db.disable_plugin("tts")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    response = client.post("/admin/plugins/tts/enable", follow_redirects=True)
    assert response.status_code == 200
    assert not db.is_plugin_enabled("tts")
    assert b"disabled on managed hosting" in response.data


def test_admin_can_uninstall_builtin_plugin(client, admin_user):
    """An admin can uninstall a built-in plugin, removing its DB row."""
    client.post("/login", data={"username": "admin", "password": "admin123"})

    # Verify chat plugin is registered
    assert db.get_plugin("chat") is not None

    resp = client.post(
        "/admin/plugins/chat/delete",
        data={"drop_data": "0", "confirm_delete_extended": "1"},
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "successfully uninstalled" in html

    # DB row removed
    assert db.get_plugin("chat") is None


def test_uninstalled_builtin_plugin_routes_return_404(client, admin_user):
    """After uninstalling a built-in plugin, its routes return 404."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    db.enable_plugin("chat")

    # Uninstall the plugin
    client.post(
        "/admin/plugins/chat/delete",
        data={"drop_data": "0", "confirm_delete_extended": "1"},
        follow_redirects=True,
    )

    # Route must now return 404
    assert client.get("/chats").status_code == 404


def test_uninstalling_external_plugin_still_works(client, admin_user):
    """Existing external-plugin deletion still works after the refactor."""
    # Register a fake external plugin row (no files on disk)
    db.register_plugin(
        "fake_external",
        name="Fake External",
        version="1.0.0",
        builtin=False,
        enabled=False,
    )
    client.post("/login", data={"username": "admin", "password": "admin123"})
    # Without the password the route only shows the confirmation page.
    resp = client.post("/admin/plugins/fake_external/delete", data={"drop_data": "0"})
    assert resp.status_code == 200
    assert b'name="password"' in resp.data
    assert db.get_plugin("fake_external") is not None
    resp = client.post(
        "/admin/plugins/fake_external/delete",
        data={"drop_data": "0", "password": "admin123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Either "deleted" (external) or deleted from DB; row should be gone
    assert db.get_plugin("fake_external") is None


# ---------------------------------------------------------------------------
# Security: Zip Slip / path traversal regression tests (CWE-22)
# ---------------------------------------------------------------------------

def _make_valid_manifest(plugin_id="test_zipslip_plugin"):
    """Return a minimal valid plugin.json manifest dict."""
    return {
        "id": plugin_id,
        "name": "Test ZipSlip Plugin",
        "version": "1.0.0",
        "bananawiki_api_version": "1.0",
    }


def _build_bwplugin(members, plugin_id="test_zipslip_plugin"):
    """Build a .bwplugin ZIP in memory.

    *members* is a list of ``(archive_name, content_bytes)`` pairs.
    A valid ``plugin.json`` is always inserted at the archive root so that
    ``validate_bwplugin`` passes; the caller provides the additional members
    that exercise the traversal path.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "plugin.json",
            json.dumps(_make_valid_manifest(plugin_id)).encode(),
        )
        zf.writestr("__init__.py", b"")
        for name, data in members:
            zf.writestr(name, data)
    buf.seek(0)
    return buf.read()


def test_import_rejects_archive_member_traversal(monkeypatch, isolated_db, tmp_path):
    """Variant 1: archive entry with ``..`` traversal is rejected."""
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", str(tmp_path / "external"))
    payload = _build_bwplugin([
        ("../../../escape.txt", b"ZIPSLIP"),
    ])

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(payload)
        tmp_path_file = tmp.name

    try:
        from bananawiki_sdk._exceptions import PluginConfigError
        try:
            plugin_loader.import_bwplugin(tmp_path_file)
            pytest.fail("PluginConfigError should have been raised")
        except PluginConfigError as exc:
            assert "invalid file path" in str(exc).lower()
    finally:
        os.unlink(tmp_path_file)


def test_import_rejects_manifest_id_traversal(monkeypatch, isolated_db, tmp_path):
    """Variant 2: plugin_id containing path separators is rejected."""
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", str(tmp_path / "external"))
    traversal_id = "../../app/static/roottrav"
    payload = _build_bwplugin([], plugin_id=traversal_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(payload)
        tmp_path_file = tmp.name

    try:
        from bananawiki_sdk._exceptions import PluginConfigError
        try:
            plugin_loader.import_bwplugin(tmp_path_file)
            pytest.fail("PluginConfigError should have been raised")
        except PluginConfigError as exc:
            assert "invalid path component" in str(exc).lower()
    finally:
        os.unlink(tmp_path_file)


def test_import_rejects_backslash_traversal(monkeypatch, isolated_db, tmp_path):
    """Archive entry using backslash as separator is rejected."""
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", str(tmp_path / "external"))
    payload = _build_bwplugin([
        ("..\\..\\escape.txt", b"ZIPSLIP"),
    ], plugin_id="test_backslash_plugin")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(payload)
        tmp_path_file = tmp.name

    try:
        from bananawiki_sdk._exceptions import PluginConfigError
        try:
            plugin_loader.import_bwplugin(tmp_path_file)
            pytest.fail("PluginConfigError should have been raised")
        except PluginConfigError as exc:
            assert "invalid file path" in str(exc).lower()
    finally:
        os.unlink(tmp_path_file)


def test_import_valid_plugin_extracts_within_target(monkeypatch, isolated_db, tmp_path):
    """A well-formed archive is extracted entirely within target_dir."""
    external_dir = str(tmp_path / "external")
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", external_dir)

    payload = _build_bwplugin([
        ("data/helper.py", b"# helper"),
    ], plugin_id="safe_plugin")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(payload)
        tmp_file = tmp.name

    try:
        plugin_loader.import_bwplugin(tmp_file)
        target = os.path.join(external_dir, "safe_plugin")
        assert os.path.isdir(target)
        assert os.path.isfile(os.path.join(target, "plugin.json"))
        assert os.path.isfile(os.path.join(target, "data", "helper.py"))
        # Confirm nothing was written outside the target directory
        for root, _dirs, files in os.walk(external_dir):
            for f in files:
                full = os.path.abspath(os.path.join(root, f))
                assert full.startswith(target + os.sep), (
                    f"File extracted outside target_dir: {full}"
                )
    finally:
        os.unlink(tmp_file)


# ---------------------------------------------------------------------------
# Security: ZIP bomb / decompressed-size protection (CWE-409)
# ---------------------------------------------------------------------------

def test_import_rejects_oversized_plugin(monkeypatch, isolated_db, tmp_path):
    """Plugin archive exceeding MAX_PLUGIN_UNCOMPRESSED_SIZE is rejected."""
    import config as _config
    monkeypatch.setattr(_config, "MAX_PLUGIN_UNCOMPRESSED_SIZE", 100)  # 100 bytes
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", str(tmp_path / "external"))

    payload = _build_bwplugin([
        ("big_file.bin", b"X" * 200),
    ], plugin_id="oversized_plugin")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(payload)
        tmp_path_file = tmp.name

    try:
        from bananawiki_sdk._exceptions import PluginConfigError
        try:
            plugin_loader.import_bwplugin(tmp_path_file)
            pytest.fail("PluginConfigError should have been raised")
        except PluginConfigError as exc:
            assert "exceeds the" in str(exc).lower()
            assert "mb limit" in str(exc).lower()
    finally:
        os.unlink(tmp_path_file)


def test_import_accepts_plugin_within_size_limit(monkeypatch, isolated_db, tmp_path):
    """Plugin archive within MAX_PLUGIN_UNCOMPRESSED_SIZE is accepted."""
    import config as _config
    monkeypatch.setattr(_config, "MAX_PLUGIN_UNCOMPRESSED_SIZE", 10 * 1024 * 1024)
    external_dir = str(tmp_path / "external")
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", external_dir)

    payload = _build_bwplugin([
        ("helper.py", b"# small file"),
    ], plugin_id="small_plugin")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(payload)
        tmp_file = tmp.name

    try:
        manifest = plugin_loader.import_bwplugin(tmp_file)
        assert manifest["id"] == "small_plugin"
        target = os.path.join(external_dir, "small_plugin")
        assert os.path.isdir(target)
        assert os.path.isfile(os.path.join(target, "plugin.json"))
        assert os.path.isfile(os.path.join(target, "helper.py"))
    finally:
        os.unlink(tmp_file)


def test_import_rejects_symbolic_link_member(monkeypatch, isolated_db, tmp_path):
    external_dir = str(tmp_path / "external")
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", external_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.json", json.dumps(_make_valid_manifest("link_plugin")))
        zf.writestr("__init__.py", "")
        link = zipfile.ZipInfo("escape-link")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        zf.writestr(link, "../../outside")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(buf.getvalue())
        path = tmp.name
    try:
        from bananawiki_sdk._exceptions import PluginConfigError
        with pytest.raises(PluginConfigError, match="Symbolic links"):
            plugin_loader.import_bwplugin(path)
        assert not os.path.exists(os.path.join(external_dir, "link_plugin"))
    finally:
        os.unlink(path)


def test_failed_import_leaves_no_partial_plugin(monkeypatch, isolated_db, tmp_path):
    import config as _config
    external_dir = str(tmp_path / "external")
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", external_dir)
    monkeypatch.setattr(_config, "MAX_PLUGIN_UNCOMPRESSED_SIZE", 256)
    payload = _build_bwplugin(
        [("first.txt", b"ok"), ("too-large.bin", b"X" * 512)],
        plugin_id="atomic_plugin",
    )
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin") as tmp:
        tmp.write(payload)
        path = tmp.name
    try:
        from bananawiki_sdk._exceptions import PluginConfigError
        with pytest.raises(PluginConfigError):
            plugin_loader.import_bwplugin(path)
        assert not os.path.exists(os.path.join(external_dir, "atomic_plugin"))
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Plugin SDK download tests
# ---------------------------------------------------------------------------

class TestPluginSDKDownload:
    """Test the /admin/plugins/sdk-download route."""

    def test_sdk_download_requires_admin(self, client, editor_user):
        """Non-admin users cannot download the SDK."""
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = client.get("/admin/plugins/sdk-download")
        assert resp.status_code in (302, 403)

    def test_sdk_download_as_admin(self, logged_in_admin):
        """Admin can download the SDK ZIP."""
        resp = logged_in_admin.get("/admin/plugins/sdk-download")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"
        assert "bananawiki-plugin-sdk.zip" in resp.headers.get(
            "Content-Disposition", ""
        )

    def test_sdk_zip_contains_expected_files(self, logged_in_admin):
        """The ZIP contains SDK source, docs, examples, and starter template."""
        resp = logged_in_admin.get("/admin/plugins/sdk-download")
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        names = zf.namelist()
        # SDK source
        assert "bananawiki_sdk/__init__.py" in names
        assert "bananawiki_sdk/_hooks.py" in names
        assert "bananawiki_sdk/_slots.py" in names
        assert "bananawiki_sdk/_database.py" in names
        assert "bananawiki_sdk/_plugin.py" in names
        assert "bananawiki_sdk/_exceptions.py" in names
        assert "bananawiki_sdk/README.md" in names
        # Documentation
        assert "docs/overview.md" in names
        assert "docs/authoring.md" in names
        assert "docs/api-reference.md" in names
        # Examples
        assert "examples/hello_world/__init__.py" in names
        assert "examples/hello_world/plugin.json" in names
        assert "examples/page_word_count/__init__.py" in names
        assert "examples/page_word_count/plugin.json" in names
        # Starter template
        assert "starter_template/plugin.json" in names
        assert "starter_template/__init__.py" in names
        # Top-level README
        assert "README.md" in names
        zf.close()

    def test_sdk_zip_no_core_source(self, logged_in_admin):
        """The ZIP must not contain BananaWiki core source code."""
        resp = logged_in_admin.get("/admin/plugins/sdk-download")
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        names = zf.namelist()
        for name in names:
            assert not name.startswith("routes/"), f"Core source leaked: {name}"
            assert not name.startswith("db/"), f"Core source leaked: {name}"
            assert not name.startswith("helpers/"), f"Core source leaked: {name}"
            assert name != "app.py", f"Core source leaked: {name}"
            assert name != "config.py", f"Core source leaked: {name}"
            assert name != "wsgi.py", f"Core source leaked: {name}"
            assert name != "plugin_loader.py", f"Core source leaked: {name}"
            assert name != "sync.py", f"Core source leaked: {name}"
            assert name != "wiki_logger.py", f"Core source leaked: {name}"
        zf.close()

    def test_sdk_starter_template_valid_json(self, logged_in_admin):
        """The starter template plugin.json is valid JSON."""
        resp = logged_in_admin.get("/admin/plugins/sdk-download")
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        manifest = json.loads(zf.read("starter_template/plugin.json"))
        assert "id" in manifest
        assert "name" in manifest
        assert "version" in manifest
        assert "bananawiki_api_version" in manifest
        zf.close()

    def test_plugins_page_shows_sdk_button(self, logged_in_admin):
        """The plugins page should show the Download Plugin SDK button."""
        resp = logged_in_admin.get("/admin/plugins")
        assert resp.status_code == 200
        assert b"Download Plugin SDK" in resp.data
        assert b"sdk-download" in resp.data


# ---------------------------------------------------------------------------
# Builtin plugin default seeding
# ---------------------------------------------------------------------------

class TestSeedBuiltinPluginDefaults:
    """Verify the curated default-enabled set and that experimental plugins
    are never auto-installed on a fresh instance."""

    DEFAULT_ENABLED = {
        "attachments", "audit", "canvas", "chat", "drafts",
        "kanban", "page_history", "tts", "user_data_export",
    }

    def _wipe_plugins(self):
        from db._connection import get_db_context
        with get_db_context() as conn:
            conn.execute("DELETE FROM plugins")
            conn.commit()

    def test_seed_enables_only_curated_default_set(self, isolated_db):
        """seed_builtin_plugins must only enable the curated default set."""
        self._wipe_plugins()
        from plugin_loader import discover_plugins
        manifests = [m for _, m, b in discover_plugins() if b]
        db.seed_builtin_plugins(manifests)

        enabled = {p["id"] for p in db.list_plugins() if p["enabled"]}
        assert enabled == self.DEFAULT_ENABLED, (
            f"Unexpected default-enabled set: {enabled}"
        )

    def test_seed_skips_experimental_plugins(self, isolated_db):
        """Experimental plugins must not be seeded as installed at all.

        No shipped plugin is experimental any more, so the rule is exercised
        with a synthetic manifest next to the real ones.
        """
        self._wipe_plugins()
        from plugin_loader import discover_plugins
        manifests = [m for _, m, b in discover_plugins() if b]
        assert not any(m.get("experimental") for m in manifests)
        manifests.append({"id": "trial_feature", "name": "Trial feature",
                          "version": "0.1.0", "experimental": True})
        db.seed_builtin_plugins(manifests)

        installed = {p["id"] for p in db.list_plugins()}
        assert "trial_feature" not in installed
        assert {m["id"] for m in manifests if not m.get("experimental")} <= installed

    def test_concurrent_seeding_installs_each_plugin_once(self, isolated_db):
        """Workers that boot together must not collide on the plugin rows."""
        self._wipe_plugins()
        from plugin_loader import discover_plugins
        manifests = [m for _, m, b in discover_plugins() if b]

        failures = []
        start = threading.Barrier(4, timeout=30)

        def seed():
            start.wait()
            try:
                db.seed_builtin_plugins(manifests)
            except Exception as error:  # noqa: BLE001 - reported below
                failures.append(error)

        workers = [threading.Thread(target=seed) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
            assert not worker.is_alive()

        assert failures == []
        installed = [plugin["id"] for plugin in db.list_plugins()]
        assert len(installed) == len(set(installed))
        expected = {m["id"] for m in manifests if not m.get("experimental")}
        assert set(installed) == expected

    def test_seed_preserves_existing_admin_choice(self, isolated_db):
        """Re-running the seed must never override an admin's enable/disable."""
        self._wipe_plugins()
        from plugin_loader import discover_plugins
        manifests = [m for _, m, b in discover_plugins() if b]
        db.seed_builtin_plugins(manifests)

        # Admin turns kanban off and turns audit off.
        db.disable_plugin("kanban")
        db.disable_plugin("audit")

        # Re-seed: existing rows are not touched.
        db.seed_builtin_plugins(manifests)
        assert not db.is_plugin_enabled("kanban")
        assert not db.is_plugin_enabled("audit")

    def test_seed_refreshes_chat_display_metadata(self, isolated_db):
        """Existing installs should show the merged Chats and Groups label."""
        self._wipe_plugins()
        db.register_plugin(
            "chat",
            name="Chat",
            version="1.0.0",
            author="BananaWiki",
            description="Private one-on-one direct messaging",
            builtin=True,
            enabled=False,
        )
        from plugin_loader import discover_plugins
        manifests = [m for _, m, b in discover_plugins() if b]

        db.seed_builtin_plugins(manifests)

        plugin = db.get_plugin("chat")
        assert plugin["name"] == "Chats and Groups"
        assert plugin["description"] == (
            "Direct messages and group chat spaces in one messaging plugin"
        )
        assert plugin["enabled"] == 0


# ---------------------------------------------------------------------------
# banana_chat → banana_ai rename migration
# ---------------------------------------------------------------------------

class TestLegacyAiChatPluginMigration:
    """Existing installs with the legacy ``banana_chat`` rows / tables must be
    migrated cleanly to ``banana_ai`` without losing user data."""

    def test_migration_renames_plugin_row(self, isolated_db):
        from db._connection import get_db_context
        from db._schema import _migrate_banana_chat_to_banana_ai

        # Simulate a legacy install.
        with get_db_context() as conn:
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS banana_ai__messages")
            cur.execute("DELETE FROM plugins WHERE id='banana_ai'")
            cur.execute(
                "INSERT INTO plugins (id, name, version, author, description, "
                "builtin, enabled) VALUES ('banana_chat', 'BananaChat', "
                "'1.0.0', 'BananaWiki', 'old', 1, 1)"
            )
            cur.execute(
                "CREATE TABLE banana_chat__messages "
                "(id INTEGER PRIMARY KEY, body TEXT)"
            )
            cur.execute(
                "INSERT INTO banana_chat__messages (id, body) "
                "VALUES (1, 'hi')"
            )
            conn.commit()

            _migrate_banana_chat_to_banana_ai(conn, cur)
            conn.commit()

            ids = {r[0] for r in cur.execute(
                "SELECT id FROM plugins WHERE id IN ('banana_chat','banana_ai')"
            ).fetchall()}
            assert ids == {"banana_ai"}

            new_row = cur.execute(
                "SELECT name FROM plugins WHERE id='banana_ai'"
            ).fetchone()
            assert new_row["name"] == "AI Chat"

            # User data preserved under the new table name.
            rows = cur.execute(
                "SELECT id, body FROM banana_ai__messages"
            ).fetchall()
            assert [(r["id"], r["body"]) for r in rows] == [(1, "hi")]

            legacy_exists = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='banana_chat__messages'"
            ).fetchone()
            assert legacy_exists is None

    def test_migration_drops_legacy_row_when_new_already_exists(self, isolated_db):
        from db._connection import get_db_context
        from db._schema import _migrate_banana_chat_to_banana_ai

        # ``isolated_db``: ``banana_ai`` no longer ships, so it won't be seeded.
        # The migration rename path handles the case where only ``banana_chat`` exists.
        with get_db_context() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO plugins (id, name, version, author, description, "
                "builtin, enabled) VALUES ('banana_chat', 'BananaChat', "
                "'1.0.0', 'BananaWiki', 'old', 1, 1)"
            )
            conn.commit()

            _migrate_banana_chat_to_banana_ai(conn, cur)
            conn.commit()

            ids = {r[0] for r in cur.execute(
                "SELECT id FROM plugins WHERE id IN ('banana_chat','banana_ai')"
            ).fetchall()}
            assert ids == {"banana_ai"}


def test_child_module_hooks_return_once_after_reenable(client, tmp_path, monkeypatch):
    """Disable removes a child module's hooks; re-enabling imports them again."""
    from bananawiki_sdk._hooks import emit_hook
    directory = tmp_path / 'child_hook'
    directory.mkdir()
    (directory / '__init__.py').write_text('from . import handlers\n')
    (directory / 'handlers.py').write_text(
        'from bananawiki_sdk import hook\n'
        '@hook("release_child_hook")\n'
        'def report(events, **kwargs):\n'
        '    events.append("called")\n'
    )
    manifest = {'id': 'release_child_hook', 'name': 'Child hook', 'version': '1.0.0', 'api_version': '1.0'}
    monkeypatch.setattr(plugin_loader, 'discover_plugins', lambda: [(str(directory), manifest, False)])
    # Plugin hooks only fire while the registry has the plugin enabled.
    db.register_plugin(manifest['id'], name=manifest['name'], version='1.0.0', enabled=True)
    try:
        for _ in range(2):
            assert plugin_loader.trigger_plugin_enable(manifest['id'], client.application)
            events = []
            emit_hook('release_child_hook', events=events)
            assert events == ['called']
            plugin_loader.trigger_plugin_disable(manifest['id'])
            events.clear()
            emit_hook('release_child_hook', events=events)
            assert events == []
    finally:
        plugin_loader.trigger_plugin_disable(manifest['id'])
