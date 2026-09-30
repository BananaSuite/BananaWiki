"""Run BananaWiki 1.4 plugins (``bananawiki_sdk``) on BananaWiki 2.

A 1.4 plugin's ``__init__.py`` creates ``plugin = Plugin("<id>")``, registers
hooks and template slots with decorators, and adds routes in
``@plugin.on_load``. The loader imports it while :func:`collecting` records
those registrations, runs ``on_load`` once at start-up, and
:func:`build_feature` turns the result into a normal
:class:`~bananawiki.wiki.registry.Feature`:

* hooks become event handlers (``after_page_update`` -> ``page.updated``,
  and so on; other hook names are events of the same name);
* template slots become slots (same names, the renderer gets one context dict);
* routes added in ``on_load`` answer 404 while the plugin is switched off;
* admin menu items become admin navigation entries;
* ``register_permission`` keys are granted by role defaults.

Not supported: ``on_enable`` when the plugin's code is not loaded yet
(enabling takes effect at the next restart, so ``on_load`` is the place for
set-up), per-user grants of plugin permissions, the ``after_impersonate_*``
hooks, and request handlers added in ``on_load`` being switched off without a
restart. Rows are dictionaries (they also accept integer indexes, like
``sqlite3.Row``).
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from flask import abort, current_app, flash, redirect, render_template, url_for
from markupsafe import Markup

__version__ = "1.0.0"
API_VERSION = "1.0"

__all__ = [
    "API_VERSION", "Plugin", "PluginAPIVersionError", "PluginConfigError", "PluginError",
    "admin_required", "db_execute", "db_query", "decrypt_value", "editor_required", "emit_hook", "encrypt_value",
    "flash", "get_current_user", "get_setting", "has_permission", "hook", "is_plugin_enabled", "log_action",
    "login_required", "rate_limit", "redirect", "render_slot", "render_template", "template_slot", "url_for",
]

log = logging.getLogger("bananawiki.plugins")


class PluginError(Exception):
    """Base exception for plugin errors."""


class PluginConfigError(PluginError):
    """A plugin manifest or configuration is invalid."""


class PluginAPIVersionError(PluginError):
    """A plugin requires an SDK API version this wiki does not provide."""


# ── Registration during loading ──────────────────────────────────────────────


@dataclass
class Collector:
    """Hooks and slots a plugin registered while it was being loaded."""

    plugin_id: str
    hooks: list[tuple[str, Callable[..., Any]]] = field(default_factory=list)
    slots: list[tuple[str, Callable[..., Any]]] = field(default_factory=list)


_collector: ContextVar[Collector | None] = ContextVar("bananawiki_sdk_collector", default=None)


@contextmanager
def collecting(plugin_id: str) -> Iterator[Collector]:
    collector = Collector(plugin_id)
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


def _record(kind: str, name: str, fn: Callable[..., Any]) -> None:
    collector = _collector.get()
    if collector is None:
        log.warning("Ignoring %s %r registered by %s outside plugin loading", kind, name,
                    getattr(fn, "__qualname__", fn))
        return
    (collector.hooks if kind == "hook" else collector.slots).append((name, fn))


def install_alias() -> None:
    """Make ``import bananawiki_sdk`` resolve to this module for 1.4 plugins."""
    sys.modules["bananawiki_sdk"] = sys.modules[__name__]


def hook(hook_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Subscribe a function to a 1.4 hook name (see ``_HOOK_EVENTS``) or a custom event."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _record("hook", hook_name, fn)
        return fn

    return decorator


def template_slot(slot_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Render ``fn(context) -> html`` into a named template slot."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _record("slot", slot_name, fn)
        return fn

    return decorator


def emit_hook(hook_name: str, **kwargs: Any) -> None:
    from ..wiki import registry

    registry.emit(hook_name, **kwargs)


def render_slot(slot_name: str, context: dict[str, Any] | None = None) -> str:
    from ..wiki import registry

    return registry.render_slot(slot_name, **(context or {}))


# Plugin-declared permissions: key -> (granted to editors, granted to users).
_declared_permissions: dict[str, tuple[bool, bool]] = {}


class Plugin:
    """The 1.4 registration object (``plugin = Plugin("my_plugin")``)."""

    def __init__(self, plugin_id: str, extends: list[str] | None = None):
        self.plugin_id = plugin_id
        self.extends = list(extends or [])
        self._on_enable_fn: Callable[[], Any] | None = None
        self._on_disable_fn: Callable[[], Any] | None = None
        self._on_load_fn: Callable[[Any], Any] | None = None
        self._permissions: list[dict[str, Any]] = []
        self._admin_menu_items: list[dict[str, str]] = []

    def on_enable(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self._on_enable_fn = fn
        return fn

    def on_disable(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self._on_disable_fn = fn
        return fn

    def on_load(self, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        self._on_load_fn = fn
        return fn

    def register_permission(self, key: str, label: str, description: str,
                            default_editor: bool = False, default_user: bool = False) -> None:
        self._permissions.append({"key": key, "label": label, "description": description,
                                  "default_editor": default_editor, "default_user": default_user})
        _declared_permissions[key] = (bool(default_editor), bool(default_user))

    def add_admin_menu_item(self, label: str, endpoint: str, icon: str = "") -> None:
        self._admin_menu_items.append({"label": label, "endpoint": endpoint, "icon": icon})

    def hook(self, hook_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return hook(hook_name)


# ── Adapter ──────────────────────────────────────────────────────────────────


def _user(user_id: Any) -> dict[str, Any] | None:
    from ..wiki import accounts

    return accounts.by_id(str(user_id)) if user_id else None


# 1.4 hook -> (1.6 event, payload translation)
_HOOK_EVENTS: dict[str, tuple[str, Callable[..., dict[str, Any]]]] = {
    "after_page_create": ("page.created", lambda page, author_id=None, **_: {"page": page, "user": _user(author_id)}),
    "after_page_update": ("page.updated", lambda page, author_id=None, **_: {"page": page, "user": _user(author_id)}),
    "after_page_delete": ("page.deleted", lambda page, actor_id=None, **_: {"page": page, "user": _user(actor_id)}),
    "after_login": ("user.login", lambda user, **_: {"user": user}),
    "after_user_create": ("user.created", lambda user, **_: {"user": user}),
}


def _event_handler(hook_name: str, fn: Callable[..., Any]) -> tuple[str, Callable[..., Any]]:
    event, translate = _HOOK_EVENTS.get(hook_name, (hook_name, None))
    if translate is None:
        return event, fn

    @functools.wraps(fn)
    def handler(**payload: Any) -> Any:
        return fn(**translate(**payload))

    return event, handler


def _slot_renderer(fn: Callable[..., Any]) -> Callable[..., Any]:
    from ..core.web import csp_nonce
    from ..wiki import auth

    @functools.wraps(fn)
    def render(**context: Any) -> Markup:
        ctx = {"user": auth.current_user(), **context, "csp_nonce": csp_nonce()}
        html = fn(ctx)
        # 1.4 slot renderers return HTML they built themselves (trusted plugin code).
        return Markup(str(html)) if html else Markup("")

    return render


def _gate(view: Callable[..., Any], plugin_id: str) -> Callable[..., Any]:
    from ..wiki import registry

    @functools.wraps(view)
    def gated(*args: Any, **kwargs: Any) -> Any:
        if not registry.is_enabled(plugin_id):
            abort(404)
        return view(*args, **kwargs)

    return gated


def build_feature(app: Any, plugin: Plugin, manifest: Any, collector: Collector) -> tuple[Any, dict[str, Any]]:
    """Run the plugin's ``on_load`` and describe it as a Feature.

    Returns ``(feature, lifecycle)`` where *lifecycle* maps ``"enable"`` and
    ``"disable"`` to the plugin's callbacks.
    """
    from ..wiki import auth
    from ..wiki.registry import Feature, NavItem

    if plugin.plugin_id != manifest.id:
        log.warning("Plugin %s calls itself %r in Plugin(); using the manifest id", manifest.id, plugin.plugin_id)
    before = set(app.view_functions)
    token = _collector.set(collector)
    try:
        if plugin._on_load_fn is not None:
            plugin._on_load_fn(app)
    finally:
        _collector.reset(token)
        for endpoint in set(app.view_functions) - before:
            app.view_functions[endpoint] = _gate(app.view_functions[endpoint], manifest.id)
    events: dict[str, list[Callable[..., Any]]] = {}
    for hook_name, fn in collector.hooks:
        event, handler = _event_handler(hook_name, fn)
        events.setdefault(event, []).append(handler)
    slots: dict[str, Callable[..., Any]] = {}
    for slot_name, fn in collector.slots:
        slots[slot_name] = _combine(slots.get(slot_name), _slot_renderer(fn))
    nav = [
        NavItem(item["label"], item["endpoint"], icon="puzzle", area="admin", order=90,
                visible=lambda user: bool(user) and auth.is_admin(user))
        for item in plugin._admin_menu_items
    ]
    feature = Feature(
        id=manifest.id, name=f"plugin.{manifest.id}.name", description=f"plugin.{manifest.id}.description",
        toggle="plugin", default_enabled=False, events=events, slots=slots, nav=nav, version=manifest.version,
        order=500,
    )
    lifecycle = {name: fn for name, fn in (("enable", plugin._on_enable_fn), ("disable", plugin._on_disable_fn)) if fn}
    return feature, lifecycle


def _combine(first: Callable[..., Any] | None, second: Callable[..., Any]) -> Callable[..., Any]:
    """Several 1.4 renderers for one slot render one after the other."""
    if first is None:
        return second

    def render(**context: Any) -> Markup:
        return Markup("").join((first(**context), second(**context)))

    return render


# ── Database helpers ─────────────────────────────────────────────────────────


class Row(dict):
    """A result row usable both as ``row["title"]`` and as ``row[0]``."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row(cursor: sqlite3.Cursor, values: tuple) -> Row:
    return Row(zip((c[0] for c in cursor.description), values, strict=True))


