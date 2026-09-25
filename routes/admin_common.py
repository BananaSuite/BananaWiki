"""Shared validation, appearance, and scheduling settings for administration."""

from flask import g, current_app
import os, re
from datetime import datetime, timezone
import db
import config
from helpers import (
    _is_valid_hex_color,
    local_datetime_to_utc,
    BUILTIN_INTERFACE_LANGUAGES,
    t,
)


THEME_FILE_EXTENSION = ".bwtheme"

MAX_THEME_FILE_BYTES = 128 * 1024

THEME_COLOR_FIELDS = {
    "dark": {
        "primary": "primary_color",
        "secondary": "secondary_color",
        "accent": "accent_color",
        "text": "text_color",
        "sidebar": "sidebar_color",
        "bg": "bg_color",
    },
    "light": {
        "primary": "light_primary_color",
        "secondary": "light_secondary_color",
        "accent": "light_accent_color",
        "text": "light_text_color",
        "sidebar": "light_sidebar_color",
        "bg": "light_bg_color",
    },
}


def _build_theme_payload(settings):
    """Return a portable .bwtheme payload for the current site palettes."""
    settings = settings or {}
    return {
        "_meta": {
            "type": "bananawiki-theme",
            "format": "bwtheme",
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        },
        "theme": {
            "default_theme_mode": (
                "light" if settings.get("default_theme_mode") == "light" else "dark"
            ),
            "dark": {
                public_key: settings.get(settings_key, "")
                for public_key, settings_key in THEME_COLOR_FIELDS["dark"].items()
            },
            "light": {
                public_key: settings.get(settings_key, "")
                for public_key, settings_key in THEME_COLOR_FIELDS["light"].items()
            },
        },
    }


def _parse_theme_payload(payload):
    """Validate a .bwtheme payload and return site_settings kwargs."""
    if not isinstance(payload, dict):
        return None, t("flash.theme_file_invalid")
    meta = payload.get("_meta")
    theme = payload.get("theme")
    if not isinstance(meta, dict) or not isinstance(theme, dict):
        return None, t("flash.theme_file_invalid")
    if meta.get("type") != "bananawiki-theme" or meta.get("format") != "bwtheme":
        return None, t("flash.theme_file_invalid")

    default_theme_mode = str(theme.get("default_theme_mode", "dark")).strip().lower()
    if default_theme_mode not in {"dark", "light"}:
        return None, t("flash.theme_file_invalid")

    updates = {"default_theme_mode": default_theme_mode}
    for palette_name, fields in THEME_COLOR_FIELDS.items():
        palette = theme.get(palette_name)
        if not isinstance(palette, dict):
            return None, t("flash.theme_file_invalid")
        for public_key, settings_key in fields.items():
            value = str(palette.get(public_key, "")).strip()
            if not _is_valid_hex_color(value):
                return None, t("flash.theme_file_invalid_color", color=public_key)
            updates[settings_key] = value
    return updates, None


def _theme_download_name(settings):
    """Return a conservative filename for exported theme files."""
    raw_name = str((settings or {}).get("site_name") or "bananawiki").strip().lower()
    safe_name = re.sub(r"[^a-z0-9]+", "-", raw_name).strip("-") or "bananawiki"
    return f"{safe_name}-theme{THEME_FILE_EXTENSION}"


def _can_grant_admin_role(current_user):
    """Return True when the actor may create or promote admins."""
    return bool(current_user and current_user["role"] in ("admin", "owner"))


def _safe_int(value, default, lo=None, hi=None):
    """Parse *value* as an ``int``, clamping to [*lo*, *hi*] and returning
    *default* when the conversion fails."""
    try:
        n = int(value or default)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _safe_float(value, default, lo=None, hi=None):
    """Parse *value* as a ``float``, clamping to [*lo*, *hi*] and returning
    *default* when the conversion fails."""
    try:
        n = float(value or default)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _safe_builtin_language_fallback(value):
    """Return a built-in fallback language code."""
    value = str(value or "").strip().lower()
    return value if value in BUILTIN_INTERFACE_LANGUAGES else "en"


def _normalize_future_settings_datetime(value):
    """Return a UTC ISO datetime for settings expiry fields."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        expires_at = local_datetime_to_utc(value)
    except OverflowError:
        # A time near year 9999 in a zone behind UTC falls past datetime.max.
        raise ValueError("out of range") from None
    exp = datetime.fromisoformat(expires_at).replace(tzinfo=timezone.utc)
    if exp <= datetime.now(timezone.utc):
        raise ValueError("past")
    return expires_at


def _revert_expired_public_access_settings(settings):
    """Persistently turn off expired public-mode/open-signup settings."""
    if not settings:
        return settings
    now = datetime.now(timezone.utc)
    updates = {}
    for enabled_key, until_key in (
        ("public_mode", "public_mode_until"),
        ("open_signup", "open_signup_until"),
    ):
        if not settings.get(enabled_key) or not settings.get(until_key):
            continue
        try:
            exp = datetime.fromisoformat(
                str(settings.get(until_key)).replace("Z", "+00:00")
            )
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if exp <= now:
            updates[enabled_key] = 0
            updates[until_key] = ""
    if updates:
        db.update_site_settings(**updates)
        try:
            if hasattr(g, "_site_settings"):
                del g._site_settings
        except RuntimeError:
            pass
        return db.get_site_settings()
    return settings


def favicon_upload_folder():
    """Return the configured custom-favicon directory for this application."""
    folder = getattr(config, "FAVICON_UPLOAD_FOLDER", None)
    if folder:
        return folder
    return os.path.join(current_app.static_folder, "favicons")
