"""Personal access tokens for the hosting portal's REST API.

A token belongs to one hosting account and carries a fixed list of scopes.
Only the SHA-256 digest of the full token is stored, so the database cannot
give a token back: its owner sees it once, when it is created, and the rows
keep a short prefix so the Account page can tell tokens apart.
"""

import hashlib
import re
import secrets
from datetime import datetime, timezone

from ._connection import get_hosting_db_context
from ._events import record_event

API_TOKEN_PREFIX = "bwh_"
# secrets.token_urlsafe(30) always produces 40 URL-safe characters.
API_TOKEN_PATTERN = re.compile(r"bwh_[A-Za-z0-9_-]{40}")
API_TOKEN_NAME_MAX_LENGTH = 60
MAX_ACTIVE_API_TOKENS = 10

API_SCOPES = ("account:read", "instances:read", "instances:manage", "admin:read")
# Offered only to platform administrators when a token is created. The API
# checks the account's admin flag again on every request that uses one.
ADMIN_API_SCOPES = frozenset({"admin:read"})

_DISPLAY_PREFIX_LENGTH = 12
_LIST_COLUMNS = "id, account_id, name, prefix, scopes, created_at, expires_at, last_used_at"


def _now():
    return datetime.now(timezone.utc)


def _describe(token_id, prefix):
    """Identify a token in the event log without ever writing the token."""
    return f"Token {token_id} ({prefix})"


