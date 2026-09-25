# SPDX-FileCopyrightText: 2026 Luca Zani and all contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
BananaWiki: Main Flask application entry point.

Started by Luca Zani on 2026-02-20; see NOTICE for the original commit.

This module creates the Flask application, registers middleware, hooks, and
routes, and re-exports key symbols for backward compatibility with the test
suite and other consumers that ``import app`` or ``from app import …``.

Utility functions live in :mod:`helpers`.  Route handlers are grouped in the
:mod:`routes` package.  Database operations live in :mod:`db`.
"""

import importlib
import json
import hashlib
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import (
    Flask, Response, render_template, render_template_string, request,
    redirect, url_for, session, flash, jsonify, abort, g,
    send_from_directory,
)
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFProtect, generate_csrf
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import db
from wiki_logger import log_request, log_action, get_logger

# Import helpers so they can be re-exported from this module
from helpers import (                                       # noqa: F401
    ALLOWED_TAGS, ALLOWED_ATTRS, _DUMMY_HASH, ROLE_LABELS, _USERNAME_RE,
    _RateLimitStore, _LOGIN_ATTEMPTS, _LOGIN_MAX_ATTEMPTS, _LOGIN_WINDOW,
    _check_login_rate_limit, _record_login_attempt, _clear_login_attempts,
    _RL_LOCK, _RL_STORE, _RL_GLOBAL_MAX, _RL_GLOBAL_WINDOW, _rl_check,
    rate_limit, _current_client_key,
    render_markdown, _make_video_iframe, _embed_videos_in_html,
    compute_char_diff, compute_diff_html, compute_formatted_diff_html,
    slugify, allowed_file, allowed_attachment,
    _is_valid_hex_color, _is_valid_username,
    _safe_referrer, get_current_user,
    login_required, editor_required, admin_required, editor_has_category_access,
    user_can_view_category, filter_visible_navigation,
    is_public_mode_active, is_easy_wiki, is_hosted_instance,
    get_site_timezone, time_ago, format_datetime, format_datetime_local_input,
    is_birthday_today,
    get_time_since_last_chat_cleanup, get_time_until_next_chat_cleanup,
    is_future,
    get_request_category_tree,
    get_request_list_categories,
    get_request_user_accessibility,
    get_request_sidebar_people,
    get_request_unread_dm_count,
    get_request_unread_group_count,
    get_request_reservations_map,
    get_enabled_interface_languages,
    get_all_interface_languages,
    normalize_language_selection,
    get_interface_language_label,
    BUILTIN_INTERFACE_LANGUAGES,
    t,
    get_js_translations,
)

# Re-export cleanup_unused_uploads (tests import it from app)
from routes.uploads import cleanup_unused_uploads           # noqa: F401

class _AutoSecureSessionInterface(SecureCookieSessionInterface):
    """Session interface that sets the ``Secure`` cookie flag dynamically.

    Instead of relying on the static ``SESSION_COOKIE_SECURE`` config,
    this checks the actual request scheme so that the session cookie works
    over both HTTP (development / LAN) and HTTPS (production behind a
    reverse proxy).

    It also implements per-session "Remember me" by overriding
    :meth:`get_expiration_time`: when the login handler stored
    ``session["_remember_me"] = True`` we use the longer
    ``BW_REMEMBER_ME_LIFETIME`` instead of the default
    ``app.permanent_session_lifetime``.
    """

    def get_cookie_secure(self, app):
        """Return ``True`` only when the current request is over HTTPS."""
        try:
            return request.is_secure
        except RuntimeError:
            # Outside a request context: fall back to the static config.
            return app.config.get("SESSION_COOKIE_SECURE", False)

    def get_expiration_time(self, app, session):
        """Return the session cookie expiration time.

        When ``session["_remember_me"]`` is truthy we extend the cookie
        lifetime to ``BW_REMEMBER_ME_LIFETIME`` (default 30 days).
        Otherwise the standard ``permanent_session_lifetime`` (7 days)
        applies.  Non-permanent sessions still expire on browser close.
        """
        if session.permanent and session.get("_remember_me"):
            lifetime = app.config.get("BW_REMEMBER_ME_LIFETIME")
            if lifetime is not None:
                return datetime.now(timezone.utc) + lifetime
        return super().get_expiration_time(app, session)


app = Flask(
    __name__,
    template_folder="app/templates",
    static_folder="app/static",
)


def _send_bananawiki_static(filename):
    # User-uploaded content (page images, custom favicons) must never be
    # served to anonymous visitors unless the wiki has public access
    # ("open access"/read-only public mode) enabled.  Everything else in
    # app/static is bundled application CSS/JS/imagery and stays public so
    # the login page and other unauthenticated surfaces keep rendering.
    if filename.startswith(("uploads/", "favicons/")):
        if not is_public_mode_active() and not get_current_user():
            return redirect(url_for("login"))
    if filename.startswith("uploads/"):
        return send_from_directory(config.UPLOAD_FOLDER, filename[len("uploads/"):])
    if filename.startswith("favicons/"):
        favicon_name = filename[len("favicons/"):]
        favicon_folder = getattr(config, "FAVICON_UPLOAD_FOLDER", None)
        if favicon_folder:
            favicon_path = os.path.abspath(os.path.join(favicon_folder, favicon_name))
            favicon_root = os.path.abspath(favicon_folder)
            if (
                os.path.commonpath([favicon_root, favicon_path]) == favicon_root
                and os.path.isfile(favicon_path)
            ):
                return send_from_directory(favicon_folder, favicon_name)
    return send_from_directory(app.static_folder, filename)


app.view_functions["static"] = _send_bananawiki_static
app.secret_key = config.SECRET_KEY
app.jinja_env.globals["source_code_url"] = config.SOURCE_CODE_URL
# Flask's MAX_CONTENT_LENGTH applies app-wide to every request body.  If we
# only set it to ``config.MAX_CONTENT_LENGTH`` (16 MB by default) then
# perfectly valid uploads to features with higher per-route caps. Most
# notably custom-page videos at 100 MB: get silently rejected with a
# generic 413 *before* the route handler ever runs.  The real cap is the
# maximum of every per-feature limit, so use that here.  Operators can
# still raise the floor explicitly via ``BW_MAX_CONTENT_LENGTH_BYTES``.
app.config["MAX_CONTENT_LENGTH"] = max(
    config.MAX_CONTENT_LENGTH,
    config.MAX_ATTACHMENT_SIZE,
    config.KANBAN_MAX_ATTACHMENT_SIZE,
    config.CUSTOM_PAGE_MAX_FILE_SIZE,
    config.CUSTOM_PAGE_MAX_VIDEO_SIZE,
    config.MAX_IMPORT_UNCOMPRESSED_SIZE,
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_NAME"] = config.SESSION_COOKIE_NAME
app.config["FORBID_PUBLIC_MODE"] = config.FORBID_PUBLIC_MODE
# Default 7-day session lifetime.  When the user ticks "Remember me" at
# login the lifetime is extended to 30 days for *that* session only: see
# routes/auth.py:login.  Lowering the default reduces the window during
# which a stolen cookie remains usable.
app.permanent_session_lifetime = timedelta(days=7)
_REMEMBER_ME_LIFETIME = timedelta(days=30)
app.config["BW_REMEMBER_ME_LIFETIME"] = _REMEMBER_ME_LIFETIME

# Reverse-proxy support ---
if config.PROXY_MODE:
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )
    app.config["PREFERRED_URL_SCHEME"] = (
        config.PREFERRED_URL_SCHEME or "https"
    )
    # Loud warning when ProxyFix is enabled but Gunicorn is bound to a
    # non-loopback address.  In that configuration a LAN attacker can
    # connect directly to Gunicorn and supply forged X-Forwarded-* headers
    # which ProxyFix will trust: bypassing rate limits and downgrading
    # cookie-secure heuristics.  ``forwarded_allow_ips`` in
    # ``gunicorn.conf.py`` mitigates this for the default loopback bind,
    # but not for ``0.0.0.0`` deployments without a fronting proxy.
    _bind_host = (config.HOST or "").strip()
    if _bind_host and _bind_host not in ("127.0.0.1", "::1", "localhost"):
        import logging as _logging
        _logging.getLogger("bananawiki").warning(
            "PROXY_MODE is enabled but Gunicorn is bound to %r (not loopback). "
            "If no trusted reverse proxy is in front, X-Forwarded-* headers "
            "from any client will be trusted, bypassing rate limits and "
            "cookie-secure detection.  Set BW_PROXY_MODE=0 or bind to "
            "127.0.0.1.",
            _bind_host,
        )

# Use the auto-secure session interface so session cookies work on both
# HTTP (dev / LAN) and HTTPS (production) without manual configuration.
app.session_interface = _AutoSecureSessionInterface()

csrf = CSRFProtect(app)


def _lazy_routes_attr(name):
    """Lazily resolve an attribute from the ``routes`` package.

    Defers the import until first call so that ``app.py`` is never imported
    at module-load time by the routes package (avoiding circular imports).
    Replaces the previous ``__import__('routes')`` pattern which exposed
    the dangerous ``__import__`` built-in inside template context.
    """
    import routes as _routes
    return getattr(_routes, name)


def _get_csp_nonce():
    """Return the per-request CSP nonce, generating one if needed."""
    if not hasattr(g, "_csp_nonce"):
        g._csp_nonce = secrets.token_hex(16)
    return g._csp_nonce

# Track the last time the lightweight temporary-item cleanup ran so we do not
# hammer the database on every single request.  Expired suspensions and all
# temporary items (pages, users, roles, page index states) are checked at most
# once every 5 minutes by a dedicated background scheduler thread (see
# ``_start_cleanup_scheduler`` below) so user requests are never blocked
# behind these maintenance queries.
_MINI_CLEANUP_INTERVAL_SECONDS = 5 * 60
_cleanup_scheduler_started = False
_cleanup_scheduler_lock = threading.Lock()
_runtime_services_started = False
_runtime_services_lock = threading.Lock()

# How often to run ``PRAGMA quick_check`` as an early-warning probe for
# database corruption.  Default once per hour; configurable via env so
# operators can tighten or loosen it without a code change.
_INTEGRITY_CHECK_INTERVAL_SECONDS = max(
    60,
    int(os.environ.get("BW_DB_INTEGRITY_CHECK_INTERVAL_SECONDS", str(60 * 60))),
)
_last_integrity_check_at = 0.0

# Expose the plugin SDK's render_slot() as a Jinja global so templates can
# call {{ render_slot("slot.name", ctx) | safe }} to render plugin content.
from bananawiki_sdk import render_slot  # noqa: E402
app.jinja_env.globals["render_slot"] = render_slot

# Register a Jinja2 test for validating hex colour strings in templates.
app.jinja_env.tests["hex_color"] = lambda v: bool(
    re.match(r'^#[0-9a-fA-F]{6}$', v or '')
)

app.jinja_env.filters["from_json"] = json.loads


def _full_path(pattern):
    """Return a predicate that matches the whole request path against *pattern*.

    Routes nested under a page or a user (``/page/<slug>/history``,
    ``/admin/users/<id>/audit``) are matched on the segment after the slug
    or id, never on the slug itself.  Substring tests used to close
    ordinary pages such as ``/page/history-of-rome`` or ``/page/tag``
    whenever the plugin with that word in its routes was disabled.
    """
    compiled = re.compile(pattern)
    return lambda path: compiled.fullmatch(path) is not None


_is_attachment_path = _full_path(r"/api/page/[^/]+/attachments(?:/.*)?|/page/[^/]+/attachments/.*")
_is_audit_path = _full_path(r"/admin/users/[^/]+/(?:audit|tags|attributions)(?:/.*)?")
_is_chat_toggle_path = _full_path(r"/admin/users/[^/]+/toggle_chat")
_is_page_history_path = _full_path(r"/page/[^/]+/(?:history(?:/.*)?|revert/.*)")
_is_page_governance_page_path = _full_path(
    r"/page/[^/]+/(?:reserve|protection|propose-edit|reservation/.*|contribution/.*)"
)
_is_page_reservation_api_path = _full_path(r"/api/pages/[^/]+/reservation(?:/.*)?")
_is_user_quota_path = _full_path(r"/admin/users/[^/]+/reservation-quota(?:/.*)?")
_is_user_export_path = _full_path(r"/admin/users/[^/]+/export")
_is_user_profile_admin_path = _full_path(r"/admin/users/[^/]+/profile")
_is_difficulty_tag_path = _full_path(r"/page/[^/]+/tag")
_is_assessment_path = _full_path(r"/page/[^/]+/assessment(?:/.*)?")
_is_tts_page_path = _full_path(r"/page/[^/]+/tts(?:/.*)?")


_BUILTIN_PLUGIN_PATH_MATCHERS = {
    "announcements": lambda path: (
        path == "/admin/announcements"
        or path.startswith("/admin/announcements/")
        or path.startswith("/announcements/")
    ),
    "attachments": lambda path: (
        path.startswith("/api/attachments/")
        or _is_attachment_path(path)
    ),
    "audit": _is_audit_path,
    "badges": lambda path: path.startswith("/badges/") or path.startswith("/admin/badges"),
    "chat": lambda path: (
        path == "/chats"
        or path.startswith("/chats/")
        or path.startswith("/admin/chats")
        or _is_chat_toggle_path(path)
        or path == "/groups"
        or path.startswith("/groups/")
        or path.startswith("/admin/groups")
    ),
    "drafts": lambda path: path.startswith("/api/draft/"),
    "page_history": _is_page_history_path,
    "page_governance": lambda path: (
        # Page reservations (check-out / cooldown / quota), page protection
        # and the per-page propose / edit / withdraw contribution routes
        _is_page_governance_page_path(path)
        or path == "/reservations"
        or _is_page_reservation_api_path(path)
        or path == "/admin/checkouts"
        or path.startswith("/admin/checkouts/")
        or path == "/settings/reservation-quota"
        or _is_user_quota_path(path)
        # Page protection: admin unlock workflow
        or path.startswith("/admin/settings/page-protection/")
        or path.startswith("/global-settings/page-protection/")
        # Contribution approval: admin dashboard
        or path == "/admin/contributions"
        or path.startswith("/admin/contributions/")
        or path.startswith("/admin/contribution-quota-requests/")
        or path == "/my-contributions"
        or path.startswith("/my-contributions/")
    ),
    "user_data_export": lambda path: (
        path == "/settings/export"
        or _is_user_export_path(path)
    ),
    "user_profiles": lambda path: (
        path == "/users"
        or path.startswith("/users/")
        or _is_user_profile_admin_path(path)
        or path.startswith("/api/user-profile-fields/")
    ),
    "kanban": lambda path: (
        path == "/kanban"
        or path.startswith("/kanban/")
        or path.startswith("/api/kanban/")
        or path.startswith("/api/embed/kanban/")
    ),
    "temporary_accounts": lambda path: (
        path.startswith("/admin/temporary")
    ),
    "custom_pages": lambda path: (
        path.startswith("/admin/custom-pages")
        or path.startswith("/_cpf/")
    ),
    "difficulty_tags": _is_difficulty_tag_path,
    "deletion_slowdown": lambda path: (
        path == "/admin/pending-deletions"
        or path.startswith("/admin/pending-deletions/")
    ),
    "canvas": lambda path: (
        path == "/canvas"
        or path.startswith("/canvas/")
        or path.startswith("/api/canvas/")
        or path.startswith("/api/embed/canvas/")
    ),
    "assessments": lambda path: (
        _is_assessment_path(path)
        or path == "/settings/assessment-points"
    ),
    "tts": lambda path: (
        _is_tts_page_path(path)
        or path == "/admin/tts-status"
        or path.startswith("/admin/tts-status/")
    ),
    "api_service": lambda path: (
        path.startswith("/api/v1/")
        or path == "/admin/api-service"
        or path.startswith("/admin/api-service/")
        or path == "/admin/banana"
        or path == "/admin/banana-toggle"
        or path.startswith("/settings/api-tokens")
        or path == "/settings/api"
        or path == "/account/api"
        or path == "/api-docs"
        or path.startswith("/api-docs/")
    ),
}

_PLUGIN_GATED_PATH_PREFIXES = (
    "/settings/export",
    "/settings/assessment-points",
    "/settings/reservation-quota",
    "/admin/announcements",
    "/admin/badges",
    "/admin/tts-status",
    "/admin/chats",
    "/admin/checkouts",
    "/admin/contributions",
    "/admin/contribution-quota-requests/",
    "/admin/groups",
    "/admin/pending-deletions",
    "/admin/settings/page-protection/",
    "/admin/users/",
    "/announcements/",
    "/api/attachments/",
    "/api/draft/",
    "/api/kanban/",
    "/api/page/",
    "/api/pages/",
    "/badges/",
    "/chats",
    "/global-settings/page-protection/",
    "/groups",
    "/kanban",
    "/my-contributions",
    "/page/",
    "/reservations",
    "/users",
    "/admin/temporary",
    "/admin/custom-pages",
    "/_cpf/",
    "/canvas",
    "/api/canvas/",
    "/api/embed/canvas/",
    "/api/embed/kanban/",
    "/api/user-profile-fields/",
    "/api/v1/",
    "/admin/api-service",
    "/admin/banana",
    "/admin/banana-toggle",
    "/settings/api-tokens",
    "/settings/api",
    "/account/api",
    "/api-docs",
)

def _get_enabled_plugins():
    """Return a map of all plugin ids (builtin and external) to their enabled state.

    Including external (manually-installed) plugins ensures that path gating and
    sidebar visibility work correctly regardless of whether a plugin was
    auto-seeded as builtin or installed by an admin via a .bwplugin archive.
    """
    all_plugin_rows = db.list_plugins()
    if all_plugin_rows:
        plugins = {plugin["id"]: bool(plugin["enabled"]) for plugin in all_plugin_rows}
    else:
        # Some tests replace the database after the app has already been imported,
        # which means the startup seeding step has not run for that fresh DB yet.
        # Fall back to the legacy "all built-ins available" behavior until rows
        # exist in the plugin registry for the current database.
        from plugin_loader import discover_plugins

        plugins = {
            manifest["id"]: True
            for _plugin_dir, manifest, is_builtin in discover_plugins()
            if is_builtin and not manifest.get("experimental")
        }

    # The registry's own view, before EasyWiki hides anything.  Plugin hooks
    # and request handlers check it through bananawiki_sdk's
    # _plugin_is_active, so a plugin disabled in any worker stops in all of
    # them.  EasyWiki only hides features; it does not stop their hooks.
    try:
        g.plugin_registry_enabled = dict(plugins)
        # Only a map read from real rows may load code (see _check_plugins).
        g.plugin_registry_live = bool(all_plugin_rows)
        # Tells a worker when a plugin it loaded was deleted or reinstalled
        # through another worker (see plugin_loader.sync_with_registry).
        g.plugin_registry_installed = {
            row["id"]: row["installed_at"] if "installed_at" in row.keys() else None
            for row in all_plugin_rows
        }
    except RuntimeError:
        pass

    # EasyWiki mode: disable advanced plugins to provide a minimal experience.
    if is_easy_wiki():
        _EASY_WIKI_DISABLED_PLUGINS = {
            "chat", "kanban", "canvas", "assessments", "badges",
            "custom_pages", "page_governance", "tts",
            "user_profiles", "user_data_export",
        }
        for pid in _EASY_WIKI_DISABLED_PLUGINS:
            if pid in plugins:
                plugins[pid] = False

    return plugins


def _get_request_enabled_plugins():
    """Return the built-in plugin enablement map cached on ``g`` for this request."""
    enabled_plugins = getattr(g, "enabled_plugins", None)
    if enabled_plugins is None:
        enabled_plugins = _get_enabled_plugins()
        g.enabled_plugins = enabled_plugins
    return enabled_plugins


def _get_request_site_settings():
    """Return site settings cached on ``g`` for this request.

    ``db.get_site_settings()`` hits the database on every call.  This wrapper
    caches the result for the lifetime of a single request so that
    ``before_request_hook`` and ``inject_globals`` (both called for every HTML
    page) do not each issue a separate SELECT against the ``site_settings``
    table.
    """
    settings = getattr(g, "_site_settings", None)
    if settings is None:
        settings = db.get_site_settings()
        g._site_settings = settings
    return settings


# Sentinel used to distinguish "not yet cached" from "cached as None".
_CACHE_MISS = object()


def _request_cache(key, loader):
    """Memoise *loader()* on ``flask.g`` for the lifetime of one request.

    ``inject_globals`` is called once per HTML response and previously made
    a separate DB query for every navigation widget (badges, profile,
    pending invites, sidebar people, unread counts, reservations).  Wrapping
    each call in this helper coalesces repeated ``db.*`` lookups inside the
    same request to a single round-trip.
    """
    cached = getattr(g, key, _CACHE_MISS)
    if cached is _CACHE_MISS:
        cached = loader()
        setattr(g, key, cached)
    return cached


def _safe_globals_call(widget_name, loader, default):
    """Run ``loader()`` and return its value, falling back to *default* on error.

    Used by :func:`inject_globals` to wrap individual sidebar / navigation
    queries so a transient database error (e.g. ``database is locked``
    while the periodic cleanup or nightly backup holds the write lock),
    a missing column right after a migration, or a single-feature glitch
    degrades that one widget rather than turning the entire page render
    into a hard 500.

    The exception is logged with the per-request ID so operators can still
    triage genuine regressions.
    """
    try:
        return loader()
    except Exception:  # noqa: BLE001 - intentional broad catch
        try:
            request_id = getattr(g, "_request_id", None)
            get_logger().warning(
                "inject_globals: %s widget failed; falling back to default (request_id=%s)",
                widget_name, request_id, exc_info=True,
            )
        except Exception:
            pass
        return default


def _plugin_enabled(plugin_id, enabled_plugins=None):
    """Return whether a built-in plugin is enabled."""
    enabled_plugins = enabled_plugins or _get_request_enabled_plugins()
    return bool(enabled_plugins.get(plugin_id))


def _normalize_interface_language(value, default="en", settings=None):
    """Return a normalized interface language code with fallback."""
    settings = settings or _get_request_site_settings() or {}
    return normalize_language_selection(value, settings, default=default)


def _get_effective_interface_language(settings, user_accessibility):
    """Resolve effective interface language with per-user override.

    Resolution order:
      1. User customization, when explicitly set (anything other than
         the sentinel ``"default"`` value).
      2. Guest session preference (set via the language switcher when
         not signed in).
      3. The site default configured by the admin in Site Settings.

    A user customization of ``"default"`` means "follow the site
    setting", so it intentionally falls through to step 3 instead of
    being treated as an explicit choice.  The browser's
    ``Accept-Language`` header is *not* consulted here: admins expect
    the site language they configured to take effect for visitors who
    have not chosen a language themselves.
    """
    settings = settings or {}
    fallback = (
        settings.get("interface_language_fallback", "en")
        if settings.get("interface_language_fallback") in ("en", "it")
        else "en"
    )

    # 1. Check for an explicit user preference (skips "default").
    if user_accessibility:
        enabled_languages = get_enabled_interface_languages(settings)
        pref = normalize_language_selection(
            user_accessibility.get("interface_language", "default"),
            settings,
            default="default",
            allow_default=True,
        )
        if pref in enabled_languages:
            return pref

    # 2. Check for a session-based preference (guest language switcher).
    session_lang = session.get("interface_language")
    if session_lang:
        pref = normalize_language_selection(session_lang, settings, default=None)
        if pref:
            return pref

    # 3. Fall back to the site default.  When user customization is
    # unset / "default", this is what keeps it in sync with the
    # admin-configured site language.
    return _normalize_interface_language(
        settings.get("interface_language", fallback),
        default=fallback,
        settings=settings,
    )


def _get_public_accessibility():
    """Return anonymous public-view customization preferences from a cookie."""
    prefs = dict(db._A11Y_DEFAULTS)
    raw = request.cookies.get("bw_public_accessibility", "")
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict):
                prefs.update({
                    k: db._clean_a11y_pref(k, v)
                    for k, v in saved.items()
                    if k in db._A11Y_DEFAULTS
                })
        except (TypeError, ValueError):
            pass
    interface_language = session.get("interface_language")
    if interface_language:
        prefs["interface_language"] = interface_language
    return prefs


def _get_disabled_plugin_for_path(path, enabled_plugins):
    """Return the disabled built-in plugin that owns *path*, if any."""
    if not path.startswith(_PLUGIN_GATED_PATH_PREFIXES):
        return None
    for plugin_id, matcher in _BUILTIN_PLUGIN_PATH_MATCHERS.items():
        if not enabled_plugins.get(plugin_id) and matcher(path):
            return plugin_id
    return None


def _get_disabled_plugin_for_endpoint(endpoint, enabled_plugins):
    """Return the plugin that registered *endpoint* if it is not enabled now.

    Covers routes a plugin added in its on_load, external plugins
    included.  Flask cannot remove a route from a running app, so without
    this a disabled or deleted plugin kept answering until a restart.
    """
    from plugin_loader import plugin_owning_endpoint

    owner = plugin_owning_endpoint(endpoint)
    if owner is not None and not enabled_plugins.get(owner):
        return owner
    return None


@app.template_filter("render_md")
def render_md_filter(text):
    """Jinja2 filter that renders Markdown to sanitised HTML."""
    from markupsafe import Markup
    return Markup(render_markdown(text or ""))


@app.template_filter("tts_human_bytes")
def tts_human_bytes_filter(value):
    """Format a byte count as a short, human-readable string.

    Used by the TTS admin status page to display individual MP3 sizes
    (typically tens to hundreds of KB) and the total cache footprint
    (sometimes several MB).  Returns ``"-"`` for ``None`` / ``0`` so
    rows without a cached file render the same dash as elsewhere.
    """
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if size <= 0:
        return "-"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def dedupe_flashed_messages(messages):
    """Return a deduplicated list of ``(category, message)`` flashed tuples."""
    unique_messages = []
    seen = set()
    for category, message in messages or []:
        key = (category, message)
        if key in seen:
            continue
        seen.add(key)
        unique_messages.append((category, message))
    return unique_messages


_ASSET_VERSION = None


def _get_asset_version():
    """Return a cache key that changes when the main stylesheet changes.

    It is a short hash of the file's contents. It used to be the file's
    modification time, which put the date the release was built or unpacked
    into every page's source, and changed whenever the file was copied even
    when its contents had not.
    """
    global _ASSET_VERSION
    if _ASSET_VERSION is None:
        try:
            css_path = os.path.join(app.static_folder, "css", "style.css")
            with open(css_path, "rb") as handle:
                _ASSET_VERSION = hashlib.sha256(handle.read()).hexdigest()[:12]
        except OSError:
            return "dev"
    return _ASSET_VERSION


@app.context_processor
def inject_globals():
    """Inject common variables into every template context."""
    # Skip the expensive context injection for JSON-only API endpoints.
    # We deliberately do NOT short-circuit on ``request.path.startswith
    # ("/api/")`` here.  Many ``/api/...`` view functions call
    # ``abort(404)`` / ``abort(403)`` from within the handler when a
    # resource is missing or the caller lacks access; Flask then dispatches
    # the matching error handler in :mod:`routes.errors`, which renders the
    # styled HTML error page.  ``request.endpoint`` still points at the
    # original ``/api/...`` view at that point, and ``routing_exception`` is
    # ``None`` (the URL routed successfully -- the 404 came from inside the
    # handler), so a path-based skip would incorrectly omit ``settings`` and
    # the rest of the globals from the error template, turning every
    # in-handler ``abort()`` on an ``/api/`` route into a hard 500.
    ep = request.endpoint or ""
    if ep.startswith("api_v1_"):
        return {}
    settings = _get_request_site_settings()
    user = get_current_user()
    enabled_plugins = _get_request_enabled_plugins()
    _public_mode = is_public_mode_active()
    all_categories = _safe_globals_call("sidebar_categories", get_request_list_categories, default=[]) if (user or _public_mode) else []
    if user and user["role"] not in ("admin", "owner"):
        all_categories = [cat for cat in all_categories if user_can_view_category(user, cat["id"])]
    # Provide the category tree for the sidebar so that every template
    # automatically gets a populated navigation even when the route handler
    # does not explicitly pass ``categories`` / ``uncategorized``.
    if user or _public_mode:
        from helpers._navigation import sidebar_navigation
        _navigation = _safe_globals_call("category_tree", lambda: sidebar_navigation(user),
                                        default={"categories": [], "uncategorized": [], "page_ids": set(), "more": {}})
        _cat_tree, _uncat = _navigation["categories"], _navigation["uncategorized"]
    else:
        _cat_tree, _uncat = [], []
        _navigation = {"categories": [], "uncategorized": [], "page_ids": set(), "more": {}}
    active_announcements = (
        _safe_globals_call(
            "active_announcements",
            lambda: db.get_active_announcements(bool(user), user["id"] if user else None),
            default=[],
        )
        if _plugin_enabled("announcements", enabled_plugins)
        else []
    )
    if user:
        user_accessibility = _safe_globals_call(
            "user_accessibility",
            lambda: get_request_user_accessibility(user["id"]),
            default={"interface_language": "default"},
        )
    else:
        user_accessibility = _get_public_accessibility()
    site_default_language = _normalize_interface_language(
        (settings or {}).get("interface_language", "en"),
        default=(settings or {}).get("interface_language_fallback", "en"),
        settings=settings or {},
    )
    current_language = _get_effective_interface_language(settings or {}, user_accessibility)
    # Expose the active language on ``g`` so ``helpers.t`` can resolve it
    # during template rendering without an explicit ``lang=`` argument.
    try:
        g._current_language = current_language
    except RuntimeError:
        pass
    # Wrap each ``db.*`` call below in :func:`_request_cache` so that a
    # template that re-invokes ``inject_globals`` (e.g. via {% include %}
    # rendering an error page) does not re-issue the query.  ``g`` is reset
    # automatically at end of request, so this is safe across requests.
    #
    # Each side-query is also wrapped in :func:`_safe_globals_call` so a
    # transient ``database is locked`` or a single-feature glitch (e.g. a
    # plugin's data table not yet migrated, a missing row) degrades that
    # one widget instead of turning the whole page into a 500.  Critical
    # state (settings, current_user, enabled_plugins) is intentionally
    # left un-wrapped: without those the request cannot be served at all
    # and a 500 is the correct response.
    badge_notification_count = _safe_globals_call(
        "badge_notification_count",
        lambda: len(_request_cache(
            "_unnotified_badges",
            lambda: db.get_unnotified_badges(user["id"]),
        )),
        default=0,
    ) if user and _plugin_enabled("badges", enabled_plugins) else 0
    sidebar_people = _safe_globals_call(
        "sidebar_people",
        lambda: get_request_sidebar_people(user),
        default=[],
    ) if user and _plugin_enabled("user_profiles", enabled_plugins) else []
    current_user_profile = _safe_globals_call(
        "current_user_profile",
        lambda: _request_cache(
            "_current_user_profile",
            lambda: db.get_user_profile(user["id"]),
        ),
        default=None,
    ) if user and _plugin_enabled("user_profiles", enabled_plugins) else None
    current_user_birthday_today = is_birthday_today(
        current_user_profile["birth_date"] if current_user_profile else ""
    ) if user and current_user_profile else False
    birthday_dismissed = False
    if current_user_birthday_today:
        birthday_dismissed = request.cookies.get("bw_birthday_dismissed") == "1"
    total_unread_dm = _safe_globals_call(
        "total_unread_dm",
        lambda: get_request_unread_dm_count(user["id"]),
        default=0,
    ) if user and _plugin_enabled("chat", enabled_plugins) else 0
    total_unread_group = _safe_globals_call(
        "total_unread_group",
        lambda: get_request_unread_group_count(user["id"]),
        default=0,
    ) if user and _plugin_enabled("chat", enabled_plugins) else 0
    sidebar_reservations = _safe_globals_call(
        "sidebar_reservations",
        lambda: get_request_reservations_map(user["id"], _navigation["page_ids"]),
        default={},
    ) if user and _plugin_enabled("page_governance", enabled_plugins) else {}
    available_interface_languages = get_enabled_interface_languages(settings or {})
    interface_languages = get_all_interface_languages(settings or {})
    current_language_label = get_interface_language_label(
        user_accessibility.get("interface_language", "default") if not user else current_language,
        settings or {},
        default_label="Default",
    )
    can_export_pdf = (
        bool(user)
        and settings
        and settings.get("pdf_export_enabled")
        and _safe_globals_call(
            "pdf_export_permission",
            lambda: db.has_permission(user, "page.export_pdf"),
            default=False,
        )
    )
    can_export_markdown = (
        bool(user)
        and settings
        and settings.get("markdown_export_enabled")
        and user["role"] in ("editor", "admin", "owner")
    )
    impersonator = None
    if session.get("impersonator_id"):
        impersonator = _safe_globals_call(
            "impersonator",
            lambda: db.get_user_by_id(session["impersonator_id"]),
            default=None,
        )
    guided_tour_state = None
    if user and session.get("guided_tour_active"):
        guided_tour_state = _safe_globals_call(
            "guided_tour_state",
            lambda: importlib.import_module("routes.onboarding").build_live_tour_state(user),
            default=None,
        )

    return {
        "current_user": user,
        "impersonator": impersonator,
        "settings": settings,
        "time_ago": time_ago,
        "format_datetime": format_datetime,
        "format_datetime_local_input": format_datetime_local_input,
        "page_history_enabled": (
            config.PAGE_HISTORY_ENABLED
            and _plugin_enabled("page_history", enabled_plugins)
        ),
        "categories": _cat_tree,
        "uncategorized": _uncat,
        "sidebar_navigation": _navigation,
        "all_categories": all_categories,
        "active_announcements": active_announcements,
        "user_accessibility": user_accessibility,
        "badge_notification_count": badge_notification_count,
        "filter_visible_navigation": filter_visible_navigation,
        "sidebar_people": sidebar_people,
        "current_user_profile": current_user_profile,
        "current_user_birthday_today": current_user_birthday_today,
        "birthday_dismissed": birthday_dismissed,
        "utcnow": datetime.now(timezone.utc).isoformat(),
        "time_since_last_chat_cleanup": get_time_since_last_chat_cleanup,
        "time_until_next_chat_cleanup": get_time_until_next_chat_cleanup,
        "is_future": is_future,
        "total_unread_dm": total_unread_dm,
        "total_unread_group": total_unread_group,
        "sidebar_reservations": sidebar_reservations,
        "enabled_plugins": enabled_plugins,
        "dedupe_flashed_messages": dedupe_flashed_messages,
        "can_export_pdf": can_export_pdf,
        "can_export_markdown": can_export_markdown,
        "public_mode_active": _public_mode,
        "current_language": current_language,
        "current_language_label": current_language_label,
        "site_default_language": site_default_language,
        "available_interface_languages": available_interface_languages,
        "interface_languages": interface_languages,
        "builtin_interface_languages": BUILTIN_INTERFACE_LANGUAGES,
        "get_interface_language_label": get_interface_language_label,
        "has_permission": db.has_permission,
        "has_endpoint": lambda ep: ep in app.view_functions,
        "user_has_kanban_sidebar_access": lambda u: _lazy_routes_attr('user_has_kanban_sidebar_access')(u, settings),
        "user_has_canvas_sidebar_access": lambda u: _lazy_routes_attr('user_has_canvas_sidebar_access')(u, settings),
        "t": t,
        "js_translations": get_js_translations(current_language),
        "csp_nonce": _get_csp_nonce(),
        "asset_version": _get_asset_version(),
        "guided_tour_state": guided_tour_state,
        "is_hosted_instance": is_hosted_instance,
        "is_easy_wiki": is_easy_wiki,
        "is_managed_hosting": config.MANAGED_HOSTING,
    }


# From here down the file is per-request machinery: the before_request
# guards run in registration order, then the after_request pass adds the
# security headers that depend on whatever those guards decided.
_SAFE_FRAME_ORIGIN_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def _is_safe_frame_origin(token):
    """Return True when *token* is a strict ``https://host[:port]`` origin.

    Used by :func:`set_security_headers` to validate values supplied via the
    internal ``X-Frame-Src-Override`` header before they are pasted into the
    Content-Security-Policy ``frame-src`` directive.

    The rules are intentionally narrow:

    * Scheme **must** be ``https``.
    * Host must be a syntactically valid DNS name (no wildcards, no IPs).
    * Optional port, 1-5 digits, in the legal 1-65535 range.
    * No path, query, fragment, userinfo, whitespace, or other characters
      that could break out of the directive.
    """
    if not token or not isinstance(token, str):
        return False
    if not token.startswith("https://"):
        return False
    rest = token[len("https://"):]
    # Reject anything that could break the directive: path, query, fragment,
    # credentials, whitespace, control chars, wildcards, angle brackets.
    if any(ch in rest for ch in "/?#@ \t\n\r\f\v\"'<>*\\"):
        return False
    if ":" in rest:
        host, _, port = rest.rpartition(":")
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            return False
    else:
        host = rest
    if not host or len(host) > 253:
        return False
    return bool(_SAFE_FRAME_ORIGIN_HOST_RE.match(host))


@app.after_request
def set_security_headers(response):
    """Add security headers to every response."""
    response.headers.pop("Server", None)
    # Unique request ID for log correlation
    request_id = getattr(g, "_request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    nonce = _get_csp_nonce()
    # X-Frame-Src-Override is an internal-only marker that custom-page routes
    # set to expand the CSP frame-src for that single response.  Its value is
    # a space-separated list of origins (e.g. "https://example.com") which we
    # validate strictly here: no wildcard, no scheme other than https, no
    # path / query / fragment.  Malformed entries are dropped, and if nothing
    # validates we fall back to the default safe set.
    override = response.headers.pop("X-Frame-Src-Override", None)
    extra_origins = []
    if override:
        for token in override.split():
            if _is_safe_frame_origin(token):
                extra_origins.append(token)
    base_frame_src = ["https://www.youtube.com", "https://player.vimeo.com"]
    frame_src_origins = " ".join(base_frame_src + extra_origins)
    frame_src = f"frame-src {frame_src_origins}; "
    # CSP violation reporting:
    #   - ``report-uri`` is the legacy directive understood by older browsers.
    #   - ``report-to`` is the modern replacement (Chromium ignores ``report-uri``
    #     for some violations once ``report-to`` semantics are available); it
    #     references a named endpoint group declared in the
    #     ``Reporting-Endpoints`` response header below.
    custom_page_sandbox = response.headers.pop("X-Custom-Page-Sandbox", None)
    sandbox_directive = "sandbox allow-scripts; " if custom_page_sandbox else ""
    response.headers["Content-Security-Policy"] = (
        sandbox_directive +
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "style-src-attr 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self' stun: turn: turns:; "
        "media-src 'self' blob: mediastream:; "
        + frame_src +
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'; "
        "report-uri /api/csp-report; "
        "report-to csp-endpoint"
    )
    response.headers["Reporting-Endpoints"] = 'csp-endpoint="/api/csp-report"'
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


def _revert_expired_public_mode_and_open_signup(settings):
    """Turn off public mode / open signup when their *_until* datetime has passed."""
    now = datetime.now(timezone.utc)
    kwargs = {}
    if settings.get("public_mode"):
        until_str = settings.get("public_mode_until") or ""
        try:
            until = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until <= now:
                kwargs["public_mode"] = 0
                kwargs["public_mode_until"] = ""
        except (ValueError, TypeError):
            pass
    if settings.get("open_signup"):
        until_str = settings.get("open_signup_until") or ""
        try:
            until = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until <= now:
                kwargs["open_signup"] = 0
                kwargs["open_signup_until"] = ""
        except (ValueError, TypeError):
            pass
    if kwargs:
        db.update_site_settings(**kwargs)


_BANANA_OVERLAY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Banana Mode</title>
<style>
body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;
background:linear-gradient(135deg,#fff8e1,#ffe0b2);color:#333;}
.banana-wrap{text-align:center;padding:2rem;}
.banana-wrap .emoji{font-size:4rem;margin-bottom:1rem;}
.banana-wrap h1{font-size:1.5rem;margin:0 0 1rem;font-weight:600;}
.banana-wrap a,.banana-wrap button{display:inline-block;padding:.6rem 1.1rem;
background:#fadb14;color:#000;border-radius:8px;text-decoration:none;
font-weight:600;margin:.3rem;border:none;cursor:pointer;font-size:.9rem;}
.banana-wrap a:hover,.banana-wrap button:hover{background:#fff060;}
</style>
</head>
<body>
<div class="banana-wrap">
<div class="emoji">\U0001f34c</div>
<h1>Banana Mode is Active</h1>
{% if user %}
  {% if user.role in ("admin", "owner") %}
    <p><a href="{{ settings_url }}">{{ settings_label }}</a></p>
  {% endif %}
  <form method="POST" action="{{ logout_url }}" style="margin:.3rem;">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button type="submit">Logout</button>
  </form>
{% else %}
  <p><a href="{{ login_url }}">Login</a></p>
{% endif %}
</div>
</body>
</html>"""


