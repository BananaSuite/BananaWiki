"""Which site settings the API may change, and how values are checked.

``PUT /api/v1/settings`` only writes keys listed in :data:`RULES`, with the
bounds and choices of the administrator's settings forms, the same feature
gates and the same hosting restrictions. Everything else is refused:
unknown keys (400), internal, platform-managed, retired and secret keys
(403). The request is all or nothing: when anything is refused or invalid
no setting changes.

A value equal to the stored one is accepted for any readable key and
changes nothing, so a client can send back what ``GET`` returned with its
own changes applied. Secret keys are never compared with the stored value,
so they cannot be guessed one request at a time.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app

from ....core.timeutil import now_sql, parse, to_sql
from ... import i18n, registry, settings
from ...templating import FAVICON_PRESETS, from_local_input
from . import tokens, webhooks

SECRET_KEYS = frozenset(settings.ENCRYPTED_COLUMNS | {
    "feedback_bot_token", "feedback_telegram_userids", "telegram_sync_token", "telegram_sync_userids",
})
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_TTS_CODE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2})?$")


class Invalid(ValueError):
    """A value a rule refuses; ``args[0]`` is a key under ``api_service.settings.invalid.``."""

    def __init__(self, reason: str, **values: Any):
        super().__init__(reason)
        self.reason = reason
        self.values = values


Parser = Callable[[Any], Any]


@dataclass(frozen=True)
class Rule:
    parse: Parser
    features: tuple[str, ...] = ()
    policy: Callable[[Any], str | None] | None = None  # returns a refusal reason key


def _flag(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if type(value) is int and value in (0, 1):
        return value
    if isinstance(value, str) and value.strip().lower() in ("0", "1", "true", "false"):
        return int(value.strip().lower() in ("1", "true"))
    raise Invalid("boolean")


def _whole(low: int, high: int) -> Parser:
    def parse_whole(value: Any) -> int:
        if isinstance(value, bool):
            raise Invalid("whole")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                raise Invalid("whole") from None
        if type(value) is not int:
            raise Invalid("whole")
        return max(low, min(high, value))

    return parse_whole


def _choice(*options: str) -> Parser:
    def parse_choice(value: Any) -> str:
        if isinstance(value, str) and value.strip().lower() in options:
            return value.strip().lower()
        raise Invalid("choice", options=", ".join(options))

    return parse_choice


def _text(maximum: int) -> Parser:
    def parse_text(value: Any) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise Invalid("string")
        value = value.strip()
        if len(value) > maximum:
            raise Invalid("too_long", maximum=maximum)
        return value

    return parse_text


def _site_name(value: Any) -> str:
    return _text(100)(value) or "BananaWiki"


def _color(value: Any) -> str:
    if isinstance(value, str) and _HEX.match(value):
        return value
    raise Invalid("color")


def _time_zone(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        try:
            ZoneInfo(value.strip())
        except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
            pass
        else:
            return value.strip()
    raise Invalid("timezone")


def _interface_language(value: Any) -> str:
    if isinstance(value, str) and value.strip() in i18n.enabled_languages():
        return value.strip()
    raise Invalid("language")


def _future_datetime(value: Any) -> str | None:
    """An expiry; an offset is honoured, a naive time is read in the site time zone."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise Invalid("datetime")
    text = value.strip()
    try:
        naive = datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo is None
        moment = from_local_input(text) if naive else parse(text, bounded=True)
        stored = to_sql(moment) if moment else None
    except (ValueError, OverflowError):
        stored = None
    if stored is None:
        raise Invalid("datetime")
    if stored <= now_sql():
        raise Invalid("past")
    return stored


def _intro_roles(value: Any) -> str:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or not all(isinstance(role, str) for role in value):
        raise Invalid("roles")
    if set(value) - {"user", "editor", "admin"}:
        raise Invalid("roles")
    return ",".join([role for role in ("user", "editor", "admin") if role in value] or ["user", "editor", "admin"])


def _tts_languages(value: Any) -> str:
    if isinstance(value, str):
        value = [code.strip() for code in value.split(",") if code.strip()]
    if (not isinstance(value, list) or not value or len(value) > 100
            or not all(isinstance(code, str) and _TTS_CODE.match(code) for code in value)):
        raise Invalid("languages")
    return ",".join(dict.fromkeys(value))


def _forbidden_by(flag: str, reason: str) -> Callable[[Any], str | None]:
    def policy(value: Any) -> str | None:
        return reason if value and getattr(current_app.config["BW"], flag) else None

    return policy


def _platform_owned(_value: Any) -> str | None:
    return "platform" if current_app.config["BW"].managed_hosting else None


FLAG = Rule(_flag)