def hash_api_token(raw_token):
    """Return the hex SHA-256 digest stored in place of *raw_token*."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def api_scopes_for_account(account):
    """Return the scopes *account* may put on a new token, in display order."""
    is_admin = bool(account and account["is_admin"])
    return tuple(scope for scope in API_SCOPES if is_admin or scope not in ADMIN_API_SCOPES)


def parse_api_token_scopes(value):
    """Split a stored scope string, dropping anything this version does not know."""
    return [scope for scope in (value or "").split() if scope in API_SCOPES]


def is_api_token_expired(row, now=None):
    """Return ``True`` once *row*'s expiry has passed.

    An expiry that cannot be parsed counts as expired, so a damaged row
    locks its token out rather than letting it live forever.
    """
    expires_at = row["expires_at"]
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= (now or _now())


def _count_active(conn, account_id, now):
    rows = conn.execute(
        "SELECT expires_at FROM hosting_api_tokens WHERE account_id=? AND revoked_at IS NULL",
        (account_id,),
    ).fetchall()
    return sum(1 for row in rows if not is_api_token_expired(row, now))


def create_api_token(account_id, name, scopes, expires_at=None, expected_session_version=None):
    """Create a token for *account_id* and return ``(token_id, raw_token)``.

    The raw token exists only in the return value. Raises ``ValueError``
    with ``api_token_name`` or ``api_token_scopes`` for bad input, and with
    ``api_token_limit`` when the account already has
    :data:`MAX_ACTIVE_API_TOKENS` tokens that are neither revoked nor expired.

    The Account page passes the ``session_version`` it read when it checked
    the password. Every password change bumps it, so if the account changed
    in the meantime the token is refused with ``api_token_stale`` instead of
    outliving the revocation that came with the change.
    """
    name = (name or "").strip()
    if not 1 <= len(name) <= API_TOKEN_NAME_MAX_LENGTH or not name.isprintable():
        raise ValueError("api_token_name")
    requested = set(scopes or ())
    if not requested or not requested.issubset(API_SCOPES):
        raise ValueError("api_token_scopes")
    scope_text = " ".join(scope for scope in API_SCOPES if scope in requested)

    raw_token = API_TOKEN_PREFIX + secrets.token_urlsafe(30)
    prefix = raw_token[:_DISPLAY_PREFIX_LENGTH]
    now = _now()
    with get_hosting_db_context() as conn:
        # The count and the insert share one write lock, so two forms
        # submitted at once cannot both slip under the limit.
        conn.execute("BEGIN IMMEDIATE")
        if expected_session_version is not None:
            account = conn.execute(
                "SELECT session_version, deleted_at FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if (account is None or account["deleted_at"]
                    or int(account["session_version"] or 0) != int(expected_session_version)):
                conn.rollback()
                raise ValueError("api_token_stale")
        if _count_active(conn, account_id, now) >= MAX_ACTIVE_API_TOKENS:
            conn.rollback()
            raise ValueError("api_token_limit")
        cur = conn.execute(
            "INSERT INTO hosting_api_tokens "
            "(account_id, name, prefix, token_hash, scopes, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, name, prefix, hash_api_token(raw_token), scope_text,
             now.isoformat(), expires_at),
        )
        token_id = cur.lastrowid
        record_event(
            conn, "account", account_id, "api.token.created", account_id,
            f"{_describe(token_id, prefix)} with scopes {scope_text}",
        )
        conn.commit()
    return token_id, raw_token


def get_active_api_token(raw_token):
    """Return the row for *raw_token* if it exists, is not revoked and has not expired.

    Anything else, including a string that does not look like a token at
    all, returns ``None``.
    """
    if not raw_token or not API_TOKEN_PATTERN.fullmatch(raw_token):
        return None
    with get_hosting_db_context() as conn:
        row = conn.execute(
            f"SELECT {_LIST_COLUMNS}, revoked_at FROM hosting_api_tokens WHERE token_hash=?",
            (hash_api_token(raw_token),),
        ).fetchone()
    if row is None or row["revoked_at"] or is_api_token_expired(row):
        return None
    return row


def touch_api_token(token_id):
    """Record that the token was just used."""
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE hosting_api_tokens SET last_used_at=? WHERE id=?",
            (_now().isoformat(), token_id),
        )
        conn.commit()


def list_api_tokens(account_id):
    """Return the account's tokens that have not been revoked, newest first.

    Expired tokens stay in the list, marked ``expired``, until their owner
    revokes them, so an integration that stopped working has an explanation.
    """
    now = _now()
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM hosting_api_tokens "
            "WHERE account_id=? AND revoked_at IS NULL ORDER BY id DESC",
            (account_id,),
        ).fetchall()
    tokens = []
    for row in rows:
        item = dict(row)
        item["scopes"] = parse_api_token_scopes(row["scopes"])
        item["expired"] = is_api_token_expired(row, now)
        tokens.append(item)
    return tokens


def revoke_api_token(account_id, token_id, actor_id=None):
    """Revoke one of *account_id*'s tokens. Returns ``False`` if there was none to revoke.

    *actor_id* defaults to the owner; pass the administrator's id when one
    revokes the token while impersonating the account.
    """
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, prefix FROM hosting_api_tokens "
            "WHERE id=? AND account_id=? AND revoked_at IS NULL",
            (token_id, account_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE hosting_api_tokens SET revoked_at=? WHERE id=?",
            (_now().isoformat(), row["id"]),
        )
        record_event(
            conn, "account", account_id, "api.token.revoked", actor_id or account_id,
            _describe(row["id"], row["prefix"]),
        )
        conn.commit()
    return True


def revoke_account_api_tokens(conn, account_id, actor_id=None, reason=""):
    """Revoke every live token of *account_id* inside the caller's transaction.

    Used where the account's password changes, so the caller commits (or
    rolls back) the revocation together with the new password. Returns the
    number of tokens revoked.
    """
    rows = conn.execute(
        "SELECT id, prefix FROM hosting_api_tokens WHERE account_id=? AND revoked_at IS NULL",
        (account_id,),
    ).fetchall()
    if not rows:
        return 0
    conn.execute(
        "UPDATE hosting_api_tokens SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
        (_now().isoformat(), account_id),
    )
    listed = ", ".join(_describe(row["id"], row["prefix"]) for row in rows)
    record_event(
        conn, "account", account_id, "api.token.revoked", actor_id,
        f"{reason}: {listed}" if reason else listed,
    )
    return len(rows)


def delete_account_api_tokens(conn, account_id):
    """Remove every token row of an account that is being deleted."""
    conn.execute("DELETE FROM hosting_api_tokens WHERE account_id=?", (account_id,))


def record_api_instance_action(instance_id, action, account_id, token_id, prefix):
    """Log a pause or resume made through the API against the instance."""
    with get_hosting_db_context() as conn:
        record_event(
            conn, "instance", instance_id, f"api.instance.{action}", account_id,
            f"Through {_describe(token_id, prefix)}",
        )
        conn.commit()
