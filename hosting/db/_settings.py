"""Hosting platform settings storage.

Provides CRUD helpers for the ``hosting_settings`` single-row table that
stores configuration for the hosting platform.
"""

from ._connection import get_hosting_db_context

# Allowed settings columns (allowlist prevents SQL injection via column names)
_ALLOWED_HOSTING_SETTINGS_COLUMNS = {
    "global_limit_enabled",
    "global_limit_max_instances",
    "global_limit_max_storage_mb",
    "global_wiki_upload_max_size_mb",
    "global_wiki_blocked_extensions",
    "bot_protection_enabled",
    "grace_period_days",
    "signup_mode",
    "ask_email_new_signup", "ask_email_existing_users", "email_required",
    "email_verification_required", "email_verification_cooldown_seconds",
    "contact_email_policy_version",
    "forbid_non_admin_public_wikis",
    "forbid_non_admin_page_builder",
    "auto_approve_public_access_requests",
    "auto_approve_page_builder_requests",
    "global_tour_enabled",
    # Global TTS GPU settings (platform-wide)
    "global_tts_gpu_enabled", "global_tts_gpu_url", "global_tts_gpu_auth_token",
    "global_tts_gpu_timeout",
    # Global TTS generation policy
    "global_tts_enabled", "global_tts_mode", "global_tts_list",
    # Account activation approval
    "hosting_activation_required",
    "hosting_activation_denied_timeout_hours",
    "hosting_activation_denied_timeout_seconds",
    # Signup use-case requirement (approval mode)
    "signup_use_case_required",
    # Admin notifications for signups awaiting approval
    "approval_notify_email",
    "approval_notify_mode",
    "approval_notify_digest_hours",
    "approval_notify_last_digest_at",
    # Platform OAuth SSO
    "platform_oauth_enabled",
    # Devtools (hosting admin tools visibility)
    "devtools_enabled",
    # Expired wiki owner permissions
    "allow_owner_delete_expired",
    "allow_owner_download_expired",
    # Email flag: block re-entry of the same flagged address
    "email_flag_block_reentry",
    # Arabic mirror mode: global RTL layout toggle
    "arabic_mirror_enabled",
    # Instance URL suffix override (NULL/empty = default to "hosting")
    "instance_url_suffix",
    "instance_suffix_disabled",
    # REST API under /api/v1 and the personal access tokens it accepts
    "api_enabled",
    # Google Drive backup settings
    "gdrive_backup_enabled",
    "gdrive_credentials_path",
    "gdrive_folder_id",
    "gdrive_retention_days",
    "gdrive_backup_time",
    "last_gdrive_backup_at",
}

_ALLOWED_SIGNUP_MODES = {"open", "invite", "closed", "approval"}


def get_hosting_activation_required() -> bool:
    """Return ``True`` if new accounts require admin approval.

    Reads the ``hosting_activation_required`` column from the hosting
    settings row.  Defaults to ``False`` when the column is missing
    or the DB is unreachable.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return False
        try:
            value = row["hosting_activation_required"]
        except (IndexError, KeyError):
            return False
        return bool(value)
    except Exception:
        return False


def get_hosting_activation_denied_timeout_seconds() -> int:
    """Return the timeout in seconds after which denied accounts are deleted.

    0 = immediate deletion, -1 = never delete, >0 = deletion after N seconds.
    Defaults to 86400 (24 hours) when the column is missing or the DB is unreachable.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return 86400
        try:
            value = row["hosting_activation_denied_timeout_seconds"]
        except (IndexError, KeyError):
            return 86400
        return int(value)
    except Exception:
        return 86400


def get_hosting_activation_denied_deletion_enabled() -> bool:
    """Return True if denied account deletion is enabled.

    When the timeout is -1, deletion is disabled (accounts stay forever).
    When the timeout is 0, deletion is immediate.
    When the timeout is > 0, deletion happens after that many seconds.
    """
    timeout = get_hosting_activation_denied_timeout_seconds()
    return timeout != -1


def get_signup_mode() -> str:
    """Return the current signup gating mode.

    Returns ``'open'`` (free signup), ``'invite'`` (invite code required),
    or ``'closed'`` (no public signups at all).  Defaults to ``'open'``
    when the row is missing or the DB is unreachable so that fresh
    installs remain functional.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return "open"
        try:
            value = row["signup_mode"]
        except (IndexError, KeyError):
            return "open"
        if value not in _ALLOWED_SIGNUP_MODES:
            return "open"
        return value
    except Exception:
        return "open"


# Default grace period (days) used when the hosting_settings row is missing
# or the column is unset.  Mirrors the schema default.
DEFAULT_GRACE_PERIOD_DAYS = 30


def get_allow_owner_delete_expired() -> bool:
    """Return ``True`` if wiki owners are permitted to delete their own expired wikis.

    When ``False`` (the default), owners cannot self-terminate an expired
    wiki; only platform administrators can do so.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return False
        try:
            value = row["allow_owner_delete_expired"]
        except (IndexError, KeyError):
            return False
        return bool(value)
    except Exception:
        return False


