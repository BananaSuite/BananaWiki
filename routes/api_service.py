"""BananaWiki: API Service plugin routes.

Provides a unified REST API with token authentication, adaptive permissions,
audit logging, and programmable admin operations.  All routes are gated
behind the ``api_service`` plugin.
"""

import functools
import json
import time
import uuid
from collections import namedtuple
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, request, jsonify, render_template, redirect, url_for, flash, session, g
from werkzeug.exceptions import RequestEntityTooLarge

import config
import db
from bananawiki_sdk import emit_hook
from helpers import (
    login_required, admin_required, get_current_user, rate_limit, t,
    editor_has_category_access, user_can_view_page, user_can_view_category,
    is_hosted_instance, normalize_language_selection, BUILTIN_INTERFACE_LANGUAGES,
    MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH,
    _is_valid_hex_color, _is_valid_username,
)
from helpers._passwords import generate_password_hash
from helpers._text import slugify
from wiki_logger import log_action
from sync import notify_change
from routes.admin_common import _normalize_future_settings_datetime
from routes.uploads import cleanup_unused_uploads
from routes.wiki_common import (
    _MAX_PAGE_CONTENT_LENGTH, _get_page_protection_context, _get_reservation_context,
)

class APIError(Exception):
    """Raised by API auth/validation helpers; caught by wrapper middleware."""
    def __init__(self, message, status_code=400):
        """Initialize with an error *message* and HTTP *status_code*."""
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _json_error(message, status_code=400):
    """Return a JSON error response tuple."""
    return jsonify({"error": message}), status_code


def _reject_browser_origin():
    """Reject browser origins with a different scheme, hostname, or effective port."""
    origin = request.headers.get("Origin")
    if origin is None:
        return None
    try:
        actual, expected = urlsplit(origin), urlsplit(request.host_url)
        if (any(character.isspace() for character in origin)
                or actual.scheme not in {"http", "https"}
                or not actual.hostname or actual.username is not None
                or actual.password is not None or actual.path not in {"", "/"}
                or actual.query or actual.fragment):
            raise ValueError("Invalid browser origin")
        actual_port = actual.port if actual.port is not None else (443 if actual.scheme == "https" else 80)
        expected_port = expected.port if expected.port is not None else (443 if expected.scheme == "https" else 80)
        actual_origin = (actual.scheme, actual.hostname, actual_port)
        expected_origin = (expected.scheme, expected.hostname, expected_port)
        if actual_origin != expected_origin:
            raise ValueError("Different browser origin")
    except ValueError:
        return _json_error("Cross-origin request rejected", 403)
    return None


def _is_admin(user_row):
    """Return True for the roles the API treats as administrators."""
    return bool(user_row) and user_row.get("role") in ("admin", "owner")


def _forced_step_error(user_row):
    """Return why this account may not use a token yet, or None.

    The web guard in app.py keeps an account that owes a password change
    or onboarding on that step. A forced password change usually follows a
    suspected compromise or a platform recovery, so a token issued before
    it must not carry on as if nothing happened.
    """
    if user_row.get("force_password_change"):
        return "This account must change its password before it can use the API"
    from routes.onboarding import _onboarding_required
    if _onboarding_required(user_row):
        return "This account must finish onboarding before it can use the API"
    return None


def _api_authenticate(required_scope=None, write_required=False):
    """Decorator for API endpoints that require token authentication.

    Usage::

        @app.route("/api/v1/users")
        @_api_authenticate("users")
        def api_list_users(token_row, user_row):
            ...

    Each call is authenticated, rate limited, recorded in the API audit log
    and has its exceptions turned into JSON error payloads, so handlers can
    raise :class:`APIError` instead of building responses themselves.
    """
    def decorator(f):
        """Bind the view to its own rate-limit bucket."""
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            """Authenticate the token, apply the limit, run the view, record the call."""
            origin_err = _reject_browser_origin()
            if origin_err:
                return origin_err

            site_settings = db.get_site_settings() or {}
            api_settings = db.get_api_service_settings(site_settings)
            if not api_settings["enabled"]:
                return _json_error("API service is disabled", 503)

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return _json_error("Missing or invalid Authorization header", 401)

            token = auth_header[7:]

            token_row, user_row = db.verify_api_service_token(token)
            if not token_row or not user_row:
                return _json_error("Invalid or expired API token", 401)

            if not db.user_has_api_access(user_row):
                return _json_error("API access is disabled for this user", 403)

            forced_step = _forced_step_error(user_row)
            if forced_step:
                return _json_error(forced_step, 403)

            # The maintenance guard in app.py leaves /api/v1/ to this check,
            # because only here is the token's owner known. Administrators
            # keep working so they can finish the maintenance.
            if site_settings.get("maintenance_mode") and not _is_admin(user_row):
                return _json_error("The wiki is in maintenance mode. Try again later.", 503)

            if required_scope:
                if not db.token_has_permission(token_row, required_scope, write_required):
                    return _json_error(
                        f"Token lacks '{required_scope}' scope with write={write_required}",
                        403,
                    )

            # Track token usage
            db.update_token_last_used(token_row["id"])

            # Per-user adaptive rate limiting
            max_req = api_settings["admin_rate_limit"] if _is_admin(user_row) else api_settings["rate_limit"]
            ip = request.remote_addr or ""
            bucket = f"api_service_user_{user_row['id']}"
            allowed = db.check_and_record_rate_limit_hit(ip, bucket, max_req, 60)
            if not allowed:
                return _json_error("Rate limit exceeded. Please slow down.", 429)

            start = time.time()
            status_code = 200
            try:
                result = f(token_row, user_row, *args, **kwargs)
                if isinstance(result, tuple):
                    status_code = result[1] if len(result) > 1 else 200
                return result
            except APIError as e:
                status_code = e.status_code
                return _json_error(e.message, status_code)
            except Exception:
                status_code = 500
                raise
            finally:
                duration_ms = int((time.time() - start) * 1000)
                db.log_api_call(
                    token_id=token_row["id"],
                    user_id=user_row["id"],
                    username=user_row.get("username", ""),
                    endpoint=request.path,
                    method=request.method,
                    status_code=status_code,
                    ip_address=request.remote_addr or "",
                    # Account creation and updates can carry passwords. Keep
                    # audit metadata without retaining request payload values.
                    request_body="",
                    duration_ms=duration_ms,
                )

        # Registration exempts only these stateless, token-verified views from
        # browser CSRF tokens. Cookie-authenticated management forms retain CSRF.
        wrapper._banana_bearer_authenticated = True
        return wrapper
    return decorator


# A JSON body larger than this is refused before it is parsed. It leaves room
# for a full 1 MB page even when every character arrives escaped, and keeps a
# single call from reading the 500 MB that MAX_CONTENT_LENGTH allows for the
# backup import routes.
_MAX_JSON_BODY_BYTES = 16 * 1024 * 1024


def _parse_json_body():
    """Parse JSON request body, raising APIError on failure."""
    # Setting the per-request limit below replaces the app-wide one, so take
    # the smaller of the two: an operator who set a lower limit keeps it.
    app_limit = current_app.config.get("MAX_CONTENT_LENGTH")
    limit = _MAX_JSON_BODY_BYTES if app_limit is None else min(app_limit, _MAX_JSON_BODY_BYTES)
    if request.content_length is not None and request.content_length > limit:
        raise APIError("Request body is too large", 413)
    # A chunked body has no Content-Length to check, so cap what is read too.
    # Werkzeug then stops reading at the cap without an error, which is why
    # a body that fills the cap is treated as one that went past it.
    request.max_content_length = limit
    try:
        raw = request.get_data(cache=True)
    except RequestEntityTooLarge:
        raise APIError("Request body is too large", 413) from None
    if len(raw) >= limit:
        raise APIError("Request body is too large", 413)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Request body must be a JSON object", 400)
    return data


def _json_flag(value, field):
    """Read a JSON boolean. 0 and 1 are accepted for older clients."""
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise APIError(f"{field} must be true or false", 400)


def _json_list(data, field, limit):
    """Return ``data[field]`` as a list of at most *limit* items."""
    items = data.get(field, [])
    if not isinstance(items, list):
        raise APIError(f"'{field}' must be an array", 400)
    if len(items) > limit:
        raise APIError(f"'{field}' is limited to {limit} items per request", 400)
    return items


