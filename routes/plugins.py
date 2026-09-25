"""
Admin plugin management routes.

Provides the ``/admin/plugins`` UI for listing, enabling/disabling,
importing, deleting, and downloading the Plugin SDK.
"""

import io
import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone

from flask import (
    render_template, request, redirect, url_for, flash, abort,
    send_file, session,
)

import db
import config
import sqlite_snapshot
from helpers import login_required, admin_required, rate_limit, get_current_user
from helpers import t  # noqa: F401  (i18n)
from helpers._constants import MAX_PASSWORD_LENGTH
from helpers._passwords import check_password_hash
from wiki_logger import log_action, get_logger
from plugin_loader import (
    import_bwplugin,
    delete_external_plugin, get_loaded_plugins,
    trigger_plugin_enable, trigger_plugin_disable,
    get_plugin_extends, get_plugin_extended_by,
    get_plugin_extension_tree, get_manifest,
    get_all_manifests, is_builtin_plugin, plugin_data_tables,
    _is_denylisted,
)
from bananawiki_sdk._exceptions import PluginConfigError, PluginAPIVersionError
from bananawiki_sdk._hooks import get_registered_hooks
from bananawiki_sdk._slots import get_registered_slots

# Settings to reset when a plugin is disabled, keyed by plugin_id.
# Each entry maps DB column names to the value they should be set to.
_PLUGIN_SETTINGS_TO_DISABLE = {
    "page_governance": {
        # Page reservations
        "page_reservations_enabled": 0,
        "page_reservation_duration_hours": 48,
        "page_reservation_cooldown_hours": 24,
        "default_reserved_pages_quota": 5,
        # Page protection
        "page_protection_enabled": 0,
        # Contribution approval
        "contribution_approval_enabled": 0,
        "default_contribution_quota": 5,
    },
    "chat": {
        "chat_dm_enabled": 0,
        "chat_allow_dm_creation": 0,
        "chat_dm_auto_clear_messages": 0,
        "chat_dm_auto_clear_attachments": 0,
        "chat_attachments_enabled": 0,
        "chat_max_message_length": 5000,
        "chat_max_attachment_size_mb": 5,
        "chat_attachments_per_day_limit": 10,
        "chat_dm_message_retention_days": 0,
        "chat_dm_attachment_retention_days": 7,
        # Group chat settings (merged into the Chats and Groups plugin)
        "chat_group_enabled": 0,
        "chat_allow_group_creation": 0,
        "profile_group_badges_enabled": 0,
        "chat_group_message_retention_days": 0,
        "chat_group_attachment_retention_days": 7,
        # Chat cleanup scheduler settings (merged from former chat_cleanup plugin)
        "chat_cleanup_enabled": 0,
        "chat_cleanup_frequency_days": 7,
        "chat_cleanup_hour": 3,
        "chat_group_auto_clear_messages": 0,
        "chat_group_auto_clear_attachments": 0,
        "chat_auto_clear_messages": 0,
        "chat_auto_clear_attachments": 0,
    },

    "user_profiles": {
        "profile_group_badges_enabled": 0,
        "profile_contribution_chart_enabled": 0,
    },
    "deletion_slowdown": {"docs_bypass_deletion_slowdown": 0},
    "drafts": {},
    "tts": {
        "tts_page_panel_enabled": 0,
        "tts_public_access_enabled": 0,
        "tts_auto_generate_enabled": 0,
    },
    "kanban": {"kanban_access": "admin", "kanban_write_access": "admin"},
    "canvas": {"canvas_access": "admin", "canvas_write_access": "admin"},
    "assessments": {"assessment_points_badge_enabled": 0},
    "custom_pages": {"custom_pages_max_video_size_mb": 100},
    "attachments": {
        "upload_mode": "allow_all",
        "upload_whitelist": "",
        "upload_blacklist": "",
        "upload_max_size_mb": 100,
    },
}


