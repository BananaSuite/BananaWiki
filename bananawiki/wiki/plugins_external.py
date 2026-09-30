"""Third-party plugins: manifests, discovery and loading at start-up.

A plugin is a directory ``<plugins folder>/<plugin_id>/`` (``BW_EXTERNAL_PLUGINS_DIR``,
``instance/plugins`` by default) holding a ``plugin.json`` manifest and a
Python package whose ``__init__.py`` defines ``FEATURE = Feature(...)``, the
same API the built-in features use. BananaWiki 1.4 plugins, which define
``plugin = Plugin(...)`` with ``bananawiki_sdk``, are loaded through the
adapter in :mod:`bananawiki.sdk.compat`.

Plugins are loaded once, when the application starts, and only when their
row in ``plugins`` (``builtin = 0``) is enabled and external plugins are
allowed (``BW_ALLOW_EXTERNAL_PLUGINS``). Code cannot be unloaded from a
running process, so there is no hot loading: switching a plugin on takes
effect at the next restart. Switching one off takes effect at once for
everything the registry controls (routes, navigation, slots, events, jobs)
because the feature's switch is its row; its code leaves memory at the next
restart.

A plugin runs inside the wiki process with the wiki's own privileges. Every
check here keeps honest plugins from making mistakes; none of them contains
a plugin that means harm.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import types
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__

log = logging.getLogger("bananawiki.plugins")

EXTENSION = "bananawiki.plugins"
MODULE_NAMESPACE = "bananawiki_plugins"
STAGING_PREFIX = ".staging-"
STALE_STAGING_SECONDS = 3600
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REQUIRES = 20

PLUGIN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

# 1.4 plugins that are no longer part of BananaWiki (see MIGRATION.md). Their
# rows stay in ``plugins`` and are shown as retired; nothing under these ids
# is ever loaded.
RETIRED_IDS = frozenset({
    "banana_ai", "feedback", "meetings", "beta_testers", "git_override", "oauth_login",
    "bw_oauth_provider", "file_manager", "banana_cad", "ea_mode", "lab_camera", "easter_egg",
    "chat_cleanup", "groups",
})

# Ids of the 1.4 built-in plugins. Whether or not a 1.6 feature carries the
# same id, an uploaded plugin may never take one of them.
LEGACY_BUILTIN_IDS = frozenset({
    "announcements", "api_service", "assessments", "attachments", "audit", "badges", "canvas", "chat",
    "custom_pages", "deletion_slowdown", "difficulty_tags", "drafts", "kanban", "page_governance",
    "page_history", "temporary_accounts", "tts", "user_data_export", "user_profiles",
})

# Names that would clash with table namespaces or module names of BananaWiki itself.
RESERVED_NAMES = frozenset({
    "user_profile_fields", "static", "api", "bananawiki", "bananawiki_sdk", MODULE_NAMESPACE,
    "core", "wiki", "hosting", "ops", "sdk", "plugins", "import", "order", "restart",
})

# field -> (required, maximum length)
_TEXT_FIELDS = {
    "id": (True, 64),
    "name": (True, 100),
    "version": (True, 50),
    "author": (False, 200),
    "description": (False, 2000),
    "min_bananawiki": (False, 50),
    "bananawiki_api_version": (False, 20),
}
SUPPORTED_LEGACY_API_MAJOR = 1


class ManifestError(ValueError):
    """A refused manifest or plugin; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = {name: str(value) for name, value in values.items()}


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    author: str = ""
    description: str = ""
    min_bananawiki: str = ""
    api_version: str = ""
    requires: tuple[str, ...] = ()

    def as_row(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "author": self.author,
                "description": self.description}


def version_tuple(text: str) -> tuple[int, ...]:
    """``"2.1.0rc1"`` -> ``(2, 1, 0)``; unparseable parts count as 0."""
    parts = []
    for piece in str(text).split(".")[:4]:
        digits = re.match(r"\d+", piece.strip())
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts)