def _child_token_settings(parent, permissions, expires_at):
    """Limit a newly issued credential to its issuing token's grants and expiry."""
    if not isinstance(permissions, dict) or set(permissions) - {"read", "write", "scopes"}:
        raise APIError("permissions must contain only read, write, and scopes", 400)
    read, write, scopes = permissions.get("read", False), permissions.get("write", False), permissions.get("scopes", [])
    if (type(read) is not bool or type(write) is not bool or not isinstance(scopes, list)
            or not all(isinstance(value, str) for value in scopes)):
        raise APIError("Use boolean read/write permissions and a list of scopes", 400)
    parent_permissions = json.loads(parent["permissions"])
    if (read and parent_permissions.get("read") is not True
            or write and parent_permissions.get("write") is not True
            or not set(scopes).issubset(parent_permissions.get("scopes", []))):
        raise APIError("A child token cannot exceed its issuing token's permissions", 403)

    parent_expiry = parent.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, str):
        raise APIError("expires_at must be an ISO-8601 timestamp", 400)
    effective_expiry = expires_at or parent_expiry
    if effective_expiry:
        try:
            expires = datetime.fromisoformat(effective_expiry.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            expires = expires.astimezone(timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise APIError("expires_at must be in the future", 400)
            if parent_expiry:
                parent_deadline = datetime.fromisoformat(parent_expiry.replace("Z", "+00:00"))
                if parent_deadline.tzinfo is None:
                    parent_deadline = parent_deadline.replace(tzinfo=timezone.utc)
                if expires > parent_deadline:
                    raise APIError("A child token cannot outlive its issuing token", 403)
            effective_expiry = expires.isoformat()
        # OverflowError: a time near year 9999 can fall past it in UTC.
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise APIError("expires_at must be an ISO-8601 timestamp", 400) from exc
    return {"read": read, "write": write, "scopes": sorted(set(scopes))}, effective_expiry


# Every scope a token can carry, and the ones whose endpoints all require an
# administrator. A token never holds a scope its owner's role cannot use, so
# the grant shown in token management matches what the token can reach.
_VALID_SCOPES = frozenset({"pages", "categories", "users", "settings", "tokens", "admin", "userbot"})
_ADMIN_ONLY_SCOPES = frozenset({"admin", "settings", "users"})


def _scopes_for_role(user_row, scopes):
    """Drop the scopes the user's role cannot use."""
    if _is_admin(user_row):
        return list(scopes)
    return [scope for scope in scopes if scope not in _ADMIN_ONLY_SCOPES]


def _require_admin(token_row, user_row):
    """Require the user to have an admin role."""
    if not _is_admin(user_row):
        raise APIError("Admin access required", 403)


def _require_editor(token_row, user_row):
    """Require the user to have an editor or admin role."""
    if user_row.get("role") not in ("editor", "admin", "owner"):
        raise APIError("Editor or admin access required", 403)


# Settings the API never returns. The admin page shows these masked, so a
# token with the settings scope must not be a way round that. The feedback
# and Telegram sync columns belong to removed features but can still hold a
# live bot token on an upgraded database.
_SECRET_SETTINGS = frozenset({
    "tts_gpu_auth_token",
    "feedback_bot_token",
    "feedback_telegram_userids",
    "telegram_sync_token",
    "telegram_sync_userids",
})


# ---------------------------------------------------------------------------
# Site settings the API may change
#
# PUT /api/v1/settings used to write any column the database layer knows.
# That let a token reset setup_done (locking the wiki into /setup), clear the
# restart cooldown, point the TTS worker at an internal address with the
# platform's GPU token, or store values that later broke the API itself.
# Now every writable key has a rule with the same bounds and choices the
# admin settings form applies, the same plugin gates (which already reflect
# EasyWiki) and the same hosting restrictions. Everything else is refused.
# ---------------------------------------------------------------------------

class _SettingError(ValueError):
    """A value a settings rule refuses; the message says what is expected."""


_SettingRule = namedtuple("_SettingRule", "parse plugins policy")


def _rule(parse, *plugins, policy=None):
    """Build a rule: *parse* validates, *plugins* must be on, *policy* may veto."""
    return _SettingRule(parse, plugins, policy)


def _flag(value, _current):
    """Accept true/false, 0/1 and their string forms."""
    if isinstance(value, bool):
        return int(value)
    if type(value) is int and value in (0, 1):
        return value
    if isinstance(value, str) and value.strip().lower() in ("0", "1", "true", "false"):
        return int(value.strip().lower() in ("1", "true"))
    raise _SettingError("must be true or false")


def _whole(low, high):
    """Accept a whole number and clamp it to [low, high], as the form does."""
    def parse(value, _current):
        """Read the number and clamp it."""
        if isinstance(value, bool):
            raise _SettingError("must be a whole number")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, str):
            # int() is the check: str.isdigit() also accepts characters
            # such as superscripts that int() then refuses.
            try:
                value = int(value.strip())
            except ValueError:
                raise _SettingError("must be a whole number") from None
        if type(value) is not int:
            raise _SettingError("must be a whole number")
        return max(low, min(high, value))
    return parse


def _choice(*options):
    """Accept one of *options*."""
    def parse(value, _current):
        """Check the value against the allowed options."""
        if isinstance(value, str) and value.strip().lower() in options:
            return value.strip().lower()
        raise _SettingError("must be one of: " + ", ".join(options))
    return parse


def _text(max_length):
    """Accept a string of at most *max_length* characters, trimmed."""
    def parse(value, _current):
        """Trim the string and check its length."""
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise _SettingError("must be a string")
        value = value.strip()
        if len(value) > max_length:
            raise _SettingError(f"cannot exceed {max_length} characters")
        return value
    return parse


def _site_name(value, current):
    """The site name: an empty value restores the default, as on the form."""
    return _text(100)(value, current) or "BananaWiki"


def _hex_color(value, _current):
    """A #rrggbb colour."""
    if isinstance(value, str) and _is_valid_hex_color(value):
        return value
    raise _SettingError("must be a colour like #aabbcc")


def _time_zone(value, _current):
    """An IANA time zone name."""
    if isinstance(value, str) and value.strip():
        try:
            ZoneInfo(value.strip())
        # Depending on the Python version, a key that names a directory of
        # the time zone database raises OSError rather than a lookup error.
        except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
            pass
        else:
            return value.strip()
    raise _SettingError("must be a valid time zone name")


def _interface_language(value, current):
    """An interface language that is installed and enabled."""
    language = None
    if isinstance(value, str):
        language = normalize_language_selection(value, current, default=None)
    if language is None:
        raise _SettingError("must be an enabled interface language")
    return language


def _future_datetime(value, _current):
    """An expiry for public mode or open sign-up, stored as the form stores it.

    An empty value clears the expiry. A time with an offset is converted to
    UTC; one without is read in the site time zone, as the form reads it.
    """
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise _SettingError("must be an ISO 8601 date and time")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return _normalize_future_settings_datetime(text)
        parsed = parsed.astimezone(timezone.utc)
    except ValueError as exc:
        if str(exc) == "past":
            raise _SettingError("must be in the future") from None
        raise _SettingError("must be an ISO 8601 date and time") from None
    except OverflowError:
        # A time near year 9999 can fall past it once moved to UTC.
        raise _SettingError("must be an ISO 8601 date and time") from None
    if parsed <= datetime.now(timezone.utc):
        raise _SettingError("must be in the future")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _intro_roles(value, _current):
    """The roles offered in the intro role switcher, as a list or a CSV string."""
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or not all(isinstance(role, str) for role in value):
        raise _SettingError("must be a list of roles")
    unknown = set(value) - {"user", "editor", "admin"}
    if unknown:
        raise _SettingError("roles must be user, editor or admin")
    # An empty choice means every role, as on the form.
    roles = [role for role in ("user", "editor", "admin") if role in value]
    return ",".join(roles or ["user", "editor", "admin"])


def _tts_languages(value, _current):
    """The TTS languages to offer, as a list or a CSV string of known codes."""
    from helpers._tts import TTS_SUPPORTED_LANGUAGE_SET, serialize_enabled_languages
    if isinstance(value, str):
        value = [code.strip() for code in value.split(",") if code.strip()]
    if not isinstance(value, list) or not value or not all(isinstance(code, str) for code in value):
        raise _SettingError("must be a non-empty list of language codes")
    unknown = sorted(set(value) - TTS_SUPPORTED_LANGUAGE_SET)
    if unknown:
        raise _SettingError("unknown language code: " + ", ".join(unknown))
    return serialize_enabled_languages(value)


def _platform_forbids(flag, message):
    """Veto turning a feature on when the hosting platform has switched it off."""
    def policy(value):
        """Return the refusal message when the platform flag is set."""
        if value and getattr(config, flag, False):
            return message
        return None
    return policy


def _platform_owned_on_hosted(_value):
    """Veto changes to a value the hosting platform sets from its own policy."""
    if is_hosted_instance():
        return "managed by the hosting platform on this instance"
    return None


# The favicon presets the settings page offers. Picking a custom favicon
# stays on that page, because it names an uploaded file.
_FAVICON_PRESETS = ("yellow", "green", "blue", "red", "orange", "cyan", "purple", "lime")

_FLAG = _rule(_flag)
_COLOR = _rule(_hex_color)

_API_SETTINGS = {
    # General settings, always on the form.
    "site_name": _rule(_site_name),
    "interface_language": _rule(_interface_language),
    "interface_language_fallback": _rule(_choice(*BUILTIN_INTERFACE_LANGUAGES)),
    "timezone": _rule(_time_zone),
    "default_theme_mode": _rule(_choice("dark", "light")),
    **{name: _COLOR for name in (
        "primary_color", "secondary_color", "accent_color",
        "text_color", "sidebar_color", "bg_color",
        "light_primary_color", "light_secondary_color", "light_accent_color",
        "light_text_color", "light_sidebar_color", "light_bg_color",
    )},
    "favicon_enabled": _FLAG,
    "favicon_type": _rule(_choice(*_FAVICON_PRESETS)),
    "maintenance_mode": _FLAG,
    "maintenance_message": _rule(_text(1000)),
    "session_limit_enabled": _FLAG,
    "suspended_account_deletion_enabled": _FLAG,
    "auto_logout_enabled": _FLAG,
    "auto_logout_hour": _rule(_whole(0, 23)),
    "pdf_export_enabled": _FLAG,
    "markdown_export_enabled": _FLAG,
    "contributor_leaderboard_enabled": _FLAG,
    "sidebar_apps_order": _rule(_text(500)),
    "new_user_intro_enabled": _FLAG,
    "onboarding_replay_disabled": _FLAG,
    "intro_role_switching_enabled": _FLAG,
    "intro_role_switching_roles": _rule(_intro_roles),
    "public_mode": _rule(_flag, policy=_platform_forbids(
        "FORBID_PUBLIC_MODE", "public access is not allowed by the hosting platform")),
    "public_mode_until": _rule(_future_datetime),
    "public_mode_message": _rule(_text(1000)),
    "public_mode_show_message": _FLAG,
    "page_builder_enabled": _rule(_flag, policy=_platform_forbids(
        "FORBID_PAGE_BUILDER", "the page builder is not allowed by the hosting platform")),
    "page_builder_access": _rule(_choice("admin", "editor", "user")),
    "open_signup": _FLAG,
    "open_signup_until": _rule(_future_datetime),
    "approval_required": _FLAG,
    "approval_denied_timeout_hours": _rule(_whole(1, 8760)),
    "approval_pending_timeout_hours": _rule(_whole(0, 8760)),
    "bot_protection_enabled": _FLAG,
    "draft_expiration_hours": _rule(_whole(0, 8760)),
    "login_app_selector": _FLAG,
    "banana_mode": _FLAG,
    # The API service's own settings, with the bounds of its admin form.
    "api_service_enabled": _FLAG,
    "api_service_rate_limit": _rule(_whole(*db.API_RATE_LIMIT_BOUNDS)),
    "api_service_admin_rate_limit": _rule(_whole(*db.API_RATE_LIMIT_BOUNDS)),
    "api_service_max_tokens_per_user": _rule(_whole(*db.API_MAX_TOKENS_BOUNDS)),
    # Attachments. The hosting platform writes the upload size from its plan.
    "upload_mode": _rule(_choice("whitelist", "blacklist", "allow_all"), "attachments"),
    "upload_whitelist": _rule(_text(2000), "attachments"),
    "upload_blacklist": _rule(_text(2000), "attachments"),
    "upload_max_size_mb": _rule(_whole(1, 2048), "attachments", policy=_platform_owned_on_hosted),
    # Page governance.
    "page_protection_enabled": _rule(_flag, "page_governance"),
    "page_reservations_enabled": _rule(_flag, "page_governance"),
    "page_reservation_duration_hours": _rule(_whole(1, 8760), "page_governance"),
    "page_reservation_cooldown_hours": _rule(_whole(0, 8760), "page_governance"),
    "default_reserved_pages_quota": _rule(_whole(1, 1000), "page_governance"),
    "reservation_quota_auto_approve_max": _rule(_whole(0, 1000000), "page_governance"),
    "contribution_approval_enabled": _rule(_flag, "page_governance"),
    "default_contribution_quota": _rule(_whole(1, 1000), "page_governance"),
    "contribution_quota_auto_approve_max": _rule(_whole(0, 1000000), "page_governance"),
    "quota_request_cooldown_hours": _rule(_whole(0, 8760), "page_governance"),
    # Text to speech. The remote GPU settings are not here on purpose.
    "tts_page_panel_enabled": _rule(_flag, "tts"),
    "tts_public_access_enabled": _rule(_flag, "tts"),
    "tts_auto_generate_enabled": _rule(_flag, "tts", policy=_platform_forbids(
        "MANAGED_TTS_DISABLED", "text to speech is switched off by the hosting platform")),
    "tts_performance_mode": _rule(_choice("auto", "balanced", "fast"), "tts"),
    "tts_enabled_languages": _rule(_tts_languages, "tts"),
    # Chat, including direct messages, groups and the cleanup schedule.
    "chat_max_message_length": _rule(_whole(100, 50000), "chat"),
    "chat_attachments_enabled": _rule(_flag, "chat"),
    "chat_max_attachment_size_mb": _rule(_whole(1, 100), "chat"),
    "chat_attachments_per_day_limit": _rule(_whole(1, 1000), "chat"),
    "chat_dm_enabled": _rule(_flag, "chat"),
    "chat_allow_dm_creation": _rule(_flag, "chat"),
    "chat_dm_message_retention_days": _rule(_whole(1, 3650), "chat"),
    "chat_dm_attachment_retention_days": _rule(_whole(1, 3650), "chat"),
    "chat_dm_auto_clear_messages": _rule(_flag, "chat"),
    "chat_dm_auto_clear_attachments": _rule(_flag, "chat"),
    "chat_group_enabled": _rule(_flag, "chat"),
    "chat_allow_group_creation": _rule(_flag, "chat"),
    "chat_group_message_retention_days": _rule(_whole(1, 3650), "chat"),
    "chat_group_attachment_retention_days": _rule(_whole(1, 3650), "chat"),
    "chat_group_auto_clear_messages": _rule(_flag, "chat"),
    "chat_group_auto_clear_attachments": _rule(_flag, "chat"),
    "chat_cleanup_enabled": _rule(_flag, "chat"),
    "chat_cleanup_frequency_days": _rule(_whole(1, 365), "chat"),
    "chat_cleanup_hour": _rule(_whole(0, 23), "chat"),
    # Other plugins.
    "profile_contribution_chart_enabled": _rule(_flag, "user_profiles"),
    "profile_group_badges_enabled": _rule(_flag, "user_profiles", "chat"),
    "kanban_access": _rule(_choice("admin", "editor", "all"), "kanban"),
    "kanban_write_access": _rule(_choice("admin", "editor", "all"), "kanban"),
    "kanban_public_access_enabled": _rule(_flag, "kanban"),
    "kanban_open_access": _rule(_flag, "kanban"),
    "canvas_access": _rule(_choice("admin", "editor", "all"), "canvas"),
    "canvas_write_access": _rule(_choice("admin", "editor", "all"), "canvas"),
    "canvas_public_access_enabled": _rule(_flag, "canvas"),
    "canvas_open_access": _rule(_flag, "canvas"),
    "assessment_points_badge_enabled": _rule(_flag, "assessments"),
    "custom_pages_max_video_size_mb": _rule(_whole(1, 2048), "custom_pages"),
    "docs_bypass_deletion_slowdown": _rule(_flag, "deletion_slowdown"),
}

# Why the columns without a rule are refused. Anything not listed here is
# refused with a generic reason.
_WIKI_MAINTAINED = "maintained by the wiki itself"
_PLATFORM_MANAGED = "managed by the hosting platform"
_OWN_PAGE = "managed on its own admin page"
_LEGACY_CHAT = "legacy setting; use the chat_dm_* and chat_group_* settings"
_READ_ONLY_REASONS = {
    "setup_done": _WIKI_MAINTAINED,
    "last_server_restart_at": _WIKI_MAINTAINED,
    "last_chat_cleanup_at": _WIKI_MAINTAINED,
    "list_order_version": _WIKI_MAINTAINED,
    "chat_cleanup_split_configured": _WIKI_MAINTAINED,
    "docs_category_id": _WIKI_MAINTAINED,
    "platform_upload_blacklist": _PLATFORM_MANAGED,
    "devtools_enabled": _PLATFORM_MANAGED,
    "tts_gpu_enabled": "remote GPU settings cannot be changed through the API",
    "tts_gpu_url": "remote GPU settings cannot be changed through the API",
    "tts_gpu_auth_token": "remote GPU settings cannot be changed through the API",
    "tts_gpu_timeout": "remote GPU settings cannot be changed through the API",
    "favicon_custom": _OWN_PAGE,
    "favicon_order": _OWN_PAGE,
    "interface_languages_json": _OWN_PAGE,
    "chat_auto_clear_messages": _LEGACY_CHAT,
    "chat_auto_clear_attachments": _LEGACY_CHAT,
    "chat_message_retention_days": _LEGACY_CHAT,
    "chat_attachment_retention_days": _LEGACY_CHAT,
}

# Saving any of these on the form marks the split chat cleanup settings as
# configured, which stops the legacy values from being used as a fallback.
_CHAT_SPLIT_SETTINGS = frozenset(
    key for key in _API_SETTINGS if key.startswith(("chat_dm_", "chat_group_", "chat_cleanup_"))
)


def _request_enabled_plugins():
    """Return the plugin map the request guard built, EasyWiki limits included."""
    plugins = getattr(g, "enabled_plugins", None)
    if plugins is None:
        plugins = {row["id"]: bool(row["enabled"]) for row in db.list_plugins()}
    return plugins


def _check_settings_update(data, current):
    """Sort a settings request into updates, invalid values and refusals.

    A value equal to the stored one is accepted for any readable column and
    changes nothing, so a client can send back what GET returned with its
    own changes applied.
    """
    enabled_plugins = _request_enabled_plugins()
    updates, invalid, refused = {}, {}, {}
    for key, value in data.items():
        if key in _SECRET_SETTINGS:
            # Never compared with the stored value: that would let a token
            # guess a secret one request at a time.
            refused[key] = "secret settings cannot be written through the API"
            continue
        if key in current and value == current[key]:
            continue
        rule = _API_SETTINGS.get(key)
        if rule is None:
            if key in current:
                refused[key] = _READ_ONLY_REASONS.get(key, "not writable through the API")
            else:
                invalid[key] = "unknown setting"
            continue
        try:
            parsed = rule.parse(value, current)
        except _SettingError as exc:
            invalid[key] = str(exc)
            continue
        if parsed == current.get(key):
            continue
        disabled = [plugin for plugin in rule.plugins if not enabled_plugins.get(plugin)]
        if disabled:
            refused[key] = f"the {disabled[0]} plugin is not enabled"
            continue
        veto = rule.policy(parsed) if rule.policy else None
        if veto:
            refused[key] = veto
            continue
        updates[key] = parsed
    return updates, invalid, refused


def _readable_page(user_row, slug):
    """Return the page the caller asked for, or raise 404.

    Uses the same rule as the web page view. A page the caller may not read
    gets 404 rather than 403, so the API does not confirm that it exists.
    """
    page = db.get_page_by_slug(slug)
    if not page or not user_can_view_page(user_row, page):
        raise APIError("Page not found", 404)
    return page


def _require_category_write(user_row, category_id):
    """Apply the editor's category write restriction, as the web editor does."""
    if not editor_has_category_access(user_row, category_id):
        raise APIError("You do not have permission to edit pages in this category", 403)


def _page_is_locked(page, user_row):
    """Return True when the page is protected or checked out by another editor."""
    return any(
        context and context.get("edit_locked")
        for context in (_get_page_protection_context(page, user_row),
                        _get_reservation_context(page, user_row))
    )


def _require_page_unlocked(page, user_row):
    """Refuse a page that is protected or checked out by another editor."""
    if _page_is_locked(page, user_row):
        raise APIError("This page is protected or checked out by another editor", 409)


def _require_permission(user_row, permission_key, message):
    """Require a permission key the web interface checks for the same action."""
    if not db.has_permission(user_row, permission_key):
        raise APIError(message, 403)


# The editor's limits for a page. Content is capped in characters, like the
# editor's check, and the title at the length the create and rename forms use.
_MAX_PAGE_TITLE_LENGTH = 200


def _page_title(value):
    """Validate a page title the way the editor does."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise APIError("title must be a string", 400)
    title = value.strip()
    if not title:
        raise APIError("title is required", 400)
    if len(title) > _MAX_PAGE_TITLE_LENGTH:
        raise APIError(f"title cannot exceed {_MAX_PAGE_TITLE_LENGTH} characters", 400)
    return title


def _page_content(value):
    """Validate page content against the editor's size limit."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise APIError("content must be a string", 400)
    if len(value) > _MAX_PAGE_CONTENT_LENGTH:
        raise APIError(f"content cannot exceed {_MAX_PAGE_CONTENT_LENGTH} characters", 400)
    return value


def _new_page_slug(value, title):
    """Return the slug for a new page, normalised the way the editor makes it.

    The editor derives a slug from a title of at most 200 characters, so a
    slug of the caller's choosing gets the same bound.
    """
    if value in (None, ""):
        return slugify(title)
    if not isinstance(value, str):
        raise APIError("slug must be a string", 400)
    slug = slugify(value)
    if len(slug) > _MAX_PAGE_TITLE_LENGTH:
        raise APIError(f"slug cannot exceed {_MAX_PAGE_TITLE_LENGTH} characters", 400)
    return slug


# SQLite keeps ids as signed 64-bit integers. A larger number cannot name a
# row, and handing one to the database raises OverflowError.
_MAX_ROW_ID = 2**63 - 1


def _row_id(value, message):
    """Read a row id sent as a JSON number or as a string of digits.

    Anything else is refused with *message* before it reaches the database:
    booleans, fractions, Infinity (which the JSON parser accepts) and ids
    outside SQLite's range used to come back as a 500.
    """
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit() and len(text) <= 19):
            raise APIError(message, 400)
        value = int(text)
    if type(value) is not int or not 0 < value <= _MAX_ROW_ID:
        raise APIError(message, 400)
    return value