def _banana_overlay_response(user):
    """Return a banana mode overlay HTML response that replaces normal page content."""
    settings_url = None
    settings_label = None
    if user and user["role"] in ("admin", "owner"):
        if db.is_plugin_enabled("api_service") and "admin_api_service" in app.view_functions:
            settings_url = url_for("admin_api_service", _anchor="banana-mode")
            settings_label = "API Service Settings"
        else:
            settings_url = url_for("admin_plugins")
            settings_label = "Plugin Settings"
    body = render_template_string(
        _BANANA_OVERLAY_TEMPLATE,
        user=user,
        settings_url=settings_url,
        settings_label=settings_label,
        logout_url=url_for("logout"),
        login_url=url_for("login"),
        csrf_token=generate_csrf(),
    )
    return body, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.before_request
def before_request_hook():
    """Enforce setup, maintenance mode, session limits, and global rate limiting."""
    _clear_request_caches()
    response = _check_managed_quota()
    if response is not None:
        return response
    response = _check_setup_and_session()
    if response is not None:
        return response
    response = _check_plugins()
    if response is not None:
        return response
    response = _check_maintenance()
    if response is not None:
        return response
    response = _check_banana_mode()
    if response is not None:
        return response
    response = _check_session_limits()
    if response is not None:
        return response
    response = _check_forced_account_steps()
    if response is not None:
        return response
    response = _check_rate_limit()
    if response is not None:
        return response
    _record_analytics()