def get_allow_owner_download_expired() -> bool:
    """Return ``True`` if wiki owners are permitted to download their expired wikis.

    When ``False`` (the default), only platform administrators can access the
    ZIP archive of a terminated wiki during the grace period.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return False
        try:
            value = row["allow_owner_download_expired"]
        except (IndexError, KeyError):
            return False
        return bool(value)
    except Exception:
        return False


def get_hosting_api_enabled() -> bool:
    """Return ``True`` if platform administrators have switched the REST API on.

    Off when the column is missing or the DB is unreachable, so a database
    that has not been migrated yet never exposes the API.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return False
        try:
            value = row["api_enabled"]
        except (IndexError, KeyError):
            return False
        return bool(value)
    except Exception:
        return False


def get_grace_period_days() -> int:
    """Return the configured grace-period window in days.

    Falls back to :data:`DEFAULT_GRACE_PERIOD_DAYS` when the row is missing,
    when the column is ``NULL``, or when the DB is unreachable.  A value of
    ``0`` is permitted and signals that data should be hard-deleted at
    termination time (no grace window).
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return DEFAULT_GRACE_PERIOD_DAYS
        try:
            value = row["grace_period_days"]
        except (IndexError, KeyError):
            return DEFAULT_GRACE_PERIOD_DAYS
        if value is None:
            return DEFAULT_GRACE_PERIOD_DAYS
        return max(0, int(value))
    except Exception:
        return DEFAULT_GRACE_PERIOD_DAYS


def get_hosting_settings():
    """Return the hosting platform settings row (id=1).

    Returns a ``sqlite3.Row`` or ``None`` if the row is missing.
    """
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM hosting_settings WHERE id = 1"
        ).fetchone()


_APPROVAL_NOTIFY_MODES = {"immediate", "digest"}

DEFAULT_APPROVAL_NOTIFY_DIGEST_HOURS = 6


def get_approval_notify_email() -> str:
    """Return the admin address for pending-approval notifications.

    Empty string means the feature is disabled.  Defaults to ``""``
    when the column is missing or the DB is unreachable.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return ""
        try:
            value = row["approval_notify_email"]
        except (IndexError, KeyError):
            return ""
        return (value or "").strip()
    except Exception:
        return ""


def get_approval_notify_mode() -> str:
    """Return ``'immediate'`` or ``'digest'`` for pending-approval notices.

    Defaults to ``'digest'`` when unset, unknown, or the DB is unreachable.
    Digest mode is the safe default: one email per interval no matter how
    many signups arrive, keeping provider quotas intact.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return "digest"
        try:
            value = (row["approval_notify_mode"] or "").strip().lower()
        except (IndexError, KeyError):
            return "digest"
        if value not in _APPROVAL_NOTIFY_MODES:
            return "digest"
        return value
    except Exception:
        return "digest"


def get_approval_notify_digest_hours() -> int:
    """Return the digest cadence in hours, clamped to 1-72.

    Defaults to 6 when unset or the DB is unreachable.
    """
    try:
        row = get_hosting_settings()
        if row is None:
            return DEFAULT_APPROVAL_NOTIFY_DIGEST_HOURS
        try:
            value = row["approval_notify_digest_hours"]
        except (IndexError, KeyError):
            return DEFAULT_APPROVAL_NOTIFY_DIGEST_HOURS
        if value is None:
            return DEFAULT_APPROVAL_NOTIFY_DIGEST_HOURS
        return min(72, max(1, int(value)))
    except Exception:
        return DEFAULT_APPROVAL_NOTIFY_DIGEST_HOURS


def get_approval_notify_last_digest_at():
    """Return the ISO timestamp of the last digest sent, or ``None``."""
    try:
        row = get_hosting_settings()
        if row is None:
            return None
        try:
            value = row["approval_notify_last_digest_at"]
        except (IndexError, KeyError):
            return None
        return value or None
    except Exception:
        return None


def update_hosting_settings(**kwargs):
    """Update one or more hosting settings columns by keyword argument.

    Only columns listed in ``_ALLOWED_HOSTING_SETTINGS_COLUMNS`` may be
    changed; any unknown column name raises :exc:`ValueError`.
    """
    if not kwargs:
        return
    invalid = set(kwargs) - _ALLOWED_HOSTING_SETTINGS_COLUMNS
    if invalid:
        raise ValueError(f"Unknown hosting settings columns: {invalid}")
    with get_hosting_db_context() as conn:
        set_parts = []
        vals = []
        for col in _ALLOWED_HOSTING_SETTINGS_COLUMNS:
            if col in kwargs:
                set_parts.append(f"{col}=?")
                vals.append(kwargs[col])
        if set_parts:
            conn.execute(
                f"UPDATE hosting_settings SET {', '.join(set_parts)} WHERE id=1",  # noqa: S608
                vals,
            )
            conn.commit()