def _page_category_id(value):
    """Return a category id from a request body, or None for no category."""
    if value in (None, "", 0):
        return None
    return _row_id(value, "Invalid category_id")


def _category_name(value):
    """Validate a category name the way the category form does."""
    name = (value or "").strip() if isinstance(value, str) else ""
    if not name:
        raise APIError("name is required", 400)
    if len(name) > 100:
        raise APIError("name cannot exceed 100 characters", 400)
    return name


def _parent_category(value, *, moving=None):
    """Validate a parent category id. None means the top level.

    With *moving*, also refuse the moves the wiki refuses: a category cannot
    become its own parent or sit under one of its own descendants.
    """
    if value in (None, ""):
        return None
    parent_id = _row_id(value, "Invalid parent_id")
    if not db.get_category(parent_id):
        raise APIError("The parent category does not exist", 400)
    if moving is not None:
        if parent_id == moving:
            raise APIError("A category cannot be moved into itself", 400)
        if db.is_descendant_of(moving, parent_id):
            raise APIError("A category cannot be moved into one of its own subcategories", 400)
    return parent_id


def _can_grant_admin_role(actor):
    """Return True when the actor may create or promote admins."""
    return _is_admin(actor)


def _ensure_can_modify_admin_target(target, actor, *, allow_self=False):
    """Raise APIError when *actor* cannot modify an admin-like target."""
    if not target:
        return
    if target["role"] == "owner" and (not allow_self or target["id"] != actor["id"]):
        raise APIError("Cannot modify a protected admin", 403)