def _clear_request_caches():
    """Generate a unique request ID and clear per-request caches."""
    g._request_id = uuid.uuid4().hex
    if hasattr(g, "_current_user"):
        del g._current_user
    if hasattr(g, "enabled_plugins"):
        del g.enabled_plugins
    if hasattr(g, "plugin_registry_enabled"):
        del g.plugin_registry_enabled
    if hasattr(g, "plugin_registry_live"):
        del g.plugin_registry_live
    if hasattr(g, "plugin_registry_installed"):
        del g.plugin_registry_installed
    if hasattr(g, "_site_settings"):
        del g._site_settings


def _check_managed_quota():
    """Managed-hosting quota guard.

    Any request whose body has no declared size is refused.  POST, PUT and
    PATCH requests are then checked against the storage quota.
    """
    if not config.MANAGED_HOSTING:
        return None
    from helpers._storage_quota import body_size_unknown
    # The quota check below sizes a body from Content-Length.  A chunked
    # body has none and would otherwise be read in full, up to the app-wide
    # MAX_CONTENT_LENGTH and far past the quota.  Browsers and the usual
    # HTTP libraries always send a length for form posts and JSON, so a
    # hosted wiki simply asks for one, whatever the method.
    if body_size_unknown(request.content_length, request.environ):
        log_action("storage_quota_blocked", request, reason="no_content_length")
        return jsonify({
            "error": t(
                "error.length_required",
                default="This wiki only accepts request bodies sent with a Content-Length header.",
            ),
        }), 411
    if request.method in ("POST", "PUT", "PATCH"):
        endpoint = (request.endpoint or "").lower()
        cleanup_words = ("delete", "remove", "clear", "purge", "logout", "export", "download")
        if not any(word in endpoint for word in cleanup_words):
            from helpers._storage_quota import mutation_would_exceed_quota
            exceeded, used, limit = mutation_would_exceed_quota(request.content_length)
            if exceeded:
                log_action("storage_quota_blocked", request, used_bytes=used, limit_bytes=limit)
                return jsonify({
                    "error": "This wiki has reached its storage limit. Delete files or contact the hosting administrator.",
                    "used_bytes": used,
                    "limit_bytes": limit,
                }), 507
    return None


