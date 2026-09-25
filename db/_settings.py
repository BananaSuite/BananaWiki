"""Site settings."""

import logging
import time

from ._connection import get_db_context, retry_on_busy
from helpers._crypto import encrypt_value, decrypt_value, ENCRYPTED_SETTINGS_COLUMNS


@retry_on_busy
def get_site_settings():
    """Return the single site_settings row (id=1), or None if not initialised.

    Sensitive columns listed in :data:`ENCRYPTED_SETTINGS_COLUMNS` are
    transparently decrypted so callers always receive plaintext values.
    The return value is a plain :class:`dict` (rather than a
    :class:`sqlite3.Row`) to allow the decrypted values to be injected.

    Wrapped in :func:`retry_on_busy` because this is THE hot path read in
    every request (``before_request_hook`` + ``inject_globals``); a
    transient ``database is locked`` here used to turn every page render
    into a 500 until the next page reload.
    """
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM site_settings WHERE id=1").fetchone()
    if row is None:
        return None
    result = dict(row)
    for col in ENCRYPTED_SETTINGS_COLUMNS:
        if col in result and result[col]:
            result[col] = decrypt_value(result[col])
    return result


_ALLOWED_SETTINGS_COLUMNS = {
    "site_name", "interface_language", "interface_language_fallback", "interface_languages_json",
    "primary_color", "secondary_color", "accent_color",
    "text_color", "sidebar_color", "bg_color",
    "light_primary_color", "light_secondary_color", "light_accent_color",
    "light_text_color", "light_sidebar_color", "light_bg_color",
    "default_theme_mode", "setup_done", "timezone",
    "favicon_enabled", "favicon_type", "favicon_custom", "favicon_order",
    "maintenance_mode", "maintenance_message",
    "session_limit_enabled", "suspended_account_deletion_enabled",
    "page_protection_enabled", "page_reservations_enabled",
    "page_reservation_duration_hours", "page_reservation_cooldown_hours",
    "default_reserved_pages_quota", "reservation_quota_auto_approve_max",
    "last_chat_cleanup_at",
    # Per-wiki server-restart cooldown timestamp
    "last_server_restart_at",
    # Legacy chat settings (backwards compatibility)
    "chat_attachments_per_day_limit", "chat_auto_clear_messages",
    "chat_auto_clear_attachments", "chat_message_retention_days",
    "chat_attachment_retention_days",
    # Global chat settings
    "chat_max_message_length", "chat_attachments_enabled",
    "chat_max_attachment_size_mb",
    # DM-specific settings
    "chat_dm_enabled", "chat_allow_dm_creation",
    "chat_dm_auto_clear_messages", "chat_dm_auto_clear_attachments",
    "chat_dm_message_retention_days", "chat_dm_attachment_retention_days",
    # Group-specific settings
    "chat_group_enabled", "chat_allow_group_creation",
    "chat_group_auto_clear_messages", "chat_group_auto_clear_attachments",
    "chat_group_message_retention_days", "chat_group_attachment_retention_days",
    # Chat cleanup schedule settings
    "chat_cleanup_enabled", "chat_cleanup_frequency_days", "chat_cleanup_hour",
    "chat_cleanup_split_configured",
    # Hidden API toggle
    "banana_mode",
    # Kanban board settings
    "kanban_access", "kanban_write_access", "kanban_public_access_enabled",
    # Canvas settings
    "canvas_access", "canvas_write_access", "canvas_public_access_enabled", "canvas_open_access",
    "kanban_open_access",     "list_order_version",
    # Login app selector (post-login app picker)
    "login_app_selector",
    # Assessments settings
    "assessment_points_badge_enabled",
    # Beta tester advantage settings
    # Custom Pages plugin
    "custom_pages_max_video_size_mb",
    # Automatic mass-logout scheduler
    "auto_logout_enabled", "auto_logout_hour",
    # Wiki documentation settings
    "docs_bypass_deletion_slowdown", "docs_category_id",
    # Upload flexibility settings
    "upload_mode", "upload_whitelist", "upload_blacklist", "upload_max_size_mb",
    "platform_upload_blacklist",
    # Profile group badges
    "profile_group_badges_enabled", "profile_contribution_chart_enabled",
    # PDF export
    "pdf_export_enabled",
    # Markdown export
    "markdown_export_enabled",
    # Public mode (unauthenticated read access)
    "public_mode", "public_mode_until", "public_mode_message",
    "public_mode_show_message",
    # Core visual page builder (disabled by default)
    "page_builder_enabled", "page_builder_access",
    # Open signup (no invite code required)
    "open_signup", "open_signup_until",
    # Contribution approval system (disabled by default)
    "contribution_approval_enabled",
    # Draft expiration (0 = disabled, hours before drafts auto-expire)
    "draft_expiration_hours",
    # Default contribution quota (how many pending contributions a user can have)
    "default_contribution_quota", "contribution_quota_auto_approve_max",
    # Bot protection (honeypot + timing checks on auth forms, enabled by default)
    "bot_protection_enabled",
    # Optional first-login visual introduction for newly-created non-admin users
    "new_user_intro_enabled", "onboarding_replay_disabled",
    "intro_role_switching_enabled", "intro_role_switching_roles",
    # TTS "Listen to this page" panel: opt-in, off by default
    "tts_page_panel_enabled",
    # TTS public playback: when public mode is active, anonymous visitors can
    # see the player and stream/download cached audio.
    "tts_public_access_enabled",
    # TTS automatic generation on page create/update: opt-in, off by default.
    # When on, the in-page panel becomes a passive player and language is
    # auto-detected via langdetect (English / Italian fallback heuristic).
    "tts_auto_generate_enabled",
    # TTS enabled languages: comma-separated list of TTS language codes restricting
    # which languages admins want the plugin to expose.  Default ``"en,it"``:
    # empty string is normalised to the default at read time.
    "tts_enabled_languages",
    # Remote GPU TTS (optional, off by default)
    "tts_gpu_enabled", "tts_gpu_url", "tts_gpu_auth_token", "tts_gpu_timeout",
    # TTS performance mode: "balanced" (default) or "fast" (lower quality, faster on low-end)
    "tts_performance_mode",
    # API Service plugin
    "api_service_enabled",
    "api_service_rate_limit",
    "api_service_admin_rate_limit",
    "api_service_max_tokens_per_user",
    # Contributor leaderboard (disabled by default)
    "contributor_leaderboard_enabled",
    # Sidebar apps ordering (comma-separated app IDs)
    "sidebar_apps_order",
    # Account approval system (admin must approve new signups)
    "approval_required",
    "approval_denied_timeout_hours",
    # Auto-deletion timeout for pending activation accounts (0 = disabled)
    "approval_pending_timeout_hours",
    # Quota request cooldown (hours users must wait after cancel/deny/approve before resubmitting, 0 = disabled)
    "quota_request_cooldown_hours",
    # Devtools (only togglable by hosting platform admin)
    "devtools_enabled",
}