def _ensure_can_modify_account(target, actor):
    """Apply the account protections of the user management pages.

    Nobody can change or delete a superuser account there, and only the
    owner can change the owner account.
    """
    if target.get("is_superuser"):
        raise APIError("This account is protected and cannot be modified", 403)
    if target["role"] == "owner" and target["id"] != actor["id"]:
        raise APIError("Cannot modify a protected admin", 403)


# Bulk user creation hashes every password, which costs about a tenth of a
# second of CPU each, so a single request is kept small.
_MAX_BULK_USERS = 20
_MAX_BULK_PAGES = 100
_USER_ROLES = ("user", "editor", "admin")


def _new_username(value):
    """Validate a username with the rules of the account forms."""
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        raise APIError("username is required", 400)
    if len(name) < 3 or len(name) > 50:
        raise APIError("username must be between 3 and 50 characters", 400)
    if not _is_valid_username(name):
        raise APIError("username can only contain letters, digits, underscores and hyphens", 400)
    return name


def _new_password(value):
    """Validate a new password with the length rules of the account forms.

    Surrounding whitespace is removed, as the API has always done.
    """
    password = value.strip() if isinstance(value, str) else ""
    if not password:
        raise APIError("password is required", 400)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise APIError(f"password must be at least {MIN_PASSWORD_LENGTH} characters", 400)
    if len(password) > MAX_PASSWORD_LENGTH:
        raise APIError(f"password cannot exceed {MAX_PASSWORD_LENGTH} characters", 400)
    return password


def _new_user_role(value):
    """Validate the role of a new account."""
    role = (value or "user").strip() if isinstance(value, (str, type(None))) else None
    if role not in _USER_ROLES:
        raise APIError("role must be one of: user, editor, admin", 400)
    return role


def _create_account(item):
    """Create one account from a validated request item and return its row."""
    username = _new_username(item.get("username"))
    password = _new_password(item.get("password"))
    role = _new_user_role(item.get("role"))
    if db.get_user_by_username(username):
        raise APIError("Username already taken", 409)
    try:
        user_id = db.create_user(username, generate_password_hash(password), role)
    except db.IntegrityError:
        # Another request took the name between the check and the insert.
        raise APIError("Username already taken", 409) from None
    updates = {}
    if db.get_site_settings().get("new_user_intro_enabled"):
        updates["intro_required"] = 1
    if item.get("force_password_change"):
        updates["force_password_change"] = 1
    if updates:
        db.update_user(user_id, **updates)
    return db.get_user_by_id(user_id)


def _safe_user_data(user_row):
    """Return a safe dict of user data (no password hash)."""
    return {
        "id": user_row["id"],
        "username": user_row["username"],
        "role": user_row["role"],
        "suspended": bool(user_row["suspended"]),
        "api_access_enabled": bool(user_row.get("api_access_enabled", 0)),
        "created_at": user_row.get("created_at", ""),
        "last_login_at": user_row.get("last_login_at", ""),
    }


def _delete_page_now_or_later(page, user_row):
    """Delete a page the way the web delete does and return True if it is queued.

    With the deletion_slowdown plugin on, the page waits out its grace
    period, unless it is a docs page and docs are set to bypass the delay.
    Plugins hear about the deletion either way, as they do from the editor.
    """
    bypass_slowdown = (
        db.is_docs_category(page["category_id"])
        and db.get_site_settings().get("docs_bypass_deletion_slowdown")
    )
    queued = db.is_plugin_enabled("deletion_slowdown") and not bypass_slowdown
    if queued:
        if not db.mark_page_pending_deletion(page["id"], user_row["id"]):
            raise APIError("Cannot delete the home page", 400)
    else:
        db.delete_page(page["id"])
    emit_hook("after_page_delete", page=page, user=user_row)
    return queued


def _page_delete_blocker(page, user_row):
    """Return why the web delete would refuse this page, or None."""
    if page["is_home"]:
        return "Cannot delete the home page"
    if not editor_has_category_access(user_row, page["category_id"]):
        return "You do not have permission to delete pages in this category"
    if _page_is_locked(page, user_row):
        return "This page is protected or checked out by another editor"
    if db.get_page_expiry(page["id"]) is not None:
        return "This page is scheduled for automatic deletion"
    if page["pending_deletion"]:
        return "This page is already pending deletion"
    return None


_API_SERVICE_ROUTES_REGISTERED = False