def _check_setup_and_session():
    """Handle setup redirects, language selection, and session token migration."""
    settings = _get_request_site_settings()
    requested_language = request.args.get("lang") or request.args.get("language")
    if requested_language:
        language = normalize_language_selection(
            requested_language, settings or {}, default=None,
        )
        if language:
            session["interface_language"] = language
    if not settings:
        if request.endpoint not in ("setup", "static", "health_check", "healthz"):
            return redirect(url_for("setup"))
        return None
    if not settings["setup_done"] and request.endpoint not in ("setup", "static", "health_check", "healthz"):
        return redirect(url_for("setup"))

    auth_session_token = session.get("auth_session_token")
    if (
        session.get("user_id") and session.get("_logged_in_at") and not auth_session_token
    ):
        session.clear()
        if request.endpoint not in ("login", "logout", "static", "health_check", "healthz"):
            flash(t("flash.session_expired_or_revoked"), "info")
            return redirect(url_for("login"))
    if auth_session_token:
        auth_session = db.get_user_session(auth_session_token)
        was_impersonating = bool(session.get("impersonator_id"))
        authenticated_user_id = session.get("impersonator_id") or session.get("user_id")
        if not auth_session or auth_session["user_id"] != authenticated_user_id:
            authenticated_user = (
                db.get_user_by_id(authenticated_user_id) if authenticated_user_id else None
            )
            session_limit_conflict = bool(
                settings.get("session_limit_enabled")
                and session.get("session_token")
                and authenticated_user and authenticated_user.get("session_token")
            )
            session.clear()
            if request.endpoint not in ("login", "logout", "static", "health_check", "healthz"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Session expired or revoked."}), 401
                next_url = request.path
                if request.query_string:
                    next_url += "?" + request.query_string.decode("utf-8")
                if session_limit_conflict:
                    return redirect(url_for("session_conflict", next=next_url))
                flash(
                    t("flash.original_admin_account_not_found") if was_impersonating and authenticated_user_id and not authenticated_user
                    else t("flash.session_expired_or_revoked"),
                    "info",
                )
                return redirect(url_for("login", next=next_url))
        else:
            g._current_auth_session = auth_session
            try:
                last_seen = datetime.fromisoformat(auth_session["last_seen_at"])
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                touch_due = (datetime.now(timezone.utc) - last_seen).total_seconds() >= 300
            except (TypeError, ValueError):
                touch_due = True
            if touch_due:
                from helpers._session_metadata import normalize_session_ip
                db.touch_user_session(auth_session_token, ip_address=normalize_session_ip(request.remote_addr))
    return None


def _check_plugins():
    """Check if the requested path's plugin is disabled.

    Also brings this worker's loaded plugins in line with the registry, so
    a plugin enabled through another worker serves its routes here too.
    """
    if request.endpoint != "static":
        enabled_plugins = _get_request_enabled_plugins()
        registry = getattr(g, "plugin_registry_enabled", None)
        if registry and getattr(g, "plugin_registry_live", False):
            from plugin_loader import sync_with_registry
            loaded = sync_with_registry(
                registry, app, getattr(g, "plugin_registry_installed", None),
            )
            if (
                loaded
                and request.routing_exception is not None
                and request.method in ("GET", "HEAD")
            ):
                # This request was routed before the plugin's routes existed
                # in this worker, so route it again.  Only reads: the CSRF
                # check has already run and skipped a request without an
                # endpoint, so a form post here keeps its 404 and is retried.
                from flask.globals import request_ctx
                request.routing_exception = None
                request_ctx.match_request()
        disabled_plugin = (
            _get_disabled_plugin_for_path(request.path, enabled_plugins)
            or _get_disabled_plugin_for_endpoint(request.endpoint, enabled_plugins)
        )
        if disabled_plugin:
            if request.path.startswith("/api/"):
                return jsonify({"error": t("error.feature_disabled")}), 404
            if request.path.startswith("/_cpf/"):
                abort(404)
            if not get_current_user():
                return redirect(url_for("login", next=request.path))
            abort(404)
    return None


def _check_maintenance():
    """Maintenance mode guard."""
    settings = _get_request_site_settings()
    if settings and settings["maintenance_mode"] and request.endpoint not in (
        "maintenance", "lockdown_legacy_redirect", "login", "logout",
        "setup", "static", "view_announcement", "admin_login",
        "force_change_password", "onboarding_setup", "onboarding_intro",
        "tour_start", "tour_step", "tour_finish", "tour_role", "tour_presenter",
        "health_check", "healthz",
    ):
        if request.path.startswith("/api/v1/"):
            return None
        user = get_current_user()
        if not user or user["role"] not in ("admin", "owner"):
            if user:
                session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": t("error.maintenance_mode")}), 403
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            return redirect(url_for("maintenance", next=next_url))
    return None


def _check_banana_mode():
    """Banana mode guard."""
    settings = _get_request_site_settings()
    if settings and settings.get("banana_mode") and request.endpoint not in (
        "admin_api_service", "admin_api_service_banana_toggle",
        "admin_banana", "banana_toggle",
        "health_check", "healthz",
    ) and not request.path.startswith("/api/v1/banana-mode"):
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "🍌 Banana mode is active. API access is temporarily disabled.",
                "banana_mode": True,
            }), 503
        if request.endpoint not in (
            "login", "logout", "signup", "setup", "static",
            "session_conflict", "session_conflict_force",
            "maintenance", "admin_login", "lockdown_legacy_redirect",
            "view_announcement", "user_settings_api",
            "force_change_password", "onboarding_setup", "onboarding_intro",
            "tour_start", "tour_step", "tour_finish",
        ):
            user = get_current_user()
            return _banana_overlay_response(user)
    return None


