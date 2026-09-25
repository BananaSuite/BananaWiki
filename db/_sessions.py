"""Persistent per-login user sessions."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from ._connection import get_db_context, retry_on_busy


SESSION_DAYS = 7
REMEMBER_SESSION_DAYS = 30
MAX_IP_ADDRESS_LENGTH = 45
MAX_USER_AGENT_LENGTH = 512
MAX_AUTH_METHOD_LENGTH = 64
_MAX_RAW_TOKEN_LENGTH = 256

_SESSION_COLUMNS = """
    id, user_id, created_at, last_seen_at, expires_at, remember_me,
    auth_method, ip_address, last_ip, user_agent, revoked_at
"""


def _bounded_metadata(value, max_length):
    """Return metadata as bounded text suitable for persistent storage."""
    if value is None:
        return ""
    return str(value)[:max_length]


def _hash_token(raw_token):
    """Return the SHA-256 hex digest for a plausibly sized raw token."""
    if not isinstance(raw_token, str) or not raw_token:
        return None
    if len(raw_token) > _MAX_RAW_TOKEN_LENGTH:
        return None
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_user_session(
    user_id,
    ip_address="",
    user_agent="",
    remember_me=False,
    auth_method="password",
    revoke_existing=False,
):
    """Create a persistent login session and return ``(row_id, raw_token)``."""
    row_id = secrets.token_urlsafe(32)
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(
        days=REMEMBER_SESSION_DAYS if remember_me else SESSION_DAYS
    )
    now_iso = now.isoformat()
    ip_address = _bounded_metadata(ip_address, MAX_IP_ADDRESS_LENGTH)
    user_agent = _bounded_metadata(user_agent, MAX_USER_AGENT_LENGTH)
    auth_method = (
        _bounded_metadata(auth_method, MAX_AUTH_METHOD_LENGTH) or "password"
    )

    with get_db_context() as conn:
        retention_cutoff = (now - timedelta(days=30)).isoformat()
        conn.execute(
            "DELETE FROM user_sessions WHERE expires_at<? "
            "OR (revoked_at IS NOT NULL AND revoked_at<?)",
            (retention_cutoff, retention_cutoff),
        )
        if revoke_existing:
            conn.execute(
                "UPDATE user_sessions SET revoked_at=? "
                "WHERE user_id=? AND revoked_at IS NULL",
                (now_iso, user_id),
            )
        conn.execute(
            """
            INSERT INTO user_sessions (
                id, token_hash, user_id, created_at, last_seen_at, expires_at,
                remember_me, auth_method, ip_address, last_ip, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                token_hash,
                user_id,
                now_iso,
                now_iso,
                expires_at.isoformat(),
                int(bool(remember_me)),
                auth_method,
                ip_address,
                ip_address,
                user_agent,
            ),
        )
        conn.commit()
    return row_id, raw_token


@retry_on_busy
def get_user_session(raw_token):
    """Return an active session row for *raw_token*, or ``None``."""
    token_hash = _hash_token(raw_token)
    if token_hash is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        return conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM user_sessions
            WHERE token_hash=? AND revoked_at IS NULL AND expires_at > ?
            """,  # noqa: S608 - column list is a module constant
            (token_hash, now),
        ).fetchone()


@retry_on_busy
def get_user_session_id(raw_token):
    """Return the active session row ID for *raw_token*, or ``None``."""
    token_hash = _hash_token(raw_token)
    if token_hash is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM user_sessions "
            "WHERE token_hash=? AND revoked_at IS NULL AND expires_at > ?",
            (token_hash, now),
        ).fetchone()
    return row["id"] if row else None


@retry_on_busy
def list_active_user_sessions(user_id):
    """Return all active sessions belonging to *user_id*, newest first."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        return conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM user_sessions
            WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?
            ORDER BY last_seen_at DESC, created_at DESC
            """,  # noqa: S608 - column list is a module constant
            (user_id, now),
        ).fetchall()


@retry_on_busy
def list_user_session_history(user_id, limit=50):
    """Return recent revoked or expired sessions for *user_id*."""
    limit = max(1, min(int(limit), 100))
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        return conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM user_sessions
            WHERE user_id=? AND (revoked_at IS NOT NULL OR expires_at <= ?)
            ORDER BY datetime(COALESCE(revoked_at, expires_at)) DESC, created_at DESC
            LIMIT ?
            """,  # noqa: S608 - column list is a module constant
            (user_id, now, limit),
        ).fetchall()


def clear_user_session_history(user_id):
    """Delete only inactive session records belonging to *user_id*."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM user_sessions WHERE user_id=? "
            "AND (revoked_at IS NOT NULL OR expires_at <= ?)",
            (user_id, now),
        )
        conn.commit()
        return cur.rowcount


def touch_user_session(raw_token, ip_address="", min_interval_seconds=300):
    """Refresh an active session's last-seen metadata when the interval elapsed."""
    token_hash = _hash_token(raw_token)
    if token_hash is None:
        return False
    interval = max(0, int(min_interval_seconds))
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff = (now - timedelta(seconds=interval)).isoformat()
    ip_address = _bounded_metadata(ip_address, MAX_IP_ADDRESS_LENGTH)
    with get_db_context() as conn:
        cur = conn.execute(
            """
            UPDATE user_sessions
            SET last_seen_at=?,
                last_ip=CASE WHEN ? != '' THEN ? ELSE last_ip END
            WHERE token_hash=?
              AND revoked_at IS NULL
              AND expires_at > ?
              AND last_seen_at <= ?
            """,
            (now_iso, ip_address, ip_address, token_hash, now_iso, cutoff),
        )
        conn.commit()
        return cur.rowcount > 0


def revoke_user_session(user_id, row_id):
    """Revoke one session only when it belongs to *user_id*."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE user_sessions SET revoked_at=? "
            "WHERE user_id=? AND id=? AND revoked_at IS NULL",
            (now, user_id, row_id),
        )
        conn.commit()
        return cur.rowcount > 0


def revoke_user_session_token(raw_token):
    """Revoke the session identified by *raw_token*."""
    token_hash = _hash_token(raw_token)
    if token_hash is None:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE user_sessions SET revoked_at=? "
            "WHERE token_hash=? AND revoked_at IS NULL",
            (now, token_hash),
        )
        conn.commit()
        return cur.rowcount > 0


def revoke_other_user_sessions(user_id, current_row_id):
    """Revoke every session for *user_id* except *current_row_id*."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE user_sessions SET revoked_at=? "
            "WHERE user_id=? AND id != ? AND revoked_at IS NULL",
            (now, user_id, current_row_id),
        )
        conn.commit()
        return cur.rowcount


def revoke_all_user_sessions(user_id):
    """Revoke every unrevoked session belonging to *user_id*."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE user_sessions SET revoked_at=? "
            "WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )
        conn.commit()
        return cur.rowcount
