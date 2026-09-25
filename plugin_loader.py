"""
BananaWiki Plugin Loader
========================

Discovers, validates, and loads plugins from ``plugins/builtin/`` and
``plugins/external/``.  Called from ``app.py`` during application startup.

Each plugin directory must contain a ``plugin.json`` manifest and an
``__init__.py`` entry point.

Where a plugin's code lives decides what it is.  A folder under
``BUILTIN_DIR`` is built-in; anything found under ``EXTERNAL_DIR`` is
external, whatever the ``builtin`` column of the ``plugins`` table says.
That column only mirrors the folder for the admin pages: an enabled plugin
can write to the database, so nothing here trusts it.

External plugins run inside the wiki process with the wiki's own
privileges.  Disabling one stops its routes, hooks, template slots, request
handlers and error handlers at once, in every worker, because each of those
checks the registry before it runs.  Code the plugin has already executed
(threads it started, objects it patched) only goes away when the wiki
restarts.
"""

import importlib
import functools
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import zipfile

from werkzeug.exceptions import HTTPException

import config
import db
from db._plugins import sync_plugin_origins
from bananawiki_sdk._exceptions import (
    PluginConfigError,
    PluginAPIVersionError,
    PluginError,
)
from bananawiki_sdk import API_VERSION
from bananawiki_sdk._database import _core_tables
from bananawiki_sdk._hooks import (
    _plugin_is_active,
    _restore_hooks,
    _snapshot_hook_fns,
    _stamp_new_hooks,
    _unregister_hooks,
)
from bananawiki_sdk._slots import (
    _restore_slots,
    _snapshot_slot_fns,
    _stamp_new_slots,
    _unregister_slots,
)


def _get_plugin_logger():
    """Lazy accessor to avoid circular imports at module load time."""
    from wiki_logger import get_logger
    return get_logger()

# Directories where plugins live (relative to project root)
# EXTERNAL_DIR can be overridden via BW_EXTERNAL_PLUGINS_DIR to support
# read-only deployments where the install tree is not writable.
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIR = os.path.join(_PROJECT_ROOT, "plugins", "builtin")
EXTERNAL_DIR = os.environ.get(
    "BW_EXTERNAL_PLUGINS_DIR",
    os.path.join(_PROJECT_ROOT, "plugins", "external"),
)

# In-memory store of loaded Plugin objects, keyed by plugin_id
_loaded_plugins = {}

# Set of plugin_ids whose ``on_load`` handler has already been called.
# Prevents duplicate route/blueprint registration when a plugin is disabled
# and re-enabled at runtime (Flask 3.x raises on duplicate Blueprint names
# and ``@app.route`` registrations).
_LOADED_ONCE = set()

# Hooks and template slots set aside when a plugin is disabled, keyed by
# plugin id, so that enabling it again in the same process restores them
# instead of importing the code a second time.  on_load runs only once per
# process, so anything it registered could not come back any other way.
_PARKED = {}

# Endpoints each plugin added to the Flask app while its on_load ran.  The
# routes stay in Flask's routing table for the life of the process, so
# app.py answers 404 for them whenever the owning plugin is not enabled.
# Endpoints of a deleted plugin are handed to _DELETED_OWNER, an id no
# plugin can have, which keeps them closed until the next restart even if
# another copy of the plugin is installed under the same id.
_ENDPOINT_OWNERS = {}
_DELETED_OWNER = "<deleted plugin>"

# Request and error handlers wrapped by _guarded and _guarded_error_handler,
# keyed by the plugin that added them.  A deleted plugin's handlers are
# handed to _DELETED_OWNER in the same way.
_GUARDED_HANDLERS = {}

# The installed_at of the registry row each external plugin's code was
# imported for in this process.  It outlives a disable (the plugin's hooks
# are only parked then), so a worker can tell that a plugin was deleted or
# reinstalled through another worker even while it is not loaded here; see
# _retire_replaced.  Built-ins are never recorded.
_IMPORTED_FOR = {}

# Plugins the registry has enabled but that could not be loaded in this
# process, with the time of the last attempt (see sync_with_registry).
_sync_failures = {}
_SYNC_RETRY_SECONDS = 300

# Messages already logged by _warn_once.  Discovery runs on every admin
# page view, so a problem folder would otherwise be reported each time.
_warned = set()

# Lock for thread-safe Blueprint registration (prevents race condition with _got_first_request)
_BLUEPRINT_REGISTRATION_LOCK = threading.Lock()
_PLUGIN_LIFECYCLE_LOCK = threading.RLock()