RULES: dict[str, Rule] = {
    "site_name": Rule(_site_name),
    "interface_language": Rule(_interface_language),
    "interface_language_fallback": Rule(_choice(*i18n.BUILTIN_LANGUAGES)),
    "timezone": Rule(_time_zone),
    "default_theme_mode": Rule(_choice("dark", "light")),
    **{name: Rule(_color) for name in (
        "primary_color", "secondary_color", "accent_color", "text_color", "sidebar_color", "bg_color",
        "light_primary_color", "light_secondary_color", "light_accent_color", "light_text_color",
        "light_sidebar_color", "light_bg_color",
    )},
    "favicon_enabled": FLAG,
    "favicon_type": Rule(_choice(*FAVICON_PRESETS)),
    "maintenance_mode": FLAG,
    "maintenance_message": Rule(_text(1000)),
    "session_limit_enabled": FLAG,
    "suspended_account_deletion_enabled": FLAG,
    "auto_logout_enabled": FLAG,
    "auto_logout_hour": Rule(_whole(0, 23)),
    "pdf_export_enabled": FLAG,
    "markdown_export_enabled": FLAG,
    "contributor_leaderboard_enabled": FLAG,
    "sidebar_apps_order": Rule(_text(500)),
    "new_user_intro_enabled": FLAG,
    "onboarding_replay_disabled": FLAG,
    "intro_role_switching_enabled": FLAG,
    "intro_role_switching_roles": Rule(_intro_roles),
    "public_mode": Rule(_flag, policy=_forbidden_by("forbid_public_mode", "platform_forbids")),
    "public_mode_until": Rule(_future_datetime),
    "public_mode_message": Rule(_text(1000)),
    "public_mode_show_message": FLAG,
    "page_builder_enabled": Rule(_flag, policy=_forbidden_by("forbid_page_builder", "platform_forbids")),
    "page_builder_access": Rule(_choice("admin", "editor", "user")),
    "open_signup": FLAG,
    "open_signup_until": Rule(_future_datetime),
    "approval_required": FLAG,
    "approval_denied_timeout_hours": Rule(_whole(1, 8760)),
    "approval_pending_timeout_hours": Rule(_whole(0, 8760)),
    "bot_protection_enabled": FLAG,
    "draft_expiration_hours": Rule(_whole(0, 8760)),
    "login_app_selector": FLAG,
    "api_service_enabled": FLAG,
    "api_service_rate_limit": Rule(_whole(*tokens.RATE_LIMIT_BOUNDS)),
    "api_service_admin_rate_limit": Rule(_whole(*tokens.RATE_LIMIT_BOUNDS)),
    "api_service_max_tokens_per_user": Rule(_whole(*tokens.MAX_TOKENS_BOUNDS)),
    "api_service_webhook_max_failures": Rule(_whole(*webhooks.MAX_FAILURES_BOUNDS)),
    "upload_mode": Rule(_choice("whitelist", "blacklist", "allow_all"), ("attachments",)),
    "upload_whitelist": Rule(_text(2000), ("attachments",)),
    "upload_blacklist": Rule(_text(2000), ("attachments",)),
    "upload_max_size_mb": Rule(_whole(1, 2048), ("attachments",), policy=_platform_owned),
    "page_protection_enabled": Rule(_flag, ("page_governance",)),
    "page_reservations_enabled": Rule(_flag, ("page_governance",)),
    "page_reservation_duration_hours": Rule(_whole(1, 8760), ("page_governance",)),
    "page_reservation_cooldown_hours": Rule(_whole(0, 8760), ("page_governance",)),
    "default_reserved_pages_quota": Rule(_whole(1, 1000), ("page_governance",)),
    "reservation_quota_auto_approve_max": Rule(_whole(0, 1000000), ("page_governance",)),
    "contribution_approval_enabled": Rule(_flag, ("page_governance",)),
    "default_contribution_quota": Rule(_whole(1, 1000), ("page_governance",)),
    "contribution_quota_auto_approve_max": Rule(_whole(0, 1000000), ("page_governance",)),
    "quota_request_cooldown_hours": Rule(_whole(0, 8760), ("page_governance",)),
    "tts_page_panel_enabled": Rule(_flag, ("tts",)),
    "tts_public_access_enabled": Rule(_flag, ("tts",)),
    "tts_auto_generate_enabled": Rule(_flag, ("tts",), policy=_forbidden_by("managed_tts_disabled",
                                                                            "platform_forbids")),
    "tts_performance_mode": Rule(_choice("auto", "balanced", "fast"), ("tts",)),
    "tts_enabled_languages": Rule(_tts_languages, ("tts",)),
    "chat_max_message_length": Rule(_whole(100, 50000), ("chat",)),
    "chat_attachments_enabled": Rule(_flag, ("chat",)),
    "chat_max_attachment_size_mb": Rule(_whole(1, 100), ("chat",)),
    "chat_attachments_per_day_limit": Rule(_whole(1, 1000), ("chat",)),
    "chat_dm_enabled": Rule(_flag, ("chat",)),
    "chat_allow_dm_creation": Rule(_flag, ("chat",)),
    "chat_dm_message_retention_days": Rule(_whole(1, 3650), ("chat",)),
    "chat_dm_attachment_retention_days": Rule(_whole(1, 3650), ("chat",)),
    "chat_dm_auto_clear_messages": Rule(_flag, ("chat",)),
    "chat_dm_auto_clear_attachments": Rule(_flag, ("chat",)),
    "chat_group_enabled": Rule(_flag, ("chat",)),
    "chat_allow_group_creation": Rule(_flag, ("chat",)),
    "chat_group_message_retention_days": Rule(_whole(1, 3650), ("chat",)),
    "chat_group_attachment_retention_days": Rule(_whole(1, 3650), ("chat",)),
    "chat_group_auto_clear_messages": Rule(_flag, ("chat",)),
    "chat_group_auto_clear_attachments": Rule(_flag, ("chat",)),
    "chat_cleanup_enabled": Rule(_flag, ("chat",)),
    "chat_cleanup_frequency_days": Rule(_whole(1, 365), ("chat",)),
    "chat_cleanup_hour": Rule(_whole(0, 23), ("chat",)),
    "profile_contribution_chart_enabled": Rule(_flag, ("user_profiles",)),
    "profile_group_badges_enabled": Rule(_flag, ("user_profiles", "chat")),
    "kanban_access": Rule(_choice("admin", "editor", "all"), ("kanban",)),
    "kanban_write_access": Rule(_choice("admin", "editor", "all"), ("kanban",)),
    "kanban_public_access_enabled": Rule(_flag, ("kanban",)),
    "kanban_open_access": Rule(_flag, ("kanban",)),
    "canvas_access": Rule(_choice("admin", "editor", "all"), ("canvas",)),
    "canvas_write_access": Rule(_choice("admin", "editor", "all"), ("canvas",)),
    "canvas_public_access_enabled": Rule(_flag, ("canvas",)),
    "canvas_open_access": Rule(_flag, ("canvas",)),
    "assessment_points_badge_enabled": Rule(_flag, ("assessments",)),
    "custom_pages_max_video_size_mb": Rule(_whole(1, 2048), ("custom_pages",)),
    "docs_bypass_deletion_slowdown": Rule(_flag, ("deletion_slowdown",)),
}