def update_site_settings(**kwargs):
    """Update one or more site settings columns by keyword argument.

    Only columns listed in ``_ALLOWED_SETTINGS_COLUMNS`` may be changed;
    any unknown column name raises :exc:`ValueError`.

    Sensitive columns listed in :data:`ENCRYPTED_SETTINGS_COLUMNS` are
    automatically encrypted before being written to the database.
    """
    for k in kwargs:
        if k not in _ALLOWED_SETTINGS_COLUMNS:
            raise ValueError(f"Invalid column: {k}")
    # Encrypt sensitive fields before persisting.
    for col in ENCRYPTED_SETTINGS_COLUMNS:
        if col in kwargs and kwargs[col]:
            kwargs[col] = encrypt_value(kwargs[col])
    with get_db_context() as conn:
        # Build SET clause iterating over the whitelist so only known-safe
        # column names end up in the SQL string.
        set_parts = []
        vals = []
        for col in _ALLOWED_SETTINGS_COLUMNS:
            if col in kwargs:
                set_parts.append(f"{col}=?")
                vals.append(kwargs[col])
        if set_parts:
            conn.execute(
                f"UPDATE site_settings SET {', '.join(set_parts)} WHERE id=1",  # noqa: S608
                vals,
            )
            conn.commit()
    _invalidate_request_settings_cache()


def _invalidate_request_settings_cache():
    """Drop ``g._site_settings`` if a Flask request context is active.

    Site settings are cached per-request on Flask's ``g`` object as a perf
    optimisation (see ``app._get_request_site_settings`` and the auth
    helpers).  When a write occurs inside a request handler we must invalidate
    that cache so the same handler observes its own write on the next read.

    The import is local and guarded so this module remains usable from
    contexts where Flask is not installed or no request is active (CLI
    scripts, background workers, fresh imports during tests).
    """
    try:
        from flask import g, has_request_context  # local import, soft dependency
    except Exception:
        return
    try:
        if has_request_context() and hasattr(g, "_site_settings"):
            del g._site_settings
    except Exception:
        pass