def _serialized_lifecycle(function):
    """Keep hook ownership and plugin state consistent during concurrent changes."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _PLUGIN_LIFECYCLE_LOCK:
            return function(*args, **kwargs)
    return wrapper


def _warn_once(key, message, *args):
    """Log *message* the first time *key* is seen in this process."""
    if key in _warned:
        return
    _warned.add(key)
    _get_plugin_logger().warning(message, *args)


# Text fields of plugin.json: (required, maximum length).  They are copied
# into the plugins table and shown on the admin pages, so anything that is
# not a short string is refused before it reaches either.
_MANIFEST_TEXT_FIELDS = {
    "id": (True, 64),
    "name": (True, 100),
    "version": (True, 50),
    "author": (False, 200),
    "description": (False, 2000),
}


def _check_manifest_fields(manifest):
    """Raise :class:`PluginConfigError` unless *manifest* is well formed."""
    if not isinstance(manifest, dict):
        raise PluginConfigError("plugin.json must contain a JSON object.")
    for field, (required, limit) in _MANIFEST_TEXT_FIELDS.items():
        if field not in manifest:
            if required:
                raise PluginConfigError(
                    f"plugin.json is missing required field '{field}'"
                )
            continue
        value = manifest[field]
        if (
            not isinstance(value, str)
            or len(value) > limit
            or (required and not value.strip())
        ):
            raise PluginConfigError(
                f"plugin.json field '{field}' must be text of at most "
                f"{limit} characters."
            )
    if "extends" in manifest:
        ext = manifest["extends"]
        if not isinstance(ext, list) or not all(isinstance(e, str) for e in ext):
            raise PluginConfigError(
                f"plugin.json 'extends' must be a list of plugin ID strings, "
                f"got {type(ext).__name__}"
            )
    permissions = manifest.get("permissions", [])
    if not isinstance(permissions, list) or not all(
        isinstance(item, str) and item.strip() for item in permissions
    ):
        raise PluginConfigError("plugin.json 'permissions' must be a list of strings.")


def _read_manifest(plugin_dir):
    """Read and validate ``plugin.json`` from *plugin_dir*.

    Returns the parsed dict.  Raises :class:`PluginConfigError` on problems.
    """
    manifest_path = os.path.join(plugin_dir, "plugin.json")
    if not os.path.isfile(manifest_path):
        raise PluginConfigError(
            f"Missing plugin.json in {plugin_dir}"
        )
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PluginConfigError(
            f"Invalid JSON in plugin.json: {exc}"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginConfigError(
            f"Cannot read plugin.json in {plugin_dir}: {exc}"
        ) from exc
    _check_manifest_fields(manifest)
    return manifest


def _check_api_version(manifest):
    """Raise if the manifest requires an incompatible API version."""
    required = manifest.get("bananawiki_api_version", API_VERSION)
    try:
        req_major = int(str(required).split(".")[0])
        cur_major = int(API_VERSION.split(".")[0])
    except (ValueError, IndexError) as err:
        raise PluginAPIVersionError(
            f"Plugin '{manifest['id']}' declares an unparseable "
            f"bananawiki_api_version '{required}'. "
            f"Expected a version string like '{API_VERSION}'."
        ) from err
    if req_major != cur_major:
        raise PluginAPIVersionError(
            f"Plugin '{manifest['id']}' requires API version {required}, "
            f"but BananaWiki SDK provides {API_VERSION}."
        )


def _within(path, root):
    """Return whether *path* is *root* or inside it, after resolving links."""
    try:
        path = os.path.realpath(path)
        root = os.path.realpath(root)
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def builtin_plugin_ids():
    """Return the ids reserved by the plugins shipped under ``BUILTIN_DIR``.

    Both the folder name and the manifest id count, and experimental
    built-ins count too even though they are not seeded, so an uploaded
    plugin can never take a built-in's place.
    """
    ids = set()
    if not os.path.isdir(BUILTIN_DIR):
        return frozenset()
    for entry in os.listdir(BUILTIN_DIR):
        plugin_dir = os.path.join(BUILTIN_DIR, entry)
        if not os.path.isfile(os.path.join(plugin_dir, "plugin.json")):
            continue
        ids.add(entry)
        try:
            ids.add(_read_manifest(plugin_dir)["id"])
        except PluginConfigError:
            continue
    return frozenset(ids)


# Table name prefixes that a built-in plugin uses outside the core schema
# where the prefix is not the plugin's own id: user_profiles keeps its custom
# fields in user_profile_fields__definitions and user_profile_fields__values.
# An upload may not take one of these as its id, and "drop data" never offers
# those tables to another plugin.
_BUILTIN_TABLE_NAMESPACES = frozenset({"user_profile_fields"})


def is_builtin_plugin(plugin_id):
    """Return whether *plugin_id* is shipped with BananaWiki.

    Decided by the ``plugins/builtin`` folder alone, never by the database.
    """
    return plugin_id in builtin_plugin_ids()


def _is_denylisted(plugin_id):
    """Return whether managed hosting forbids *plugin_id*.

    The comparison ignores case, like SQLite table names and some file
    systems do.  The list is keyed on ids only, so it is a policy switch
    and not a boundary: the same code uploaded under another id is not
    caught by it.
    """
    if not getattr(config, "MANAGED_HOSTING", False):
        return False
    denied = {str(item).casefold() for item in getattr(config, "MANAGED_PLUGIN_DENYLIST", ())}
    return str(plugin_id).casefold() in denied


def _id_taken_by(plugin_id):
    """Return ``"builtin"`` or ``"installed"`` if *plugin_id* is in use, else ``None``.

    Ids are compared case-insensitively.  A folder in ``EXTERNAL_DIR``
    without a registry row counts as installed: it is registered again at
    the next start.
    """
    folded = plugin_id.casefold()
    reserved = builtin_plugin_ids() | _BUILTIN_TABLE_NAMESPACES
    if any(existing.casefold() == folded for existing in reserved):
        return "builtin"
    if any(row["id"].casefold() == folded for row in db.list_plugins()):
        return "installed"
    if os.path.isdir(EXTERNAL_DIR):
        for entry in os.listdir(EXTERNAL_DIR):
            if entry.casefold() == folded:
                return "installed"
    return None


def discover_plugins():
    """Return a list of ``(plugin_dir, manifest, is_builtin)`` tuples for
    every plugin found on disk.

    Built-ins come first and win every id clash.  ``is_builtin`` is decided
    by the folder alone.  An external folder is skipped when:

    * its name starts with a dot (an upload still being unpacked, or one a
      crash left behind),
    * its folder name differs from the id in its manifest,
    * its id is already taken, compared case-insensitively,
    * managed hosting forbids its id.
    """
    results = []
    seen = set()
    if os.path.isdir(BUILTIN_DIR):
        for entry in sorted(os.listdir(BUILTIN_DIR)):
            plugin_dir = os.path.join(BUILTIN_DIR, entry)
            if not os.path.isfile(os.path.join(plugin_dir, "plugin.json")):
                continue
            try:
                manifest = _read_manifest(plugin_dir)
            except PluginConfigError:
                continue
            folded = manifest["id"].casefold()
            if folded in seen:
                continue
            seen.add(folded)
            results.append((plugin_dir, manifest, True))

    if getattr(config, "ALLOW_EXTERNAL_PLUGINS", True) and os.path.isdir(EXTERNAL_DIR):
        for entry in sorted(os.listdir(EXTERNAL_DIR)):
            if entry.startswith("."):
                continue
            plugin_dir = os.path.join(EXTERNAL_DIR, entry)
            if not os.path.isfile(os.path.join(plugin_dir, "plugin.json")):
                continue
            try:
                manifest = _read_manifest(plugin_dir)
            except PluginConfigError:
                continue
            plugin_id = manifest["id"]
            if not _plugin_id_is_valid(plugin_id) or plugin_id != entry:
                _warn_once(
                    ("folder", plugin_dir),
                    "[plugin-loader] Ignoring %s: an external plugin's folder "
                    "must be named after the id in its plugin.json.",
                    plugin_dir,
                )
                continue
            if plugin_id.casefold() in seen:
                _warn_once(
                    ("duplicate", plugin_dir),
                    "[plugin-loader] Ignoring external plugin %s: the id '%s' "
                    "already belongs to another plugin. The folder can be removed.",
                    plugin_dir,
                    plugin_id,
                )
                continue
            if _is_denylisted(plugin_id):
                _warn_once(
                    ("denylist", plugin_dir),
                    "[plugin-loader] Ignoring external plugin '%s': managed "
                    "hosting does not allow this id.",
                    plugin_id,
                )
                continue
            seen.add(plugin_id.casefold())
            results.append((plugin_dir, manifest, False))

    return results


def _load_plugin_module(plugin_dir, manifest):
    """Import the plugin's ``__init__.py`` and return the module."""
    plugin_id = manifest["id"]
    if not _within(plugin_dir, BUILTIN_DIR):
        _get_plugin_logger().warning(
            "[plugin-loader] Loading EXTERNAL plugin '%s' from %s: "
            "external plugins run with full application privileges. "
            "Only install plugins from sources you trust.",
            plugin_id,
            plugin_dir,
        )
    module_name = f"_bw_plugin_{plugin_id}"
    # Child modules can declare hooks too. Re-import them after a disable so
    # their decorators register again instead of reusing an inactive cache.
    for cached_name in list(sys.modules):
        if cached_name.startswith(module_name + "."):
            del sys.modules[cached_name]

    spec = importlib.util.spec_from_file_location(
        module_name,
        os.path.join(plugin_dir, "__init__.py"),
        submodule_search_locations=[plugin_dir],
    )
    if spec is None:
        raise PluginError(f"Cannot find __init__.py for plugin '{plugin_id}'")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Request-level handler registries of the Flask app that a plugin's on_load
