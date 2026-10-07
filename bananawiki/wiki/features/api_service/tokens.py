"""API tokens: issuing, verifying and revoking bearer credentials.

Tokens live in ``api_service__tokens``. Only an HMAC-SHA256 digest of the
raw token is stored (keyed by the instance secret with the 1.4 label, so
tokens issued by 1.4 keep working); the raw value is shown once when it is
created.

A password change, an administrator's password reset and a suspension
revoke the account's tokens through events, but events do not reach a
feature that is switched off. So each token also records which password it
was issued under (``credential_stamp``), and :func:`authenticate` refuses,
for good, a token whose owner's password has changed since or who was
suspended after it was issued. A token carries its own grant, ``permissions`` JSON
``{"read": bool, "write": bool, "scopes": [...]}``, which can only narrow
what the owning account may do: every endpoint still applies the owner's
role and permissions exactly as the web interface does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import current_app

from ....core import crypto
from ....core.timeutil import is_past, now_sql, parse, sql_in, to_sql, utcnow
from ... import settings
from ...db import db
from .errors import ApiError
from .serialize import iso

SCOPES = ("pages", "categories", "kanban", "canvas", "users", "settings", "tokens", "admin", "userbot")
ADMIN_ONLY_SCOPES = frozenset({"admin", "settings", "users"})
USERBOT_TOKEN_NAME = "userbot"
MAX_NAME = 64
RATE_LIMIT_BOUNDS = (1, 10000)
MAX_TOKENS_BOUNDS = (1, 100)
# Tokens expire at most this far ahead (or never, without an expiry).
MAX_EXPIRY_YEARS = 10
LAST_USED_RESOLUTION_SECONDS = 60
_PUBLIC_COLUMNS = "id, user_id, name, permissions, last_used_at, expires_at, active, created_at"


@dataclass(frozen=True)
class Grant:
    read: bool
    write: bool
    scopes: tuple[str, ...]

    def allows(self, scope: str, *, write: bool = False) -> bool:
        return scope in self.scopes and (self.write if write else self.read)

    def as_dict(self) -> dict[str, Any]:
        return {"read": self.read, "write": self.write, "scopes": list(self.scopes)}


NO_GRANT = Grant(False, False, ())


def parse_grant(raw: str | None) -> Grant:
    """Read a stored ``permissions`` value. Anything malformed grants nothing."""
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError):
        return NO_GRANT
    if not isinstance(data, dict):
        return NO_GRANT
    scopes = data.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        return NO_GRANT
    return Grant(data.get("read") is True, data.get("write") is True, tuple(sorted(set(scopes))))


# ── Service settings ──────────────────────────────────────────────────────────


def _bounded(value: Any, default: int, bounds: tuple[int, int]) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(bounds[0], min(bounds[1], number))


def service_settings() -> dict[str, Any]:
    """The API switches, clamped so a bad stored value cannot break the API."""
    s = settings.load()
    return {
        "enabled": bool(s.get("api_service_enabled")),
        "rate_limit": _bounded(s.get("api_service_rate_limit"), 60, RATE_LIMIT_BOUNDS),
        "admin_rate_limit": _bounded(s.get("api_service_admin_rate_limit"), 120, RATE_LIMIT_BOUNDS),
        "max_tokens_per_user": _bounded(s.get("api_service_max_tokens_per_user"), 5, MAX_TOKENS_BOUNDS),
    }


def is_admin_role(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("role") in ("admin", "owner")


def has_api_access(user: dict[str, Any] | None) -> bool:
    """Administrators always may use the API; others need ``users.api_access_enabled``."""
    if not user:
        return False
    return is_admin_role(user) or bool(user.get("api_access_enabled"))


def scopes_for_role(user: dict[str, Any], scopes: list[str] | tuple[str, ...]) -> list[str]:
    """Drop the scopes the account's role can never use."""
    known = [scope for scope in scopes if scope in SCOPES]
    if is_admin_role(user):
        return sorted(set(known))
    return sorted(set(known) - ADMIN_ONLY_SCOPES)


