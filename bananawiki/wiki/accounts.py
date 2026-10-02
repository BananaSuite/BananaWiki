"""Account operations shared by sign-up, administration, the API and hosting.

Every write that changes who can sign in lives here so that the same rules
(username shape, password policy, session revocation, events, audit trail)
apply whichever part of the application triggers it.
"""

from __future__ import annotations

import re
import secrets
import string
from typing import Any

from flask import current_app

from ..core import passwords
from ..core.timeutil import now_sql
from .db import db
from .registry import emit

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,50}$")
RESERVED_USERNAMES = frozenset({"admin", "system", "deleted", "anonymous", "api", "root", "bananawiki"})
_ID_ALPHABET = string.ascii_lowercase + string.digits


class AccountError(ValueError):
    """A refused account change; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def new_user_id() -> str:
    while True:
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(8))
        if not db.scalar("SELECT 1 FROM users WHERE id = ?", (candidate,)):
            return candidate


def validate_username(username: str | None, *, allow_reserved: bool = False) -> str:
    name = (username or "").strip()
    if not USERNAME_RE.fullmatch(name):
        raise AccountError("auth.error.username_invalid")
    if not allow_reserved and name.lower() in RESERVED_USERNAMES:
        raise AccountError("auth.error.username_reserved")
    return name


def validate_password(password: str | None) -> str:
    problem = passwords.validate_password(password)
    if problem:
        raise AccountError(problem, minimum=passwords.MIN_LENGTH, maximum=passwords.MAX_LENGTH)
    return password  # type: ignore[return-value]


def hash_password(password: str) -> str:
    return passwords.hash_password(password, current_app.config["BW"].password_hash_method)


def by_id(user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    return db.one("SELECT * FROM users WHERE id = ?", (str(user_id),))


def by_username(username: str | None) -> dict[str, Any] | None:
    if not username:
        return None
    return db.one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),))


def username_taken(username: str, *, except_id: str | None = None) -> bool:
    row = by_username(username)
    return row is not None and row["id"] != except_id


def create(
    username: str,
    password: str | None,
    *,
    role: str = "user",
    custom_role_id: int | None = None,
    approval_status: str = "approved",
    invite_code: str | None = None,
    force_password_change: bool = False,
    password_hash: str | None = None,
    extra: dict[str, Any] | None = None,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Create an account. Pass *password* (validated and hashed) or a ready *password_hash*."""
    name = validate_username(username)
    if role not in ("user", "editor", "admin", "owner"):
        raise AccountError("auth.error.invalid_role")
    if password_hash is None:
        password_hash = hash_password(validate_password(password))
    with db.transaction():
        if username_taken(name):
            raise AccountError("auth.error.username_taken")
        user_id = new_user_id()
        values: dict[str, Any] = {
            "id": user_id,
            "username": name,
            "password": password_hash,
            "role": role,
            "custom_role_id": custom_role_id,
            "approval_status": approval_status,
            "invite_code": invite_code,
            "force_password_change": 1 if force_password_change else 0,
            "created_at": now_sql(),
        }
        values.update(extra or {})
        db.insert("users", values)
    user = by_id(user_id)
    assert user is not None
    if emit_event:
        emit("user.created", user=user)
    return user


def set_password(user_id: str, new_password: str, *, keep_session_id: str | None = None,
                 require_change: bool = False, expected_password_hash: str | None = None) -> None:
    """Change a password and sign the account out everywhere else."""
    from .auth import revoke_sessions

    hashed = hash_password(validate_password(new_password))
    with db.transaction():
        if expected_password_hash is not None and db.scalar(
            "SELECT password FROM users WHERE id = ?", (user_id,)
        ) != expected_password_hash:
            raise AccountError("auth.error.current_password_wrong")
        db.execute(
            "UPDATE users SET password = ?, force_password_change = ? WHERE id = ?",
            (hashed, 1 if require_change else 0, user_id),
        )
        revoke_sessions(user_id, except_session_id=keep_session_id)
    emit("user.password_changed", user_id=user_id)


def rename(user: dict[str, Any], new_username: str, *, changed_by: str | None = None) -> dict[str, Any]:
    name = validate_username(new_username)
    if name == user["username"]:
        return user
    with db.transaction():
        if username_taken(name, except_id=user["id"]):
            raise AccountError("auth.error.username_taken")
        db.execute("UPDATE users SET username = ? WHERE id = ?", (name, user["id"]))
        db.execute(
            "INSERT INTO username_history (user_id, old_username, new_username, changed_at) VALUES (?, ?, ?, ?)",
            (user["id"], user["username"], name, now_sql()),
        )
    renamed = by_id(user["id"])
    assert renamed is not None
    emit("user.renamed", user=renamed, old_username=user["username"], changed_by=changed_by)
    return renamed


def owners_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users WHERE role = 'owner'", default=0))


def delete(user: dict[str, Any], *, deleted_by: str | None = None, protect_last_admin: bool = False,
           protect_superuser: bool = False, expected_password_hash: str | None = None) -> None:
    """Delete an account and everything it owns (authorship becomes anonymous)."""
    with db.transaction():
        current = by_id(user["id"])
        if current is None:
            return
        if expected_password_hash is not None and current["password"] != expected_password_hash:
            raise AccountError("auth.error.current_password_wrong")
        if protect_superuser and current.get("is_superuser"):
            raise AccountError("users.error.protected_account")
        if current["role"] == "owner" and owners_count() <= 1:
            raise AccountError("admin.users.error.last_owner")
        if protect_last_admin and current["role"] in ("admin", "owner") and not current.get("suspended"):
            active = db.scalar("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'owner') AND suspended = 0")
            if active <= 1:
                raise AccountError("users.error.last_admin")
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    emit("user.deleted", user=current, deleted_by=deleted_by)


def display_name(user: dict[str, Any] | None) -> str:
    return user["username"] if user else "—"
