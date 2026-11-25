"""Site settings: the single ``site_settings`` row.

Reads are cached for the duration of a request. Writes go through
:func:`update` which only accepts real, non-internal columns, encrypts secret
columns, and never touches columns it was not given.
"""

from __future__ import annotations

import os
from typing import Any

from flask import current_app, g, has_request_context

from ..core import crypto
from ..core.sqlite import column_names
from ..core.timeutil import is_future
from .db import db

ENCRYPTED_COLUMNS = frozenset({"tts_gpu_auth_token", "mail_smtp_password", "mail_api_key"})

# Columns that only the application itself maintains.
INTERNAL_COLUMNS = frozenset({
    "id", "setup_done", "last_chat_cleanup_at", "last_server_restart_at", "list_order_version",
    "last_backup_sent_at", "platform_upload_blacklist",
})

# Columns of features that no longer exist, or of options merged into others. Kept in the table, never
# written. favicon_enabled: the chosen icon is always used (version 22). The Kanban, canvas, page
# builder and chat access settings: permissions and each item's visibility decide; api_service_enabled:
# the API's plugin switch is its one switch (version 23).
RETIRED_COLUMN_PREFIXES = ("telegram_sync_", "feedback_", "beta_")
RETIRED_COLUMNS = frozenset({
    "banana_mode", "devtools_enabled", "favicon_enabled",
    "kanban_access", "kanban_write_access", "kanban_open_access", "kanban_public_access_enabled",
    "canvas_access", "canvas_write_access", "canvas_open_access", "canvas_public_access_enabled",
    "page_builder_access", "chat_dm_enabled", "chat_group_enabled", "chat_allow_dm_creation",
    "chat_allow_group_creation", "chat_attachments_enabled", "api_service_enabled",
    # Version 24: settings few administrators ever changed, now fixed (see FIXED) or folded into another.
    "public_mode_show_message", "tts_enabled_languages", "tts_performance_mode",
    "tts_gpu_enabled", "tts_gpu_timeout", "chat_max_message_length", "chat_cleanup_enabled",
    "chat_cleanup_frequency_days", "chat_cleanup_hour", "chat_dm_auto_clear_messages",
    "chat_dm_auto_clear_attachments", "chat_group_auto_clear_messages", "chat_group_auto_clear_attachments",
    "chat_cleanup_split_configured", "profile_group_badges_enabled",
})

# Version 24: the value every wiki uses for a setting that is no longer offered (stored values are ignored).
# A pair is an environment variable that overrides the default, for operators who need another value.
FIXED: dict[str, Any] = {
    "approval_pending_timeout_hours": 0,  # unreviewed sign-ups wait until someone decides
    "approval_denied_timeout_hours": 24,
    "onboarding_replay_disabled": 0,
    "intro_role_switching_enabled": 1,
    "intro_role_switching_roles": "user,editor,admin",
    "markdown_export_enabled": 1,  # Markdown downloads follow the right to edit the page
    "profile_contribution_chart_enabled": 1,
    "quota_request_cooldown_hours": 0,
    "auto_logout_hour": 0,
    "page_reservation_cooldown_hours": 24,
    "reservation_quota_auto_approve_max": 0,
    "contribution_quota_auto_approve_max": 0,
    "interface_language_fallback": "en",
    "attention_email_interval_minutes": 60,
    "attention_email_daily_hour": 8,
    "audit_log_retention_days": ("BW_AUDIT_RETENTION_DAYS", 365),
    "api_service_rate_limit": ("BW_API_RATE_LIMIT", 60),
    "api_service_admin_rate_limit": ("BW_API_ADMIN_RATE_LIMIT", 120),
    "api_service_max_tokens_per_user": ("BW_API_MAX_TOKENS_PER_USER", 5),
    "api_service_webhook_max_failures": ("BW_API_WEBHOOK_MAX_FAILURES", 20),
}


def fixed_values() -> dict[str, Any]:
    """:data:`FIXED` with the operators' environment overrides applied (whole numbers only)."""
    values: dict[str, Any] = {}
    for name, value in FIXED.items():
        if isinstance(value, tuple):
            variable, default = value
            raw = os.environ.get(variable, "").strip()
            values[name] = int(raw) if raw.isdigit() else default
        else:
            values[name] = value
    return values


def _secret_key() -> str:
    return current_app.config["BW"].secret_key


def load() -> dict[str, Any]:
    """Return the settings row (decrypted), cached per request."""
    if has_request_context():
        cached = g.get("_site_settings")
        if cached is not None:
            return cached
    row = db.one("SELECT * FROM site_settings WHERE id = 1") or {}
    if row:
        row.update(fixed_values())
    for column in ENCRYPTED_COLUMNS:
        if column in row:
            row[column] = crypto.decrypt(_secret_key(), row[column])
    if has_request_context():
        g._site_settings = row
    return row


def get(name: str, default: Any = None) -> Any:
    value = load().get(name)
    return default if value is None else value


def invalidate() -> None:
    if has_request_context():
        g.pop("_site_settings", None)


def writable_columns() -> frozenset[str]:
    cache = current_app.extensions.setdefault("bananawiki.settings_columns", None)
    if cache is None:
        columns = column_names(db.conn, "site_settings")
        cache = frozenset(
            c for c in columns
            if c not in INTERNAL_COLUMNS and c not in RETIRED_COLUMNS and c not in FIXED
            and not c.startswith(RETIRED_COLUMN_PREFIXES)
        )
        current_app.extensions["bananawiki.settings_columns"] = cache
    return cache


def update(values: dict[str, Any], *, internal: bool = False) -> None:
    """Persist *values*. Unknown or internal keys raise ``KeyError``."""
    if not values:
        return
    allowed = writable_columns()
    stored: dict[str, Any] = {}
    for key, value in values.items():
        if key not in allowed and not (internal and key in INTERNAL_COLUMNS):
            raise KeyError(f"Not a writable setting: {key}")
        if key in ENCRYPTED_COLUMNS and value:
            value = crypto.encrypt(_secret_key(), str(value))
        stored[key] = value
    db.update("site_settings", stored, "id = 1")
    invalidate()


# Derived switches used across the application


def _cfg():
    return current_app.config["BW"]


def close_public_mode() -> None:
    """Treat public mode as off for the rest of this request (see :func:`public_mode_active`).

    The request pipeline calls it for a non-administrator on a public view (a
    published custom page, the API guide) while maintenance mode is on: the page
    itself stays open, but its shell lists no pages and shows no page content.
    """
    g.public_mode_closed = True


def public_mode_active() -> bool:
    if has_request_context() and g.get("public_mode_closed"):
        return False
    if _cfg().forbid_public_mode:
        return False
    settings = load()
    if not settings.get("public_mode"):
        return False
    until = settings.get("public_mode_until")
    return not until or is_future(until)


def open_signup_active() -> bool:
    settings = load()
    if not settings.get("open_signup"):
        return False
    until = settings.get("open_signup_until")
    return not until or is_future(until)


def approval_required() -> bool:
    return bool(get("approval_required"))


def setup_done() -> bool:
    return bool(get("setup_done"))


def maintenance_active() -> bool:
    return bool(get("maintenance_mode"))


def site_name() -> str:
    return str(get("site_name", "BananaWiki") or "BananaWiki")