# ── Storage ───────────────────────────────────────────────────────────────────


def digest(raw: str) -> str:
    return crypto.token_digest(current_app.config["BW"].secret_key, crypto.TOKEN_LABEL_API, raw)


def credential_stamp(password_hash: str | None) -> str | None:
    """Fingerprint of the password (hash) a token is issued under; changes with every new password."""
    if not password_hash:
        return None
    return hashlib.sha256(("bananawiki-api-token:" + password_hash).encode("utf-8")).hexdigest()


def create(user_id: str, *, name: str = "", grant: Grant, expires_at: str | None = None) -> tuple[str, int]:
    """Issue a token; return ``(raw_token, token_id)``. The raw value is never stored."""
    raw = crypto.new_token(32)
    token_id = db.insert("api_service__tokens", {
        "user_id": user_id,
        "name": (name or "").strip()[:MAX_NAME],
        "token_hash": digest(raw),
        "permissions": json.dumps(grant.as_dict()),
        "expires_at": expires_at,
        "active": 1,
        "created_at": now_sql(),
        "credential_stamp": credential_stamp(db.scalar("SELECT password FROM users WHERE id = ?", (user_id,))),
    })
    return raw, token_id


def _outdated(token: dict[str, Any], owner: dict[str, Any]) -> bool:
    """The owner changed password, or was suspended, after the token was issued."""
    stamp = token.get("credential_stamp")
    if stamp and stamp != credential_stamp(owner["password"]):
        return True
    return bool(db.scalar(
        "SELECT 1 FROM suspension_audit WHERE user_id = ? AND action = 'suspend' AND created_at >= ? LIMIT 1",
        (owner["id"], token["created_at"]),
    ))