def parse_manifest(data: object) -> Manifest:
    """Validate a decoded ``plugin.json``."""
    if not isinstance(data, dict):
        raise ManifestError("plugins.error.manifest_not_object")
    values: dict[str, str] = {}
    for name, (required, limit) in _TEXT_FIELDS.items():
        if name not in data:
            if required:
                raise ManifestError("plugins.error.manifest_missing", field=name)
            continue
        value = data[name]
        if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
            raise ManifestError("plugins.error.manifest_field", field=name, limit=limit)
        values[name] = value.strip()
    plugin_id = values["id"]
    if not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ManifestError("plugins.error.id_invalid")
    if "__" in plugin_id:
        raise ManifestError("plugins.error.id_double_underscore")
    if data.get("builtin") not in (None, False):
        raise ManifestError("plugins.error.manifest_builtin")
    requires: list[str] = []
    for key in ("requires", "extends"):
        listed = data.get(key, [])
        if not isinstance(listed, list) or not all(isinstance(x, str) and PLUGIN_ID_RE.fullmatch(x) for x in listed):
            raise ManifestError("plugins.error.manifest_requires", field=key)
        requires.extend(x for x in listed if x not in requires)
    if len(requires) > MAX_REQUIRES:
        raise ManifestError("plugins.error.manifest_requires", field="requires")
    minimum = values.get("min_bananawiki", "")
    if minimum and version_tuple(minimum) > version_tuple(__version__):
        raise ManifestError("plugins.error.needs_newer", version=minimum, current=__version__)
    api = values.get("bananawiki_api_version", "")
    if api and version_tuple(api)[0] != SUPPORTED_LEGACY_API_MAJOR:
        raise ManifestError("plugins.error.api_version", version=api)
    return Manifest(
        id=plugin_id, name=values["name"], version=values["version"], author=values.get("author", ""),
        description=values.get("description", ""), min_bananawiki=minimum, api_version=api,
        requires=tuple(requires),
    )