# Endpoints that stay reachable while an account owes a mandatory step.
# Without these the user could neither complete the step nor log out.
_FORCED_STEP_ALWAYS_ALLOWED = {
    "login", "logout", "setup", "static",
    "session_conflict", "session_conflict_force",
    "maintenance", "lockdown_legacy_redirect",
    "health_check", "healthz",
}
_FORCED_PASSWORD_ALLOWED = _FORCED_STEP_ALWAYS_ALLOWED | {"force_change_password"}
_ONBOARDING_ALLOWED = _FORCED_PASSWORD_ALLOWED | {"onboarding_setup"}


def _forced_step_response(target_endpoint):
    """Send the caller back to the step it still owes.

    A read is redirected so the browser lands on the right page; anything
    that could change state is refused outright, so the requirement cannot
    be skipped by POSTing directly to another route.
    """
    if request.path.startswith("/api/"):
        return jsonify({"error": t("error.forbidden")}), 403
    if request.method != "GET":
        return abort(403)
    return redirect(url_for(target_endpoint))


def _check_forced_account_steps():
    """Keep an account that owes a password change or onboarding on that step."""
    endpoint = request.endpoint or ""
    if endpoint == "static" or request.path.startswith("/api/v1/"):
        return None
    user = get_current_user()
    if not user:
        return None

    if user.get("force_password_change"):
        if endpoint in _FORCED_PASSWORD_ALLOWED:
            return None
        return _forced_step_response("force_change_password")

    onboarding = importlib.import_module("routes.onboarding")
    if onboarding._onboarding_required(user):
        if endpoint in _ONBOARDING_ALLOWED:
            return None
        return _forced_step_response("onboarding_setup")
    return None


