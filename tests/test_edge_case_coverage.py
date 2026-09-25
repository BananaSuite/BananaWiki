"""
Additional edge-case coverage for BananaWiki.

Focuses on areas with thin coverage:
- bananawiki_sdk db helpers (db_query / db_execute)
- bananawiki_sdk hook system
- bananawiki_sdk template slot system
- bananawiki_sdk re-exports (get_setting)
- Authentication edge cases
- Admin route edge cases
- Validation helper edge cases
- DB user-suspension logic
- Invite code edge cases
- API route edge cases
"""

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# bananawiki_sdk: db helpers
# ---------------------------------------------------------------------------

class TestSDKDbQueryEdgeCases:
    """db_query must be read-only and reject write-like statements."""

    def test_select_returns_list(self, isolated_db):
        from bananawiki_sdk._database import db_query
        rows = db_query("SELECT 1 AS n")
        assert isinstance(rows, list)
        assert rows[0]["n"] == 1

    def test_select_with_params(self, isolated_db):
        from bananawiki_sdk._database import db_query
        rows = db_query("SELECT ? AS val", [42])
        assert rows[0]["val"] == 42

    def test_select_from_core_table_allowed(self, isolated_db):
        from bananawiki_sdk._database import db_query
        rows = db_query("SELECT COUNT(*) AS cnt FROM users")
        assert rows[0]["cnt"] >= 0

    def test_insert_raises_plugin_error(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError, match="read-only"):
            db_query("INSERT INTO users (id) VALUES ('x')")

    def test_update_raises_plugin_error(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_query("UPDATE users SET role='admin'")

    def test_delete_raises_plugin_error(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_query("DELETE FROM users WHERE id='x'")

    def test_drop_raises_plugin_error(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_query("DROP TABLE users")

    def test_create_raises_plugin_error(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_query("CREATE TABLE foo (id INTEGER)")

    def test_replace_raises_plugin_error(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_query("REPLACE INTO users (id) VALUES ('x')")

    def test_alter_raises_plugin_error(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_query("ALTER TABLE users ADD COLUMN foo TEXT")

    def test_empty_result_returns_empty_list(self, isolated_db):
        from bananawiki_sdk._database import db_query
        rows = db_query("SELECT * FROM users WHERE id='nonexistent_xyz'")
        assert rows == []

    def test_whitespace_before_select_allowed(self, isolated_db):
        """Leading whitespace should not trigger the write rejection."""
        from bananawiki_sdk._database import db_query
        rows = db_query("   SELECT 1 AS x")
        assert rows[0]["x"] == 1

    def test_case_insensitive_write_detection(self, isolated_db):
        from bananawiki_sdk._database import db_query
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_query("insert into users (id) values ('x')")


class TestSDKDbExecuteEdgeCases:
    """db_execute must protect core tables while allowing plugin-owned tables."""

    def test_insert_into_core_table_raises(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError, match="core table"):
            db_execute("INSERT INTO users (id, username) VALUES ('x', 'y')")

    def test_update_core_table_raises(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("UPDATE users SET role='admin'")

    def test_delete_from_core_table_raises(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("DELETE FROM pages WHERE id=1")

    def test_drop_core_table_raises(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("DROP TABLE users")

    def test_alter_core_table_raises(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("ALTER TABLE pages ADD COLUMN foo TEXT")

    def test_create_plugin_table_allowed(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        # Creating a new plugin-owned table should succeed without error
        db_execute("CREATE TABLE IF NOT EXISTS my_plugin_data (id INTEGER PRIMARY KEY, val TEXT)")

    def test_insert_into_plugin_table_returns_rowid(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        db_execute("CREATE TABLE IF NOT EXISTS my_plugin_data (id INTEGER PRIMARY KEY, val TEXT)")
        rowid = db_execute("INSERT INTO my_plugin_data (val) VALUES (?)", ["hello"])
        assert rowid == 1

    def test_insert_multiple_rows_increments_rowid(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        db_execute("CREATE TABLE IF NOT EXISTS my_plugin_rows (id INTEGER PRIMARY KEY, v TEXT)")
        r1 = db_execute("INSERT INTO my_plugin_rows (v) VALUES (?)", ["a"])
        r2 = db_execute("INSERT INTO my_plugin_rows (v) VALUES (?)", ["b"])
        assert r2 > r1

    def test_update_plugin_table_succeeds(self, isolated_db):
        from bananawiki_sdk._database import db_execute, db_query
        db_execute("CREATE TABLE IF NOT EXISTS my_plugin_upd (id INTEGER PRIMARY KEY, v TEXT)")
        db_execute("INSERT INTO my_plugin_upd (v) VALUES (?)", ["original"])
        db_execute("UPDATE my_plugin_upd SET v=? WHERE id=1", ["updated"])
        rows = db_query("SELECT v FROM my_plugin_upd WHERE id=1")
        assert rows[0]["v"] == "updated"

    def test_replace_into_core_table_raises(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("REPLACE INTO users (id, username) VALUES ('x', 'y')")

    def test_case_insensitive_table_check(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("INSERT INTO USERS (id) VALUES ('x')")

    def test_backtick_quoted_core_table_raises(self, isolated_db):
        """Backtick-quoted table names must not bypass core-table protection."""
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError, match="core table"):
            db_execute("INSERT INTO `users` (id, username) VALUES ('x', 'y')")

    def test_bracket_quoted_core_table_raises(self, isolated_db):
        """Bracket-quoted table names must not bypass core-table protection."""
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError, match="core table"):
            db_execute("INSERT INTO [users] (id, username) VALUES ('x', 'y')")

    def test_backtick_quoted_update_core_table_raises(self, isolated_db):
        """Backtick-quoted UPDATE must not bypass core-table protection."""
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("UPDATE `pages` SET title='hacked'")

    def test_bracket_quoted_delete_core_table_raises(self, isolated_db):
        """Bracket-quoted DELETE must not bypass core-table protection."""
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("DELETE FROM [pages] WHERE id=1")

    def test_kanban_tables_are_core_protected(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        for tbl in ("kanban_boards", "kanban_columns", "kanban_tickets"):
            with pytest.raises(PluginError):
                db_execute(f"DELETE FROM {tbl} WHERE id=1")

    def test_plugins_table_is_core_protected(self, isolated_db):
        from bananawiki_sdk._database import db_execute
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            db_execute("DELETE FROM plugins WHERE id='chat'")


# ---------------------------------------------------------------------------
# bananawiki_sdk: hook system
# ---------------------------------------------------------------------------

class TestSDKHookSystem:
    """Hook registration, emission, and unregistration."""

    def setup_method(self):
        from bananawiki_sdk._hooks import _clear_all_hooks
        _clear_all_hooks()

    def teardown_method(self):
        from bananawiki_sdk._hooks import _clear_all_hooks
        _clear_all_hooks()

    def test_hook_registers_and_is_called(self):
        from bananawiki_sdk._hooks import hook, emit_hook
        calls = []

        @hook("test_event")
        def handler(**kwargs):
            calls.append(kwargs)

        emit_hook("test_event", key="val")
        assert len(calls) == 1
        assert calls[0]["key"] == "val"

    def test_multiple_subscribers_all_called(self):
        from bananawiki_sdk._hooks import hook, emit_hook
        results = []

        @hook("multi_test")
        def h1(**kwargs):
            results.append("h1")

        @hook("multi_test")
        def h2(**kwargs):
            results.append("h2")

        emit_hook("multi_test")
        assert "h1" in results
        assert "h2" in results

    def test_emit_unknown_hook_does_nothing(self):
        from bananawiki_sdk._hooks import emit_hook
        # Should not raise
        emit_hook("no_such_hook_xyz")

    def test_subscriber_exception_does_not_propagate(self):
        from bananawiki_sdk._hooks import hook, emit_hook
        ran_after = []

        @hook("error_hook")
        def bad_handler(**kwargs):
            raise RuntimeError("intentional error")

        @hook("error_hook")
        def good_handler(**kwargs):
            ran_after.append(True)

        # Should not raise; good_handler should still run
        emit_hook("error_hook")
        assert ran_after == [True]

    def test_get_registered_hooks_returns_counts(self):
        from bananawiki_sdk._hooks import hook, get_registered_hooks

        @hook("count_hook")
        def h1(**kwargs):
            pass

        @hook("count_hook")
        def h2(**kwargs):
            pass

        counts = get_registered_hooks()
        assert counts.get("count_hook") == 2

    def test_clear_all_hooks_removes_everything(self):
        from bananawiki_sdk._hooks import hook, get_registered_hooks, _clear_all_hooks

        @hook("temp_hook")
        def h(**kwargs):
            pass

        _clear_all_hooks()
        assert get_registered_hooks() == {}

    def test_unregister_hooks_removes_by_plugin_id(self):
        from bananawiki_sdk._hooks import hook, get_registered_hooks, _unregister_hooks

        @hook("plugin_hook")
        def h(**kwargs):
            pass

        h._bw_plugin_id = "my_plugin"

        _unregister_hooks("my_plugin")
        counts = get_registered_hooks()
        assert "plugin_hook" not in counts

    def test_hook_fn_has_bw_hooks_attribute(self):
        from bananawiki_sdk._hooks import hook

        @hook("attr_test")
        def handler(**kwargs):
            pass

        assert hasattr(handler, "_bw_hooks")
        assert "attr_test" in handler._bw_hooks

    def test_same_function_can_subscribe_to_multiple_hooks(self):
        from bananawiki_sdk._hooks import hook, emit_hook
        results = []

        def multi(**kwargs):
            results.append(kwargs.get("h"))

        hook("ha")(multi)
        hook("hb")(multi)

        emit_hook("ha", h="a")
        emit_hook("hb", h="b")
        assert "a" in results
        assert "b" in results

    def test_snapshot_and_stamp_new_hooks(self):
        """Bare @hook() functions get _bw_plugin_id via stamp_new_hooks."""
        from bananawiki_sdk._hooks import (
            hook, get_registered_hooks, _snapshot_hook_fns,
            _stamp_new_hooks, _unregister_hooks,
        )

        snap = _snapshot_hook_fns()

        @hook("stamped_hook")
        def h(**kwargs):
            pass

        _stamp_new_hooks("test_plugin", snap)

        assert getattr(h, "_bw_plugin_id", None) == "test_plugin"

        # Now unregistering should remove it
        _unregister_hooks("test_plugin")
        assert "stamped_hook" not in get_registered_hooks()

    def test_stamp_new_hooks_preserves_existing_plugin_id(self):
        """stamp_new_hooks must not overwrite an already-set _bw_plugin_id."""
        from bananawiki_sdk._hooks import (
            hook, _snapshot_hook_fns, _stamp_new_hooks,
        )

        snap = _snapshot_hook_fns()

        @hook("preserve_hook")
        def h(**kwargs):
            pass

        h._bw_plugin_id = "original"
        _stamp_new_hooks("other_plugin", snap)

        assert h._bw_plugin_id == "original"

    def test_stamp_does_not_affect_pre_snapshot_hooks(self):
        """stamp_new_hooks must not touch hooks registered before the snapshot."""
        from bananawiki_sdk._hooks import (
            hook, _snapshot_hook_fns, _stamp_new_hooks,
        )

        @hook("old_hook")
        def old(**kwargs):
            pass

        snap = _snapshot_hook_fns()

        @hook("new_hook")
        def new(**kwargs):
            pass

        _stamp_new_hooks("new_plugin", snap)

        assert not hasattr(old, "_bw_plugin_id")
        assert getattr(new, "_bw_plugin_id", None) == "new_plugin"


# ---------------------------------------------------------------------------
# bananawiki_sdk: template slot system
# ---------------------------------------------------------------------------

class TestSDKTemplateSlotSystem:
    """Template slot registration and rendering."""

    def setup_method(self):
        from bananawiki_sdk._slots import _clear_all_slots
        _clear_all_slots()

    def teardown_method(self):
        from bananawiki_sdk._slots import _clear_all_slots
        _clear_all_slots()

    def test_slot_renders_html(self):
        from bananawiki_sdk._slots import template_slot, render_slot

        @template_slot("page.below_content")
        def renderer(context):
            return "<p>Extra content</p>"

        html = render_slot("page.below_content")
        assert "<p>Extra content</p>" in html

    def test_multiple_renderers_concatenated(self):
        from bananawiki_sdk._slots import template_slot, render_slot

        @template_slot("sidebar.bottom")
        def r1(context):
            return "A"

        @template_slot("sidebar.bottom")
        def r2(context):
            return "B"

        html = render_slot("sidebar.bottom")
        assert "A" in html
        assert "B" in html

    def test_empty_slot_returns_empty_string(self):
        from bananawiki_sdk._slots import render_slot
        html = render_slot("nonexistent.slot.xyz")
        assert html == ""

    def test_renderer_exception_does_not_propagate(self):
        from bananawiki_sdk._slots import template_slot, render_slot
        recovered = []

        @template_slot("error.slot")
        def bad_renderer(context):
            raise ValueError("intentional")

        @template_slot("error.slot")
        def good_renderer(context):
            recovered.append(True)
            return "OK"

        result = render_slot("error.slot")
        assert "OK" in result
        assert recovered == [True]

    def test_context_passed_to_renderer(self):
        from bananawiki_sdk._slots import template_slot, render_slot
        received = []

        @template_slot("ctx.slot")
        def renderer(context):
            received.append(context)
            return ""

        render_slot("ctx.slot", context={"page": "home"})
        assert received[0]["page"] == "home"

    def test_none_context_defaults_to_empty_dict(self):
        from bananawiki_sdk._slots import template_slot, render_slot
        received = []

        @template_slot("none.ctx.slot")
        def renderer(context):
            received.append(context)
            return ""

        render_slot("none.ctx.slot", context=None)
        assert received[0] == {}

    def test_get_registered_slots_returns_counts(self):
        from bananawiki_sdk._slots import template_slot, get_registered_slots

        @template_slot("count.slot")
        def r1(context):
            return ""

        @template_slot("count.slot")
        def r2(context):
            return ""

        counts = get_registered_slots()
        assert counts.get("count.slot") == 2

    def test_clear_all_slots_removes_everything(self):
        from bananawiki_sdk._slots import template_slot, get_registered_slots, _clear_all_slots

        @template_slot("temp.slot")
        def r(context):
            return ""

        _clear_all_slots()
        assert get_registered_slots() == {}

    def test_slot_fn_has_bw_slots_attribute(self):
        from bananawiki_sdk._slots import template_slot

        @template_slot("attr.slot")
        def renderer(context):
            return ""

        assert hasattr(renderer, "_bw_slots")
        assert "attr.slot" in renderer._bw_slots

    def test_unregister_slots_by_plugin_id(self):
        from bananawiki_sdk._slots import template_slot, get_registered_slots, _unregister_slots

        @template_slot("plugin.slot")
        def r(context):
            return ""

        r._bw_plugin_id = "my_plugin"

        _unregister_slots("my_plugin")
        counts = get_registered_slots()
        assert "plugin.slot" not in counts

    def test_snapshot_and_stamp_new_slots(self):
        """Bare @template_slot() functions get _bw_plugin_id via stamp_new_slots."""
        from bananawiki_sdk._slots import (
            template_slot, get_registered_slots, _snapshot_slot_fns,
            _stamp_new_slots, _unregister_slots,
        )

        snap = _snapshot_slot_fns()

        @template_slot("stamped.slot")
        def r(context):
            return ""

        _stamp_new_slots("test_plugin", snap)

        assert getattr(r, "_bw_plugin_id", None) == "test_plugin"

        # Now unregistering should remove it
        _unregister_slots("test_plugin")
        assert "stamped.slot" not in get_registered_slots()

    def test_stamp_new_slots_preserves_existing_plugin_id(self):
        """stamp_new_slots must not overwrite an already-set _bw_plugin_id."""
        from bananawiki_sdk._slots import (
            template_slot, _snapshot_slot_fns, _stamp_new_slots,
        )

        snap = _snapshot_slot_fns()

        @template_slot("preserve.slot")
        def r(context):
            return ""

        r._bw_plugin_id = "original"
        _stamp_new_slots("other_plugin", snap)

        assert r._bw_plugin_id == "original"

    def test_stamp_does_not_affect_pre_snapshot_slots(self):
        """stamp_new_slots must not touch slots registered before the snapshot."""
        from bananawiki_sdk._slots import (
            template_slot, _snapshot_slot_fns, _stamp_new_slots,
        )

        @template_slot("old.slot")
        def old(context):
            return ""

        snap = _snapshot_slot_fns()

        @template_slot("new.slot")
        def new(context):
            return ""

        _stamp_new_slots("new_plugin", snap)

        assert not hasattr(old, "_bw_plugin_id")
        assert getattr(new, "_bw_plugin_id", None) == "new_plugin"

    def test_disabled_plugin_slot_skipped_when_enabled_plugins_on_g(self, client):
        """render_slot skips renderers whose plugin is disabled in g.enabled_plugins.

        This ensures EasyWiki mode (which sets enabled_plugins on g without
        touching the DB) actually hides UI injected by disabled plugins.
        """
        from bananawiki_sdk._slots import template_slot, render_slot
        from flask import g

        called = []

        @template_slot("easy.gate.slot")
        def disabled_renderer(context):
            called.append("disabled")
            return "<p>disabled plugin output</p>"

        disabled_renderer._bw_plugin_id = "my_disabled_plugin"

        @template_slot("easy.gate.slot")
        def enabled_renderer(context):
            called.append("enabled")
            return "<p>enabled plugin output</p>"

        enabled_renderer._bw_plugin_id = "my_enabled_plugin"

        with client.application.test_request_context("/"):
            g.enabled_plugins = {
                "my_disabled_plugin": False,
                "my_enabled_plugin": True,
            }
            result = render_slot("easy.gate.slot")

        assert "disabled plugin output" not in result
        assert "enabled plugin output" in result
        assert "disabled" not in called
        assert "enabled" in called

    def test_slot_without_plugin_id_always_runs(self, client):
        """render_slot must not skip renderers that have no _bw_plugin_id stamp.

        Core (non-plugin) renderers may be registered without a plugin_id and
        must always execute, even when g.enabled_plugins is present.
        """
        from bananawiki_sdk._slots import template_slot, render_slot
        from flask import g

        @template_slot("core.slot")
        def core_renderer(context):
            return "<p>core output</p>"

        # No _bw_plugin_id set

        with client.application.test_request_context("/"):
            g.enabled_plugins = {}  # empty: nothing enabled
            result = render_slot("core.slot")

        assert "core output" in result


# ---------------------------------------------------------------------------
# bananawiki_sdk: re-exports (get_setting)
# ---------------------------------------------------------------------------

class TestSDKGetSetting:
    def test_get_existing_setting(self, admin_user):
        from bananawiki_sdk._re_exports import get_setting
        name = get_setting("site_name")
        assert name is not None

    def test_get_nonexistent_setting_returns_none(self, admin_user):
        from bananawiki_sdk._re_exports import get_setting
        val = get_setting("totally_nonexistent_key_xyz_abc")
        assert val is None

    def test_get_setup_done_setting(self, admin_user):
        from bananawiki_sdk._re_exports import get_setting
        # admin_user fixture calls db.update_site_settings(setup_done=1)
        val = get_setting("setup_done")
        assert val == 1

    def test_has_permission_with_valid_user(self, admin_user):
        import db
        from bananawiki_sdk._re_exports import has_permission
        user = db.get_user_by_id(admin_user)
        # Admins have all permissions
        result = has_permission(user, "page.create")
        assert result is True

    def test_is_plugin_enabled_for_enabled_plugin(self, isolated_db):
        import db
        from bananawiki_sdk._re_exports import is_plugin_enabled
        # conftest enables all builtin plugins
        assert is_plugin_enabled("chat") is True

    def test_is_plugin_enabled_for_disabled_plugin(self, isolated_db):
        import db
        from bananawiki_sdk._re_exports import is_plugin_enabled
        db.disable_plugin("chat")
        assert is_plugin_enabled("chat") is False

    def test_is_plugin_enabled_for_unknown_plugin(self, isolated_db):
        from bananawiki_sdk._re_exports import is_plugin_enabled
        assert is_plugin_enabled("totally_nonexistent_plugin") is False


# ---------------------------------------------------------------------------
# Validation helpers edge cases
# ---------------------------------------------------------------------------

class TestValidationHelpers:
    def test_allowed_file_valid_extensions(self):
        from helpers._validation import allowed_file
        assert allowed_file("photo.jpg") is True
        assert allowed_file("image.PNG") is True
        assert allowed_file("anim.gif") is True
        assert allowed_file("picture.webp") is True
        assert allowed_file("img.jpeg") is True

    def test_allowed_file_rejects_svg(self):
        from helpers._validation import allowed_file
        assert allowed_file("icon.svg") is False

    def test_allowed_file_rejects_no_extension(self):
        from helpers._validation import allowed_file
        assert allowed_file("noext") is False

    def test_allowed_file_rejects_dangerous_types(self):
        from helpers._validation import allowed_file
        for name in ("shell.sh", "script.py", "page.html", "exec.exe", "lib.js"):
            assert allowed_file(name) is False, f"{name} should be rejected"

    def test_allowed_attachment_accepts_pdf(self):
        from helpers._validation import allowed_attachment
        assert allowed_attachment("doc.pdf") is True

    def test_allowed_attachment_rejects_exe(self):
        from helpers._validation import allowed_attachment
        assert allowed_attachment("malware.exe") is False

    def test_hex_color_validation_valid(self):
        from helpers._validation import _is_valid_hex_color
        # Only 7-char #RRGGBB format is valid
        assert _is_valid_hex_color("#aabbcc") is True
        assert _is_valid_hex_color("#AABBCC") is True
        assert _is_valid_hex_color("#123456") is True
        assert _is_valid_hex_color("#0a1b2c") is True

    def test_hex_color_validation_invalid(self):
        from helpers._validation import _is_valid_hex_color
        assert _is_valid_hex_color("red") is False
        assert _is_valid_hex_color("#gg0000") is False
        assert _is_valid_hex_color("") is False
        assert _is_valid_hex_color("#12345") is False   # 5 chars
        assert _is_valid_hex_color("#ABC") is False     # 3 chars (short form not accepted)
        assert _is_valid_hex_color("#AABBCCDD") is False  # 8 chars

    def test_username_validation_valid(self):
        from helpers._validation import _is_valid_username
        assert _is_valid_username("alice") is True
        assert _is_valid_username("bob_123") is True
        assert _is_valid_username("User-Name") is True

    def test_username_validation_invalid(self):
        from helpers._validation import _is_valid_username
        assert _is_valid_username("") is False
        assert _is_valid_username("a b") is False  # space
        assert _is_valid_username("user@name") is False  # @ not allowed
        assert _is_valid_username("user.name") is False  # . not allowed


# ---------------------------------------------------------------------------
# User suspension edge cases
# ---------------------------------------------------------------------------

class TestUserSuspensionEdgeCases:
    def test_unsuspended_user_is_not_active_suspension(self):
        import db
        uid = db.create_user("testuser", generate_password_hash("pw"))
        user = db.get_user_by_id(uid)
        assert db.is_suspension_active(user) is False

    def test_permanent_suspension_is_active(self):
        import db
        uid = db.create_user("susp_user", generate_password_hash("pw"))
        # Permanent suspension: suspended=1, suspended_until=None
        db.update_user(uid, suspended=1, suspended_until=None)
        user = db.get_user_by_id(uid)
        assert db.is_suspension_active(user) is True

    def test_timed_suspension_future_date_is_active(self):
        import db
        uid = db.create_user("timed_user", generate_password_hash("pw"))
        future = "2099-01-01T00:00:00"
        db.update_user(uid, suspended=1, suspended_until=future)
        user = db.get_user_by_id(uid)
        assert db.is_suspension_active(user) is True

    def test_timed_suspension_past_date_is_not_active(self):
        import db
        uid = db.create_user("past_user", generate_password_hash("pw"))
        past = "2000-01-01T00:00:00"
        db.update_user(uid, suspended=1, suspended_until=past)
        user = db.get_user_by_id(uid)
        assert db.is_suspension_active(user) is False

    def test_check_suspension_expired_clears_past_suspension(self):
        import db
        uid = db.create_user("exp_user", generate_password_hash("pw"))
        past = "2000-01-01T00:00:00"
        db.update_user(uid, suspended=1, suspended_until=past)
        db.check_suspension_expired(uid)
        user = db.get_user_by_id(uid)
        assert user["suspended"] == 0

    def test_check_suspension_expired_keeps_future_suspension(self):
        import db
        uid = db.create_user("fut_user", generate_password_hash("pw"))
        future = "2099-01-01T00:00:00"
        db.update_user(uid, suspended=1, suspended_until=future)
        db.check_suspension_expired(uid)
        user = db.get_user_by_id(uid)
        assert user["suspended"] == 1

    def test_none_user_is_not_suspended(self):
        import db
        assert db.is_suspension_active(None) is False


# ---------------------------------------------------------------------------
# Auth route edge cases
# ---------------------------------------------------------------------------

class TestAuthRouteEdgeCases:
    def test_login_with_wrong_password_fails(self, client, admin_user):
        resp = client.post("/login", data={"username": "admin", "password": "wrongpass"})
        # A failed login renders the login page (200). It must NOT redirect to home
        assert resp.status_code == 200

    def test_login_with_nonexistent_user_is_handled(self, client, admin_user):
        resp = client.post("/login", data={"username": "nobody", "password": "pw"})
        # Invalid username also stays on the login page
        assert resp.status_code == 200

    def test_logout_requires_post(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.get("/logout")
        # GET on logout should 405 or redirect
        assert resp.status_code in (302, 405)

    def test_setup_redirects_when_already_done(self, client, admin_user):
        # admin_user fixture sets setup_done=1
        resp = client.get("/setup")
        assert resp.status_code == 302

    def test_maintenance_page_accessible_when_maintenance_enabled(self, client, admin_user):
        import db
        db.update_site_settings(maintenance_mode=1)
        resp = client.get("/maintenance")
        assert resp.status_code == 200

    def test_session_conflict_page_accessible(self, client, admin_user):
        resp = client.get("/session-conflict")
        assert resp.status_code in (200, 302)

    def test_login_redirects_to_home_on_success(self, client, admin_user):
        resp = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Admin user management edge cases
# ---------------------------------------------------------------------------

class TestAdminUserManagementEdgeCases:
    def test_admin_cannot_delete_self(self, logged_in_admin, admin_user):
        resp = logged_in_admin.post(
            f"/admin/users/{admin_user}/edit",
            data={"action": "delete"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Exact error message from the route handler
        assert "Cannot delete your own account" in html
        import db
        assert db.get_user_by_id(admin_user) is not None

    def test_admin_can_delete_regular_user(self, logged_in_admin):
        import db
        uid = db.create_user("todel", generate_password_hash("pw"))
        resp = logged_in_admin.post(
            f"/admin/users/{uid}/edit",
            data={"action": "delete"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert db.get_user_by_id(uid) is None

    def test_admin_create_user_with_valid_data(self, logged_in_admin):
        import db
        resp = logged_in_admin.post("/admin/users/create", data={
            "username": "newadminuser",
            "password": "Str0ngPass",
            "confirm_password": "Str0ngPass",
            "role": "user",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_user_by_username("newadminuser") is not None

    def test_admin_create_user_duplicate_username_fails(self, logged_in_admin, admin_user):
        import db
        resp = logged_in_admin.post("/admin/users/create", data={
            "username": "admin",  # already exists
            "password": "Str0ngPass",
            "confirm_password": "Str0ngPass",
            "role": "user",
        }, follow_redirects=True)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Exact error message from admin_create_user route
        assert "Username already taken" in html
        # Only one user named "admin" should exist
        assert len([u for u in db.list_users() if u["username"] == "admin"]) == 1

    def test_admin_users_list_loads(self, logged_in_admin):
        resp = logged_in_admin.get("/admin/users")
        assert resp.status_code == 200

    def test_non_admin_cannot_access_admin_users(self, logged_in_user):
        resp = logged_in_user.get("/admin/users")
        assert resp.status_code in (302, 403)

    def test_editor_cannot_access_admin_panel(self, logged_in_editor):
        resp = logged_in_editor.get("/admin/users")
        assert resp.status_code in (302, 403)

    def test_admin_can_suspend_user(self, logged_in_admin):
        import db
        uid = db.create_user("suspendme", generate_password_hash("pw"))
        resp = logged_in_admin.post(
            f"/admin/users/{uid}/edit",
            data={"action": "suspend", "suspend_duration": "permanent"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        user = db.get_user_by_id(uid)
        assert user["suspended"] == 1

    def test_admin_can_unsuspend_user(self, logged_in_admin):
        import db
        uid = db.create_user("unsusp", generate_password_hash("pw"))
        db.update_user(uid, suspended=1, suspended_until=None)
        resp = logged_in_admin.post(
            f"/admin/users/{uid}/edit",
            data={"action": "unsuspend"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        user = db.get_user_by_id(uid)
        assert user["suspended"] == 0


# ---------------------------------------------------------------------------
# Admin settings edge cases
# ---------------------------------------------------------------------------

class TestAdminSettingsEdgeCases:
    _BASE = {
        "site_name": "BananaWiki",
        "timezone": "UTC",
        "primary_color": "#8fa0d4",
        "secondary_color": "#151520",
        "accent_color": "#6e8aca",
        "text_color": "#c8ccd8",
        "sidebar_color": "#111118",
        "bg_color": "#0d0d14",
    }

    def test_admin_settings_page_loads(self, logged_in_admin):
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200

    def test_admin_settings_update_site_name(self, logged_in_admin):
        import db
        resp = logged_in_admin.post("/global-settings", data={
            **self._BASE,
            "site_name": "My Custom Wiki",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["site_name"] == "My Custom Wiki"

    def test_admin_settings_invalid_color_rejected(self, logged_in_admin):
        import db
        resp = logged_in_admin.post("/global-settings", data={
            **self._BASE,
            "primary_color": "notacolor",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        # Color should not have been updated to invalid value
        assert settings["primary_color"] != "notacolor"


# ---------------------------------------------------------------------------
# API route edge cases
# ---------------------------------------------------------------------------

class TestAPIRouteEdgeCases:
    def test_search_api_requires_login(self, client, admin_user):
        resp = client.get("/api/pages/search?q=test")
        assert resp.status_code in (302, 401, 403)

    def test_search_api_returns_json_when_logged_in(self, logged_in_admin):
        resp = logged_in_admin.get("/api/pages/search?q=test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_preview_api_requires_login(self, client, admin_user):
        resp = client.post("/api/preview", json={"content": "# Hello"})
        assert resp.status_code in (302, 401, 403)

    def test_preview_api_returns_html_when_logged_in(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": "# Hello"},
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_preview_api_with_empty_content(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": ""},
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_my_drafts_returns_json(self, admin_user, logged_in_editor):
        resp = logged_in_editor.get("/api/draft/mine")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Wiki route edge cases
# ---------------------------------------------------------------------------

class TestWikiRouteEdgeCases:
    def test_home_redirects_when_no_pages(self, logged_in_admin):
        resp = logged_in_admin.get("/")
        assert resp.status_code in (200, 302)

    def test_nonexistent_page_returns_404(self, logged_in_admin):
        resp = logged_in_admin.get("/page/totally-nonexistent-page-xyz")
        assert resp.status_code == 404

    def test_create_and_view_page(self, logged_in_admin, admin_user):
        import db
        cat_id = db.create_category("Test Cat")
        db.create_page("Test Page", "test-page", "# Hello", cat_id, admin_user)
        resp = logged_in_admin.get("/page/test-page")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Test Page" in html

    def test_page_edit_requires_editor_role(self, logged_in_user):
        import db
        uid = db.create_user("editowner", generate_password_hash("pw"), role="editor")
        cat_id = db.create_category("Cat")
        db.create_page("My Page", "my-page", "content", cat_id, uid)
        resp = logged_in_user.get("/page/my-page/edit")
        assert resp.status_code in (302, 403)

    def test_page_history_requires_login(self, client, admin_user):
        import db
        cat_id = db.create_category("HC")
        db.create_page("Hist Page", "hist-page", "c", cat_id, admin_user)
        resp = client.get("/page/hist-page/history")
        assert resp.status_code in (302, 401, 403)

    def test_page_history_loads_for_logged_in_user(self, logged_in_admin, admin_user):
        import db
        cat_id = db.create_category("HC2")
        db.create_page("Hist Page 2", "hist-page-2", "c", cat_id, admin_user)
        resp = logged_in_admin.get("/page/hist-page-2/history")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Generate random ID edge cases
# ---------------------------------------------------------------------------

class TestGenerateRandomIdEdgeCases:
    def test_default_length_is_12(self):
        import db
        rid = db.generate_random_id()
        assert len(rid) == 12

    def test_custom_length_respected(self):
        import db
        for length in (4, 8, 16, 32):
            rid = db.generate_random_id(length=length)
            assert len(rid) == length

    def test_result_is_alphanumeric(self):
        import db
        import re
        rid = db.generate_random_id(length=20)
        assert re.match(r'^[A-Za-z0-9]+$', rid)

    def test_uniqueness_across_many_calls(self):
        import db
        ids = {db.generate_random_id() for _ in range(50)}
        # With 12 chars of base-62, collisions are astronomically unlikely
        assert len(ids) == 50


# ---------------------------------------------------------------------------
# Invite code DB edge cases
# ---------------------------------------------------------------------------

class TestInviteCodeDBEdgeCases:
    def test_generate_code_has_correct_format(self, admin_user):
        import db
        import re
        code = db.generate_invite_code(admin_user)
        assert re.match(r'^[A-Z0-9]{4}-[A-Z0-9]{4}$', code)

    def test_validate_nonexistent_code_returns_none(self, admin_user):
        import db
        result = db.validate_invite_code("XXXX-XXXX")
        assert result is None

    def test_list_invite_codes_includes_generated(self, admin_user):
        import db
        code = db.generate_invite_code(admin_user)
        codes = db.list_invite_codes()
        code_values = [c["code"] for c in codes]
        assert code in code_values

    def test_generated_code_is_valid(self, admin_user):
        import db
        code = db.generate_invite_code(admin_user)
        result = db.validate_invite_code(code)
        assert result is not None

    def test_delete_code_removes_it(self, admin_user):
        import db
        code = db.generate_invite_code(admin_user)
        codes_before = db.list_invite_codes()
        code_row = next(c for c in codes_before if c["code"] == code)
        db.delete_invite_code(code_row["id"])
        codes_after = db.list_invite_codes()
        assert not any(c["code"] == code for c in codes_after)