# The last-backup timestamps below live in the database rather than in
# process memory so every worker sees the same value.
@retry_on_busy
def get_last_backup_sent_at() -> float:
    """Return the Unix epoch timestamp of the last backup send.

    Returns ``0.0`` when no backup has been sent yet or if the DB is
    unreachable (e.g. during early startup).
    """
    try:
        with get_db_context() as conn:
            row = conn.execute(
                "SELECT last_backup_sent_at FROM site_settings WHERE id=1"
            ).fetchone()
            return float(row["last_backup_sent_at"]) if row else 0.0
    except Exception:
        return 0.0


def check_and_set_backup_sent(min_interval: float) -> bool:
    """Atomically check whether *min_interval* seconds have elapsed since the
    last backup send and, if so, record the current time.

    Uses ``BEGIN IMMEDIATE`` so only one Gunicorn worker can win the race.

    Returns ``True`` when this caller may proceed with the backup, or
    ``False`` when another worker already recorded a send within the
    interval window.
    """
    with get_db_context() as conn:
        try:
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_backup_sent_at FROM site_settings WHERE id=1"
            ).fetchone()
            last = float(row["last_backup_sent_at"]) if row else 0.0
            if now - last < min_interval:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE site_settings SET last_backup_sent_at=? WHERE id=1",
                (now,),
            )
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            logging.getLogger("bananawiki").warning("Backup scheduling could not claim its database slot", exc_info=True)
            return False


@retry_on_busy
def get_last_server_restart_at():
    """Return the UTC ISO timestamp of the last admin-triggered server
    restart for this wiki, or ``None`` if no restart has been recorded.

    The value is stored on the local ``site_settings`` row, so each wiki
    (main wiki + every hosted instance) has its own independent value:
    triggering a restart on ``wiki.example.com`` does not affect the
    cooldown of ``foo-hosting.example.com``.
    """
    try:
        with get_db_context() as conn:
            row = conn.execute(
                "SELECT last_server_restart_at FROM site_settings WHERE id=1"
            ).fetchone()
            if row is None:
                return None
            return row["last_server_restart_at"]
    except Exception:
        return None


def check_and_claim_server_restart(min_interval_seconds: float) -> bool:
    """Atomically claim the right to perform an admin-triggered server
    restart for this wiki.

    Uses ``BEGIN IMMEDIATE`` so only one Gunicorn worker wins the race
    when an admin double-clicks the restart button.  Returns ``True``
    when this caller may proceed with the restart, ``False`` when
    another worker already claimed it within *min_interval_seconds*.

    The cooldown is enforced **per wiki**: it lives on the local
    ``site_settings`` row, so different hosted instances do not share
    a cooldown window with the main wiki or with each other.
    """
    from datetime import datetime, timezone as _tz
    with get_db_context() as conn:
        try:
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_server_restart_at FROM site_settings WHERE id=1"
            ).fetchone()
            last_str = (row["last_server_restart_at"] if row else None) or ""
            if last_str:
                try:
                    last_dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
                    if now - last_dt.timestamp() < min_interval_seconds:
                        conn.rollback()
                        return False
                except (ValueError, TypeError):
                    pass
            # Claim the restart slot immediately to block other workers.
            conn.execute(
                "UPDATE site_settings SET last_server_restart_at=? WHERE id=1",
                (datetime.now(_tz.utc).isoformat(),),
            )
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            # Fail closed: refuse the restart on unexpected DB errors so
            # we never bypass the cooldown silently.
            return False


def check_and_claim_chat_cleanup(min_interval_seconds: float) -> bool:
    """Atomically claim the right to run a scheduled chat cleanup cycle.

    Uses ``BEGIN IMMEDIATE`` so only one Gunicorn worker proceeds with the
    actual cleanup work. Returns ``True`` when this caller may proceed,
    ``False`` when another worker has already claimed cleanup within
    *min_interval_seconds*.
    """
    from datetime import datetime, timezone as _tz
    with get_db_context() as conn:
        try:
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_chat_cleanup_at FROM site_settings WHERE id=1"
            ).fetchone()
            last_str = (row["last_chat_cleanup_at"] if row else None) or ""
            if last_str:
                try:
                    last_dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
                    if now - last_dt.timestamp() < min_interval_seconds:
                        conn.rollback()
                        return False
                except (ValueError, TypeError):
                    pass
            # Claim the cleanup slot immediately to block other workers
            conn.execute(
                "UPDATE site_settings SET last_chat_cleanup_at=? WHERE id=1",
                (datetime.now(_tz.utc).isoformat(),),
            )
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            logging.getLogger("bananawiki").warning("Chat cleanup could not claim its database slot", exc_info=True)
            return False