# may add to, with what a handler returns while its plugin is not enabled.
_GUARDED_HANDLER_REGISTRIES = (
    ("before_request_funcs", "none"),
    ("after_request_funcs", "response"),
    ("teardown_request_funcs", "none"),
    ("template_context_processors", "context"),
    ("url_value_preprocessors", "none"),
    ("url_default_functions", "none"),
)


def _handler_snapshot(app):
    """Record the request handlers registered on *app* right now."""
    snapshot = {}
    for attr, _fallback in _GUARDED_HANDLER_REGISTRIES:
        registry = getattr(app, attr, None) or {}
        snapshot[attr] = {key: {id(fn) for fn in fns} for key, fns in registry.items()}
    snapshot["teardown_appcontext_funcs"] = {
        id(fn) for fn in getattr(app, "teardown_appcontext_funcs", ())
    }
    # Error handlers are keyed rather than listed, so keep the handler
    # itself: one the plugin replaces is what answers again while it is off.
    snapshot["error_handler_spec"] = {
        (scope, code, exc_class): fn
        for scope, codes in (getattr(app, "error_handler_spec", None) or {}).items()
        for code, handlers in codes.items()
        for exc_class, fn in handlers.items()
    }
    return snapshot


def _guarded(plugin_id, fn, fallback):
    """Wrap a plugin's request handler so it only runs while the plugin is enabled.

    The owner is read from the wrapper at call time, so deleting the plugin
    can hand the handler to _DELETED_OWNER and keep it off even when a new
    copy is installed under the same id.
    """
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        if _plugin_is_active(guarded._bw_plugin_id):
            return fn(*args, **kwargs)
        if fallback == "response":
            return args[0] if args else None
        if fallback == "context":
            return {}
        return None
    guarded._bw_plugin_id = plugin_id
    _GUARDED_HANDLERS.setdefault(plugin_id, []).append(guarded)
    return guarded


def _guarded_error_handler(plugin_id, fn, previous):
    """Wrap a plugin's error handler so it only runs while the plugin is enabled.

    While the plugin is off, the handler it replaced (the core's 404 page,
    for example) answers again.  Where it replaced nothing, the error goes
    on to Flask's own handling, as if the plugin had never registered one:
    an HTTP error is returned as it is, anything else is raised again.
    """
    @functools.wraps(fn)
    def guarded(error):
        if _plugin_is_active(guarded._bw_plugin_id):
            return fn(error)
        if previous is not None:
            return previous(error)
        if isinstance(error, HTTPException):
            return error
        raise error
    guarded._bw_plugin_id = plugin_id
    _GUARDED_HANDLERS.setdefault(plugin_id, []).append(guarded)
    return guarded


def _guard_new_handlers(plugin_id, app, snapshot):
    """Wrap every request handler added to *app* since *snapshot*."""
    for attr, fallback in _GUARDED_HANDLER_REGISTRIES:
        registry = getattr(app, attr, None)
        if not registry:
            continue
        for key, fns in registry.items():
            before = snapshot[attr].get(key, set())
            for index, fn in enumerate(fns):
                if id(fn) not in before:
                    fns[index] = _guarded(plugin_id, fn, fallback)
    funcs = getattr(app, "teardown_appcontext_funcs", None)
    if funcs is not None:
        before = snapshot["teardown_appcontext_funcs"]
        for index, fn in enumerate(funcs):
            if id(fn) not in before:
                funcs[index] = _guarded(plugin_id, fn, "none")
    spec = getattr(app, "error_handler_spec", None)
    if spec:
        before = snapshot["error_handler_spec"]
        for scope, codes in spec.items():
            for code, handlers in codes.items():
                for exc_class, fn in list(handlers.items()):
                    previous = before.get((scope, code, exc_class))
                    if fn is not previous:
                        handlers[exc_class] = _guarded_error_handler(plugin_id, fn, previous)


def _run_on_load(plugin_id, on_load, app):
    """Call a plugin's on_load and record what it added to *app*.

    New endpoints are remembered so app.py can close them while the plugin
    is off, and new request and error handlers are wrapped so they stop
    running.  View functions the plugin replaced rather than added, and
    anything it changed outside Flask's registries, are out of reach until
    a restart.
    """
    endpoints_before = set(app.view_functions)
    handlers_before = _handler_snapshot(app)
    # Flask 3.x raises AssertionError if register_blueprint() is
    # called after the first request has been processed.  When a
    # plugin is enabled at runtime (via the admin UI), we are
    # already inside a request, so _got_first_request is True.
    # Temporarily reset the flag so Blueprint registration
    # succeeds, then restore the original value.
    # Use a lock to prevent race conditions with concurrent requests.
    with _BLUEPRINT_REGISTRATION_LOCK:
        previous = getattr(app, "_got_first_request", False)
        try:
            app._got_first_request = False
            on_load(app)
        finally:
            app._got_first_request = previous
            for endpoint in set(app.view_functions) - endpoints_before:
                _ENDPOINT_OWNERS[endpoint] = plugin_id
            _guard_new_handlers(plugin_id, app, handlers_before)


