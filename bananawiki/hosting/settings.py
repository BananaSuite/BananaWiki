"""The single ``hosting_settings`` row (id 1) and typed accessors.

The shared GPU text-to-speech token is stored encrypted (``fernet:`` prefix,
key derived from ``HOSTING_SECRET_KEY`` by :mod:`bananawiki.core.crypto`);
a plaintext value written by 1.4 still reads correctly and is encrypted in
place at start-up.
"""

from __future__ import annotations

import json
from typing import Any

from flask import current_app, g, has_request_context

from ..core import crypto
from ..core.timeutil import now_sql
from .db import db

SIGNUP_MODES = ("open", "invite", "approval", "closed")
# Whether an address is asked for: at sign-up (signup_email) and of accounts without one (existing_email).
EMAIL_POLICIES = ("required", "optional", "off")
# The portal's easter eggs: key -> settings column (all on unless an administrator turns one off).
EASTER_EGGS = {"shades": "easter_shades", "konami": "easter_konami", "peek": "easter_peek"}
APPROVAL_NOTIFY_MODES = ("immediate", "digest", "daily")
NOTIFY_INTERVAL_MINUTES = (5, 4320)
TTS_MODES = ("all", "whitelist", "blacklist")
DEFAULT_GRACE_PERIOD_DAYS = 30
# Who may use custom domains besides administrators: nobody else, the accounts an administrator allows, everyone.
CUSTOM_DOMAIN_POLICIES = ("admins", "selected", "everyone")

WRITABLE = frozenset({
    "custom_domains_policy",
    "global_limit_enabled", "global_limit_max_instances", "global_limit_max_storage_mb",
    "global_wiki_upload_max_size_mb", "global_wiki_blocked_extensions", "bot_protection_enabled",
    "grace_period_days", "signup_mode", "ask_email_new_signup", "ask_email_existing_users", "email_required",
    "email_verification_required", "email_verification_cooldown_seconds", "forbid_non_admin_public_wikis",
    "forbid_non_admin_page_builder", "auto_approve_public_access_requests", "auto_approve_page_builder_requests",
    "global_tour_enabled", "global_tts_gpu_enabled", "global_tts_gpu_url", "global_tts_gpu_auth_token",
    "global_tts_gpu_timeout", "global_tts_enabled", "global_tts_mode", "global_tts_list",
    "hosting_activation_required", "hosting_activation_denied_timeout_seconds", "signup_use_case_required",
    "approval_notify_email", "approval_notify_mode", "approval_notify_digest_hours",
    "approval_notify_last_digest_at", "approval_notify_admins", "approval_notify_interval_minutes",
    "approval_notify_daily_hour", "platform_oauth_enabled", "allow_owner_delete_expired",
    "allow_owner_download_expired", "email_flag_block_reentry", "instance_url_suffix", "instance_suffix_disabled",
    "api_enabled", "gdrive_backup_enabled", "gdrive_credentials_path", "gdrive_folder_id",
    "gdrive_retention_days", "gdrive_backup_time", "admin_mfa_required", "owner_renewal_enabled",
    "signup_email", "existing_email", "email_shared", "email_paused", "email_paused_at", "email_paused_held",
    "email_notices", "attention_email_sources", *EASTER_EGGS.values(),
})


def load(*, fresh: bool = False) -> dict[str, Any]:
    """The settings row, cached unless a transactional decision needs current policy."""
    if has_request_context():
        cached = g.get("_hosting_settings")
        if cached is not None and not fresh:
            return cached
    row = db.one("SELECT * FROM hosting_settings WHERE id = 1") or {}
    if has_request_context():
        g._hosting_settings = row
    return row


def get(key: str, default: Any = None) -> Any:
    value = load().get(key)
    return default if value is None else value


def flag(key: str, default: bool = False) -> bool:
    return bool(get(key, 1 if default else 0))


def update(**values: Any) -> None:
    unknown = set(values) - WRITABLE
    if unknown:
        raise ValueError(f"Unknown hosting settings: {sorted(unknown)}")
    if "global_tts_gpu_auth_token" in values:
        values["global_tts_gpu_auth_token"] = crypto.encrypt(_secret_key(), values["global_tts_gpu_auth_token"]) or ""
    if values:
        db.update("hosting_settings", values, "id = 1")
    if has_request_context():
        g.pop("_hosting_settings", None)


def _secret_key() -> str:
    return current_app.config["HOSTING"].secret_key


# Typed accessors


def signup_mode() -> str:
    mode = get("signup_mode", "open")
    return mode if mode in SIGNUP_MODES else "open"