def _check_session_limits():
    """Enforce mass logout and optional session limit."""
    settings = _get_request_site_settings()
    if not settings:
        return None
    if request.endpoint in (
        "login", "logout", "setup", "static", "session_conflict", "session_conflict_force",
        "health_check", "healthz"
    ):
        return None
    if "impersonator_id" not in session:
        uid = session.get("user_id")
        if uid:
            user = get_current_user()
            if user:
                stored_token = user["session_token"]
                session_token = session.get("session_token")
                if stored_token is None and session_token is None:
                    if settings["session_limit_enabled"]:
                        new_token = uuid.uuid4().hex
                        session["session_token"] = new_token
                        with db.write_serialized():
                            db.update_user(uid, session_token=new_token)
                elif stored_token is None:
                    # The stored token was cleared: a logout elsewhere, an
                    # administrator's mass logout, or the scheduled one.
                    if settings["session_limit_enabled"]:
                        session.clear()
                        flash(t("flash.session_invalidated_by_admin"), "error")
                        return redirect(url_for("login"))
                    session.pop("session_token", None)
                elif stored_token != session_token:
                    if settings["session_limit_enabled"]:
                        session.clear()
                        next_url = request.path
                        if request.query_string:
                            next_url += "?" + request.query_string.decode("utf-8")
                        return redirect(url_for("session_conflict", next=next_url))
                    # The limit is off, so adopt the stored token rather than
                    # ending a session the operator did not ask to end.
                    session["session_token"] = stored_token
    return None