def _align_plugin_id(plugin_obj, plugin_id):
    """Make the plugin object carry the id from its manifest."""
    if plugin_obj is None:
        return
    current = getattr(plugin_obj, "plugin_id", plugin_id)
    if current == plugin_id:
        return
    _warn_once(
        ("plugin_id", plugin_id),
        "[plugin-loader] Plugin '%s' creates Plugin(%r); using the id from "
        "its plugin.json instead.",
        plugin_id,
        current,
    )
    try:
        plugin_obj.plugin_id = plugin_id
    except AttributeError:
        pass


def _activate(plugin_id, plugin_dir, manifest, app, *, fresh):
    """Load *plugin_id* into this process and return its plugin object.

    With *fresh* false, a plugin that was loaded and then disabled in this
    process gets its parked hooks and slots back instead of a second
    import.  Startup passes ``fresh=True`` and always imports.

    Everything the plugin registers while it loads, including inside
    on_load, is stamped with the manifest id, overwriting whatever id the
    code put on it, so disabling the plugin reliably removes it.
    """
    _check_api_version(manifest)
    module_name = f"_bw_plugin_{plugin_id}"
    parked = _PARKED.pop(plugin_id, None)
    if (
        not fresh
        and parked is not None
        and plugin_id in _LOADED_ONCE
        and module_name in sys.modules
    ):
        _restore_hooks(parked[0])
        _restore_slots(parked[1])
        mod = sys.modules[module_name]
    else:
        _unregister_hooks(plugin_id)
        _unregister_slots(plugin_id)
        hook_snap = _snapshot_hook_fns()
        slot_snap = _snapshot_slot_fns()
        if not _within(plugin_dir, BUILTIN_DIR):
            # Recorded before any of the code runs, so what it registers is
            # accounted for even if loading fails half way.
            _IMPORTED_FOR[plugin_id] = _registry_installed_at(plugin_id)
        try:
            mod = _load_plugin_module(plugin_dir, manifest)
            plugin_obj = getattr(mod, "plugin", None)
            _align_plugin_id(plugin_obj, plugin_id)
            if plugin_id not in _LOADED_ONCE:
                on_load = getattr(plugin_obj, "_on_load_fn", None) if plugin_obj is not None else None
                if on_load:
                    _run_on_load(plugin_id, on_load, app)
                _LOADED_ONCE.add(plugin_id)
        except BaseException:
            # A plugin that failed half way must not leave hooks behind.
            _stamp_new_hooks(plugin_id, hook_snap, overwrite=True)
            _stamp_new_slots(plugin_id, slot_snap, overwrite=True)
            _unregister_hooks(plugin_id)
            _unregister_slots(plugin_id)
            raise
        _stamp_new_hooks(plugin_id, hook_snap, overwrite=True)
        _stamp_new_slots(plugin_id, slot_snap, overwrite=True)
    plugin_obj = getattr(mod, "plugin", None)
    builtin = _within(plugin_dir, BUILTIN_DIR)
    _loaded_plugins[plugin_id] = {
        "module": mod,
        "manifest": manifest,
        "plugin_obj": plugin_obj,
        "dir": plugin_dir,
        "builtin": builtin,
        # Which registry row this code was imported for; see _retire_replaced.
        "installed_at": (
            _IMPORTED_FOR.get(plugin_id) if not builtin
            else _registry_installed_at(plugin_id)
        ),
    }
    return plugin_obj


def _registry_installed_at(plugin_id):
    """Return the ``installed_at`` of *plugin_id*'s registry row, or ``None``."""
    try:
        row = db.get_plugin(plugin_id)
    except Exception:
        return None
    if row is None or "installed_at" not in row.keys():
        return None
    return row["installed_at"]


def _drop_request_registry_view():
    """Forget the registry map cached for the current request, if any.

    ``app.py`` reads the registry once per request, before the handler
    runs.  A request that enables or disables a plugin would otherwise keep
    dispatching hooks by the state it started with.
    """
    try:
        from flask import g, has_app_context
        if has_app_context():
            g.pop("plugin_registry_enabled", None)
    except ImportError:
        pass


def _fire(plugin_obj, attr, plugin_id, label):
    """Call a lifecycle callback, logging instead of raising on failure."""
    callback = getattr(plugin_obj, attr, None) if plugin_obj is not None else None
    if not callback:
        return
    try:
        callback()
    except Exception as exc:
        _get_plugin_logger().error("[plugin-loader] Error in %s for '%s': %s", label, plugin_id, exc)


@_serialized_lifecycle
def load_enabled_plugins(app):
    """Discover all plugins, seed the DB, and load those that are enabled.

    This is the main entry point called from ``app.py``.  One broken
    plugin is logged and skipped; it never stops the wiki from starting.
    """
    _remove_stale_staging_dirs()
    plugins = discover_plugins()

    # Seed all built-in plugins into the DB
    builtin_manifests = [m for _, m, b in plugins if b]
    db.seed_builtin_plugins(builtin_manifests)

    # Seed discovered external plugins so they appear in Admin → Plugins
    for _dir, manifest, is_builtin in plugins:
        if is_builtin:
            continue
        try:
            db.register_plugin(
                manifest["id"],
                name=manifest.get("name", manifest["id"]),
                version=manifest.get("version", "0.0.0"),
                author=manifest.get("author", ""),
                description=manifest.get("description", ""),
                builtin=False,
                enabled=False,
                update_existing=False,
            )
        except Exception as exc:
            _get_plugin_logger().error(
                "[plugin-loader] Could not register plugin '%s': %s", manifest["id"], exc
            )

    try:
        sync_plugin_origins(
            builtin_manifests,
            [m["id"] for _, m, b in plugins if not b],
        )
    except Exception as exc:
        _get_plugin_logger().error("[plugin-loader] Could not update plugin origins: %s", exc)

    # Load enabled plugins
    for plugin_dir, manifest, _is_builtin in plugins:
        plugin_id = manifest["id"]
        try:
            if _is_denylisted(plugin_id):
                if db.is_plugin_enabled(plugin_id):
                    db.disable_plugin(plugin_id)
                    _get_plugin_logger().warning(
                        "[plugin-loader] Disabled hosted-restricted built-in '%s'",
                        plugin_id,
                    )
                continue
            if not db.is_plugin_enabled(plugin_id):
                continue
            _activate(plugin_id, plugin_dir, manifest, app, fresh=True)
        except Exception as exc:
            _get_plugin_logger().error("[plugin-loader] Failed to load plugin '%s': %s", plugin_id, exc)


