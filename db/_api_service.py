"""API Service plugin: database functions for tokens, audit logging, and settings."""

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from hashlib import sha256

import config
from ._connection import get_db_context, retry_on_busy


# Bounds for the API service settings. The admin form clamps to the same
# range; the reader applies it again so a bad stored value cannot turn
# every API call into a 500.
API_RATE_LIMIT_BOUNDS = (1, 10000)
API_MAX_TOKENS_BOUNDS = (1, 100)


def _derive_hmac_key():
    """Derive an HMAC key from SECRET_KEY for token hashing."""
    return hashlib.sha256(
        b"BW-API-SERVICE-HMAC:" + config.SECRET_KEY.encode("utf-8")
    ).digest()


def _hash_token(token):
    """Return the HMAC-SHA256 hex digest of *token* for storage."""
    return hmac.new(
        key=_derive_hmac_key(),
        msg=token.encode("utf-8"),
        digestmod=sha256,
    ).hexdigest()


def _default_permissions():
    """Return default permissions JSON for a new API token."""
    return json.dumps({"read": True, "write": False, "scopes": ["pages"]})


def create_api_token(user_id, name="", permissions=None, expires_at=None):
    """Generate a new API token for *user_id*. Returns the raw token string."""
    raw_token = secrets.token_urlsafe(32)
    hashed = _hash_token(raw_token)
    created_at = datetime.now(timezone.utc).isoformat()
    perms_json = json.dumps(permissions) if permissions else _default_permissions()

    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO api_service__tokens "
            "(user_id, name, token_hash, permissions, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, hashed, perms_json, expires_at, created_at),
        )
        conn.commit()

    return raw_token


@retry_on_busy
def verify_api_service_token(token):
    """Verify *token* and return ``(token_row, user_row)`` or ``None``.

    Checks that the token exists, is active, has not expired, and the owning
    user is not suspended.  The user row is needed for permission checks,
    and carries the forced-step flags so the caller can hold a token back
    while its account owes a password change or onboarding.
    """
    hashed = _hash_token(token)

    with get_db_context() as conn:
        row = conn.execute(
            """
            SELECT t.*, u.id as _user_id, u.username, u.role, u.suspended,
                   u.api_access_enabled, u.is_superuser,
                   u.force_password_change, u.onboarding_required
            FROM api_service__tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
            """,
            (hashed,),
        ).fetchone()

    if not row:
        return None, None

    if not row["active"]:
        return None, None

    if row["expires_at"]:
        try:
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            # Older versions of the token form stored the datetime-local
            # value without an offset. Comparing that with an aware time
            # raised TypeError, so those tokens were refused outright.
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                return None, None
        except (ValueError, TypeError, AttributeError):
            return None, None

    if row["suspended"]:
        return None, None

    token_row = dict(row)
    user_row = dict(row)

    token_row["id"] = row["id"]
    user_row["id"] = row["_user_id"]
    return token_row, user_row


def update_token_last_used(token_id):
    """Update the last_used_at timestamp for *token_id*."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE api_service__tokens SET last_used_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), token_id),
        )
        conn.commit()


@retry_on_busy
def list_user_tokens(user_id):
    """Return all active tokens for a user (without the hash)."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT id, user_id, name, permissions, last_used_at, expires_at, "
            "active, created_at "
            "FROM api_service__tokens WHERE user_id=? AND active=1 "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@retry_on_busy
def count_user_tokens(user_id):
    """Count active tokens for a user."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM api_service__tokens "
            "WHERE user_id=? AND active=1",
            (user_id,),
        ).fetchone()
    return row["cnt"] if row else 0


@retry_on_busy
def get_token_by_id(token_id):
    """Get a single token row by id."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM api_service__tokens WHERE id=?",
            (token_id,),
        ).fetchone()
    return dict(row) if row else None


def revoke_api_service_token(token_id):
    """Revoke (deactivate) a token by id. Returns True if found."""
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE api_service__tokens SET active=0 WHERE id=? AND active=1",
            (token_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def revoke_user_api_service_tokens(user_id, reason=""):
    """Revoke every active API token of *user_id* and return how many there were.

    This covers personal tokens, tokens issued by other tokens and the
    userbot token alike. Call it whenever the account's password is changed
    or reset: a token taken while the old password was known must stop
    working with it. *reason* only goes to the server log, so it should be
    a fixed description of the action rather than user input.
    """
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE api_service__tokens SET active=0 WHERE user_id=? AND active=1",
            (user_id,),
        )
        conn.commit()
        revoked = cur.rowcount
    if revoked:
        logging.getLogger("bananawiki").info(
            "Revoked %d API token(s) of user %s (%s)",
            revoked, user_id, reason or "no reason given",
        )
    return revoked


def revoke_all_user_tokens(user_id):
    """Revoke all tokens for a user. Kept for the admin page's revoke-all button."""
    return revoke_user_api_service_tokens(user_id, reason="revoked by an administrator")


