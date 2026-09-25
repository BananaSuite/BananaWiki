"""API tokens for admin-only programmatic access."""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from hashlib import sha256

import config
from ._connection import get_db_context, retry_on_busy


def _derive_hmac_key():
    """Derive a per-instance HMAC key from the Flask secret key.

    The derived key is unique to each installation, so a stolen database
    cannot be used to verify candidate tokens offline on a different
    instance.
    """
    return hashlib.sha256(
        b"BW-API-TOKEN-HMAC:" + config.SECRET_KEY.encode("utf-8")
    ).digest()


def _hash_token(token):
    """Return an HMAC-SHA256 digest (hex) of *token*.

    Using HMAC with a per-instance key derived from the Flask secret key
    prevents offline brute-force attempts even if the database is
    compromised.
    """
    return hmac.new(
        key=_derive_hmac_key(),
        msg=token.encode("utf-8"),
        digestmod=sha256,
    ).hexdigest()


def generate_api_token(user_id):
    """Generate a new API token for *user_id*.

    Replaces any existing token for the user. Returns the raw token string
    (which is not stored, only its hash is persisted).
    """
    raw_token = secrets.token_urlsafe(32)
    hashed = _hash_token(raw_token)
    created_at = datetime.now(timezone.utc).isoformat()

    with get_db_context() as conn:
        # Remove any existing token for this user
        conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
        # Insert new token
        conn.execute(
            "INSERT INTO api_tokens (user_id, token_hash, created_at) VALUES (?, ?, ?)",
            (user_id, hashed, created_at),
        )
        conn.commit()

    return raw_token


@retry_on_busy
def verify_api_token(token):
    """Verify *token* and return the owning user row if valid, else None.

    A token is valid if:
      - It exists in api_tokens.
      - The owning user has an admin role (admin or owner).
      - The owning user is not suspended.

    Suspended or demoted users have their tokens rejected (401) but not deleted,
    so tokens can resume working when the user is restored.
    """
    hashed = _hash_token(token)

    with get_db_context() as conn:
        row = conn.execute(
            """
            SELECT u.*
            FROM api_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
            """,
            (hashed,),
        ).fetchone()

    if not row:
        return None

    # Check admin role and not suspended
    if row["role"] not in ("admin", "owner"):
        return None
    if row["suspended"]:
        return None

    return row


@retry_on_busy
def get_user_api_token_info(user_id):
    """Return the api_tokens row for *user_id*, or None if none exists.

    Does not return the token itself (only the hash is stored).
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id, user_id, created_at FROM api_tokens WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row


def revoke_api_token(user_id):
    """Delete the API token for *user_id*. Returns True if a token was deleted."""
    with get_db_context() as conn:
        cur = conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0


def delete_api_tokens_for_user(user_id):
    """Permanently delete the API token for *user_id*.

    Called when a user account is deleted. Only one token per user is allowed.
    """
    revoke_api_token(user_id)