def get_loaded_plugins():
    """Return the dict of currently loaded plugin info."""
    return dict(_loaded_plugins)


def plugin_owning_endpoint(endpoint):
    """Return the id of the plugin whose on_load registered *endpoint*.

    Returns ``None`` for core endpoints.  Endpoints of a plugin deleted in
    this process belong to a placeholder id that is never enabled.
    """
    if not endpoint:
        return None
    return _ENDPOINT_OWNERS.get(endpoint)



def get_all_manifests():
    """Return a dict of all discovered plugin manifests keyed by plugin_id.

    Re-reads from disk on every call so it always reflects the current
    set of installed plugins on disk.
    """
    manifests = {}
    for _dir, manifest, _is_builtin in discover_plugins():
        manifests[manifest["id"]] = manifest
    return manifests


def get_manifest(plugin_id):
    """Return the manifest dict for *plugin_id*, or ``None`` if not found."""
    return get_all_manifests().get(plugin_id)


def get_plugin_extends(plugin_id):
    """Return a list of plugin IDs that *plugin_id* extends (requires).

    Returns an empty list if the plugin has no ``extends`` declaration
    or if the plugin is not found.
    """
    manifest = get_manifest(plugin_id)
    if manifest is None:
        return []
    return manifest.get("extends", [])


def get_plugin_extended_by(plugin_id):
    """Return a list of plugin IDs that require *plugin_id* to function.

    This is the inverse of :func:`get_plugin_extends`: it scans all
    manifests for ``extends`` entries that include *plugin_id*.
    """
    result = []
    for pid, manifest in get_all_manifests().items():
        ext = manifest.get("extends", [])
        if plugin_id in ext:
            result.append(pid)
    return result


def get_plugin_extension_tree(plugin_id):
    """Return the full dependency chain for *plugin_id*.

    Walks the ``extends`` declarations breadth-first to collect every
    plugin that must be enabled for *plugin_id* to function. Includes
    transitive dependencies.
    """
    seen = set()
    queue = list(get_plugin_extends(plugin_id))
    chain = []
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        chain.append(pid)
        for dep in get_plugin_extends(pid):
            if dep not in seen:
                queue.append(dep)
    return chain


@_serialized_lifecycle
def trigger_plugin_enable(plugin_id, app):
    """Dynamically load and activate a plugin after it has been enabled in the DB.

    If the plugin is already loaded this is a no-op (the on_load hook was
    already invoked at startup).  Otherwise the plugin module is imported now
    and its ``on_load`` hook is called so that any routes or hooks it registers
    become immediately active without a server restart.  Other workers load
    it at their next request through :func:`sync_with_registry`.

    The plugin's ``on_enable`` hook is called regardless, so long as the
    plugin object exposes one.

    Code is only loaded for a plugin whose registry row is enabled.  The
    admin page enables the row, after the password and the database
    snapshot for external code, and only then calls this.  A caller that
    skipped that step, such as a batch toggle given an uploaded plugin's
    id, gets ``False`` and nothing runs.

    Returns ``True`` if the plugin was successfully loaded (or was already
    loaded), ``False`` on any import / API-version error.
    """
    _drop_request_registry_view()
    if plugin_id in _loaded_plugins:
        # Already loaded, just fire on_enable
        _fire(_loaded_plugins[plugin_id].get("plugin_obj"), "_on_enable_fn", plugin_id, "on_enable")
        return True

    # Reject denylisted plugins at runtime (not just at startup).
    if _is_denylisted(plugin_id):
        _get_plugin_logger().warning(
            "[plugin-loader] Rejected runtime enable of denylisted built-in '%s'",
            plugin_id,
        )
        return False

    if not db.is_plugin_enabled(plugin_id):
        _get_plugin_logger().warning(
            "[plugin-loader] Not loading plugin '%s': it is not enabled in the "
            "plugin registry.",
            plugin_id,
        )
        return False

    for plugin_dir, manifest, _is_builtin in discover_plugins():
        if manifest["id"] != plugin_id:
            continue
        try:
            plugin_obj = _activate(plugin_id, plugin_dir, manifest, app, fresh=False)
        except Exception as exc:
            _get_plugin_logger().error("[plugin-loader] Failed to dynamically load plugin '%s': %s", plugin_id, exc)
            return False
        _sync_failures.pop(plugin_id, None)
        _fire(plugin_obj, "_on_enable_fn", plugin_id, "on_enable")
        return True

    return False


@_serialized_lifecycle
def trigger_plugin_disable(plugin_id):
    """Call a plugin's ``on_disable`` hook and remove it from the loaded set.

    This is called immediately after the admin disables a plugin so the hook
    fires right away.  The plugin's hooks and template slots are set aside
    so enabling it again restores them.  Its routes stay in Flask's routing
    table (they cannot be removed from a running app) and are closed by
    ``before_request_hook`` in ``app.py``: built-in plugins through
    ``_BUILTIN_PLUGIN_PATH_MATCHERS``, every plugin through the endpoints
    recorded while its on_load ran.

    Returns ``True`` if the plugin was loaded (and therefore its hook was
    potentially invoked), ``False`` if it was not loaded.
    """
    _drop_request_registry_view()
    loaded_info = _loaded_plugins.pop(plugin_id, None)
    # Keep _LOADED_ONCE set: routes registered by on_load persist in Flask's
    # routing table and must not be re-registered on re-enable.
    if not loaded_info:
        return False
    _fire(loaded_info.get("plugin_obj"), "_on_disable_fn", plugin_id, "on_disable")
    _PARKED[plugin_id] = (_unregister_hooks(plugin_id), _unregister_slots(plugin_id))
    return True