def delete_tokens_for_user(user_id):
    """Permanently delete all tokens for a user (called on account deletion)."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM api_service__tokens WHERE user_id=?", (user_id,))
        conn.commit()


@retry_on_busy
def list_all_tokens():
    """Return all active tokens across all users (admin view, without hashes)."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT t.id, t.user_id, u.username, t.name, t.permissions, "
            "t.last_used_at, t.expires_at, t.active, t.created_at "
            "FROM api_service__tokens t "
            "JOIN users u ON u.id = t.user_id "
            "WHERE t.active=1 "
            "ORDER BY t.created_at DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def log_api_call(token_id, user_id, username, endpoint, method,
                 status_code, ip_address="", request_body="",
                 duration_ms=0):
    """Record an API call in the audit log."""
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO api_service__audit_log "
            "(token_id, user_id, username, endpoint, method, status_code, "
            "ip_address, request_body, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id, user_id, username, endpoint, method, status_code,
                ip_address, request_body, duration_ms,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


@retry_on_busy
def get_audit_log(limit=100, offset=0, user_id=None):
    """Return audit log entries, most recent first."""
    with get_db_context() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM api_service__audit_log "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM api_service__audit_log "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


@retry_on_busy
def count_audit_log_entries(user_id=None):
    """Count audit log entries."""
    with get_db_context() as conn:
        if user_id:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM api_service__audit_log WHERE user_id=?",
                (user_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM api_service__audit_log"
            ).fetchone()
    return row["cnt"] if row else 0


def clear_audit_log(before_days=90):
    """Delete audit log entries older than *before_days*."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM api_service__audit_log "
            "WHERE created_at < datetime('now', ?)",
            (f"-{before_days} days",),
        )
        conn.commit()


def _bounded_setting(value, default, bounds):
    """Read a stored whole number, falling back to *default* when it is not one."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    low, high = bounds
    return max(low, min(high, number))


@retry_on_busy
def get_api_service_settings(settings=None):
    """Return API service settings from site_settings, with defaults.

    Pass *settings* when the caller already holds the site settings row, to
    save a second read. Values that are missing, not numbers or out of range
    fall back to the default or the nearest bound.
    """
    if settings is None:
        from ._settings import get_site_settings
        settings = get_site_settings() or {}
    return {
        "enabled": bool(settings.get("api_service_enabled", 0)),
        "rate_limit": _bounded_setting(
            settings.get("api_service_rate_limit"), 60, API_RATE_LIMIT_BOUNDS),
        "admin_rate_limit": _bounded_setting(
            settings.get("api_service_admin_rate_limit"), 120, API_RATE_LIMIT_BOUNDS),
        "max_tokens_per_user": _bounded_setting(
            settings.get("api_service_max_tokens_per_user"), 5, API_MAX_TOKENS_BOUNDS),
    }


def update_api_service_settings(**kwargs):
    """Update API service settings in site_settings."""
    from ._settings import update_site_settings
    api_keys = {
        "api_service_enabled", "api_service_rate_limit",
        "api_service_admin_rate_limit", "api_service_max_tokens_per_user",
    }
    safe = {k: v for k, v in kwargs.items() if k in api_keys}
    if safe:
        update_site_settings(**safe)


def user_has_api_access(user):
    """Check if a user has API access.

    Admins always have access.  Regular users only have access if the
    `api_access_enabled` flag is set on their account.
    """
    if not user:
        return False
    if user.get("role") in ("admin", "owner"):
        return True
    return bool(user.get("api_access_enabled", 0))


def token_has_permission(token_row, scope, write_required=False):
    """Check token limits independently of the owning account's role."""
    try:
        perms = json.loads(token_row.get("permissions", "{}"))
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(perms, dict):
        return False
    if perms.get("write" if write_required else "read") is not True:
        return False
    allowed_scopes = perms.get("scopes", [])
    return (isinstance(allowed_scopes, list)
            and all(isinstance(value, str) for value in allowed_scopes)
            and scope in allowed_scopes)


# Userbots are ordinary API service tokens carrying the ``userbot`` scope;
# these functions superseded the standalone db/_userbots.py module.


def create_userbot_token(user_id):
    """Create a userbot API token for *user_id*. Returns the raw token string.

    Creates an API service token with ``userbot`` scope.  Replaces any
    existing userbot token for the user.
    """
    # Revoke any existing userbot tokens first
    revoke_userbot_tokens(user_id)
    raw_token = secrets.token_urlsafe(32)
    hashed = _hash_token(raw_token)
    created_at = datetime.now(timezone.utc).isoformat()
    perms = json.dumps({"read": True, "write": True, "scopes": ["userbot"]})

    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO api_service__tokens "
            "(user_id, name, token_hash, permissions, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, "userbot", hashed, perms, created_at),
        )
        conn.commit()

    return raw_token


@retry_on_busy
def get_userbot_token_info(user_id):
    """Return metadata for the user's active userbot token, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id, user_id, name, created_at, last_used_at "
            "FROM api_service__tokens "
            "WHERE user_id=? AND name='userbot' AND active=1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def revoke_userbot_tokens(user_id):
    """Revoke all userbot tokens for *user_id*."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE api_service__tokens SET active=0 "
            "WHERE user_id=? AND name='userbot' AND active=1",
            (user_id,),
        )
        conn.commit()