def _check_rate_limit():
    """Global rate limit guard."""
    if request.endpoint and request.endpoint not in ("static", "health_check", "healthz"):
        ip = _current_client_key()
        if not _rl_check(ip, "global", _RL_GLOBAL_MAX, _RL_GLOBAL_WINDOW):
            log_action("rate_limited_global", request)
            if request.path.startswith("/api/"):
                return jsonify({"error": t("error.rate_limit_exceeded")}), 429
            try:
                categories, uncategorized = get_request_category_tree()
            except Exception:  # noqa: BLE001
                categories, uncategorized = [], []
            return render_template("wiki/429.html", categories=categories,
                                   uncategorized=uncategorized), 429
    return None


def _record_analytics():
    """Per-wiki analytics roll-up (best-effort, never blocks requests)."""
    user = get_current_user()
    log_request(request, user)
    _ANALYTICS_SKIP_ENDPOINTS = (
        "static", "uploaded_image", "uploaded_attachment",
        "favicon_redirect", "health_check", "healthz",
    )
    if request.endpoint and request.endpoint not in _ANALYTICS_SKIP_ENDPOINTS:
        db.record_request("request")
        if request.endpoint == "view_page" and request.method == "GET":
            db.record_request("page_view")


@app.before_request
def _resolve_request_language():
    """Set ``g._current_language`` before route handlers run.

    Route handlers call ``flash(t(key))`` which resolves translations
    immediately via ``g._current_language``.  Without this hook, the
    language would only be set in ``inject_globals`` (a context
    processor that runs at template-render time), so flash messages
    set inside route handlers would always render in English even when
    the rest of the page was Italian or another language.

    This hook is registered after the main ``before_request_hook`` so
    cache invalidation there does not toss the values we compute here.
    """
    if request.endpoint == "static":
        return
    settings = _get_request_site_settings()
    if not settings:
        try:
            default_language = os.environ.get("BW_DEFAULT_INTERFACE_LANGUAGE", "en")
            if request.method == "POST" and request.endpoint == "setup":
                form_lang = request.form.get("interface_language")
            else:
                form_lang = None
            g._current_language = normalize_language_selection(
                form_lang
                or session.get("interface_language")
                or default_language,
                {},
                default="en",
            )
        except RuntimeError:
            pass
        return
    user = get_current_user()
    if user:
        user_accessibility = get_request_user_accessibility(user["id"])
    else:
        user_accessibility = {
            "interface_language": session.get("interface_language", "default")
        }
    try:
        g._current_language = _get_effective_interface_language(
            settings, user_accessibility
        )
    except Exception:
        try:
            g._current_language = "en"
        except RuntimeError:
            pass


# Stable identifier for this worker: used as the cleanup-lease holder so
# the leader's expiry does not trigger another worker on the same machine
# to needlessly try to take over.
_CLEANUP_HOLDER_ID = f"{os.getpid()}-{secrets.token_hex(4)}"


def _run_periodic_cleanup_once():
    """Run one pass of all periodic cleanup tasks.

    Uses :func:`db.try_acquire_cleanup_lease` so that only one worker
    across the Gunicorn process group runs cleanup per interval.  When a
    different worker holds the lease this function returns immediately.
    The loser does not duplicate any DB work.

    Each task is wrapped in its own try/except so a failure in one task does
    not prevent the others from running.  Errors are logged via the standard
    application logger; the function never raises.
    """
    _logger = get_logger()
    # Lease TTL is set to 2x the interval so that if the leader crashes
    # mid-cleanup another worker will reliably pick up the next round.
    lease_ttl = max(60, _MINI_CLEANUP_INTERVAL_SECONDS * 2)
    try:
        acquired = db.try_acquire_cleanup_lease(_CLEANUP_HOLDER_ID, ttl_seconds=lease_ttl)
    except Exception:
        _logger.exception("Failed to acquire cleanup lease: running cleanup anyway")
        acquired = True  # fail-open so a broken lease table never blocks cleanup
    if not acquired:
        return

    def _safe_cleanup(name, fn):
        try:
            fn()
        except Exception:
            _logger.exception("Unexpected error in %s", name)

    try:
        _safe_cleanup("suspension cleanup", db.cleanup_expired_suspensions)
        _safe_cleanup("group timeout cleanup", db.cleanup_expired_group_timeouts)
        _safe_cleanup("temporary-item cleanup", db.cleanup_all_expired_temporary)
        _safe_cleanup("rate-limit cleanup", db.prune_stale_rate_limit_hits)
        _safe_cleanup("pending-deletion cleanup", db.cleanup_expired_pending_deletions)

        # Cleanup tasks that need site_settings: fetch once and reuse.
        _safe_cleanup("denied-user cleanup", lambda: _cleanup_denied_users(_logger))
        _safe_cleanup("pending-user cleanup", lambda: _cleanup_pending_users(_logger))
        _safe_cleanup("public-mode/open-signup expiry check", lambda: _cleanup_expired_toggles())

        _safe_cleanup("WAL checkpoint", lambda: _run_wal_checkpoint())
        _safe_cleanup("DB integrity check", lambda: _run_integrity_check())
    finally:
        try:
            db.release_cleanup_lease(_CLEANUP_HOLDER_ID)
        except Exception:
            # The lease will expire naturally via TTL.  Log but do not
            # raise: this is best-effort cleanup.
            _logger.exception("Failed to release cleanup lease")


def _cleanup_denied_users(_logger):
    """Cleanup denied accounts whose timeout has expired."""
    settings = db.get_site_settings()
    if settings:
        timeout_hours = settings.get("approval_denied_timeout_hours", 24)
        deleted = db.cleanup_denied_users(timeout_hours=timeout_hours)
        if deleted:
            _logger.info("Cleaned up %d denied user(s) past the %d-hour timeout", deleted, timeout_hours)


def _cleanup_pending_users(_logger):
    """Cleanup pending accounts whose activation timeout has expired."""
    settings = db.get_site_settings()
    if settings:
        timeout_hours = settings.get("approval_pending_timeout_hours", 0)
        if timeout_hours > 0:
            deleted = db.cleanup_pending_users(timeout_hours=timeout_hours)
            if deleted:
                _logger.info("Cleaned up %d pending user(s) past the %d-hour timeout", deleted, timeout_hours)