def _retire_replaced(installed):
    """Unload external plugins whose registry row was deleted or replaced.

    *installed* maps every registered plugin id to its ``installed_at``.
    Deleting a plugin unloads it only in the worker that handled the
    request.  In the others its code stays imported, loaded or with its
    hooks parked after a disable; while the row is missing its hooks,
    routes and handlers are closed by the registry checks, but a new copy
    installed under the same id would switch the old code back on.  So a
    worker forgets an external plugin as soon as it sees the row gone or
    carrying a different ``installed_at`` than the one its code was imported
    for, exactly as the deleting worker did, and the new copy is imported
    from its own files when it is enabled.  Built-in rows are only removed
    and re-seeded at startup, so built-ins are left alone.
    """
    # A copy, because another thread of this worker may be enabling or
    # deleting a plugin while this request looks.
    stale = [
        plugin_id for plugin_id, imported_for in list(_IMPORTED_FOR.items())
        if plugin_id not in installed
        or (imported_for is not None and installed[plugin_id] != imported_for)
    ]
    if not stale:
        return
    with _PLUGIN_LIFECYCLE_LOCK:
        for plugin_id in stale:
            if plugin_id not in _IMPORTED_FOR:
                continue
            _loaded_plugins.pop(plugin_id, None)
            _unregister_hooks(plugin_id)
            _unregister_slots(plugin_id)
            _forget_deleted_plugin(plugin_id)
            _get_plugin_logger().warning(
                "[plugin-loader] Unloaded plugin '%s' in this worker: it was "
                "deleted or reinstalled through another one. Its routes stay "
                "closed until the wiki restarts.",
                plugin_id,
            )


def sync_with_registry(enabled, app, installed=None):
    """Load the plugins the registry has enabled but this process has not.

    Enabling a plugin runs its code only in the Gunicorn worker that handled
    the admin's request.  ``app.py`` calls this at the start of every
    request with the registry's enabled map, so the other workers load the
    plugin too and serve its routes and hooks.  ``on_enable`` is not called
    again here: it already ran once, where the admin enabled the plugin.

    Disabling needs no counterpart, because the hooks, slots, endpoints and
    request handlers of a plugin all check the registry before they run.

    With *installed* (plugin id to ``installed_at`` for every registry row),
    external plugins deleted or reinstalled through another worker are
    unloaded here first; see :func:`_retire_replaced`.

    A plugin that cannot be loaded in this process (its files are missing,
    it needs another SDK version, managed hosting forbids it) is retried at
    most every few minutes rather than on every request.

    Returns ``True`` when it loaded at least one plugin.
    """
    if installed is not None:
        _retire_replaced(installed)
    now = time.monotonic()
    missing = [
        plugin_id for plugin_id, is_enabled in enabled.items()
        if is_enabled
        and plugin_id not in _loaded_plugins
        and now - _sync_failures.get(plugin_id, float("-inf")) >= _SYNC_RETRY_SECONDS
    ]
    if not missing:
        return False
    loaded_any = False
    with _PLUGIN_LIFECYCLE_LOCK:
        available = None
        for plugin_id in missing:
            if plugin_id in _loaded_plugins:
                continue
            if available is None:
                available = {m["id"]: (d, m) for d, m, _b in discover_plugins()}
            entry = available.get(plugin_id)
            if entry is None or _is_denylisted(plugin_id):
                _sync_failures[plugin_id] = now
                continue
            try:
                _activate(plugin_id, entry[0], entry[1], app, fresh=False)
            except Exception as exc:
                _sync_failures[plugin_id] = now
                _get_plugin_logger().error(
                    "[plugin-loader] Failed to load plugin '%s' in this worker: %s", plugin_id, exc
                )
            else:
                _sync_failures.pop(plugin_id, None)
                loaded_any = True
    return loaded_any


def _forget_deleted_plugin(plugin_id):
    """Drop what this process keeps about an external plugin that was deleted.

    Its cached modules and parked hooks go, so a new copy installed under
    the same id is imported from its own files.  Its endpoints and request
    handlers cannot be removed from Flask, so they are handed to a
    placeholder owner that is never enabled and stay off until the next
    restart.
    """
    _PARKED.pop(plugin_id, None)
    _LOADED_ONCE.discard(plugin_id)
    _IMPORTED_FOR.pop(plugin_id, None)
    _sync_failures.pop(plugin_id, None)
    module_name = f"_bw_plugin_{plugin_id}"
    for cached_name in list(sys.modules):
        if cached_name == module_name or cached_name.startswith(module_name + "."):
            del sys.modules[cached_name]
    for endpoint, owner in list(_ENDPOINT_OWNERS.items()):
        if owner == plugin_id:
            _ENDPOINT_OWNERS[endpoint] = _DELETED_OWNER
    for handler in _GUARDED_HANDLERS.pop(plugin_id, ()):
        handler._bw_plugin_id = _DELETED_OWNER


# Upload staging folders are created inside EXTERNAL_DIR so the final move
# is a rename on one file system.  Their names start with a dot, which
# discovery skips, and a crash can leave one behind; startup removes those
# once they are old enough that no import can still be writing to them.
_STAGING_PREFIX = ".staging-"
_STAGING_MAX_AGE_SECONDS = 3600
# Staging folders from before the prefix existed were named ".<id>-XXXXXXXX".
_LEGACY_STAGING_RE = re.compile(r"^\.[A-Za-z][A-Za-z0-9_-]{0,63}-[a-z0-9_]{8}$")


def _remove_stale_staging_dirs():
    """Delete upload staging folders that a crashed import left behind."""
    if not os.path.isdir(EXTERNAL_DIR):
        return
    cutoff = time.time() - _STAGING_MAX_AGE_SECONDS
    for entry in os.listdir(EXTERNAL_DIR):
        if not (entry.startswith(_STAGING_PREFIX) or _LEGACY_STAGING_RE.fullmatch(entry)):
            continue
        path = os.path.join(EXTERNAL_DIR, entry)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_mtime > cutoff:
            continue
        shutil.rmtree(path, ignore_errors=True)


# Plugin ids coming from an uploaded .bwplugin end up as directory names
# and module names, so they are validated against a strict pattern before
# anything on disk is touched.

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _plugin_id_is_valid(plugin_id):
    """Return whether *plugin_id* is a portable, single path component."""
    return bool(
        isinstance(plugin_id, str)
        and _PLUGIN_ID_RE.fullmatch(plugin_id)
    )