def _check_single_statement(sql: str, params: Any) -> None:
    if ";" in sql.strip().rstrip(";"):
        raise PluginError("Multi-statement SQL is not allowed. Use a single statement.")
    if params is not None and not isinstance(params, (list, tuple)):
        raise PluginError("Query parameters must be a list or tuple.")


@contextmanager
def _connection(read_only: bool) -> Iterator[sqlite3.Connection]:
    from ..wiki import plugins_external

    prefixes = plugins_external.runtime().table_prefixes()
    loading = _collector.get()
    if loading is not None:
        prefixes.append(f"{loading.plugin_id}__")
    conn = current_app.extensions["bananawiki.database"].connect()
    conn.row_factory = _row
    try:
        with plugins_external.guarded(conn, prefixes, read_only=read_only):
            yield conn
    except plugins_external.TableGuardError as error:
        raise PluginError(_guard_message(error, read_only)) from error
    finally:
        conn.close()


def _guard_message(error: Any, read_only: bool) -> str:
    if error.reason == "read_only" or read_only:
        return "db_query is read-only. Use db_execute to write to the plugin's own tables."
    if error.reason == "not_owned":
        return (f"db_execute cannot write to '{error.detail}'. Plugins may only write to tables "
                "named '<plugin_id>__...'.")
    if error.reason == "pragma":
        return f"PRAGMA {error.detail} is not available to plugins."
    return "ATTACH and DETACH are not available to plugins."


