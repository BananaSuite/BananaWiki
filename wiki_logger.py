"""
BananaWiki: Logging module
Logs requests, actions and events in detail with configurable verbosity levels.

Logging Levels:
  - off:      No logging
  - minimal:  Only critical errors and warnings
  - medium:   Errors, warnings, and important actions (auth, admin)
  - verbose:  Medium + all user actions (default)
  - debug:    All of the above + HTTP requests and debug info
"""

import logging
import threading
from private_logs import PrivateFileHandler
import re

import config


_logger = None
_logger_lock = threading.RLock()
_log_level = None

# Pattern to strip characters that could forge log entries
_LOG_UNSAFE_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")

# Logging level constants
LOG_OFF = "off"
LOG_MINIMAL = "minimal"
LOG_MEDIUM = "medium"
LOG_VERBOSE = "verbose"
LOG_DEBUG = "debug"

# Action categories for level filtering
CRITICAL_ACTIONS = {
    "admin_delete_user", "admin_change_role", "admin_deattribute_all",
    "admin_bulk_delete", "admin_force_release_checkout", "setup_complete",
    "import_site", "admin_delete_profile", "admin_mass_logout",
    "admin_impersonate_start", "admin_impersonate_stop", "plugin_deleted",
    "admin_toggle_superuser", "export_site", "export_site_refused",
    "import_site_refused", "plugin_imported", "plugin_code_trusted",
    "plugin_password_rejected"
}
IMPORTANT_ACTIONS = {
    "login_success", "login_failed", "signup_success", "logout",
    "change_password", "change_username", "delete_account",
    "admin_edit_profile", "generate_invite_code", "delete_invite_code",
    "update_settings", "plugin_enabled", "plugin_disabled",
    "admin_create_user", "admin_suspend", "admin_unsuspend",
    "banana_mode_toggled", "admin_create_role", "admin_edit_role",
    "admin_reorder_user_tags",
    "admin_delete_role", "admin_assign_role", "admin_unassign_role",
    "admin_request_page_unprotect", "admin_force_unprotect_page",
    "admin_set_user_quota", "review_reservation_quota_request",
    "admin_set_own_quota", "submit_reservation_quota_request",
    "cancel_reservation_quota_request",
    "admin_update_api_service_settings", "admin_toggle_api_access",
    "admin_revoke_api_token", "admin_revoke_all_api_tokens",
    "admin_set_contribution_quota", "review_contribution_quota_request",
    "delete_page", "revert_page", "move_page",
    "protect_page", "unprotect_page",
    "create_category", "edit_category", "delete_category", "move_category",
    "set_page_expiry", "clear_page_expiry", "set_user_expiry",
    "clear_user_expiry", "set_role_expiry", "clear_role_expiry",
    "restore_pending_deletion"
}
USER_ACTIONS = {
    "edit_page", "create_page",
    "upload_image", "delete_image", "view_page", "profile_update",
    "reserve_page", "release_reservation", "send_message", "delete_message",
    "create_group", "join_group", "leave_group", "kick_member",
    "kanban_create_column", "kanban_update_column", "kanban_delete_column",
    "kanban_reorder_columns", "kanban_create_ticket", "kanban_update_ticket",
    "kanban_delete_ticket", "kanban_move_ticket", "kanban_reorder_tickets",
    "kanban_create_board", "kanban_edit_board", "kanban_delete_board",
    "kanban_transfer_ownership", "kanban_update_sharing", "kanban_update_settings",
    "kanban_upload_attachment", "kanban_delete_attachment"
}


def _get_log_level():
    """Get the configured logging level."""
    global _log_level
    if _log_level is not None:
        return _log_level

    # Get LOGGING_LEVEL config
    level = getattr(config, "LOGGING_LEVEL", "verbose").lower()
    if level not in {LOG_OFF, LOG_MINIMAL, LOG_MEDIUM, LOG_VERBOSE, LOG_DEBUG}:
        level = LOG_VERBOSE  # Default to verbose if invalid

    _log_level = level
    return _log_level


def get_logger():
    """Configure this process's logger once, including concurrent first requests."""
    with _logger_lock:
        return _configure_logger()


def _configure_logger():
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger("bananawiki")
    level = _get_log_level()

    # Set logger level based on configured level
    if level == LOG_OFF:
        _logger.setLevel(logging.CRITICAL + 1)  # Disable all logging
    elif level == LOG_MINIMAL:
        _logger.setLevel(logging.WARNING)
    else:
        _logger.setLevel(logging.DEBUG)

    if level != LOG_OFF:
        fh = PrivateFileHandler(config.LOG_FILE)
        fh.setLevel(logging.DEBUG)
        # Enhanced format with more context
        fmt = logging.Formatter(
            "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        fh.setFormatter(fmt)
        _logger.addHandler(fh)

    # Console handler for INFO and above
    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO if level != LOG_OFF else logging.CRITICAL + 1)
    sh.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    _logger.addHandler(sh)

    return _logger


