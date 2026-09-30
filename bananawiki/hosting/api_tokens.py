"""Personal access tokens for the REST API (``hosting_api_tokens``).

Format and storage are unchanged from 1.4: ``bwh_`` followed by 40 URL-safe
characters, stored as the hex SHA-256 of the whole token plus a 12-character
display prefix. The owner sees a token once, when it is created.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from ..core.crypto import sha256_hex
from ..core.timeutil import is_past, now_sql
from . import events
from .db import db
from .errors import ServiceError

PREFIX = "bwh_"
PATTERN = re.compile(r"bwh_[A-Za-z0-9_-]{40}")
NAME_MAX_LENGTH = 60
MAX_ACTIVE = 10
SCOPES = ("account:read", "instances:read", "instances:manage", "admin:read")
ADMIN_SCOPES = frozenset({"admin:read"})
EXPIRY_CHOICES = {"30": 30, "90": 90, "365": 365, "never": None}
_DISPLAY_PREFIX = 12


def scopes_for(account: dict[str, Any]) -> tuple[str, ...]:
    return tuple(s for s in SCOPES if account.get("is_admin") or s not in ADMIN_SCOPES)


def parse_scopes(value: str | None) -> list[str]:
    return [scope for scope in (value or "").split() if scope in SCOPES]


def expired(row: dict[str, Any]) -> bool:
    return bool(row.get("expires_at")) and is_past(row["expires_at"])


def _describe(token_id: int, prefix: str) -> str:
    return f"Token {token_id} ({prefix})"


def create(account: dict[str, Any], name: str, scopes: list[str], expires_at: str | None,
           expected_session_version: int) -> tuple[int, str]:
    """Create a token and return ``(id, raw_token)``.

    Refused with ``stale`` when the account's session version changed since
    the caller checked the password (a password change revokes tokens).
    """
    name = (name or "").strip()
    if not 1 <= len(name) <= NAME_MAX_LENGTH or not name.isprintable():
        raise ServiceError("hosting.api_tokens.invalid_name", max=NAME_MAX_LENGTH)
    offered = scopes_for(account)
    if not scopes or any(scope not in offered for scope in scopes):
        raise ServiceError("hosting.api_tokens.invalid_scopes")
    scope_text = " ".join(s for s in SCOPES if s in scopes)
    raw = PREFIX + secrets.token_urlsafe(30)
    prefix = raw[:_DISPLAY_PREFIX]
    with db.transaction():
        current = db.one("SELECT session_version, deleted_at FROM accounts WHERE id = ?", (account["id"],))
        if current is None or current["deleted_at"] or int(current["session_version"] or 0) != expected_session_version:
            raise ServiceError("hosting.api_tokens.stale")
        rows = db.all("SELECT expires_at FROM hosting_api_tokens WHERE account_id = ? AND revoked_at IS NULL",
                      (account["id"],))
        if sum(1 for row in rows if not expired(row)) >= MAX_ACTIVE:
            raise ServiceError("hosting.api_tokens.limit_reached", max=MAX_ACTIVE)
        token_id = db.insert("hosting_api_tokens", {
            "account_id": account["id"], "name": name, "prefix": prefix, "token_hash": sha256_hex(raw),
            "scopes": scope_text, "created_at": now_sql(), "expires_at": expires_at,
        })
        events.record("account", account["id"], "api.token.created", account["id"],
                      f"{_describe(token_id, prefix)} with scopes {scope_text}")
    return token_id, raw


def lookup(raw: str | None) -> dict[str, Any] | None:
    """The live token row for *raw*, or None (revoked, expired or malformed)."""
    if not raw or not PATTERN.fullmatch(raw):
        return None
    row = db.one("SELECT * FROM hosting_api_tokens WHERE token_hash = ?", (sha256_hex(raw),))
    if row is None or row["revoked_at"] or expired(row):
        return None
    return row


def touch(token_id: int) -> None:
    db.execute("UPDATE hosting_api_tokens SET last_used_at = ? WHERE id = ?", (now_sql(), token_id))


def list_for(account_id: str) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT id, name, prefix, scopes, created_at, expires_at, last_used_at FROM hosting_api_tokens "
        "WHERE account_id = ? AND revoked_at IS NULL ORDER BY id DESC", (account_id,),
    )
    return [{**row, "scopes": parse_scopes(row["scopes"]), "expired": expired(row)} for row in rows]


def revoke(account_id: str, token_id: int, actor_id: str) -> bool:
    with db.transaction():
        row = db.one("SELECT id, prefix FROM hosting_api_tokens WHERE id = ? AND account_id = ? AND revoked_at IS NULL",
                     (token_id, account_id))
        if row is None:
            return False
        db.execute("UPDATE hosting_api_tokens SET revoked_at = ? WHERE id = ?", (now_sql(), token_id))
        events.record("account", account_id, "api.token.revoked", actor_id, _describe(row["id"], row["prefix"]))
    return True


def revoke_all(account_id: str, actor_id: str | None, reason: str) -> int:
    """Revoke every live token of the account (inside the caller's transaction)."""
    rows = db.all("SELECT id, prefix FROM hosting_api_tokens WHERE account_id = ? AND revoked_at IS NULL", (account_id,))
    if not rows:
        return 0
    db.execute("UPDATE hosting_api_tokens SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
               (now_sql(), account_id))
    listed = ", ".join(_describe(r["id"], r["prefix"]) for r in rows)
    events.record("account", account_id, "api.token.revoked", actor_id, f"{reason}: {listed}")
    return len(rows)