def register_api_service_routes(app):
    """Register API Service routes on the Flask app.

    Idempotent: subsequent calls are no-ops.
    """
    global _API_SERVICE_ROUTES_REGISTERED
    if _API_SERVICE_ROUTES_REGISTERED and "settings_api_tokens" in app.view_functions:
        return
    _API_SERVICE_ROUTES_REGISTERED = True

    @app.route("/api/v1/status")
    @rate_limit(30, 60)
    def api_v1_status():
        """Public endpoint: returns API status without authentication."""
        settings = db.get_api_service_settings()
        return jsonify({
            "ok": True,
            "api_enabled": settings["enabled"],
            "version": "1.0.0",
            "service": "BananaWiki API Service",
        })

    @app.route("/api/v1/users", methods=["GET"])
    @_api_authenticate("users")
    def api_v1_list_users(token_row, user_row):
        """List all users."""
        if not _is_admin(user_row):
            return _json_error("Admin access required", 403)
        users = db.list_users()
        return jsonify({"ok": True, "users": [_safe_user_data(u) for u in users]})

    @app.route("/api/v1/users/<user_id>", methods=["GET"])
    @_api_authenticate("users")
    def api_v1_get_user(token_row, user_row, user_id):
        """Get a single user by id."""
        if not _is_admin(user_row):
            return _json_error("Admin access required", 403)
        target = db.get_user_by_id(user_id)
        if not target:
            return _json_error("User not found", 404)
        return jsonify({"ok": True, "user": _safe_user_data(target)})

    @app.route("/api/v1/users", methods=["POST"])
    @_api_authenticate("users", write_required=True)
    def api_v1_create_user(token_row, user_row):
        """Create a new user.  Admin only."""
        _require_admin(token_row, user_row)
        data = _parse_json_body()
        if not data.get("username") or not data.get("password"):
            raise APIError("username and password are required", 400)
        new_user = _create_account(data)
        log_action("api_create_user", request, user=user_row,
                    extra={"target_username": new_user["username"], "target_role": new_user["role"]})
        return jsonify({"ok": True, "user": _safe_user_data(new_user)}), 201

    @app.route("/api/v1/users/bulk", methods=["POST"])
    @_api_authenticate("users", write_required=True)
    def api_v1_bulk_create_users(token_row, user_row):
        """Bulk create users.  Admin only."""
        _require_admin(token_row, user_row)
        data = _parse_json_body()
        users_data = _json_list(data, "users", _MAX_BULK_USERS)
        if not users_data:
            raise APIError("'users' must be a non-empty array", 400)
        if not all(isinstance(item, dict) for item in users_data):
            raise APIError("Each item in 'users' must be an object", 400)
        created = []
        errors = []
        for item in users_data:
            username = item.get("username") if isinstance(item.get("username"), str) else ""
            try:
                created.append(_safe_user_data(_create_account(item)))
            except APIError as exc:
                errors.append({"username": username, "error": exc.message})
        log_action("api_bulk_create_users", request, user=user_row,
                    extra={"created": len(created), "errors": len(errors)})
        return jsonify({"ok": True, "created": created, "errors": errors}), 201 if created else 200

    @app.route("/api/v1/users/<user_id>", methods=["PUT"])
    @_api_authenticate("users", write_required=True)
    def api_v1_update_user(token_row, user_row, user_id):
        """Update a user.  Admin only."""
        _require_admin(token_row, user_row)
        target = db.get_user_by_id(user_id)
        if not target:
            return _json_error("User not found", 404)
        _ensure_can_modify_account(target, user_row)
        data = _parse_json_body()
        updates = {}
        if "role" in data:
            role = data["role"].strip() if isinstance(data["role"], str) else None
            if role not in ("user", "editor", "admin", "owner"):
                raise APIError("Invalid role", 400)
            if role == "owner":
                raise APIError("Cannot assign protected admin via API", 403)
            if (
                target["role"] in ("admin", "owner")
                and role not in ("admin", "owner")
                and db.count_admins() <= 1
            ):
                raise APIError("Cannot demote the last admin", 400)
            updates["role"] = role
        if "suspended" in data:
            suspended = _json_flag(data["suspended"], "suspended")
            if (
                suspended
                and target["role"] in ("admin", "owner")
                and db.count_admins() <= 1
            ):
                raise APIError("Cannot suspend the last admin", 400)
            updates["suspended"] = 1 if suspended else 0
        # An empty password leaves the current one in place, as before.
        if "password" in data and not (isinstance(data["password"], str) and not data["password"].strip()):
            updates["password"] = generate_password_hash(_new_password(data["password"]))
        if "api_access_enabled" in data:
            updates["api_access_enabled"] = 1 if _json_flag(data["api_access_enabled"], "api_access_enabled") else 0
        revoked_tokens = None
        if updates:
            changed = list(updates)
            if "password" in updates:
                # As in the admin password form: end the account's web
                # sessions. Its API tokens go too, since whoever held the
                # old password may have issued them.
                updates["session_token"] = uuid.uuid4().hex
            db.update_user(user_id, **updates)
            if "password" in updates:
                db.revoke_all_user_sessions(user_id)
                revoked_tokens = db.revoke_user_api_service_tokens(
                    user_id, reason="password changed through the API")
            log_action("api_update_user", request, user=user_row,
                        extra={"target_user_id": user_id, "updates": changed})
        updated = db.get_user_by_id(user_id)
        payload = {"ok": True, "user": _safe_user_data(updated)}
        if revoked_tokens is not None:
            payload["api_tokens_revoked"] = revoked_tokens
        return jsonify(payload)

    @app.route("/api/v1/users/<user_id>", methods=["DELETE"])
    @_api_authenticate("users", write_required=True)
    def api_v1_delete_user(token_row, user_row, user_id):
        """Delete a user.  Admin only."""
        _require_admin(token_row, user_row)
        target = db.get_user_by_id(user_id)
        if not target:
            return _json_error("User not found", 404)
        if target["is_superuser"]:
            raise APIError("This account is protected and cannot be modified", 403)
        if target["role"] == "owner":
            raise APIError("Cannot delete a protected admin", 403)
        if target["role"] in ("admin", "owner") and db.count_admins() <= 1:
            raise APIError("Cannot delete the last admin", 400)
        if target["is_home"] if "is_home" in target else False:
            raise APIError("Cannot delete the home user", 400)
        db.delete_user(user_id)
        log_action("api_delete_user", request, user=user_row,
                    extra={"target_user_id": user_id, "target_username": target["username"]})
        return jsonify({"ok": True, "message": f"User '{target['username']}' has been deleted"})

    @app.route("/api/v1/pages", methods=["GET"])
    @_api_authenticate("pages")
    def api_v1_list_pages(token_row, user_row):
        """List the pages the caller is allowed to read."""
        pages = [dict(row) for row in db.list_all_pages() if user_can_view_page(user_row, row)]
        return jsonify({
            "ok": True,
            "pages": [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "slug": p["slug"],
                    "category_id": p.get("category_id"),
                    "is_home": bool(p.get("is_home", False)),
                    "created_at": p.get("created_at", ""),
                    "last_edited_at": p.get("last_edited_at", ""),
                    "last_edited_by": p.get("last_edited_by", ""),
                }
                for p in pages
            ],
        })

    @app.route("/api/v1/pages/<slug>", methods=["GET"])
    @_api_authenticate("pages")
    def api_v1_get_page(token_row, user_row, slug):
        """Get a single page by slug."""
        page = _readable_page(user_row, slug)
        return jsonify({
            "ok": True,
            "page": {
                "id": page["id"],
                "title": page["title"],
                "slug": page["slug"],
                "content": page.get("content", ""),
                "category_id": page.get("category_id"),
                "is_home": bool(page["is_home"]),
                "created_at": page.get("created_at", ""),
                "last_edited_at": page.get("last_edited_at", ""),
                "last_edited_by": page.get("last_edited_by", ""),
            },
        })

    @app.route("/api/v1/pages", methods=["POST"])
    @_api_authenticate("pages", write_required=True)
    def api_v1_create_page(token_row, user_row):
        """Create a new page.  Requires editor+ role."""
        _require_editor(token_row, user_row)
        data = _parse_json_body()
        title = _page_title(data.get("title"))
        content = _page_content(data.get("content", ""))
        slug = _new_page_slug(data.get("slug"), title)
        category_id = _page_category_id(data.get("category_id"))
        existing = db.get_page_by_slug(slug)
        if existing:
            raise APIError(f"Page with slug '{slug}' already exists", 409)
        if category_id is not None and not db.get_category(category_id):
            raise APIError("Category does not exist", 400)
        _require_category_write(user_row, category_id)
        page_id = db.create_page(title, slug, content, category_id=category_id,
                                  user_id=user_row["id"])
        page = db.get_page(page_id)
        log_action("api_create_page", request, user=user_row,
                    extra={"slug": slug, "title": title})
        notify_change("page_create", f"Page '{title}' created via API")
        emit_hook("after_page_create", page=page, user=user_row)
        return jsonify({
            "ok": True,
            "page": {
                "id": page["id"],
                "title": page["title"],
                "slug": page["slug"],
            },
        }), 201

    @app.route("/api/v1/pages/<slug>", methods=["PUT"])
    @_api_authenticate("pages", write_required=True)
    def api_v1_update_page(token_row, user_row, slug):
        """Update a page.  Requires editor+ role."""
        _require_editor(token_row, user_row)
        page = _readable_page(user_row, slug)
        _require_category_write(user_row, page["category_id"])
        _require_page_unlocked(page, user_row)
        data = _parse_json_body()
        updates = {}
        if "title" in data:
            updates["title"] = _page_title(data["title"])
        if "content" in data:
            updates["content"] = _page_content(data["content"])
        if "category_id" in data:
            updates["category_id"] = _page_category_id(data["category_id"])
            destination = updates["category_id"]
            if destination != page.get("category_id"):
                if page["is_home"]:
                    raise APIError("Cannot move the home page", 400)
                if destination is not None and not db.get_category(destination):
                    raise APIError("Category does not exist", 400)
                # Moving a page writes to both categories, as in the editor.
                _require_category_write(user_row, destination)
        changed = bool(updates)
        if changed:
            content_was_replaced = "content" in updates
            title = updates.pop("title", page.get("title", ""))
            content = updates.pop("content", page.get("content", ""))
            category_id = updates.pop("category_id", page.get("category_id"))
            db.update_page(
                page["id"], title, content, user_row["id"],
                builder_json="" if content_was_replaced else None,
                builder_public=False if content_was_replaced else None,
            )
            if category_id != page.get("category_id"):
                db.update_page_category(page["id"], category_id)
        updated = db.get_page(page["id"])
        log_action("api_update_page", request, user=user_row,
                    extra={"slug": slug})
        notify_change("page_update", f"Page '{slug}' updated via API")
        if changed:
            # Plugins such as TTS and canvas keep derived data per page and
            # need to hear about API edits as they do about editor saves.
            emit_hook("after_page_update", page=updated, user=user_row)
        return jsonify({
            "ok": True,
            "page": {
                "id": updated["id"],
                "title": updated["title"],
                "slug": updated["slug"],
                "content": updated.get("content", ""),
            },
        })

    @app.route("/api/v1/pages/<slug>", methods=["DELETE"])
    @_api_authenticate("pages", write_required=True)
    def api_v1_delete_page(token_row, user_row, slug):
        """Delete a page.  Requires editor+ role and the page.delete permission."""
        _require_editor(token_row, user_row)
        page = _readable_page(user_row, slug)
        _require_permission(user_row, "page.delete",
                            "You do not have permission to delete pages")
        if page["is_home"]:
            raise APIError("Cannot delete the home page", 400)
        _require_category_write(user_row, page["category_id"])
        _require_page_unlocked(page, user_row)
        if db.get_page_expiry(page["id"]) is not None:
            raise APIError("This page is scheduled for automatic deletion", 409)
        if page["pending_deletion"]:
            raise APIError("This page is already pending deletion", 409)
        # The deletion_slowdown plugin promises a grace period, and a bearer
        # token is exactly the case it is there for.
        if _delete_page_now_or_later(page, user_row):
            log_action("api_page_pending_deletion", request, user=user_row,
                       extra={"slug": slug, "title": page["title"]})
            notify_change("page_pending_delete", f"Page '{slug}' queued for deletion via API (48h grace period)")
            return jsonify({
                "ok": True,
                "pending_deletion": True,
                "message": f"Page '{slug}' will be deleted after the grace period",
            }), 202
        cleanup_unused_uploads()
        log_action("api_delete_page", request, user=user_row,
                    extra={"slug": slug, "title": page["title"]})
        notify_change("page_delete", f"Page '{slug}' deleted via API")
        return jsonify({"ok": True, "message": f"Page '{slug}' has been deleted"})

    @app.route("/api/v1/pages/bulk", methods=["POST"])
    @_api_authenticate("pages", write_required=True)
    def api_v1_bulk_create_pages(token_row, user_row):
        """Bulk create pages.  Admin only.

        Every item is validated before any page is written, so a malformed
        item leaves the wiki untouched. Conflicts found while creating, such
        as a slug that already exists, are reported per item.
        """
        _require_admin(token_row, user_row)
        data = _parse_json_body()
        pages_data = _json_list(data, "pages", _MAX_BULK_PAGES)
        if not pages_data:
            raise APIError("'pages' must be a non-empty array", 400)
        planned = []
        for index, item in enumerate(pages_data):
            if not isinstance(item, dict):
                raise APIError(f"pages[{index}] must be an object", 400)
            try:
                title = _page_title(item.get("title"))
                planned.append({
                    "title": title,
                    "content": _page_content(item.get("content", "")),
                    "slug": _new_page_slug(item.get("slug"), title),
                    "category_id": _page_category_id(item.get("category_id")),
                })
            except APIError as exc:
                raise APIError(f"pages[{index}]: {exc.message}", exc.status_code) from None
        created = []
        errors = []
        for item in planned:
            if db.get_page_by_slug(item["slug"]):
                errors.append({"title": item["title"], "error": f"slug '{item['slug']}' already exists"})
                continue
            if item["category_id"] is not None and not db.get_category(item["category_id"]):
                errors.append({"title": item["title"], "error": "Category does not exist"})
                continue
            page_id = db.create_page(item["title"], item["slug"], item["content"],
                                     category_id=item["category_id"], user_id=user_row["id"])
            page = db.get_page(page_id)
            emit_hook("after_page_create", page=page, user=user_row)
            created.append({"id": page["id"], "title": page["title"], "slug": page["slug"]})
        log_action("api_bulk_create_pages", request, user=user_row,
                    extra={"created": len(created), "errors": len(errors)})
        return jsonify({"ok": True, "created": created, "errors": errors}), 201 if created else 200

    @app.route("/api/v1/pages/bulk-delete", methods=["POST"])
    @_api_authenticate("pages", write_required=True)
    def api_v1_bulk_delete_pages(token_row, user_row):
        """Bulk delete pages by slug or id.  Admin only.

        Each page goes through the checks of a single delete: the home page,
        protected or checked-out pages, pages scheduled for automatic
        deletion and pages already pending deletion are skipped.
        """
        _require_admin(token_row, user_row)
        _require_permission(user_row, "page.delete",
                            "You do not have permission to delete pages")
        data = _parse_json_body()
        slugs = _json_list(data, "slugs", _MAX_BULK_PAGES)
        ids = _json_list(data, "ids", _MAX_BULK_PAGES)
        if not slugs and not ids:
            raise APIError("Provide 'slugs' or 'ids' array", 400)
        if len(slugs) + len(ids) > _MAX_BULK_PAGES:
            raise APIError(f"Bulk delete limited to {_MAX_BULK_PAGES} pages at a time", 400)
        if not all(isinstance(slug, str) for slug in slugs):
            raise APIError("'slugs' must contain strings", 400)
        if not all(type(page_id) is int
                   or (isinstance(page_id, str) and page_id.isascii() and page_id.isdigit())
                   for page_id in ids):
            raise APIError("'ids' must contain page ids", 400)
        pages = [db.get_page_by_slug(slug) for slug in slugs]
        for page_id in ids:
            # An id past SQLite's integer range cannot name a page. Count it
            # as not found rather than let the database raise OverflowError,
            # and do not convert digit strings too long to be an id at all.
            if isinstance(page_id, str):
                page_id = int(page_id) if len(page_id) <= 19 else 0
            pages.append(db.get_page(page_id) if 0 < page_id <= _MAX_ROW_ID else None)
        deleted = 0
        not_found = 0
        skipped = 0
        seen = set()
        for page in pages:
            if not page:
                not_found += 1
                continue
            if page["id"] in seen or _page_delete_blocker(page, user_row):
                skipped += 1
                continue
            seen.add(page["id"])
            _delete_page_now_or_later(page, user_row)
            deleted += 1
        if deleted:
            cleanup_unused_uploads()
        log_action("api_bulk_delete_pages", request, user=user_row,
                    extra={"deleted": deleted, "not_found": not_found, "skipped": skipped})
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "not_found": not_found,
            "skipped": skipped,
            "message": f"{deleted} page(s) deleted, {skipped} skipped, {not_found} not found",
        })

    @app.route("/api/v1/pages/bulk-edit", methods=["POST"])
    @_api_authenticate("pages", write_required=True)
    def api_v1_bulk_edit_pages(token_row, user_row):
        """Bulk edit pages: update title/content/category for multiple pages.  Admin only.

        As with bulk create, malformed items are refused before anything is
        written, and per-page problems are reported per item.
        """
        _require_admin(token_row, user_row)
        data = _parse_json_body()
        edits = _json_list(data, "edits", _MAX_BULK_PAGES)
        if not edits:
            raise APIError("'edits' must be a non-empty array", 400)
        planned = []
        for index, item in enumerate(edits):
            if not isinstance(item, dict):
                raise APIError(f"edits[{index}] must be an object", 400)
            try:
                if not isinstance(item.get("slug"), str) or not item["slug"].strip():
                    raise APIError("slug is required", 400)
                changes = {}
                if "title" in item:
                    changes["title"] = _page_title(item["title"])
                if "content" in item:
                    changes["content"] = _page_content(item["content"])
                if "category_id" in item:
                    changes["category_id"] = _page_category_id(item["category_id"])
            except APIError as exc:
                raise APIError(f"edits[{index}]: {exc.message}", exc.status_code) from None
            planned.append((item["slug"].strip(), changes))
        updated = 0
        errors = []
        for slug, changes in planned:
            page = db.get_page_by_slug(slug)
            if not page:
                errors.append({"slug": slug, "error": "page not found"})
                continue
            if not changes:
                continue
            if _page_is_locked(page, user_row):
                errors.append({"slug": slug, "error": "page is protected or checked out by another editor"})
                continue
            category_id = changes.get("category_id", page.get("category_id"))
            if category_id != page.get("category_id"):
                if page["is_home"]:
                    errors.append({"slug": slug, "error": "cannot move the home page"})
                    continue
                if category_id is not None and not db.get_category(category_id):
                    errors.append({"slug": slug, "error": "category does not exist"})
                    continue
            content_was_replaced = "content" in changes
            db.update_page(
                page["id"], changes.get("title", page.get("title", "")),
                changes.get("content", page.get("content", "")), user_row["id"],
                builder_json="" if content_was_replaced else None,
                builder_public=False if content_was_replaced else None,
            )
            if category_id != page.get("category_id"):
                db.update_page_category(page["id"], category_id)
            emit_hook("after_page_update", page=db.get_page(page["id"]), user=user_row)
            updated += 1
        log_action("api_bulk_edit_pages", request, user=user_row,
                    extra={"updated": updated, "errors": len(errors)})
        return jsonify({"ok": True, "updated": updated, "errors": errors})

    @app.route("/api/v1/categories", methods=["GET"])
    @_api_authenticate("categories")
    def api_v1_list_categories(token_row, user_row):
        """List the categories the caller is allowed to read."""
        cats = [c for c in db.list_categories() if user_can_view_category(user_row, c["id"])]
        return jsonify({
            "ok": True,
            "categories": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "parent_id": c.get("parent_id"),
                    "sort_order": c.get("sort_order", 0),
                }
                for c in cats
            ],
        })

    @app.route("/api/v1/categories", methods=["POST"])
    @_api_authenticate("categories", write_required=True)
    def api_v1_create_category(token_row, user_row):
        """Create a category.  Requires category.create, as in the wiki."""
        _require_editor(token_row, user_row)
        _require_permission(user_row, "category.create",
                            "You do not have permission to create categories")
        data = _parse_json_body()
        name = _category_name(data.get("name"))
        parent_id = _parent_category(data.get("parent_id"))
        cat_id = db.create_category(name, parent_id=parent_id)
        cat = db.get_category(cat_id)
        log_action("api_create_category", request, user=user_row,
                    extra={"name": name, "category_id": cat_id})
        return jsonify({
            "ok": True,
            "category": {"id": cat["id"], "name": cat["name"], "parent_id": cat.get("parent_id")},
        }), 201

    @app.route("/api/v1/categories/<int:cat_id>", methods=["PUT"])
    @_api_authenticate("categories", write_required=True)
    def api_v1_update_category(token_row, user_row, cat_id):
        """Rename or move a category, with the permissions the wiki requires.

        Renaming needs category.edit and moving needs category.reorder. Both
        are validated before either is applied, so a request that fails part
        way leaves the category untouched.
        """
        _require_editor(token_row, user_row)
        cat = db.get_category(cat_id)
        if not cat:
            return _json_error("Category not found", 404)
        data = _parse_json_body()
        name = parent_id = None
        if "name" in data:
            _require_permission(user_row, "category.edit",
                                "You do not have permission to rename categories")
            name = _category_name(data["name"])
        moving = "parent_id" in data
        if moving:
            _require_permission(user_row, "category.reorder",
                                "You do not have permission to move categories")
            parent_id = _parent_category(data["parent_id"], moving=cat_id)
        if name is not None:
            db.update_category(cat_id, name)
        if moving and parent_id != cat.get("parent_id"):
            db.update_category_parent(cat_id, parent_id)
        updated = db.get_category(cat_id)
        log_action("api_update_category", request, user=user_row,
                    extra={"category_id": cat_id})
        return jsonify({
            "ok": True,
            "category": {"id": updated["id"], "name": updated["name"], "parent_id": updated.get("parent_id")},
        })

    @app.route("/api/v1/categories/<int:cat_id>", methods=["DELETE"])
    @_api_authenticate("categories", write_required=True)
    def api_v1_delete_category(token_row, user_row, cat_id):
        """Delete a category.  Admin only."""
        _require_admin(token_row, user_row)
        cat = db.get_category(cat_id)
        if not cat:
            return _json_error("Category not found", 404)
        db.delete_category(cat_id)
        log_action("api_delete_category", request, user=user_row,
                    extra={"category_id": cat_id, "name": cat["name"]})
        return jsonify({"ok": True, "message": f"Category '{cat['name']}' has been deleted"})

    @app.route("/api/v1/settings", methods=["GET"])
    @_api_authenticate("settings")
    def api_v1_get_settings(token_row, user_row):
        """Get site settings.  Admin only."""
        _require_admin(token_row, user_row)
        settings = db.get_site_settings()
        if not settings:
            return _json_error("Settings not found", 404)
        safe_settings = {k: v for k, v in settings.items()
                         if k not in _SECRET_SETTINGS}
        return jsonify({"ok": True, "settings": safe_settings})

    @app.route("/api/v1/settings", methods=["PUT"])
    @_api_authenticate("settings", write_required=True)
    def api_v1_update_settings(token_row, user_row):
        """Update site settings.  Admin only.

        Only the settings in the API schema can change, with the validation
        of the admin settings form. The whole request is refused when any
        value is invalid (400) or not allowed to change (403), so nothing is
        half applied.
        """
        _require_admin(token_row, user_row)
        data = _parse_json_body()
        if not data:
            raise APIError("No valid settings provided", 400)
        current = db.get_site_settings() or {}
        updates, invalid, refused = _check_settings_update(data, current)
        if invalid:
            return jsonify({
                "error": "Invalid settings: " + ", ".join(sorted(invalid)),
                "invalid": invalid,
            }), 400
        if refused:
            return jsonify({
                "error": "These settings cannot be changed through the API: " + ", ".join(sorted(refused)),
                "refused": refused,
            }), 403
        if not updates:
            return jsonify({"ok": True, "updated": [], "message": "Settings unchanged"})

        # Values the settings form changes together with these.
        if "favicon_type" in updates:
            updates["favicon_custom"] = ""
        if updates.get("public_mode") == 0:
            updates["public_mode_until"] = ""
        if updates.get("open_signup") == 0:
            updates["open_signup_until"] = ""
        if _CHAT_SPLIT_SETTINGS.intersection(updates):
            updates["chat_cleanup_split_configured"] = 1
        db.update_site_settings(**updates)
        # Tidy up shares that the narrower access level no longer allows,
        # as the settings form does.
        if "kanban_access" in updates:
            db.kanban_revoke_role_shares_for_restricted_roles(updates["kanban_access"])
            db.kanban_remove_all_invalid_assignees(updates["kanban_access"])
        if "canvas_access" in updates:
            db.canvas_revoke_role_shares_for_restricted_roles(updates["canvas_access"])
        changed = sorted(key for key in updates if key in data)
        log_action("api_update_settings", request, user=user_row,
                    extra={"updated_keys": changed})
        notify_change("settings_update", "Site settings updated via API")
        return jsonify({"ok": True, "updated": changed, "message": "Settings updated successfully"})

    # These token endpoints act on the caller's own tokens only.  The
    # /api/v1/admin/tokens set further down does the same work for any
    # user and is gated on the admin scope instead.

    @app.route("/api/v1/tokens", methods=["GET"])
    @_api_authenticate("tokens")
    def api_v1_list_own_tokens(token_row, user_row):
        """List the current user's own API tokens."""
        tokens = db.list_user_tokens(user_row["id"])
        safe_tokens = []
        for token in tokens:
            safe_tokens.append({
                "id": token["id"],
                "name": token["name"],
                "permissions": json.loads(token.get("permissions", "{}")),
                "last_used_at": token.get("last_used_at"),
                "expires_at": token.get("expires_at"),
                "active": bool(token["active"]),
                "created_at": token["created_at"],
            })
        return jsonify({"ok": True, "tokens": safe_tokens})

    @app.route("/api/v1/tokens", methods=["POST"])
    @_api_authenticate("tokens", write_required=True)
    def api_v1_create_token(token_row, user_row):
        """Create a new API token for the current user."""
        data = _parse_json_body()
        settings = db.get_api_service_settings()
        max_tokens = settings["max_tokens_per_user"]
        current_count = db.count_user_tokens(user_row["id"])
        if current_count >= max_tokens:
            raise APIError(f"Maximum of {max_tokens} active tokens allowed", 400)
        name = data.get("name") or ""
        if not isinstance(name, str):
            raise APIError("name must be a string", 400)
        name = name.strip()[:64]
        permissions = data.get("permissions", {"read": True, "write": False, "scopes": ["pages"]})
        permissions, expires_at = _child_token_settings(token_row, permissions, data.get("expires_at"))
        if _scopes_for_role(user_row, permissions["scopes"]) != permissions["scopes"]:
            raise APIError("Only administrators can hold the admin, settings or users scope", 403)
        raw_token = db.create_api_token(
            user_id=user_row["id"],
            name=name,
            permissions=permissions,
            expires_at=expires_at,
        )
        log_action("api_create_token", request, user=user_row,
                    extra={"token_name": name})
        return jsonify({
            "ok": True,
            "token": raw_token,
            "warning": "Save this token now. It will not be shown again",
        }), 201

    @app.route("/api/v1/tokens/<int:token_id>", methods=["DELETE"])
    @_api_authenticate("tokens", write_required=True)
    def api_v1_revoke_token(token_row, user_row, token_id):
        """Revoke (deactivate) one of the current user's tokens."""
        token = db.get_token_by_id(token_id)
        if not token or token["user_id"] != user_row["id"]:
            return _json_error("Token not found", 404)
        db.revoke_api_service_token(token_id)
        log_action("api_revoke_token", request, user=user_row,
                    extra={"token_id": token_id})
        return jsonify({"ok": True, "message": "Token has been revoked"})

    @app.route("/api/v1/admin/tokens", methods=["GET"])
    @_api_authenticate("admin")
    def api_v1_admin_list_tokens(token_row, user_row):
        """List all API tokens across all users.  Admin only."""
        _require_admin(token_row, user_row)
        tokens = db.list_all_tokens()
        safe = []
        for token in tokens:
            safe.append({
                "id": token["id"],
                "user_id": token["user_id"],
                "username": token.get("username", ""),
                "name": token["name"],
                "permissions": json.loads(token.get("permissions", "{}")),
                "last_used_at": token.get("last_used_at"),
                "expires_at": token.get("expires_at"),
                "active": bool(token["active"]),
                "created_at": token["created_at"],
            })
        return jsonify({"ok": True, "tokens": safe})

    @app.route("/api/v1/admin/tokens/<int:token_id>/revoke", methods=["POST"])
    @_api_authenticate("admin", write_required=True)
    def api_v1_admin_revoke_token(token_row, user_row, token_id):
        """Revoke any user's token.  Admin only."""
        _require_admin(token_row, user_row)
        token = db.get_token_by_id(token_id)
        if not token:
            return _json_error("Token not found", 404)
        target = db.get_user_by_id(token["user_id"])
        _ensure_can_modify_admin_target(target, user_row, allow_self=True)
        db.revoke_api_service_token(token_id)
        log_action("api_admin_revoke_token", request, user=user_row,
                    extra={"token_id": token_id, "target_user_id": token["user_id"]})
        return jsonify({"ok": True, "message": "Token has been revoked"})

    @app.route("/api/v1/admin/users/<user_id>/api-access", methods=["PUT"])
    @_api_authenticate("admin", write_required=True)
    def api_v1_admin_toggle_api_access(token_row, user_row, user_id):
        """Enable or disable API access for a user.  Admin only."""
        _require_admin(token_row, user_row)
        target = db.get_user_by_id(user_id)
        if not target:
            return _json_error("User not found", 404)
        _ensure_can_modify_account(target, user_row)
        data = _parse_json_body()
        enabled = _json_flag(data.get("enabled", True), "enabled")
        db.update_user(user_id, api_access_enabled=1 if enabled else 0)
        log_action("api_toggle_user_api_access", request, user=user_row,
                    extra={"target_user_id": user_id, "enabled": enabled})
        return jsonify({
            "ok": True,
            "message": f"API access has been {'enabled' if enabled else 'disabled'} for user",
        })

    @app.route("/api/v1/admin/audit-log", methods=["GET"])
    @_api_authenticate("admin")
    def api_v1_admin_audit_log(token_row, user_row):
        """Get the API audit log.  Admin only."""
        _require_admin(token_row, user_row)
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)
        # SQLite reads LIMIT -1 as "no limit", and a huge offset overflows
        # its integer type, so keep both inside sensible bounds.
        limit = max(1, min(limit, 500))
        offset = max(0, min(offset, 2**31 - 1))
        entries = db.get_audit_log(limit=limit, offset=offset)
        total = db.count_audit_log_entries()
        return jsonify({
            "ok": True,
            "entries": entries,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @app.route("/api/v1/admin/audit-log", methods=["DELETE"])
    @_api_authenticate("admin", write_required=True)
    def api_v1_admin_clear_audit_log(token_row, user_row):
        """Clear the API audit log.  Admin only."""
        _require_admin(token_row, user_row)
        if not user_row.get("is_superuser", 0):
            raise APIError("Only superusers can clear API audit logs", 403)
        before_days = request.args.get("before_days", 90, type=int)
        before_days = max(1, min(before_days, 3650))
        db.clear_audit_log(before_days=before_days)
        log_action("api_clear_audit_log", request, user=user_row,
                    extra={"before_days": before_days})
        return jsonify({"ok": True, "message": f"Audit log entries older than {before_days} days cleared"})

    # Banana mode takes the whole wiki offline for everyone but admins, so
    # the admin scope alone is not enough: the owner must be an admin too.

    @app.route("/api/v1/banana-mode", methods=["GET"])
    @_api_authenticate("admin")
    def api_v1_banana_mode_status(token_row, user_row):
        """Return the current banana mode state.  Admin only."""
        _require_admin(token_row, user_row)
        settings = db.get_site_settings()
        current_mode = bool(settings and settings["banana_mode"])
        return jsonify({"ok": True, "banana_mode": current_mode})

    @app.route("/api/v1/banana-mode", methods=["POST"])
    @_api_authenticate("admin", write_required=True)
    def api_v1_banana_mode_toggle(token_row, user_row):
        """Toggle banana mode on/off.  Admin only."""
        _require_admin(token_row, user_row)
        settings = db.get_site_settings()
        current_mode = bool(settings and settings["banana_mode"])
        new_mode = not current_mode
        db.update_site_settings(banana_mode=1 if new_mode else 0)
        log_action("banana_mode_toggled", request, user=user_row,
                    extra={"enabled": 1 if new_mode else 0})
        status = "enabled" if new_mode else "disabled"
        return jsonify({
            "ok": True, "banana_mode": new_mode,
            "changed": True, "message": f"Banana mode has been {status}.",
        })

    @app.route("/api/v1/userbot/me", methods=["GET"])
    @_api_authenticate("userbot")
    def api_v1_userbot_me(_token_row, user):
        """Return the authenticated userbot account details.

        Requires an API service token with ``userbot`` scope.
        """
        profile = db.get_user_profile(user["id"])
        token_info = db.get_userbot_token_info(user["id"])
        return jsonify({
            "ok": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "userbot_enabled": bool(user.get("userbot_enabled", 0)),
                "userbot_mode_lock": user.get("userbot_mode_lock", "unlocked"),
                "userbot_enable_count": int(user.get("userbot_enable_count", 0) or 0),
                "userbot_disable_count": int(user.get("userbot_disable_count", 0) or 0),
            },
            "profile": {
                "real_name": profile["real_name"] if profile else "",
                "bio": profile["bio"] if profile else "",
                "page_published": bool(profile["page_published"]) if profile else False,
                "page_disabled_by_admin": bool(profile["page_disabled_by_admin"]) if profile else False,
            },
            "token": {
                "created_at": token_info["created_at"] if token_info else None,
                "last_used_at": token_info["last_used_at"] if token_info else None,
            },
        })

    @app.route("/api/v1/userbot/profile", methods=["POST"])
    @_api_authenticate("userbot", write_required=True)
    def api_v1_userbot_profile(_token_row, user):
        """Update basic profile fields for the authenticated userbot account.

        Requires an API service token with ``userbot`` scope and write access.
        """
        payload = _parse_json_body()
        real_name = str(payload.get("real_name", "")).strip()[:100]
        bio = str(payload.get("bio", "")).strip()[:500]
        page_published = payload.get("page_published")
        profile = db.get_user_profile(user["id"])
        if page_published is not None and profile and profile.get("page_disabled_by_admin") and bool(page_published):
            return _json_error("Profile is disabled by an admin", 403)
        kwargs = {"real_name": real_name, "bio": bio}
        if page_published is not None:
            kwargs["page_published"] = bool(page_published)
        db.upsert_user_profile(user["id"], **kwargs)
        log_action("userbot_update_profile", request, user=user)
        updated = db.get_user_profile(user["id"])
        return jsonify({
            "ok": True,
            "profile": {
                "real_name": updated["real_name"] if updated else "",
                "bio": updated["bio"] if updated else "",
                "page_published": bool(updated["page_published"]) if updated else False,
            },
        })

    # Everything from here down renders HTML for a browser session.  The
    # /api/v1 handlers above answer with JSON and authenticate on a token
    # alone, so the two halves must not share helpers that assume a session.

    @app.route("/admin/api-service")
    @login_required
    @admin_required
    def admin_api_service():
        """Admin management page for the API Service."""
        settings = db.get_api_service_settings()
        site_settings = db.get_site_settings()
        tokens = db.list_all_tokens()
        users = db.list_users()
        audit = db.get_audit_log(limit=200)
        total_audit = db.count_audit_log_entries()
        return render_template(
            "api_service/admin.html",
            api_settings=settings,
            banana_enabled=bool(site_settings and site_settings.get("banana_mode")),
            tokens=tokens,
            users=users,
            audit_entries=audit,
            total_audit=total_audit,
        )

    def _toggle_banana_mode_from_ui():
        """Flip the banana_mode site setting and report the new state."""
        site_settings = db.get_site_settings()
        current = bool(site_settings and site_settings.get("banana_mode"))
        new_mode = 0 if current else 1
        db.update_site_settings(banana_mode=new_mode)
        log_action(
            "banana_mode_toggled",
            request,
            user=get_current_user(),
            extra={"enabled": new_mode},
        )
        if new_mode:
            flash(t("flash.banana_mode_has_been_enabled"), "success")
        else:
            flash(t("flash.banana_mode_has_been_disabled"), "success")
        return redirect(url_for("admin_api_service", _anchor="banana-mode"))

    @app.route("/admin/api-service/banana-mode/toggle", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_api_service_banana_toggle():
        """POST target for the Banana Mode switch on the API Service admin page."""
        return _toggle_banana_mode_from_ui()

    @app.route("/admin/banana", methods=["GET"])
    @login_required
    @admin_required
    def admin_banana():
        """Legacy Banana Mode page URL: redirect to the unified API Service page."""
        return redirect(url_for("admin_api_service", _anchor="banana-mode"), code=301)

    @app.route("/admin/banana-toggle", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def banana_toggle():
        """Legacy Banana Mode toggle URL."""
        return _toggle_banana_mode_from_ui()

    @app.route("/admin/api-service/settings", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_api_service_settings():
        """Update API Service settings."""
        enabled = 1 if request.form.get("enabled") else 0
        rate_limit_val = request.form.get("rate_limit", 60, type=int)
        admin_rate_limit = request.form.get("admin_rate_limit", 120, type=int)
        max_tokens = request.form.get("max_tokens_per_user", 5, type=int)
        rate_low, rate_high = db.API_RATE_LIMIT_BOUNDS
        tokens_low, tokens_high = db.API_MAX_TOKENS_BOUNDS
        rate_limit_val = max(rate_low, min(rate_limit_val, rate_high))
        admin_rate_limit = max(rate_low, min(admin_rate_limit, rate_high))
        max_tokens = max(tokens_low, min(max_tokens, tokens_high))
        db.update_api_service_settings(
            api_service_enabled=enabled,
            api_service_rate_limit=rate_limit_val,
            api_service_admin_rate_limit=admin_rate_limit,
            api_service_max_tokens_per_user=max_tokens,
        )
        current_user = get_current_user()
        log_action(
            "admin_update_api_service_settings",
            request,
            user=current_user,
            enabled=bool(enabled),
            rate_limit=rate_limit_val,
            admin_rate_limit=admin_rate_limit,
            max_tokens_per_user=max_tokens,
        )
        flash(t("flash.api_service_settings_updated"), "success")
        return redirect(url_for("admin_api_service"))

    @app.route("/admin/api-service/tokens/<int:token_id>/revoke", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(30, 60)
    def admin_api_service_revoke_token(token_id):
        """Revoke a token from the admin panel."""
        token = db.get_token_by_id(token_id)
        if not token:
            flash(t("flash.api_token_not_found"), "error")
        else:
            current_user = get_current_user()
            target = db.get_user_by_id(token["user_id"])
            if target and target["role"] == "owner" and target["id"] != current_user["id"]:
                flash(t("flash.api_cannot_modify_owner"), "error")
                return redirect(url_for("admin_api_service"))
            db.revoke_api_service_token(token_id)
            log_action("admin_revoke_api_token", request, user=current_user,
                        extra={"token_id": token_id, "target_user_id": token["user_id"]})
            flash(t("flash.api_token_revoked"), "success")
        return redirect(url_for("admin_api_service"))

    @app.route("/admin/api-service/tokens/revoke-all", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_api_service_revoke_all_tokens():
        """Revoke all tokens for a specific user."""
        user_id = request.form.get("user_id", "").strip()
        if not user_id:
            flash(t("flash.api_user_id_required"), "error")
        else:
            user = db.get_user_by_id(user_id)
            current_user = get_current_user()
            if user and user["role"] == "owner" and user["id"] != current_user["id"]:
                flash(t("flash.api_cannot_modify_owner"), "error")
                return redirect(url_for("admin_api_service"))
            db.revoke_all_user_tokens(user_id)
            username = user["username"] if user else user_id
            log_action("admin_revoke_all_api_tokens", request, user=current_user,
                        extra={"target_user_id": user_id})
            flash(t("flash.api_all_tokens_revoked_for_user", username=username), "success")
        return redirect(url_for("admin_api_service"))

    @app.route("/admin/api-service/users/<user_id>/toggle-access", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(30, 60)
    def admin_api_service_toggle_access(user_id):
        """Toggle API access for a user."""
        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.api_user_not_found"), "error")
            return redirect(url_for("admin_api_service"))
        if target["role"] == "owner":
            flash(t("flash.api_cannot_modify_owner"), "error")
            return redirect(url_for("admin_api_service"))
        current_user = get_current_user()
        current = bool(target.get("api_access_enabled", 0))
        new_val = 0 if current else 1
        db.update_user(user_id, api_access_enabled=new_val)
        log_action(
            "admin_toggle_api_access",
            request,
            user=current_user,
            target_user=target["username"],
            target_user_id=user_id,
            enabled=bool(new_val),
        )
        flash(t("flash.api_access_enabled_for_user" if new_val else "flash.api_access_disabled_for_user", username=target['username']), "success")
        return redirect(url_for("admin_api_service"))

    @app.route("/admin/api-service/clear-audit", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_api_service_clear_audit():
        """Clear old audit log entries."""
        current_user = get_current_user()
        if not current_user["is_superuser"]:
            flash(t("flash.only_superusers_can_clear_api_audit_log"), "error")
            return redirect(url_for("admin_api_service"))
        days = request.form.get("before_days", 90, type=int)
        days = max(1, min(days, 3650))
        db.clear_audit_log(before_days=days)
        log_action("admin_clear_api_audit_log", request, user=current_user, before_days=days)
        flash(t("flash.api_audit_cleared", days=days), "success")
        return redirect(url_for("admin_api_service"))

    @app.route("/settings/api-tokens")
    @login_required
    def settings_api_tokens():
        """User-facing API token management page."""
        user = get_current_user()
        api_settings = db.get_api_service_settings()
        tokens = db.list_user_tokens(user["id"])
        max_tokens = api_settings["max_tokens_per_user"]
        current_count = db.count_user_tokens(user["id"])
        new_userbot_token = session.pop("new_userbot_token", None)
        return render_template(
            "api_service/tokens.html",
            tokens=tokens,
            max_tokens=max_tokens,
            current_count=current_count,
            api_settings=api_settings,
            user_api_access=db.user_has_api_access(user),
            userbot_token=db.get_userbot_token_info(user["id"]),
            new_userbot_token=new_userbot_token,
        )

    @app.route("/account/api", methods=["GET", "POST"])
    @login_required
    def account_api_redirect():
        """Legacy API path: redirect to the unified API token settings."""
        return redirect(url_for("settings_api_tokens"), code=301)

    @app.route("/settings/api", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def user_settings_api():
        """Legacy API token route: redirect to the unified API token settings."""
        return redirect(url_for("settings_api_tokens"), code=301)

    @app.route("/settings/api-tokens/create", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def settings_api_tokens_create():
        """Create a new API token."""
        user = get_current_user()
        api_settings = db.get_api_service_settings()
        if not api_settings["enabled"]:
            flash(t("flash.api_service_disabled"), "error")
            return redirect(url_for("settings_api_tokens"))
        if not db.user_has_api_access(user):
            flash(t("flash.api_permission_denied_create_tokens"), "error")
            return redirect(url_for("settings_api_tokens"))
        max_tokens = api_settings["max_tokens_per_user"]
        current_count = db.count_user_tokens(user["id"])
        if current_count >= max_tokens:
            flash(t("flash.api_max_tokens_reached", max=max_tokens), "error")
            return redirect(url_for("settings_api_tokens"))
        name = (request.form.get("name") or "").strip()[:64]
        read_only = bool(request.form.get("read_only"))
        # The form only offers the admin, settings and users scopes to
        # administrators; a hand-made request from anyone else loses them.
        scopes = [s for s in request.form.getlist("scopes") if s in _VALID_SCOPES]
        scopes = sorted(set(_scopes_for_role(user, scopes))) or ["pages"]
        # Every token can read. "Read only" takes away writing, it does not
        # swap reading for writing.
        permissions = {"read": True, "write": not read_only, "scopes": scopes}
        expires_at = None
        raw_expiry = (request.form.get("expires_at") or "").strip()
        if raw_expiry:
            # The datetime-local field has no offset. Read it in the site
            # time zone, like the other expiry fields, and store it in UTC
            # with the offset so token checks compare like with like.
            try:
                expires_at = _normalize_future_settings_datetime(raw_expiry) + "+00:00"
            # OverflowError: a time near year 9999 can fall past it in UTC.
            except (ValueError, OverflowError) as exc:
                if str(exc) == "past":
                    flash(t("flash.expiry_date_must_be_in_the_future"), "error")
                else:
                    flash(t("flash.invalid_expiration_date_format"), "error")
                return redirect(url_for("settings_api_tokens"))
        raw_token = db.create_api_token(
            user_id=user["id"],
            name=name,
            permissions=permissions,
            expires_at=expires_at,
        )
        log_action("user_create_api_token", request, user=user,
                    extra={"token_name": name, "scopes": scopes})
        flash(
            t("flash.api_token_created", name=name or 'Unnamed', token=f"<code>{raw_token}</code>"),
            "success",
        )
        return redirect(url_for("settings_api_tokens"))

    @app.route("/settings/api-tokens/userbot", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def settings_api_tokens_userbot():
        """Enable or disable Userbot automation from the unified API settings page."""
        user = get_current_user()
        api_settings = db.get_api_service_settings()
        enable = request.form.get("enable") == "1"
        if enable and not api_settings["enabled"]:
            flash(t("flash.api_service_disabled"), "error")
            return redirect(url_for("settings_api_tokens", _anchor="userbot-automation"))
        if enable and not db.user_has_api_access(user):
            flash(t("flash.api_permission_denied_create_tokens"), "error")
            return redirect(url_for("settings_api_tokens", _anchor="userbot-automation"))

        lock_mode = user["userbot_mode_lock"] or "unlocked"
        if enable and lock_mode == "force_disabled":
            flash(t("flash.userbot_automation_is_locked_by_an_admin_and"), "error")
            return redirect(url_for("settings_api_tokens", _anchor="userbot-automation"))
        if not enable and lock_mode == "force_enabled":
            flash(t("flash.userbot_automation_is_locked_by_an_admin_and_efdaff"), "error")
            return redirect(url_for("settings_api_tokens", _anchor="userbot-automation"))

        if enable:
            raw_token = db.create_userbot_token(user["id"])
            db.update_user(
                user["id"],
                userbot_enabled=1,
                userbot_enable_count=int(user["userbot_enable_count"] or 0) + 1,
            )
            session["new_userbot_token"] = raw_token
            log_action("enable_userbot_mode", request, user=user)
            flash(t("flash.userbot_automation_mode_has_been_successfully_enabled_copy"), "success")
        else:
            db.revoke_userbot_tokens(user["id"])
            db.update_user(
                user["id"],
                userbot_enabled=0,
                userbot_disable_count=int(user["userbot_disable_count"] or 0) + 1,
            )
            log_action("disable_userbot_mode", request, user=user)
            flash(t("flash.userbot_automation_mode_has_been_successfully_disabled"), "success")
        return redirect(url_for("settings_api_tokens", _anchor="userbot-automation"))

    @app.route("/settings/api-tokens/<int:token_id>/revoke", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def settings_api_tokens_revoke(token_id):
        """Revoke a token."""
        user = get_current_user()
        token = db.get_token_by_id(token_id)
        if not token or token["user_id"] != user["id"]:
            flash(t("flash.api_token_not_found"), "error")
        else:
            db.revoke_api_service_token(token_id)
            log_action("user_revoke_api_token", request, user=user,
                        extra={"token_id": token_id, "token_name": token["name"]})
            flash(t("flash.api_token_revoked"), "success")
        return redirect(url_for("settings_api_tokens"))

    @app.route("/api-docs")
    def api_documentation():
        """API documentation page with endpoints and examples."""
        return render_template("api_service/docs.html")

    csrf = app.extensions.get("csrf")
    if csrf is not None:
        for view in app.view_functions.values():
            if getattr(view, "_banana_bearer_authenticated", False):
                csrf.exempt(view)