def db_query(sql: str, params: Any = None) -> list[Row]:
    """Run one read-only statement and return its rows."""
    _check_single_statement(sql, params)
    with _connection(read_only=True) as conn:
        return conn.execute(sql, params or []).fetchall()


def db_execute(sql: str, params: Any = None) -> int | None:
    """Run one statement that may only change tables named ``<plugin_id>__…``; return ``lastrowid``."""
    _check_single_statement(sql, params)
    with _connection(read_only=False) as conn:
        return conn.execute(sql, params or []).lastrowid


# ── Core re-exports ──────────────────────────────────────────────────────────


def get_current_user() -> dict[str, Any] | None:
    from ..wiki import auth

    return auth.current_user()


def has_permission(user: dict[str, Any] | None, key: str) -> bool:
    from ..wiki import auth, permissions

    if key in permissions.CATALOGUE:
        return auth.has_permission(key, user)
    if key not in _declared_permissions or not user:
        return False
    editors, users = _declared_permissions[key]
    return auth.is_admin(user) or (editors and auth.has_role("editor", user)) or users


def is_plugin_enabled(plugin_id: str) -> bool:
    from ..wiki import registry

    return registry.is_enabled(plugin_id)


def get_setting(key: str) -> Any:
    from ..wiki import settings

    return settings.get(key)


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    from ..wiki import auth

    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if auth.current_user() is None:
            return auth.redirect_to_login()
        return view(*args, **kwargs)

    return wrapper


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    from ..wiki import auth

    return auth.admin_required(view)


def editor_required(view: Callable[..., Any]) -> Callable[..., Any]:
    from ..wiki import auth

    return auth.editor_required(view)


def rate_limit(max_requests: int = 60, window: int = 60) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Limit a view to *max_requests* per *window* seconds per client, across workers."""

    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        bucket = f"plugin:{view.__module__}.{view.__qualname__}"[:120]

        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from ..core.ratelimit import SqlLimiter
            from ..core.web import client_ip
            from ..wiki import auth
            from ..wiki.db import db

            if not SqlLimiter(db.session).hit(client_ip(), bucket, max_requests, window):
                return auth.deny(429, "error.429.title")
            return view(*args, **kwargs)

        return wrapper

    return decorate


def log_action(action: str, request: Any = None, user: Any = None, **details: Any) -> None:
    username = user.get("username") if isinstance(user, dict) else user
    log.info("plugin action %s user=%s %s", action, username or "-",
             " ".join(f"{key}={value!r}" for key, value in sorted(details.items())))


def encrypt_value(value: str | None) -> str | None:
    from ..core import crypto

    return crypto.encrypt(current_app.config["BW"].secret_key, value)


def decrypt_value(value: str | None) -> str:
    from ..core import crypto

    return crypto.decrypt(current_app.config["BW"].secret_key, value)