def read_manifest(directory: Path) -> Manifest:
    path = directory / "plugin.json"
    if path.is_symlink() or not path.is_file():
        raise ManifestError("plugins.error.manifest_missing_file")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ManifestError("plugins.error.manifest_invalid_json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ManifestError("plugins.error.manifest_invalid_json") from error
    return parse_manifest(data)


def reserved_ids(feature_ids: Iterable[str]) -> frozenset[str]:
    """Case-folded ids an external plugin may not use."""
    return frozenset(x.casefold() for x in (*feature_ids, *LEGACY_BUILTIN_IDS, *RETIRED_IDS, *RESERVED_NAMES))


# ── Table guard ──────────────────────────────────────────────────────────────

_SQLITE_INTERNAL = frozenset({"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema",
                              "sqlite_sequence", "sqlite_stat1", "sqlite_stat4"})
_READ_ONLY_PRAGMAS = frozenset({"table_info", "table_xinfo", "table_list", "index_list", "index_info",
                                "index_xinfo", "foreign_key_list"})


def _codes(*pairs: tuple[str, tuple[int, ...]]) -> dict[int, tuple[int, ...]]:
    return {getattr(sqlite3, name): positions for name, positions in pairs if hasattr(sqlite3, name)}


# Write actions -> positions of the authorizer arguments that name an object.
_WRITE_ACTIONS = _codes(
    ("SQLITE_INSERT", (0,)), ("SQLITE_UPDATE", (0,)), ("SQLITE_DELETE", (0,)),
    ("SQLITE_CREATE_TABLE", (0,)), ("SQLITE_CREATE_TEMP_TABLE", (0,)),
    ("SQLITE_DROP_TABLE", (0,)), ("SQLITE_DROP_TEMP_TABLE", (0,)), ("SQLITE_ALTER_TABLE", (1,)),
    ("SQLITE_ANALYZE", (0,)), ("SQLITE_CREATE_INDEX", (0, 1)), ("SQLITE_CREATE_TEMP_INDEX", (0, 1)),
    ("SQLITE_DROP_INDEX", (0, 1)), ("SQLITE_DROP_TEMP_INDEX", (0, 1)), ("SQLITE_CREATE_TRIGGER", (0, 1)),
    ("SQLITE_CREATE_TEMP_TRIGGER", (0, 1)), ("SQLITE_DROP_TRIGGER", (0, 1)),
    ("SQLITE_DROP_TEMP_TRIGGER", (0, 1)), ("SQLITE_CREATE_VIEW", (0,)), ("SQLITE_CREATE_TEMP_VIEW", (0,)),
    ("SQLITE_DROP_VIEW", (0,)), ("SQLITE_DROP_TEMP_VIEW", (0,)), ("SQLITE_CREATE_VTABLE", (0,)),
    ("SQLITE_DROP_VTABLE", (0,)),
)
_ALWAYS_DENIED = frozenset(getattr(sqlite3, n) for n in ("SQLITE_ATTACH", "SQLITE_DETACH") if hasattr(sqlite3, n))


class TableGuardError(sqlite3.DatabaseError):
    """A statement a plugin may not run; ``reason`` says why."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def table_authorizer(prefixes: Iterable[str], denied: list[tuple[str, str]], *, read_only: bool = False):
    """SQLite authorizer letting a plugin write only to objects named ``<id>__…``.

    Reads are allowed everywhere. ``ATTACH``, ``DETACH`` and every ``PRAGMA``
    except schema introspection are refused. *denied* collects why a
    statement was refused so the caller can raise a clear error.
    """
    allowed = tuple(p.casefold() for p in prefixes)

    def owned(name: str | None, db_name: str | None) -> bool:
        lowered = (name or "").casefold().removeprefix("sqlite_autoindex_")
        return db_name == "temp" or lowered in _SQLITE_INTERNAL or (bool(allowed) and lowered.startswith(allowed))

    def authorize(action: int, arg1: str | None, arg2: str | None, db_name: str | None, _source: str | None):
        if action in _ALWAYS_DENIED:
            denied.append(("attach", ""))
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() not in _READ_ONLY_PRAGMAS:
            denied.append(("pragma", arg1 or ""))
            return sqlite3.SQLITE_DENY
        positions = _WRITE_ACTIONS.get(action)
        if positions is None:
            return sqlite3.SQLITE_OK
        names = [(arg1, arg2)[p] for p in positions]
        if read_only and not all((n or "").casefold() in _SQLITE_INTERNAL for n in names if n):
            denied.append(("read_only", names[0] or ""))
            return sqlite3.SQLITE_DENY
        for name in names:
            if name and not owned(name, db_name):
                denied.append(("not_owned", name))
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


@contextmanager
def guarded(conn: sqlite3.Connection, prefixes: Iterable[str], *, read_only: bool = False):
    """Run statements on *conn* under :func:`table_authorizer`."""
    denied: list[tuple[str, str]] = []
    conn.set_authorizer(table_authorizer(prefixes, denied, read_only=read_only))
    try:
        yield
    except sqlite3.DatabaseError as error:
        if denied:
            raise TableGuardError(*denied[0]) from error
        raise
    finally:
        conn.set_authorizer(None)


# ── Runtime state ────────────────────────────────────────────────────────────


@dataclass
class PluginState:
    """What start-up found out about one plugin directory."""

    id: str
    path: Path
    manifest: Manifest | None = None
    loaded: bool = False
    legacy: bool = False
    error_key: str = ""
    error_values: dict[str, str] = field(default_factory=dict)
    lifecycle: dict[str, Any] = field(default_factory=dict)

    def fail(self, key: str, **values: Any) -> None:
        self.error_key = key
        self.error_values = {name: str(value) for name, value in values.items()}


class Runtime:
    """External plugins known to this process (``app.extensions['bananawiki.plugins']``)."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.directory = Path(cfg.folders.plugins)
        self.plugins: dict[str, PluginState] = {}
        # Directories that could not even be identified (bad or missing manifest).
        self.invalid: dict[str, PluginState] = {}
        # Which process built the app: under Gunicorn with preload_app it is the master.
        self.loaded_pid = os.getpid()

    @property
    def allowed(self) -> bool:
        return bool(self.cfg.allow_external_plugins)

    def loaded_ids(self) -> list[str]:
        return [pid for pid, state in self.plugins.items() if state.loaded]

    def table_prefixes(self) -> list[str]:
        return [f"{pid}__" for pid in self.loaded_ids()]

    def denied(self, plugin_id: str) -> bool:
        return plugin_id.casefold() in {x.casefold() for x in self.cfg.managed_plugin_denylist}

    def remove_stale_staging(self) -> None:
        """Delete upload folders a crash left half unpacked."""
        if not self.directory.is_dir():
            return
        cutoff = time.time() - STALE_STAGING_SECONDS
        for entry in self.directory.iterdir():
            try:
                if entry.name.startswith(STAGING_PREFIX) and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue

    def discover(self, feature_ids: Iterable[str]) -> None:
        """Read every plugin directory's manifest; nothing is imported here."""
        self.plugins.clear()
        self.invalid.clear()
        if not self.directory.is_dir():
            return
        reserved = reserved_ids(feature_ids)
        seen: set[str] = set()
        for entry in sorted(self.directory.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            state = PluginState(id=entry.name, path=entry)
            try:
                manifest = read_manifest(entry)
            except ManifestError as error:
                state.fail(error.key, **error.values)
                self.invalid[entry.name] = state
                continue
            state.manifest = manifest
            if manifest.id != entry.name:
                state.fail("plugins.error.folder_name", folder=entry.name, id=manifest.id)
                self.invalid[entry.name] = state
                continue
            folded = manifest.id.casefold()
            if folded in reserved or folded in seen:
                state.fail("plugins.error.id_reserved" if folded in reserved else "plugins.error.id_duplicate",
                           id=manifest.id)
                self.invalid[entry.name] = state
                continue
            seen.add(folded)
            if not (entry / "__init__.py").is_file():
                state.fail("plugins.error.no_package")
            self.plugins[manifest.id] = state

    def sync_rows(self, db: Any) -> dict[str, dict[str, Any]]:
        """Give every discovered plugin its row (disabled when new); return all rows."""
        from ..core.timeutil import now_sql

        rows = {row["id"]: row for row in db.all("SELECT * FROM plugins")}
        for state in self.plugins.values():
            manifest = state.manifest
            assert manifest is not None
            row = rows.get(state.id)
            if row is None:
                db.execute(
                    "INSERT OR IGNORE INTO plugins (id, name, version, author, description, builtin, enabled, "
                    "installed_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
                    (state.id, manifest.name, manifest.version, manifest.author, manifest.description, now_sql()),
                )
            elif row["builtin"]:
                state.fail("plugins.error.id_reserved", id=state.id)
            elif any(row[k] != v for k, v in manifest.as_row().items()):
                db.update("plugins", manifest.as_row(), "id = ? AND builtin = 0", (state.id,))
        return {row["id"]: row for row in db.all("SELECT * FROM plugins")}

    def notify(self, plugin_id: str, event: str) -> None:
        """Run a 1.4 ``on_enable``/``on_disable`` callback if its code is loaded here."""
        state = self.plugins.get(plugin_id)
        callback = state.lifecycle.get(event) if state and state.loaded else None
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - a plugin callback must not break the admin page
            log.exception("Plugin %s failed in on_%s", plugin_id, event)


def runtime() -> Runtime:
    from flask import current_app

    return current_app.extensions[EXTENSION]


# ── Loading ──────────────────────────────────────────────────────────────────


def _forget_modules(package: str) -> None:
    for name in [n for n in sys.modules if n == package or n.startswith(package + ".")]:
        del sys.modules[name]


def _import_package(package: str, path: Path) -> types.ModuleType:
    if MODULE_NAMESPACE not in sys.modules:
        namespace = types.ModuleType(MODULE_NAMESPACE)
        namespace.__path__ = []
        sys.modules[MODULE_NAMESPACE] = namespace
    _forget_modules(package)
    spec = importlib.util.spec_from_file_location(package, path / "__init__.py",
                                                  submodule_search_locations=[str(path)])
    if spec is None or spec.loader is None:
        raise ManifestError("plugins.error.no_package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return module


def _apply_schema(app: Any, state: PluginState, package: str) -> None:
    """Run the plugin's ``schema.upgrade(conn)``; it may only touch ``<id>__`` objects."""
    if not (state.path / "schema.py").is_file():
        return
    upgrade = getattr(importlib.import_module(f"{package}.schema"), "upgrade", None)
    if not callable(upgrade):
        raise ManifestError("plugins.error.schema_upgrade")
    conn = app.extensions["bananawiki.database"].connect()
    try:
        with guarded(conn, [f"{state.id}__"]):
            conn.execute("BEGIN IMMEDIATE")
            try:
                upgrade(conn)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


def _check_feature(app: Any, reg: Any, feature: Any, state: PluginState) -> None:
    from .registry import Feature

    if not isinstance(feature, Feature):
        raise ManifestError("plugins.error.no_feature")
    if feature.id != state.id:
        raise ManifestError("plugins.error.feature_id", id=feature.id)
    if feature.id in reg.features:
        raise ManifestError("plugins.error.id_reserved", id=feature.id)
    names = [bp.name for bp in feature.blueprints]
    clash = [n for n in names if n in app.blueprints] or [n for n in names if names.count(n) > 1]
    if clash:
        raise ManifestError("plugins.error.blueprint_clash", name=clash[0])


def _build_feature(app: Any, state: PluginState, package: str) -> Any:
    from ..sdk import compat

    compat.install_alias()
    with compat.collecting(state.id) as collector:
        module = _import_package(package, state.path)
    feature = getattr(module, "FEATURE", None)
    if feature is None and isinstance(getattr(module, "plugin", None), compat.Plugin):
        state.legacy = True
        assert state.manifest is not None
        feature, state.lifecycle = compat.build_feature(app, module.plugin, state.manifest, collector)
    return feature


def load_plugin(app: Any, reg: Any, catalog: Any, state: PluginState) -> bool:
    """Import one enabled plugin and register its feature. Failures are recorded, never raised."""
    from .registry import require_feature

    package = f"{MODULE_NAMESPACE}.{state.id}"
    try:
        feature = _build_feature(app, state, package)
        _check_feature(app, reg, feature, state)
        _apply_schema(app, state, package)
        if feature.init_app:
            feature.init_app(app)
    except ManifestError as error:
        state.fail(error.key, **error.values)
    except Exception as error:  # noqa: BLE001 - a broken plugin must not stop the wiki
        log.exception("Plugin %s could not be loaded", state.id)
        state.fail("plugins.error.load_failed", detail=f"{type(error).__name__}: {error}")
    if state.error_key:
        _forget_modules(package)
        log.warning("Plugin %s not loaded: %s %s", state.id, state.error_key, state.error_values)
        return False
    assert state.manifest is not None
    feature.toggle = "plugin"
    feature.default_enabled = False  # without its row (deleted) a plugin is off
    feature.version = state.manifest.version
    feature.package_dir = state.path
    for blueprint in feature.blueprints:
        blueprint.before_request(require_feature(feature.id))
    reg.add(feature)
    if (state.path / "translations").is_dir():
        catalog.add_directory(state.path / "translations")
    for blueprint in feature.blueprints:
        app.register_blueprint(blueprint)
    state.loaded = True
    log.info("Loaded plugin %s %s%s", state.id, state.manifest.version, " (1.4 adapter)" if state.legacy else "")
    return True


def load(app: Any, reg: Any, catalog: Any) -> Runtime:
    """Discover external plugins and load the enabled ones (called once by ``create_app``)."""
    from .db import connection_scope

    rt = Runtime(app.config["BW"])
    app.extensions[EXTENSION] = rt
    rt.remove_stale_staging()
    rt.discover(reg.features)
    if not rt.plugins and not rt.invalid:
        return rt
    with app.app_context(), connection_scope(app.extensions["bananawiki.database"]) as session:
        rows = rt.sync_rows(session)
        if not rt.allowed:
            log.info("External plugins are turned off (BW_ALLOW_EXTERNAL_PLUGINS); none were loaded.")
            return rt
        for state in rt.plugins.values():
            row = rows.get(state.id)
            if state.error_key or not row or not row["enabled"] or row["builtin"]:
                continue
            if rt.denied(state.id):
                state.fail("plugins.error.denylisted")
                continue
            load_plugin(app, reg, catalog, state)
    return rt
