"""Userbot API token helpers."""

import datetime as _dt
import hashlib
import hmac
import secrets
from hashlib import sha256

import config
from ._connection import get_db_context, retry_on_busy


def _derive_hmac_key():
    """Derive a per-instance HMAC key from the Flask secret key."""
    return hashlib.sha256(
        b"BW-USERBOT-TOKEN-HMAC:" + config.SECRET_KEY.encode("utf-8")
    ).digest()


def _hash_userbot_token(token):
    """Return an HMAC-SHA256 digest (hex) of *token*."""
    return hmac.new(
        key=_derive_hmac_key(),
        msg=token.encode("utf-8"),
        digestmod=sha256,
    ).hexdigest()


def generate_userbot_api_token(user_id):
    """Generate and persist a new userbot API token for *user_id*."""
    raw_token = secrets.token_urlsafe(32)
    hashed = _hash_userbot_token(raw_token)
    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO userbot_api_tokens (user_id, token_hash, created_at, last_used_at) VALUES (?, ?, ?, NULL)",
            (user_id, hashed, created_at),
        )
        conn.commit()
    return raw_token


@retry_on_busy
def get_userbot_api_token_info(user_id):
    """Return token metadata for *user_id* or None if no token exists."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT id, user_id, created_at, last_used_at FROM userbot_api_tokens WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def revoke_userbot_api_token(user_id):
    """Delete the userbot API token for *user_id*."""
    with get_db_context() as conn:
        cur = conn.execute("DELETE FROM userbot_api_tokens WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0


@retry_on_busy
def verify_userbot_api_token(token):
    """Verify a userbot API token and return the owning user row if valid."""
    hashed = _hash_userbot_token(token)
    with get_db_context() as conn:
        row = conn.execute(
            """
            SELECT u.*
            FROM userbot_api_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
            """,
            (hashed,),
        ).fetchone()
    if not row:
        return None
    if row["suspended"]:
        return None
    if not row["userbot_enabled"]:
        return None
    return row


def mark_userbot_api_token_used(user_id):
    """Update ``last_used_at`` for the user's active userbot token."""
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "UPDATE userbot_api_tokens SET last_used_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