def _inspect_bwplugin_archive(zip_path):
    """Validate archive structure and return ``(manifest, prefix, members)``.

    ``members`` contains ``(ZipInfo, relative_parts)`` pairs and is safe to
    extract with ordinary file operations.  This single inspection rejects
    traversal, links, devices, duplicate names, ambiguous manifests, ZIP
    bombs, and files placed outside the plugin's one allowed root.
    """
    if not zipfile.is_zipfile(zip_path):
        raise PluginConfigError("The uploaded file is not a valid ZIP archive.")

    max_files = getattr(config, "MAX_PLUGIN_FILE_COUNT", 5_000)
    max_total = getattr(config, "MAX_PLUGIN_UNCOMPRESSED_SIZE", 50 * 1024 * 1024)
    max_single = getattr(config, "MAX_PLUGIN_SINGLE_FILE_SIZE", 10 * 1024 * 1024)
    max_ratio = getattr(config, "MAX_PLUGIN_COMPRESSION_RATIO", 200)

    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise PluginConfigError(
                f"Plugin archive contains {len(infos)} entries, which exceeds "
                f"the {max_files}-entry limit."
            )

        normalized = []
        seen = set()
        seen_folded = set()
        total = 0
        for info in infos:
            raw = info.filename
            if not raw or "\x00" in raw:
                raise PluginConfigError("The plugin archive contains an invalid file path.")
            name = raw.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise PluginConfigError("The plugin archive contains an absolute file path.")
            parts = tuple(part for part in name.split("/") if part not in ("", "."))
            if not parts or any(part == ".." for part in parts):
                if info.is_dir() and not parts:
                    continue
                raise PluginConfigError("The plugin archive contains an invalid file path.")
            key = "/".join(parts)
            folded = key.casefold()
            if key in seen or folded in seen_folded:
                raise PluginConfigError("The plugin archive contains duplicate file paths.")
            seen.add(key)
            seen_folded.add(folded)

            mode = (info.external_attr >> 16) & 0o170000
            if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
                raise PluginConfigError(
                    "Symbolic links and special files are not allowed in plugins."
                )
            if info.file_size < 0 or info.file_size > max_single:
                raise PluginConfigError("A plugin file exceeds the per-file size limit.")
            total += int(info.file_size)
            if total > max_total:
                total_mb = round(total / (1024 * 1024), 2)
                limit_mb = round(max_total / (1024 * 1024), 2)
                raise PluginConfigError(
                    f"Plugin archive uncompressed size ({total_mb} MB) "
                    f"exceeds the {limit_mb} MB limit."
                )
            if (
                (info.file_size > 0 and info.compress_size == 0)
                or (
                    info.compress_size > 0
                    and info.file_size / info.compress_size > max_ratio
                )
            ):
                raise PluginConfigError("Plugin archive has an unsafe compression ratio.")
            normalized.append((info, parts))

        manifest_entries = [
            (info, parts) for info, parts in normalized
            if parts[-1] == "plugin.json" and len(parts) <= 2
        ]
        if len(manifest_entries) != 1:
            raise PluginConfigError(
                "The .bwplugin archive must contain exactly one plugin.json "
                "at its root or inside one top-level directory."
            )
        manifest_info, manifest_parts = manifest_entries[0]
        prefix_parts = manifest_parts[:-1]
        for _info, parts in normalized:
            if prefix_parts and parts[:len(prefix_parts)] != prefix_parts:
                raise PluginConfigError(
                    "Every plugin file must be inside the same top-level directory."
                )
        expected_init = prefix_parts + ("__init__.py",)
        if not any(parts == expected_init for _info, parts in normalized):
            raise PluginConfigError("The plugin archive is missing __init__.py.")

        with zf.open(manifest_info) as fh:
            try:
                raw_manifest = fh.read(max_single + 1)
                manifest = json.loads(raw_manifest.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PluginConfigError(f"Invalid JSON in plugin.json: {exc}") from exc

    return manifest, prefix_parts, normalized


def validate_bwplugin(zip_path):
    """Validate a ``.bwplugin`` ZIP file.

    Returns the parsed manifest dict on success.
    Raises :class:`PluginConfigError` or :class:`PluginAPIVersionError` on
    failure.
    """
    manifest, _prefix_parts, _members = _inspect_bwplugin_archive(zip_path)

    _check_manifest_fields(manifest)
    if not _plugin_id_is_valid(manifest["id"]):
        raise PluginConfigError(
            "Plugin ID contains an invalid path component or character. It "
            "must start with a letter and contain only letters, numbers, "
            "underscores, or hyphens (64 characters maximum)."
        )
    # "<id>__" starts the names of a plugin's own tables.  An id containing
    # "__" would make those prefixes overlap with another plugin's.
    if "__" in manifest["id"]:
        raise PluginConfigError(
            "Plugin ID must not contain two underscores in a row: "
            "'<id>__' is reserved as the prefix of the plugin's tables."
        )
    if manifest.get("builtin") not in (None, False):
        raise PluginConfigError(
            "An uploaded plugin cannot declare itself built-in. Remove "
            "\"builtin\" from plugin.json or set it to false."
        )
    _check_api_version(manifest)
    # Each extended plugin must already be installed
    for dep_id in manifest.get("extends", []):
        dep = db.get_plugin(dep_id)
        if dep is None:
            raise PluginConfigError(
                f"Plugin '{manifest['id']}' extends '{dep_id}', "
                f"but '{dep_id}' is not installed. "
                f"Install the required plugin first."
            )
    return manifest


def import_bwplugin(zip_path):
    """Import a validated ``.bwplugin`` file.

    Extracts the archive to ``plugins/external/<plugin_id>/`` and registers
    the plugin in the database as disabled.  No plugin code runs.

    An upload can only add a new plugin.  An id that belongs to a built-in
    or to anything already installed (compared case-insensitively) is
    refused, so an upload never changes an existing registry row.

    Returns the manifest dict.
    """
    if not getattr(config, "ALLOW_EXTERNAL_PLUGINS", True):
        raise PluginConfigError(
            "External plugins are turned off here. They are disabled on managed "
            "BananaWiki Hosting outside a tenant container, and on a self-hosted "
            "wiki with BW_ALLOW_EXTERNAL_PLUGINS=0."
        )
    manifest = validate_bwplugin(zip_path)
    plugin_id = manifest["id"]

    if not _plugin_id_is_valid(plugin_id):
        raise PluginConfigError("Plugin ID contains an invalid path component.")
    if _is_denylisted(plugin_id):
        raise PluginConfigError(
            f"The plugin id '{plugin_id}' is not allowed on managed hosting."
        )
    taken = _id_taken_by(plugin_id)
    if taken == "builtin":
        raise PluginConfigError(
            f"'{plugin_id}' is reserved by a built-in plugin, and an uploaded "
            f"plugin cannot replace a built-in. Give the plugin another id."
        )
    if taken:
        raise PluginConfigError(
            f"A plugin with the id '{plugin_id}' is already installed. "
            f"Delete it before installing another copy."
        )

    os.makedirs(EXTERNAL_DIR, exist_ok=True)
    external_root = os.path.abspath(EXTERNAL_DIR)
    target_dir = os.path.abspath(os.path.join(external_root, plugin_id))
    if os.path.commonpath([external_root, target_dir]) != external_root:
        raise PluginConfigError("Plugin ID contains an invalid path component.")

    if os.path.lexists(target_dir):
        raise PluginConfigError(
            f"Directory already exists for plugin '{plugin_id}'."
        )

    _manifest, prefix_parts, members = _inspect_bwplugin_archive(zip_path)
    staging_dir = tempfile.mkdtemp(prefix=f"{_STAGING_PREFIX}{plugin_id}-", dir=external_root)
    registered = False
    bytes_written = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info, archive_parts in members:
                rel_parts = archive_parts[len(prefix_parts):]
                if not rel_parts:
                    continue
                dest = os.path.abspath(os.path.join(staging_dir, *rel_parts))
                if os.path.commonpath([staging_dir, dest]) != staging_dir:
                    raise PluginConfigError("The plugin archive contains an invalid file path.")
                if info.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "xb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > config.MAX_PLUGIN_UNCOMPRESSED_SIZE:
                            raise PluginConfigError(
                                "Plugin archive actual extracted size exceeds the allowed limit."
                            )
                        dst.write(chunk)
        # Register (disabled) before the folder appears under its real name,
        # and only as a new row: if the id was taken in the meantime the
        # import fails here instead of rewriting someone else's row.
        registered = db.register_plugin(
            plugin_id,
            name=manifest["name"],
            version=manifest["version"],
            author=manifest.get("author", ""),
            description=manifest.get("description", ""),
            builtin=False,
            enabled=False,
            update_existing=False,
        )
        if not registered:
            raise PluginConfigError(
                f"A plugin with the id '{plugin_id}' is already installed."
            )
        os.replace(staging_dir, target_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if registered:
            db.remove_plugin(plugin_id)
        raise
    return manifest


# Table names a plugin may own.  Anything else is never dropped.
_SAFE_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def plugin_data_tables(plugin_id):
    """Return the database tables that "drop data" would delete for *plugin_id*.

    A table belongs to an external plugin when its name starts with exactly
    ``<plugin_id>__`` (case included), it is not a core table, and no other
    known plugin with a longer id has its own ``<id>__`` prefix at the start
    of the name.  That last rule matters for ids that end in an underscore:
    ``foo___data`` starts with ``foo__`` but belongs to the plugin ``foo_``.
    The old query used ``LIKE '<id>__%'``, where ``_`` matches any character
    and case is ignored, so a plugin called ``user`` took ``user_sessions``
    with it.

    Built-in plugins own no droppable tables: theirs are part of the core
    schema, and a dropped core table is not created again at the next
    start.  The same goes for the namespaces in _BUILTIN_TABLE_NAMESPACES.
    """
    if (
        not _plugin_id_is_valid(plugin_id)
        or is_builtin_plugin(plugin_id)
        or plugin_id.casefold() in _BUILTIN_TABLE_NAMESPACES
    ):
        return []
    prefix = f"{plugin_id}__"
    other_ids = {row["id"] for row in db.list_plugins()}
    other_ids |= builtin_plugin_ids() | _BUILTIN_TABLE_NAMESPACES
    other_ids |= {manifest["id"] for _dir, manifest, _b in discover_plugins()}
    other_ids.discard(plugin_id)
    longer_prefixes = [
        f"{other}__" for other in other_ids
        if len(other) > len(plugin_id) and f"{other}__".startswith(prefix)
    ]
    protected = _core_tables()
    with db.get_db_context() as conn:
        names = [
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
    owned = []
    for name in sorted(names):
        if not name.startswith(prefix) or not _SAFE_TABLE_RE.fullmatch(name):
            continue
        if name.lower() in protected:
            continue
        if any(name.startswith(other) for other in longer_prefixes):
            continue
        owned.append(name)
    return owned


def delete_external_plugin(plugin_id, drop_data=False, drop_tables=None):
    """Delete a plugin's registration and optionally its data.

    For **external** plugins the plugin directory under ``plugins/external/``
    is also removed.  For **built-in** plugins only the database row is
    removed; the plugin files (part of the BananaWiki distribution) are
    preserved.  The built-in plugin will be re-registered as *disabled* the
    next time the server starts and ``seed_builtin_plugins`` runs.

    Data is only ever dropped for external plugins, and only tables that
    :func:`plugin_data_tables` reports.  *drop_tables* names the tables the
    admin was shown and agreed to; anything else is kept.  ``drop_data=True``
    without *drop_tables* drops every table the plugin owns.

    Returns ``True`` on success, ``False`` if the plugin was not found.
    """
    row = db.get_plugin(plugin_id)
    if not row:
        return False
    builtin = is_builtin_plugin(plugin_id)

    to_drop = []
    if not builtin and (drop_data or drop_tables):
        owned = plugin_data_tables(plugin_id)
        if drop_tables is None:
            to_drop = owned
        else:
            wanted = set(drop_tables)
            to_drop = [name for name in owned if name in wanted]

    deleted = db.remove_plugin(plugin_id)
    if not deleted:
        return False
    trigger_plugin_disable(plugin_id)

    if not builtin:
        ext_root = os.path.abspath(EXTERNAL_DIR)
        target_dir = os.path.abspath(os.path.join(ext_root, plugin_id))
        if os.path.commonpath([ext_root, target_dir]) == ext_root:
            try:
                if os.path.islink(target_dir):
                    os.unlink(target_dir)
                elif os.path.isdir(target_dir):
                    shutil.rmtree(target_dir)
            except OSError as exc:
                _get_plugin_logger().error(
                    "[plugin-loader] Could not remove the files of plugin '%s': %s", plugin_id, exc
                )
        _forget_deleted_plugin(plugin_id)

    if to_drop:
        from db import get_db_context
        with get_db_context() as conn:
            for name in to_drop:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')  # noqa: S608
            conn.commit()
    return True
