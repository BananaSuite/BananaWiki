"""Site settings: the single ``site_settings`` row.

Reads are cached for the duration of a request. Writes go through
:func:`update` which only accepts real, non-internal columns, encrypts secret
columns, and never touches columns it was not given.
"""

from __future__ import annotations

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

# Columns of features that no longer exist. Kept in the table, never written.
RETIRED_COLUMN_PREFIXES = ("telegram_sync_", "feedback_", "beta_")
RETIRED_COLUMNS = frozenset({"banana_mode", "devtools_enabled"})


def _secret_key() -> str:
    return current_app.config["BW"].secret_key


def load() -> dict[str, Any]:
    """Return the settings row (decrypted), cached per request."""
    if has_request_context():
        cached = g.get("_site_settings")
        if cached is not None:
            return cached
    row = db.one("SELECT * FROM site_settings WHERE id = 1") or {}
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
            if c not in INTERNAL_COLUMNS and c not in RETIRED_COLUMNS and not c.startswith(RETIRED_COLUMN_PREFIXES)
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


def bump_counter(name: str) -> int:
    """Atomically increment an internal integer counter column."""
    if name not in INTERNAL_COLUMNS:
        raise KeyError(name)
    with db.transaction():
        db.execute(f'UPDATE site_settings SET "{name}" = COALESCE("{name}", 0) + 1 WHERE id = 1')
        value = int(db.scalar(f'SELECT "{name}" FROM site_settings WHERE id = 1', default=0))
    invalidate()
    return value


# ── Derived switches used across the application ─────────────────────────────


def _cfg():
    return current_app.config["BW"]


def public_mode_active() -> bool:
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