def _cleanup_expired_toggles():
    """Revert temporary public mode and open signup when their scheduled
    end time has passed.  Re-fetch settings here because we are not in
    a request context so we cannot reuse a per-request cache.
    """
    settings = db.get_site_settings()
    if settings:
        _revert_expired_public_mode_and_open_signup(settings)


def _run_wal_checkpoint():
    """Run a passive WAL checkpoint."""
    with db.get_db_context() as _wal_conn:
        _wal_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


def _run_integrity_check():
    """Early-warning integrity probe.  Run at most once per
    ``_INTEGRITY_CHECK_INTERVAL_SECONDS`` rather than every
    cleanup pass: ``PRAGMA quick_check`` is O(N) over the DB
    and we don't want to add measurable load to short cleanup intervals.
    """
    global _last_integrity_check_at
    _now = time.monotonic()
    if _now - _last_integrity_check_at >= _INTEGRITY_CHECK_INTERVAL_SECONDS:
        _last_integrity_check_at = _now
        result = db.integrity_check()
        if result and result.lower() != "ok":
            get_logger().error(
                "PRAGMA quick_check reported corruption: %s",
                result,
            )


def _start_cleanup_scheduler():
    """Spawn a daemon thread that runs the periodic cleanup loop.

    Idempotent: subsequent calls (e.g. during test re-imports of ``app``)
    return immediately if a scheduler thread is already running.

    Note: with multiple Gunicorn workers each worker spawns its own thread
    and so the cleanup may run several times per interval.  This is safe
    because every cleanup operation is idempotent on the database; the
    redundancy costs a few extra DB queries every five minutes which is
    negligible compared to per-request execution.
    """
    global _cleanup_scheduler_started
    with _cleanup_scheduler_lock:
        if _cleanup_scheduler_started:
            return
        _cleanup_scheduler_started = True

    def _loop():
        """Background loop: sleep, run cleanup, repeat."""
        # Stagger initial run a few seconds after boot so we don't compete
        # with route registration / DB migrations on a cold start.
        time.sleep(15)
        while True:
            try:
                _run_periodic_cleanup_once()
            except Exception:  # belt-and-braces; should never fire
                try:
                    get_logger().exception("Unhandled error in cleanup loop")
                except Exception:
                    pass
            time.sleep(_MINI_CLEANUP_INTERVAL_SECONDS)

    thread = threading.Thread(
        target=_loop,
        name="bw-cleanup-scheduler",
        daemon=True,
    )
    thread.start()


def _running_under_pytest():
    """Return True when the app is imported by pytest."""
    import sys
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def start_runtime_services(*, force=False):
    """Start background runtime services exactly once per process.

    Gunicorn imports this module in the master process when ``preload_app`` is
    enabled.  Background threads must not start before fork, so production
    Gunicorn sets ``BANANAWIKI_SKIP_BACKGROUND_SERVICES=1`` during preload and
    calls this function from ``post_worker_init`` with ``force=True``.
    """
    global _runtime_services_started
    with _runtime_services_lock:
        if _runtime_services_started:
            return False
        if not force:
            if os.environ.get("BANANAWIKI_SKIP_BACKGROUND_SERVICES") == "1":
                return False
            if _running_under_pytest():
                return False
        _runtime_services_started = True

    # Legacy sync module, now a no-op stub (Telegram backup removed).
    try:
        from sync import start_daily_backup_scheduler
        start_daily_backup_scheduler()
    except Exception:
        pass

    try:
        _start_cleanup_scheduler()
    except Exception:
        get_logger().exception("Failed to start periodic cleanup scheduler")

    try:
        from federation.runtime import start as start_federation
        start_federation()
    except Exception:
        get_logger().exception("Failed to start federation polling")

    try:
        from routes.tts import start_tts_runtime_services
        start_tts_runtime_services()
    except Exception:
        get_logger().exception("Failed to start TTS runtime services")

    return True


# The handlers below are deliberately outside the usual auth and CSRF
# guards so external monitors and browsers can reach them unauthenticated.
@app.get("/health")
@csrf.exempt
def health_check():
    """Lightweight liveness probe: no auth required."""
    try:
        db.get_site_settings()
        return {"status": "ok"}, 200
    except Exception:  # noqa: BLE001
        # Do not include exception details in the unauthenticated response:
        # raw error strings can disclose filesystem paths and internal state.
        return {"status": "error"}, 503


@app.route("/healthz")
@csrf.exempt
def healthz():
    """Lightweight liveness/readiness probe.

    Returns 200 with a JSON ``{"status": "ok"}`` payload.  By default the
    response is process-only (no DB query) so an open prober cannot
    pressure the database with a tight loop.  Pass ``?deep=1`` to also
    verify database reachability: orchestrators should use the deep
    variant only at low frequency (e.g. once per minute).

    No authentication is required: the response leaks no internal state
    beyond pass/fail.
    """
    deep = request.args.get("deep") in ("1", "true", "yes")
    if not deep:
        return jsonify({"status": "ok"}), 200
    try:
        db.get_site_settings()
        return jsonify({"status": "ok", "db": "ok"}), 200
    except Exception:
        get_logger().exception("/healthz deep database probe failed")
        return jsonify({"status": "error", "db": "fail"}), 503


@app.route("/robots.txt")
@csrf.exempt
def robots_txt():
    """Serve a static robots.txt that disallows admin/auth/api crawling.

    Public wikis benefit from search-engine indexing of public pages, but
    admin, auth, and API endpoints should never be indexed.  Returned as a
    plain ``text/plain`` body so search-engine crawlers can consume it
    without parsing HTML.
    """
    body = (
        "User-agent: *\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Disallow: /login\n"
        "Disallow: /signup\n"
        "Disallow: /logout\n"
        "Disallow: /setup\n"
        "Disallow: /account/\n"
        "Disallow: /session-conflict\n"
        "Disallow: /chats/\n"
        "Disallow: /kanban/\n"
        "Disallow: /canvas/\n"
        "Disallow: /groups/\n"
        "Allow: /\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/api/csp-report", methods=["POST"])
@csrf.exempt
@rate_limit(30, 60)
def csp_report():
    """Receive Content-Security-Policy violation reports from browsers.

    Browsers POST a JSON body when a CSP rule is violated.  We log it so
    operators can identify misconfigured resources or injection attempts.

    Browsers send ``Content-Type: application/csp-report`` (not
    ``application/json``), so ``get_json()`` would return ``None`` without
    ``force=True``.  The report body is also capped at 4 KB before logging
    so a hostile peer cannot inflate log volume by submitting megabytes
    per request.
    """
    import logging
    logger = logging.getLogger("bananawiki.csp")
    _MAX_REPORT_BYTES = 4096
    try:
        raw = request.get_data(cache=False, as_text=False) or b""
        if len(raw) > _MAX_REPORT_BYTES:
            raw = raw[:_MAX_REPORT_BYTES]
        try:
            report = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            report = {"_truncated_or_invalid": raw[:256].decode("utf-8", errors="replace")}
        if isinstance(report, dict):
            payload = report.get("csp-report", report)
        else:
            payload = report
        logger.warning("CSP violation: %s", payload)
    except Exception:
        logger.exception("Failed to process CSP report")
    return "", 204


# Import-time bootstrap.  The order of the next few statements is load
# bearing: each step assumes the tables and services created above it.
db.init_db()
get_logger()

from routes import register_core_routes  # noqa: E402
register_core_routes(app)

# Load enabled plugins: seeds the builtin plugin registry and loads any
# enabled external plugins that provide additional routes / hooks.
# Must run AFTER db.init_db() so the ``plugins`` table exists.
from plugin_loader import load_enabled_plugins  # noqa: E402
load_enabled_plugins(app)

# Start background services for direct/dev imports.  Production Gunicorn
# preloading skips this and starts them from ``post_worker_init`` instead.
start_runtime_services()

if __name__ == "__main__":
    # Development-only entry point.
    # For production, use:  gunicorn wsgi:app -c gunicorn.conf.py
    print(" * WARNING: Flask development server -- not for production.")
    print(" * Production:  gunicorn wsgi:app -c gunicorn.conf.py")

    # When running the dev server directly there is no gunicorn in front of
    # us to enforce ``forwarded_allow_ips``, so trusting X-Forwarded-* headers
    # would let any LAN client spoof remote_addr and bypass per-IP rate
    # limits.  Strip ProxyFix off the WSGI stack for the dev server only.
    if isinstance(app.wsgi_app, ProxyFix):
        app.wsgi_app = app.wsgi_app.app  # type: ignore[attr-defined]
        print(" * ProxyFix disabled for dev server (set BW_PROXY_MODE=0 in production only behind a trusted proxy).")

    app.run(host=config.HOST, port=config.PORT, debug=False)