def _external_plugins_off_message():
    """Return why external plugins are unavailable on this wiki."""
    if config.MANAGED_HOSTING:
        return t("admin.plugins.external_requires_isolation")
    return t("admin.plugins.external_disabled_self_hosted",
             default="External plugins are turned off on this wiki "
                     "(BW_ALLOW_EXTERNAL_PLUGINS=0). Built-in plugins can still be used.")


def _plugin_restriction(plugin):
    """Return a user-facing reason when *plugin* cannot run here."""
    plugin_id = plugin["id"]
    if _is_denylisted(plugin_id):
        return t("admin.plugins.restricted_builtin")
    if not is_builtin_plugin(plugin_id) and not config.ALLOW_EXTERNAL_PLUGINS:
        return _external_plugins_off_message()
    return ""


def _with_origin(plugin):
    """Return *plugin* as a dict whose ``builtin`` flag follows the plugin's folder.

    The column in the database is only a mirror that an enabled plugin
    could rewrite, so the admin pages show where the code actually lives.
    """
    plugin = dict(plugin)
    plugin["builtin"] = is_builtin_plugin(plugin["id"])
    return plugin


def _password_status():
    """Check the password re-entered on a plugin confirmation form.

    Returns ``"ok"``, ``"missing"`` (the form has not asked for it yet) or
    ``"wrong"``.  Installing, enabling or deleting external code hands over
    or takes away complete control of the wiki, so it asks for the password
    of the person at the keyboard: while an admin impersonates someone,
    that is the admin's own password, not the impersonated account's.
    """
    password = request.form.get("password", "")
    if not password:
        return "missing"
    account_id = session.get("impersonator_id") or session.get("user_id")
    account = db.get_user_by_id(account_id) if account_id else None
    if (
        account
        and len(password) <= MAX_PASSWORD_LENGTH
        and check_password_hash(account["password"], password)
    ):
        return "ok"
    return "wrong"


def _reject_wrong_password(action, **details):
    """Flash and record a failed password confirmation."""
    flash(t("flash.incorrect_password"), "error")
    log_action("plugin_password_rejected", request, user=get_current_user(),
               plugin_action=action, **details)


# Snapshots written by this module: "<UTC stamp>-<plugin id>[-n].db".  Older
# versions of the hosting platform kept quarantine and before-restore copies
# in the same folder; pruning leaves those alone.
_SNAPSHOT_NAME_RE = re.compile(r"^\d{8}T\d{6}Z-[A-Za-z0-9_-]+\.db$")
_SNAPSHOT_KEEP = 5