def _sanitize(value):
    """Strip control characters (newlines, etc.) to prevent log injection."""
    return _LOG_UNSAFE_RE.sub("", str(value))


def log_request(request, user=None):
    """Log an incoming HTTP request (only at debug level)."""
    level = _get_log_level()
    if level != LOG_DEBUG:
        return

    logger = get_logger()
    ip = _sanitize(request.remote_addr or "unknown")
    method = _sanitize(request.method)
    path = _sanitize(request.path)
    ua = _sanitize(request.headers.get("User-Agent", ""))
    if isinstance(user, str):
        username = _sanitize(user)
    elif user:
        username = _sanitize(user["username"])
    else:
        username = "anonymous"

    # Enhanced request logging with more context, using %-format so log
    # aggregators can de-duplicate the message template.
    logger.debug(
        "HTTP %-6s | %-40s | user=%-20s | ip=%-15s | ua=%.60s",
        method, path, username, ip, ua,
    )


_SENSITIVE_FIELDS = {"password", "current_password", "new_password",
                     "confirm_password", "secret", "token", "session"}


def _should_log_action(action):
    """Determine if an action should be logged at the current level."""
    level = _get_log_level()

    if level == LOG_OFF:
        return False
    if level == LOG_DEBUG or level == LOG_VERBOSE:
        return True  # Log everything
    if level == LOG_MEDIUM:
        # Log critical and important actions only
        return action in CRITICAL_ACTIONS or action in IMPORTANT_ACTIONS
    if level == LOG_MINIMAL:
        # Log only critical actions
        return action in CRITICAL_ACTIONS

    return True  # Default to logging


def _get_impersonation_details(user=None):
    """Return log fields that tie impersonated actions to the real admin."""
    try:
        from flask import session
    except Exception:
        return {}

    try:
        impersonator_id = session.get("impersonator_id")
        effective_user_id = session.get("user_id")
    except RuntimeError:
        return {}

    if not impersonator_id or not effective_user_id:
        return {}

    user_id = None
    if user and not isinstance(user, str):
        try:
            user_id = user["id"]
        except (KeyError, TypeError, IndexError):
            user_id = None

    # Starting/stopping impersonation are admin actions performed by the real
    # admin, even though the session is being switched around them.
    if user_id and user_id == impersonator_id:
        return {}

    try:
        import db
        impersonator = db.get_user_by_id(impersonator_id)
        effective_user = db.get_user_by_id(effective_user_id)
    except Exception:
        impersonator = None
        effective_user = None

    details = {
        "impersonated_by_id": impersonator_id,
        "impersonated_user_id": effective_user_id,
    }
    if impersonator:
        details["impersonated_by"] = impersonator["username"]
    if effective_user:
        details["impersonated_user"] = effective_user["username"]
    return details


def log_action(action, request, user=None, **details):
    """Log a specific action with extra detail (sensitive fields are redacted).

    ``user`` may be a user row *or* a plain string holding the actor's
    username.  Only the username is written to the log. No other fields
    from the user row are retained.

    Actions are categorized and filtered based on the logging level:
      - minimal: Only critical admin actions
      - medium:  Critical + important actions (auth, admin operations)
      - verbose: All actions including user actions (default)
      - debug:   All actions with maximum detail
    """
    if not _should_log_action(action):
        return

    logger = get_logger()
    ip = _sanitize(request.remote_addr or "unknown")
    # Extract only the username: discard the rest of the user dict so that
    # sensitive fields (password hash, session tokens, …) are never logged.
    if isinstance(user, str):
        username = _sanitize(user)
    elif user:
        username = _sanitize(user["username"])
    else:
        username = "anonymous"
    safe_action = _sanitize(action)

    impersonation_details = _get_impersonation_details(user)
    combined_details = {**impersonation_details, **details}

    # Redact sensitive fields
    safe_details = {k: ("***" if k in _SENSITIVE_FIELDS else _sanitize(v))
                    for k, v in combined_details.items()}

    # Enhanced formatting with categorization
    category = "CRITICAL" if action in CRITICAL_ACTIONS else \
               "IMPORTANT" if action in IMPORTANT_ACTIONS else \
               "USER" if action in USER_ACTIONS else "OTHER"

    # Build detail string with better formatting
    detail_parts = []
    for k, v in safe_details.items():
        # Truncate very long values for readability
        str_v = str(v)
        if len(str_v) > 100:
            str_v = str_v[:97] + "..."
        detail_parts.append(f"{k}={str_v}")
    detail_str = " ".join(detail_parts)

    # Log with structured format, using %-format so log aggregators can
    # de-duplicate the message template.
    if detail_str:
        logger.info(
            "[%-8s] %-30s | user=%-20s | ip=%-15s | %s",
            category, safe_action, username, ip, detail_str,
        )
    else:
        logger.info(
            "[%-8s] %-30s | user=%-20s | ip=%-15s",
            category, safe_action, username, ip,
        )