def authenticate(raw: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(token, owner)`` for an active, unexpired, still valid token, else None."""
    if not raw or len(raw) > 512:
        return None
    token = db.one("SELECT * FROM api_service__tokens WHERE token_hash = ?", (digest(raw),))
    if token is None or not token["active"]:
        return None
    if token["expires_at"] and (parse(token["expires_at"]) is None or is_past(token["expires_at"])):
        return None
    owner = db.one("SELECT * FROM users WHERE id = ?", (token["user_id"],))
    if owner is None:
        return None
    if _outdated(token, owner):
        revoke(token["id"])
        return None
    return token, owner


def touch(token: dict[str, Any]) -> None:
    """Record use, at most once a minute per token to spare the database."""
    last = parse(token.get("last_used_at"))
    if last is None or (utcnow() - last).total_seconds() >= LAST_USED_RESOLUTION_SECONDS:
        db.execute("UPDATE api_service__tokens SET last_used_at = ? WHERE id = ?", (now_sql(), token["id"]))


def get(token_id: int) -> dict[str, Any] | None:
    return db.one(f"SELECT {_PUBLIC_COLUMNS} FROM api_service__tokens WHERE id = ?", (token_id,))


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    return db.all(
        f"SELECT {_PUBLIC_COLUMNS} FROM api_service__tokens WHERE user_id = ? AND active = 1 "
        "ORDER BY created_at DESC, id DESC",
        (user_id,),
    )


def list_all() -> list[dict[str, Any]]:
    return db.all(
        "SELECT t.id, t.user_id, u.username, u.role, t.name, t.permissions, t.last_used_at, t.expires_at, "
        "t.active, t.created_at FROM api_service__tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.active = 1 ORDER BY t.created_at DESC, t.id DESC"
    )


def count_active(user_id: str) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM api_service__tokens WHERE user_id = ? AND active = 1 AND name != ?",
        (user_id, USERBOT_TOKEN_NAME), default=0,
    ))


def revoke(token_id: int) -> bool:
    return db.execute("UPDATE api_service__tokens SET active = 0 WHERE id = ? AND active = 1",
                      (token_id,)).rowcount > 0


def revoke_all_for_user(user_id: str) -> int:
    """Revoke every token of an account (password changes, administrator action)."""
    return db.execute("UPDATE api_service__tokens SET active = 0 WHERE user_id = ? AND active = 1",
                      (user_id,)).rowcount


def public_view(token: dict[str, Any], *, with_owner: bool = False) -> dict[str, Any]:
    """What the API returns about a token (never its digest)."""
    view = {
        "id": token["id"],
        "name": token["name"],
        "permissions": parse_grant(token.get("permissions")).as_dict(),
        "last_used_at": iso(token.get("last_used_at")),
        "expires_at": iso(token.get("expires_at")),
        "active": bool(token["active"]),
        "created_at": iso(token.get("created_at")),
    }
    if with_owner:
        view["user_id"] = token["user_id"]
        view["username"] = token.get("username", "")
    return view


# ── Issuing rules ─────────────────────────────────────────────────────────────


def check_quota(user_id: str) -> None:
    maximum = service_settings()["max_tokens_per_user"]
    if count_active(user_id) >= maximum:
        raise ApiError(400, "token_limit", maximum=maximum)


def expiry_sql(moment: datetime | None) -> str:
    """The stored form of an expiry; refuse a missing or past time, or one too far ahead."""
    stored = to_sql(moment)
    if stored is None:
        raise ApiError(400, "invalid_expiry")
    if stored <= now_sql():
        raise ApiError(400, "expiry_in_past")
    if stored > sql_in(days=366 * MAX_EXPIRY_YEARS):
        raise ApiError(400, "expiry_too_far", years=MAX_EXPIRY_YEARS)
    return stored


def future_expiry(value: Any) -> str | None:
    """Parse an ISO-8601 expiry (naive values are UTC); refuse past, distant or unreadable times."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ApiError(400, "invalid_expiry")
    return expiry_sql(parse(value.strip()))  # unbounded: 9999-12-31 is too far ahead, not unreadable


def child_grant(parent: dict[str, Any], owner: dict[str, Any], permissions: Any,
                expires_at: Any) -> tuple[Grant, str | None]:
    """The grant and expiry for a token issued through the API by *parent*.

    It can never exceed the issuing token: no flag or scope the parent lacks,
    no later expiry (an omitted expiry inherits the parent's), and no scope
    the owner's role cannot use.
    """
    if not isinstance(permissions, dict) or set(permissions) - {"read", "write", "scopes"}:
        raise ApiError(400, "invalid_permissions")
    read, write = permissions.get("read", False), permissions.get("write", False)
    scopes = permissions.get("scopes", [])
    if (type(read) is not bool or type(write) is not bool or not isinstance(scopes, list)
            or not all(isinstance(scope, str) and scope in SCOPES for scope in scopes)):
        raise ApiError(400, "invalid_permissions")
    parent_grant = parse_grant(parent.get("permissions"))
    if (read and not parent_grant.read) or (write and not parent_grant.write) \
            or not set(scopes) <= set(parent_grant.scopes):
        raise ApiError(403, "exceeds_parent")
    if scopes_for_role(owner, scopes) != sorted(set(scopes)):
        raise ApiError(403, "admin_scope_forbidden")
    expiry = future_expiry(expires_at)
    parent_expiry = parent.get("expires_at")
    if expiry is None:
        expiry = parent_expiry
    elif parent_expiry and expiry > (to_sql(parse(parent_expiry)) or ""):
        raise ApiError(403, "outlives_parent")
    return Grant(read, write, tuple(sorted(set(scopes)))), expiry


def web_grant(user: dict[str, Any], scopes: list[str], *, read_only: bool) -> Grant:
    """The grant chosen on the token form: always readable, writable unless read only."""
    allowed = scopes_for_role(user, scopes) or ["pages"]
    return Grant(True, not read_only, tuple(allowed))