def approval_required() -> bool:
    return signup_mode() == "approval" or flag("hosting_activation_required")


def grace_period_days() -> int:
    try:
        return max(0, int(get("grace_period_days", DEFAULT_GRACE_PERIOD_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_GRACE_PERIOD_DAYS


def denied_timeout_seconds() -> int:
    """-1 never deletes denied accounts, 0 deletes them at once, >0 after that many seconds."""
    try:
        return int(get("hosting_activation_denied_timeout_seconds", 86400))
    except (TypeError, ValueError):
        return 86400


def approval_notify_mode() -> str:
    mode = str(get("approval_notify_mode", "digest")).lower()
    return mode if mode in APPROVAL_NOTIFY_MODES else "digest"


def approval_notify_interval_minutes() -> int:
    low, high = NOTIFY_INTERVAL_MINUTES
    try:
        return min(high, max(low, int(get("approval_notify_interval_minutes", 360))))
    except (TypeError, ValueError):
        return 360


def approval_notify_daily_hour() -> int:
    try:
        return min(23, max(0, int(get("approval_notify_daily_hour", 8))))
    except (TypeError, ValueError):
        return 8


def signup_email() -> str:
    """Whether the sign-up form asks for an address: ``required``, ``optional`` or ``off``."""
    value = get("signup_email", "optional")
    return value if value in EMAIL_POLICIES else "optional"


def existing_email() -> str:
    """What accounts without an address are asked: ``required`` (they must add one), ``optional`` or ``off``."""
    value = get("existing_email", "off")
    return value if value in EMAIL_POLICIES else "off"


def email_policy_columns(signup: str, existing: str) -> dict[str, int]:
    """The 1.4 columns that say the same as *signup* and *existing*, kept current for older releases."""
    return {"ask_email_new_signup": int(signup != "off"), "ask_email_existing_users": int(existing != "off"),
            "email_required": int("required" in (signup, existing))}


def email_shared() -> bool:
    """Whether several accounts may use the same address."""
    return flag("email_shared")


def email_paused() -> bool:
    """Whether an administrator paused every email (nothing is sent to anyone, nothing is kept for later)."""
    return flag("email_paused")


def set_email_paused(paused: bool) -> None:
    """Pause or resume every email; pausing starts the count of messages held back from zero."""
    if paused == email_paused():
        return
    if paused:
        update(email_paused=1, email_paused_at=now_sql(), email_paused_held=0)
    else:
        update(email_paused=0)


def count_held_back() -> None:
    """One more message was not sent because email is paused."""
    db.execute("UPDATE hosting_settings SET email_paused_held = email_paused_held + 1 WHERE id = 1")


def switches(key: str) -> dict[str, bool]:
    """A JSON column of ``name -> 0/1`` (``email_notices``, ``attention_email_sources``); unreadable: {}."""
    try:
        value = json.loads(get(key, "") or "{}")
    except ValueError:
        return {}
    return {str(name): bool(on) for name, on in value.items()} if isinstance(value, dict) else {}


def switched_on(key: str, name: str) -> bool:
    """Whether *name* is on in the JSON column *key*; anything not stored is on."""
    return switches(key).get(name, True)


def custom_domains_policy() -> str:
    """Who may use custom domains (:data:`CUSTOM_DOMAIN_POLICIES`); anything unknown means administrators only."""
    value = str(get("custom_domains_policy") or "admins")
    return value if value in CUSTOM_DOMAIN_POLICIES else "admins"


def easter_eggs() -> dict[str, bool]:
    """``{"shades", "konami", "peek"} -> on`` for the portal's own pages."""
    return {egg: flag(column, True) for egg, column in EASTER_EGGS.items()}


def verification_cooldown() -> int:
    try:
        return max(30, int(get("email_verification_cooldown_seconds", 60)))
    except (TypeError, ValueError):
        return 60


def tts_gpu_token() -> str:
    return crypto.decrypt(_secret_key(), get("global_tts_gpu_auth_token", ""))


def encrypt_legacy_secrets(secret_key: str) -> None:
    """Encrypt a plaintext GPU token left by 1.4 (runs once per start)."""
    stored = db.scalar("SELECT global_tts_gpu_auth_token FROM hosting_settings WHERE id = 1", default="")
    if stored and not crypto.is_encrypted(stored):
        db.execute(
            "UPDATE hosting_settings SET global_tts_gpu_auth_token = ? WHERE id = 1",
            (crypto.encrypt(secret_key, stored),),
        )