def _create_plugin_safety_snapshot(plugin_id):
    """Copy the database aside before external code runs or plugin data is dropped.

    Taken on every installation, self-hosted and hosted, before a
    non-built-in plugin is enabled and before an external plugin's tables
    are dropped.  The copy lives in
    ``<instance dir>/plugin_safety_snapshots/`` and the newest few are
    kept.  It covers the database only; docs/plugins/overview.md explains
    how to restore it.  Raises when the copy cannot be made, and the caller
    then stops: enabling external code or dropping tables without a way
    back is what the snapshot exists to prevent.
    """
    root = os.path.join(config.INSTANCE_DIR, "plugin_safety_snapshots")
    os.makedirs(root, mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = "".join(ch for ch in plugin_id if ch.isalnum() or ch in "_-")[:64]
    target = os.path.join(root, f"{stamp}-{safe_id}.db")
    counter = 1
    while os.path.lexists(target):
        counter += 1
        target = os.path.join(root, f"{stamp}-{safe_id}-{counter}.db")
    sqlite_snapshot.snapshot(config.DATABASE_PATH, target)
    if os.name != "nt":
        os.chmod(target, 0o600)
    ours = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not _SNAPSHOT_NAME_RE.fullmatch(name) or os.path.islink(path):
            continue
        if "-operator-quarantine" in name or "-before-restore" in name:
            continue
        ours.append(path)
    ours.sort(key=os.path.getmtime, reverse=True)
    for stale in ours[_SNAPSHOT_KEEP:]:
        try:
            os.unlink(stale)
        except OSError:
            pass
    return target


def register_plugin_routes(app):
    """Register admin plugin-management routes on *app*."""

    @app.route("/admin/plugins")
    @login_required
    @admin_required
    def admin_plugins():
        """List all installed plugins."""
        plugins = db.list_plugins()
        # Enrich each plugin with extends / extended_by info from manifests
        all_manifests = get_all_manifests()
        enriched = []
        for p in plugins:
            p = _with_origin(p)
            p["extends"] = get_plugin_extends(p["id"])
            p["extended_by"] = get_plugin_extended_by(p["id"])
            p["extension_tree"] = get_plugin_extension_tree(p["id"])
            p["extended_by_enabled"] = [eid for eid in p["extended_by"]
                                        if db.is_plugin_enabled(eid)]
            p["extends_missing"] = [dep_id for dep_id in p["extends"]
                                    if not db.is_plugin_enabled(dep_id)]
            p["restriction"] = _plugin_restriction(p)
            enriched.append(p)
        return render_template("admin/plugins.html", plugins=enriched,
                               all_manifests=all_manifests,
                               managed_hosting=config.MANAGED_HOSTING,
                               external_plugins_allowed=config.ALLOW_EXTERNAL_PLUGINS,
                               external_plugins_off=(
                                   "" if config.ALLOW_EXTERNAL_PLUGINS
                                   else _external_plugins_off_message()
                               ))

    @app.route("/admin/plugins/<plugin_id>")
    @login_required
    @admin_required
    def admin_plugin_detail(plugin_id):
        """Show details for a single plugin."""
        plugin = db.get_plugin(plugin_id)
        if not plugin:
            abort(404)
        plugin = _with_origin(plugin)
        loaded = get_loaded_plugins()
        loaded_info = loaded.get(plugin_id, {})
        manifest = loaded_info.get("manifest", {}) or get_manifest(plugin_id) or {}
        hooks = get_registered_hooks()
        slots = get_registered_slots()
        # Extension info
        extends = get_plugin_extends(plugin_id)
        extended_by = get_plugin_extended_by(plugin_id)
        extension_tree = get_plugin_extension_tree(plugin_id)
        all_manifests = get_all_manifests()
        return render_template(
            "admin/plugin_detail.html",
            plugin=plugin,
            manifest=manifest,
            hooks=hooks,
            slots=slots,
            extends=extends,
            extended_by=extended_by,
            extension_tree=extension_tree,
            all_manifests=all_manifests,
            restriction=_plugin_restriction(plugin),
        )

    @app.route("/admin/plugins/<plugin_id>/enable", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_enable_plugin(plugin_id):
        """Enable a plugin and activate it immediately without a server restart.

        A non-built-in plugin, or a non-built-in dependency enabled along
        with it, first gets a confirmation page that says what external
        code can do and asks for the admin's password again.  A snapshot of
        the database is taken before any of that code runs.
        """
        plugin = db.get_plugin(plugin_id)
        if not plugin:
            abort(404)
        plugin = _with_origin(plugin)
        restriction = _plugin_restriction(plugin)
        if restriction:
            flash(restriction, "error")
            return redirect(url_for("admin_plugins"))

        # Dependency check: ensure all extended plugins are enabled
        extends_ids = get_plugin_extends(plugin_id)
        missing_deps = [dep_id for dep_id in extends_ids
                        if not db.is_plugin_enabled(dep_id)]
        force = request.form.get("force") == "1"
        enable_deps = request.form.get("enable_deps") == "1" and not force
        if missing_deps and not force and not enable_deps:
            # Show the confirmation template
            dep_names = []
            for dep_id in missing_deps:
                dep_manifest = get_manifest(dep_id)
                dep_name = dep_manifest.get("name", dep_id) if dep_manifest else dep_id
                dep_names.append((dep_id, dep_name))
            return render_template(
                "admin/plugin_enable_confirm.html",
                plugin=plugin,
                plugin_manifest=get_manifest(plugin_id),
                missing_deps=dep_names,
            )

        deps_to_enable = missing_deps if enable_deps else []
        # Every dependency is checked before anything is enabled, so a refused
        # one never leaves the others half switched on.
        for dep_id in deps_to_enable:
            dep_plugin = db.get_plugin(dep_id)
            dep_restriction = (
                _plugin_restriction(dep_plugin) if dep_plugin else ""
            )
            if dep_restriction:
                flash(dep_restriction, "error")
                return redirect(url_for("admin_plugins"))

        external_ids = [
            pid for pid in (*deps_to_enable, plugin_id)
            if not is_builtin_plugin(pid)
        ]
        if external_ids:
            status = _password_status()
            if status != "ok":
                if status == "wrong":
                    _reject_wrong_password("enable", plugin_id=plugin_id)
                external_names = []
                for pid in external_ids:
                    pid_manifest = get_manifest(pid)
                    external_names.append(
                        (pid, pid_manifest.get("name", pid) if pid_manifest else pid)
                    )
                return render_template(
                    "admin/plugin_enable_external.html",
                    plugin=plugin,
                    plugin_manifest=get_manifest(plugin_id),
                    external_plugins=external_names,
                    force=force,
                    enable_deps=enable_deps,
                )
            try:
                snapshot = _create_plugin_safety_snapshot(plugin_id)
            except (OSError, ValueError, TimeoutError, sqlite3.Error) as exc:
                get_logger().error(
                    "Plugin safety snapshot before enabling '%s' failed: %s", plugin_id, exc
                )
                flash(t("admin.plugins.snapshot_failed",
                        default="The database could not be copied before the plugin "
                                "ran, so the plugin was not enabled."), "error")
                return redirect(url_for("admin_plugins"))
            log_action("plugin_code_trusted", request, user=get_current_user(),
                       plugin_id=plugin_id, external_plugins=",".join(external_ids),
                       password_confirmed=True,
                       snapshot=os.path.basename(snapshot))

        if missing_deps and force:
            flash(t("flash.plugin_enabled_with_missing_extensions",
                    name=plugin['name'], deps=", ".join(missing_deps)), "warning")
        for dep_id in deps_to_enable:
            dep_manifest = get_manifest(dep_id)
            dep_name = dep_manifest.get("name", dep_id) if dep_manifest else dep_id
            if not db.is_plugin_enabled(dep_id):
                db.enable_plugin(dep_id)
                trigger_plugin_enable(dep_id, app)
                log_action("plugin_enabled", request, user=get_current_user(),
                           plugin_id=dep_id,
                           origin="builtin" if is_builtin_plugin(dep_id) else "external")
                flash(t("flash.plugin_name_has_been_successfully_enabled",
                        name=dep_name), "success")

        db.enable_plugin(plugin_id)
        if not trigger_plugin_enable(plugin_id, app):
            db.disable_plugin(plugin_id)
            if external_ids:
                flash(t("admin.plugins.enable_failed_rolled_back"), "error")
            else:
                # No snapshot is taken for code that ships with BananaWiki.
                flash(t("admin.plugins.enable_failed_builtin",
                        default="The plugin could not start and was disabled again."), "error")
            return redirect(url_for("admin_plugins"))
        log_action("plugin_enabled", request, user=get_current_user(), plugin_id=plugin_id,
                   origin="builtin" if plugin["builtin"] else "external")
        flash(t("flash.plugin_name_has_been_successfully_enabled", name=plugin['name']), "success")
        return redirect(url_for("admin_plugins"))

    @app.route("/admin/plugins/<plugin_id>/disable", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_disable_plugin(plugin_id):
        """Disable a plugin and deactivate it immediately without a server restart."""
        plugin = db.get_plugin(plugin_id)
        if not plugin:
            abort(404)

        # Check if any enabled plugins extend this one
        extended_by = [eid for eid in get_plugin_extended_by(plugin_id)
                       if db.is_plugin_enabled(eid)]
        cascade = request.form.get("cascade") == "1"
        force = request.form.get("force") == "1"

        if extended_by and not cascade and not force:
            # Show the confirmation template
            dependent_names = []
            for eid in extended_by:
                e_manifest = get_manifest(eid)
                e_name = e_manifest.get("name", eid) if e_manifest else eid
                e_plugin = db.get_plugin(eid)
                dependent_names.append((eid, e_name, e_plugin["name"] if e_plugin else e_name))
            return render_template(
                "admin/plugin_disable_confirm.html",
                plugin=plugin,
                plugin_manifest=get_manifest(plugin_id),
                dependent_plugins=dependent_names,
            )

        db.disable_plugin(plugin_id)
        trigger_plugin_disable(plugin_id)

        # When a plugin is disabled, proactively reset its related feature-toggle
        # settings in the DB so the feature cannot remain active without its plugin.
        settings_to_reset = _PLUGIN_SETTINGS_TO_DISABLE.get(plugin_id)
        if settings_to_reset:
            db.update_site_settings(**settings_to_reset)

        # Dependency cascade: disable all plugins that extend this one
        if cascade:
            for eid in extended_by:
                db.disable_plugin(eid)
                trigger_plugin_disable(eid)
                log_action("plugin_disabled", request, user=get_current_user(), plugin_id=eid)
                e_manifest = get_manifest(eid)
                e_name = e_manifest.get("name", eid) if e_manifest else eid
                flash(t("flash.plugin_name_has_been_successfully_disabled", name=e_name), "success")

        log_action("plugin_disabled", request, user=get_current_user(), plugin_id=plugin_id)
        flash(t("flash.plugin_name_has_been_successfully_disabled", name=plugin['name']), "success")
        return redirect(url_for("admin_plugins"))

    @app.route("/admin/plugins/<plugin_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_delete_plugin(plugin_id):
        """Uninstall a plugin and optionally drop its data.

        Both external and built-in plugins can be uninstalled.  For built-in
        plugins the plugin files are preserved on disk; the plugin will
        reappear as *disabled* the next time the server starts, and its
        data is never dropped.

        Deleting an external plugin always goes through a confirmation page
        that asks for the admin's password again.  Dropping its data is a
        separate choice on that page, off unless ticked, which lists the
        exact tables; only tables that were listed are dropped.
        """
        plugin = db.get_plugin(plugin_id)
        if not plugin:
            abort(404)
        plugin = _with_origin(plugin)

        # Warn about plugins that extend this one
        extended_by = get_plugin_extended_by(plugin_id)
        extended_by_enabled = [eid for eid in extended_by if db.is_plugin_enabled(eid)]
        dependent_names = []
        for eid in extended_by_enabled:
            e_manifest = get_manifest(eid)
            e_name = e_manifest.get("name", eid) if e_manifest else eid
            dependent_names.append((eid, e_name))
        # Also show disabled dependents
        for eid in extended_by:
            if eid not in extended_by_enabled:
                e_manifest = get_manifest(eid)
                e_name = e_manifest.get("name", eid) if e_manifest else eid
                dependent_names.append((eid, e_name))

        drop_tables = []
        if plugin["builtin"]:
            if extended_by_enabled and request.form.get("confirm_delete_extended") != "1":
                return render_template(
                    "admin/plugin_delete_confirm.html",
                    plugin=plugin,
                    dependent_plugins=dependent_names,
                )
        else:
            data_tables = plugin_data_tables(plugin_id)
            status = _password_status()
            if status != "ok":
                if status == "wrong":
                    _reject_wrong_password("delete", plugin_id=plugin_id)
                return render_template(
                    "admin/plugin_delete_confirm.html",
                    plugin=plugin,
                    dependent_plugins=dependent_names if extended_by else [],
                    data_tables=data_tables,
                )
            if request.form.get("drop_data") == "1":
                shown = set(request.form.getlist("data_table"))
                drop_tables = [name for name in data_tables if name in shown]

        snapshot = ""
        if drop_tables:
            # A dropped table cannot come back without a backup, so copy the
            # database first and delete nothing if that fails.
            try:
                snapshot = os.path.basename(_create_plugin_safety_snapshot(plugin_id))
            except (OSError, ValueError, TimeoutError, sqlite3.Error) as exc:
                get_logger().error(
                    "Plugin safety snapshot before dropping the data of '%s' failed: %s",
                    plugin_id, exc,
                )
                flash(t("admin.plugins.drop_snapshot_failed",
                        default="The database could not be copied first, so nothing "
                                "was deleted."), "error")
                return redirect(url_for("admin_plugins"))

        ok = delete_external_plugin(plugin_id, drop_tables=drop_tables)
        if ok:
            log_action("plugin_deleted", request, user=get_current_user(),
                       plugin_id=plugin_id, drop_data=bool(drop_tables),
                       dropped_tables=",".join(drop_tables), snapshot=snapshot,
                       origin="builtin" if plugin["builtin"] else "external",
                       password_confirmed=not plugin["builtin"])
            if plugin["builtin"]:
                flash(
                    t("flash.plugin_name_has_been_successfully_uninstalled_it_will", name=plugin['name']),
                    "success",
                )
            else:
                flash(t("flash.plugin_name_has_been_successfully_deleted", name=plugin['name']),
                      "success")
        else:
            flash(t("flash.failed_to_delete_plugin"), "error")
        return redirect(url_for("admin_plugins"))

    @app.route("/admin/plugins/import", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_import_plugin():
        """Import an external .bwplugin file.

        The upload form asks for the admin's password again: the plugin is
        registered disabled and runs nothing yet, but it is the first step
        of handing complete control of the wiki to its author.
        """
        if not config.ALLOW_EXTERNAL_PLUGINS:
            flash(_external_plugins_off_message(), "error")
            return redirect(url_for("admin_plugins"))
        f = request.files.get("plugin_file")
        if not f or not f.filename:
            flash(t("flash.no_file_selected"), "error")
            return redirect(url_for("admin_plugins"))
        if not f.filename.endswith(".bwplugin"):
            flash(t("flash.only_bwplugin_files_are_accepted"), "error")
            return redirect(url_for("admin_plugins"))
        status = _password_status()
        if status != "ok":
            if status == "wrong":
                _reject_wrong_password("import", plugin_file=f.filename)
            else:
                flash(t("admin.plugins.password_required",
                        default="Enter your password to confirm."), "error")
            return redirect(url_for("admin_plugins"))

        # Save to a temp file for validation
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bwplugin")
        try:
            f.save(tmp.name)
            tmp.close()
            manifest = import_bwplugin(tmp.name)
            log_action("plugin_imported", request, user=get_current_user(),
                       plugin_id=manifest["id"], password_confirmed=True)
            flash(t("flash.plugin_name_imported_and_disabled_enable_it_when", name=manifest['name']), "success")
        except (PluginConfigError, PluginAPIVersionError) as exc:
            flash(t("flash.plugin_import_failed_exc", exc=exc), "error")
        except Exception as exc:
            flash(t("flash.unexpected_error_during_import_exc", exc=exc), "error")
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        return redirect(url_for("admin_plugins"))

    # Files from the SDK package to include in the SDK ZIP.
    _SDK_SOURCE_FILES = (
        "__init__.py",
        "_plugin.py",
        "_hooks.py",
        "_slots.py",
        "_database.py",
        "_re_exports.py",
        "_exceptions.py",
        "README.md",
    )

    # Documentation files from docs/plugins/ to include.
    _DOC_FILES = (
        "overview.md",
        "authoring.md",
        "api-reference.md",
    )

    def _build_sdk_zip():
        """Build an in-memory ZIP archive containing the Plugin SDK."""
        buf = io.BytesIO()
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # SDK source ---
            sdk_dir = os.path.join(base, "bananawiki_sdk")
            for fname in _SDK_SOURCE_FILES:
                fpath = os.path.join(sdk_dir, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, f"bananawiki_sdk/{fname}")

            # Documentation ---
            docs_dir = os.path.join(base, "docs", "plugins")
            for fname in _DOC_FILES:
                fpath = os.path.join(docs_dir, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, f"docs/{fname}")

            # Example plugins ---
            examples_dir = os.path.join(docs_dir, "examples")
            if os.path.isdir(examples_dir):
                for example_name in sorted(os.listdir(examples_dir)):
                    example_path = os.path.join(examples_dir, example_name)
                    if not os.path.isdir(example_path):
                        continue
                    for efile in sorted(os.listdir(example_path)):
                        efile_path = os.path.join(example_path, efile)
                        if os.path.isfile(efile_path):
                            zf.write(
                                efile_path,
                                f"examples/{example_name}/{efile}",
                            )

            # Starter template ---
            zf.writestr("starter_template/plugin.json", (
                '{\n'
                '    "id": "my_plugin",\n'
                '    "name": "My Plugin",\n'
                '    "version": "1.0.0",\n'
                '    "author": "Your Name",\n'
                '    "description": "A short description of your plugin.",\n'
                '    "bananawiki_api_version": "1.0"\n'
                '}\n'
            ))
            zf.writestr("starter_template/plugin_with_extends.json", (
                '{\n'
                '    "id": "my_extension_plugin",\n'
                '    "name": "My Extension Plugin",\n'
                '    "version": "1.0.0",\n'
                '    "author": "Your Name",\n'
                '    "description": "Extends another plugin.",\n'
                '    "extends": ["other_plugin_id"],\n'
                '    "bananawiki_api_version": "1.0"\n'
                '}\n'
            ))
            zf.writestr("starter_template/__init__.py", (
                '"""My Plugin: starter template for BananaWiki plugins."""\n'
                '\n'
                'from bananawiki_sdk import (\n'
                '    Plugin, hook, template_slot,\n'
                '    db_query, db_execute,\n'
                '    login_required, rate_limit,\n'
                '    get_current_user, flash, redirect, url_for,\n'
                ')\n'
                '\n'
                'plugin = Plugin("my_plugin")\n'
                '\n'
                '\n'
                '@plugin.on_load\n'
                'def setup(app):\n'
                '    """Register routes, create tables, and set up hooks."""\n'
                '\n'
                '    # Create plugin-owned tables (prefix with plugin id)\n'
                '    db_execute(\n'
                '        "CREATE TABLE IF NOT EXISTS my_plugin__data "\n'
                '        "(id INTEGER PRIMARY KEY AUTOINCREMENT, "\n'
                '        " key TEXT NOT NULL, value TEXT NOT NULL)"\n'
                '    )\n'
                '\n'
                '    @app.route("/my-plugin")\n'
                '    @login_required\n'
                '    def my_plugin_index():\n'
                '        """Main plugin page."""\n'
                '        user = get_current_user()\n'
                '        return f"<h1>My Plugin</h1>"\n'
                '\n'
                '    @app.route("/my-plugin/action", methods=["POST"])\n'
                '    @login_required\n'
                '    @rate_limit(max_requests=10, window=60)\n'
                '    def my_plugin_action():\n'
                '        """Handle a form submission."""\n'
                '        flash(t("flash.action_completed"), "success")\n'
                '        return redirect(url_for("my_plugin_index"))\n'
                '\n'
                '\n'
                '@plugin.on_enable\n'
                'def enabled():\n'
                '    """Called when an admin enables this plugin."""\n'
                '    pass\n'
                '\n'
                '\n'
                '@plugin.on_disable\n'
                'def disabled():\n'
                '    """Called when an admin disables this plugin."""\n'
                '    pass\n'
                '\n'
                '\n'
                '@hook("after_page_update")\n'
                'def on_page_update(page, user, **kwargs):\n'
                '    """React to page edits: replace with your logic."""\n'
                '    pass\n'
                '\n'
                '\n'
                '@template_slot("page.below_content")\n'
                'def render_below_page(context):\n'
                '    """Inject HTML below wiki page content."""\n'
                '    page = context.get("page")\n'
                '    if not page:\n'
                '        return ""\n'
                '    return ""\n'
            ))

            # README for the SDK ---
            zf.writestr("README.md", (
                "# BananaWiki Plugin SDK\n\n"
                "This archive contains everything you need to build\n"
                "custom plugins for BananaWiki.\n\n"
                "## Contents\n\n"
                "| Directory | Description |\n"
                "|-----------|-------------|\n"
                "| `bananawiki_sdk/` | The Plugin SDK source. The public API your plugins import from |\n"
                "| `docs/` | Plugin documentation (overview, authoring guide, API reference) |\n"
                "| `examples/` | Working example plugins you can study and adapt |\n"
                "| `starter_template/` | A ready-to-use plugin template with all boilerplate |\n\n"
                "## Quick Start\n\n"
                "1. Copy `starter_template/` to a new directory and rename it\n"
                "   (e.g. `my_awesome_plugin/`).\n"
                "2. Edit `plugin.json`: set `id`, `name`, `version`, and `description`.\n"
                "3. Edit `__init__.py`: add your routes, hooks, and logic.\n"
                "4. Package as a `.bwplugin` file:\n"
                "   ```\n"
                "   cd my_awesome_plugin/\n"
                "   zip -r ../my_awesome_plugin.bwplugin plugin.json __init__.py\n"
                "   ```\n"
                "5. Upload via **Admin → Plugins → Import External Plugin**.\n\n"
                "## Documentation\n\n"
                "- `docs/overview.md`: Architecture and plugin system overview\n"
                "- `docs/authoring.md`: Step-by-step guide to building plugins\n"
                "- `docs/api-reference.md`: Complete SDK API reference\n"
                "- `bananawiki_sdk/README.md`: SDK package README with quick reference\n\n"
                "## SDK API Highlights\n\n"
                "```python\n"
                "from bananawiki_sdk import (\n"
                "    Plugin,              # Plugin registration class\n"
                "    hook, emit_hook,     # Event subscription and emission\n"
                "    template_slot,       # Inject HTML into core templates\n"
                "    db_query,            # Read-only SQL access to core tables\n"
                "    db_execute,          # Write access to plugin-owned tables\n"
                "    login_required,      # Auth decorator\n"
                "    admin_required,      # Admin-only decorator\n"
                "    rate_limit,          # Per-IP rate limiting\n"
                "    get_current_user,    # Current logged-in user\n"
                "    has_permission,      # Permission check\n"
                "    is_plugin_enabled,   # Check if another plugin is active\n"
                "    get_setting,         # Read site settings\n"
                ")\n"
                "```\n\n"
                "## Core Hooks\n\n"
                "| Hook | Payload | When |\n"
                "|------|---------|------|\n"
                "| `after_page_create` | `page`, `user` | New page saved |\n"
                "| `after_page_update` | `page`, `user` | Page edited |\n"
                "| `after_page_delete` | `page`, `user` | Page deleted |\n"
                "| `after_login` | `user` | User logs in |\n"
                "| `after_user_create` | `user` | New account created |\n\n"
                "## Template Slots\n\n"
                "| Slot | Context | Location |\n"
                "|------|---------|----------|\n"
                "| `page.below_content` | `page`, `user` | Below wiki page body |\n"
                "| `sidebar.bottom` | `user` | Bottom of navigation sidebar |\n\n"
                "## Requirements\n\n"
                "- BananaWiki with SDK API version `1.0` or later\n"
                "- Python 3.10+\n"
                "- No access to BananaWiki source code is needed: \n"
                "  the SDK provides all the interfaces your plugin needs.\n"
            ))

        buf.seek(0)
        return buf

    @app.route("/admin/plugins/sdk-download")
    @login_required
    @admin_required
    def admin_plugins_sdk_download():
        """Download the Plugin SDK as a ZIP archive."""
        buf = _build_sdk_zip()
        log_action("plugin_sdk_downloaded", request, user=get_current_user())
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name="bananawiki-plugin-sdk.zip",
        )
