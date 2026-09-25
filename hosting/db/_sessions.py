"""Persistent session storage for hosting platform accounts."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from ._api_tokens import revoke_account_api_tokens
from ._connection import get_hosting_db_context


SESSION_DAYS = 7
MAX_IP_ADDRESS_LENGTH = 45
MAX_USER_AGENT_LENGTH = 512
MAX_AUTH_METHOD_LENGTH = 64
_MAX_RAW_TOKEN_LENGTH = 256

_SESSION_COLUMNS = """
    id, account_id, session_version, created_at, last_seen_at, expires_at,
    auth_method, ip_address, last_ip, user_agent, revoked_at
"""


def _utc_now():
    return datetime.now(timezone.utc)


def _bounded_metadata(value, max_length):
    if value is None:
        return ""
    return str(value)[:max_length]


def _token_hash(raw_token):
    if not isinstance(raw_token, str) or not raw_token:
        return None
    if len(raw_token) > _MAX_RAW_TOKEN_LENGTH:
        return None
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_hosting_session(
    account_id,
    session_version,
    ip_address="",
    user_agent="",
    auth_method="password",
):
    """Create a seven-day session and return ``(row_id, raw_token)``."""
    row_id = secrets.token_urlsafe(32)
    raw_token = secrets.token_urlsafe(32)
    digest = _token_hash(raw_token)
    now = _utc_now()
    now_iso = now.isoformat()
    ip_address = _bounded_metadata(ip_address, MAX_IP_ADDRESS_LENGTH)
    user_agent = _bounded_metadata(user_agent, MAX_USER_AGENT_LENGTH)
    auth_method = (
        _bounded_metadata(auth_method, MAX_AUTH_METHOD_LENGTH) or "password"
    )

    with get_hosting_db_context() as conn:
        retention_cutoff = (now - timedelta(days=30)).isoformat()
        conn.execute(
            "DELETE FROM hosting_account_sessions WHERE expires_at<? "
            "OR (revoked_at IS NOT NULL AND revoked_at<?)",
            (retention_cutoff, retention_cutoff),
        )
        conn.execute(
            "INSERT INTO hosting_account_sessions "
            "(id, token_hash, account_id, session_version, created_at, "
            "last_seen_at, expires_at, auth_method, ip_address, last_ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                digest,
                account_id,
                int(session_version),
                now_iso,
                now_iso,
                (now + timedelta(days=SESSION_DAYS)).isoformat(),
                auth_method,
                ip_address,
                ip_address,
                user_agent,
            ),
        )
        conn.commit()

    return row_id, raw_token


def get_hosting_session(raw_token):
    """Return the active session for ``raw_token``, or ``None``."""
    digest = _token_hash(raw_token)
    if digest is None:
        return None

    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        return conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM hosting_account_sessions "
            "WHERE token_hash=? AND revoked_at IS NULL AND expires_at > ?",
            (digest, now),
        ).fetchone()


def get_hosting_session_id(raw_token):
    """Return the active session row ID for ``raw_token``, or ``None``."""
    digest = _token_hash(raw_token)
    if digest is None:
        return None

    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM hosting_account_sessions "
            "WHERE token_hash=? AND revoked_at IS NULL AND expires_at > ?",
            (digest, now),
        ).fetchone()
        return row["id"] if row is not None else None


def list_active_hosting_sessions(account_id):
    """Return active sessions for an account, most recently seen first."""
    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        return conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM hosting_account_sessions "
            "WHERE account_id=? AND revoked_at IS NULL AND expires_at > ? "
            "ORDER BY last_seen_at DESC, created_at DESC",
            (account_id, now),
        ).fetchall()


def list_hosting_session_history(account_id, limit=50):
    """Return recent revoked or expired sessions for an account."""
    limit = max(1, min(int(limit), 100))
    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        return conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM hosting_account_sessions "
            "WHERE account_id=? AND (revoked_at IS NOT NULL OR expires_at <= ?) "
            "ORDER BY datetime(COALESCE(revoked_at, expires_at)) DESC, "
            "created_at DESC LIMIT ?",
            (account_id, now, limit),
        ).fetchall()


def clear_hosting_session_history(account_id):
    """Delete only inactive session records belonging to an account."""
    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "DELETE FROM hosting_account_sessions WHERE account_id=? "
            "AND (revoked_at IS NOT NULL OR expires_at <= ?)",
            (account_id, now),
        )
        conn.commit()
        return result.rowcount


def touch_hosting_session(
    raw_token,
    ip_address="",
    min_interval_seconds=300,
):
    """Update an active session if its last touch is older than the interval."""
    digest = _token_hash(raw_token)
    if digest is None:
        return False

    now = _utc_now()
    cutoff = now - timedelta(seconds=max(0, int(min_interval_seconds)))
    ip_address = _bounded_metadata(ip_address, MAX_IP_ADDRESS_LENGTH)
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE hosting_account_sessions SET last_seen_at=?, "
            "last_ip=CASE WHEN ? != '' THEN ? ELSE last_ip END "
            "WHERE token_hash=? AND revoked_at IS NULL AND expires_at > ? "
            "AND last_seen_at <= ?",
            (
                now.isoformat(),
                ip_address,
                ip_address,
                digest,
                now.isoformat(),
                cutoff.isoformat(),
            ),
        )
        conn.commit()
        return result.rowcount == 1


def revoke_hosting_session(account_id, row_id):
    """Revoke an account-owned session by its opaque row ID."""
    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE hosting_account_sessions SET revoked_at=? "
            "WHERE account_id=? AND id=? AND revoked_at IS NULL",
            (now, account_id, row_id),
        )
        conn.commit()
        return result.rowcount == 1


def revoke_hosting_session_token(raw_token):
    """Revoke the session identified by ``raw_token``."""
    digest = _token_hash(raw_token)
    if digest is None:
        return False

    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE hosting_account_sessions SET revoked_at=? "
            "WHERE token_hash=? AND revoked_at IS NULL",
            (_utc_now().isoformat(), digest),
        )
        conn.commit()
        return result.rowcount == 1


def revoke_other_hosting_sessions(account_id, current_row_id):
    """Revoke all other unrevoked sessions owned by an account."""
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE hosting_account_sessions SET revoked_at=? "
            "WHERE account_id=? AND id<>? AND revoked_at IS NULL",
            (_utc_now().isoformat(), account_id, current_row_id),
        )
        conn.commit()
        return result.rowcount


def revoke_all_hosting_sessions(account_id):
    """Revoke every unrevoked session owned by an account."""
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE hosting_account_sessions SET revoked_at=? "
            "WHERE account_id=? AND revoked_at IS NULL",
            (_utc_now().isoformat(), account_id),
        )
        conn.commit()
        return result.rowcount


def update_hosting_session_version(raw_token, new_version):
    """Update the version stored on an active session."""
    digest = _token_hash(raw_token)
    if digest is None:
        return False

    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE hosting_account_sessions SET session_version=? "
            "WHERE token_hash=? AND revoked_at IS NULL AND expires_at > ?",
            (int(new_version), digest, now),
        )
        conn.commit()
        return result.rowcount == 1


def change_hosting_password_preserving_session(account_id, password_hash, raw_token):
    """Atomically rotate credentials while preserving only the current session."""
    digest = _token_hash(raw_token)
    now = _utc_now().isoformat()
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT session_version FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        new_version = int(row["session_version"] or 0) + 1
        conn.execute(
            "UPDATE accounts SET password=?, session_version=? WHERE id=?",
            (password_hash, new_version, account_id),
        )
        conn.execute(
            "UPDATE hosting_account_sessions SET revoked_at=? "
            "WHERE account_id=? AND revoked_at IS NULL "
            "AND (? IS NULL OR token_hash<>?)",
            (now, account_id, digest, digest),
        )
        if digest is not None:
            conn.execute(
                "UPDATE hosting_account_sessions SET session_version=? "
                "WHERE account_id=? AND token_hash=? AND revoked_at IS NULL "
                "AND expires_at>?",
                (new_version, account_id, digest, now),
            )
        revoke_account_api_tokens(conn, account_id, account_id, "Password changed")
        conn.commit()
        return new_version