# Why readable keys without a rule are refused (anything else: "not_writable").
READ_ONLY_REASONS = {
    "setup_done": "internal", "last_server_restart_at": "internal", "last_chat_cleanup_at": "internal",
    "list_order_version": "internal", "chat_cleanup_split_configured": "internal",
    "docs_category_id": "internal", "last_backup_sent_at": "internal",
    "platform_upload_blacklist": "platform",
    "tts_gpu_enabled": "remote_gpu", "tts_gpu_url": "remote_gpu", "tts_gpu_timeout": "remote_gpu",
    "favicon_custom": "own_page", "favicon_order": "own_page", "interface_languages_json": "own_page",
    "chat_auto_clear_messages": "legacy_chat", "chat_auto_clear_attachments": "legacy_chat",
    "chat_message_retention_days": "legacy_chat", "chat_attachment_retention_days": "legacy_chat",
}

_CHAT_SPLIT = frozenset(key for key in RULES if key.startswith(("chat_dm_", "chat_group_", "chat_cleanup_")))


def is_retired(key: str) -> bool:
    return key in settings.RETIRED_COLUMNS or key.startswith(settings.RETIRED_COLUMN_PREFIXES)


def readable(current: dict[str, Any]) -> dict[str, Any]:
    """The settings ``GET /api/v1/settings`` returns: no secrets, no retired columns."""
    return {key: value for key, value in current.items() if key not in SECRET_KEYS and not is_retired(key)}


def check(data: dict[str, Any], current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Sort a request into ``(updates, invalid, refused)``; the last two map key -> reason."""
    updates: dict[str, Any] = {}
    invalid: dict[str, str] = {}
    refused: dict[str, str] = {}
    visible = readable(current)
    for key, value in data.items():
        if key in SECRET_KEYS:
            refused[key] = _reason("secret")
            continue
        if is_retired(key):
            refused[key] = _reason("retired")
            continue
        if key in visible and value == visible[key]:
            continue
        rule = RULES.get(key)
        if rule is None or key not in current:
            if key in current:
                refused[key] = _reason(READ_ONLY_REASONS.get(key, "not_writable"))
            else:
                invalid[key] = i18n.t("api_service.settings.invalid.unknown")
            continue
        try:
            parsed = rule.parse(value)
        except Invalid as error:
            invalid[key] = i18n.t(f"api_service.settings.invalid.{error.reason}", **error.values)
            continue
        if parsed == current.get(key):
            continue
        missing = [feature for feature in rule.features if not registry.is_enabled(feature)]
        if missing:
            refused[key] = _reason("feature_off", feature=missing[0])
            continue
        veto = rule.policy(parsed) if rule.policy else None
        if veto:
            refused[key] = _reason(veto)
            continue
        updates[key] = parsed
    return updates, invalid, refused


def _reason(key: str, **values: Any) -> str:
    return i18n.t(f"api_service.settings.refused.{key}", **values)


def with_companions(updates: dict[str, Any]) -> dict[str, Any]:
    """Values the settings form changes together with these."""
    result = dict(updates)
    if "favicon_type" in result:
        result["favicon_custom"] = ""
    if result.get("public_mode") == 0:
        result["public_mode_until"] = None
    if result.get("open_signup") == 0:
        result["open_signup_until"] = None
    if _CHAT_SPLIT.intersection(result):
        result["chat_cleanup_split_configured"] = 1
    return result
