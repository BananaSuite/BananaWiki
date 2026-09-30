"""Accounts (administrators, ``users`` scope).

Usernames and passwords follow the account forms (``accounts.py``). Protected
accounts (superusers, and the owner for anyone but the owner) cannot be
changed, the last administrator cannot be demoted, suspended or deleted,
and a password change signs the account out everywhere and revokes its
API tokens.
"""

from __future__ import annotations

from typing import Any

from .... import accounts, auth, settings
from ....db import db
from .. import serialize, tokens
from ..errors import ApiError, flag, from_service, invalid, items, json_body, page_window, window_fields
from . import bp, caller, ok, require_admin, requires

MAX_BULK_USERS = 20
NEW_USER_ROLES = ("user", "editor", "admin")


def _target(user_id: str) -> dict[str, Any]:
    target = accounts.by_id(user_id)
    if target is None:
        raise ApiError(404, "user_not_found")
    return target


def _admins() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'owner')", default=0))


def _check_modifiable(target: dict[str, Any]) -> None:
    if target.get("is_superuser") and target["id"] != caller()["id"]:
        raise ApiError(403, "protected_account")
    if target["role"] == "owner" and target["id"] != caller()["id"]:
        raise ApiError(403, "protected_account")


def _password(value: Any) -> str:
    password = value.strip() if isinstance(value, str) else ""
    if not password:
        raise invalid("password", "required")
    return password


def _create(item: dict[str, Any]) -> dict[str, Any]:
    role = item.get("role") or "user"
    if role not in NEW_USER_ROLES:
        raise invalid("role", "choice", options=", ".join(NEW_USER_ROLES))
    if not isinstance(item.get("username"), str):
        raise invalid("username", "required")
    extra = {"intro_required": 1} if settings.get("new_user_intro_enabled") else None
    try:
        return accounts.create(item["username"], _password(item.get("password")), role=role, extra=extra,
                               force_password_change=bool(item.get("force_password_change")))
    except accounts.AccountError as error:
        raise from_service(error, 409 if error.key == "auth.error.username_taken" else 400) from None


@bp.get("/users")
@requires("users")
def list_users():
    require_admin()
    limit, offset = page_window()
    rows = db.all("SELECT * FROM users ORDER BY username COLLATE NOCASE, id LIMIT ? OFFSET ?", (limit + 1, offset))
    return ok(users=[serialize.user(row) for row in rows[:limit]], **window_fields(limit, offset, len(rows)))


@bp.get("/users/<user_id>")
@requires("users")
def get_user(user_id: str):
    require_admin()
    return ok(user=serialize.user(_target(user_id)))


@bp.post("/users")
@requires("users", write=True)
def create_user():
    require_admin()
    return ok(201, user=serialize.user(_create(json_body())))


@bp.post("/users/bulk")
@requires("users", write=True)
def bulk_create_users():
    """At most 20 accounts per request: each password hash costs noticeable CPU."""
    require_admin()
    entries = items(json_body(), "users", MAX_BULK_USERS)
    if not entries:
        raise invalid("users", "required")
    if not all(isinstance(item, dict) for item in entries):
        raise invalid("users", "object")
    created, errors = [], []
    for item in entries:
        try:
            created.append(serialize.user(_create(item)))
        except ApiError as error:
            payload = error.payload()
            name = item.get("username") if isinstance(item.get("username"), str) else ""
            errors.append({"username": name, "error": payload["error"], "code": payload["code"]})
    return ok(201 if created else 200, created=created, errors=errors)


@bp.put("/users/<user_id>")
@requires("users", write=True)
def update_user(user_id: str):
    require_admin()
    target = _target(user_id)
    _check_modifiable(target)
    data = json_body()
    changes: dict[str, Any] = {}
    last_admin = target["role"] in ("admin", "owner") and _admins() <= 1
    if "role" in data:
        role = data["role"]
        if role == "owner":
            raise ApiError(403, "cannot_assign_owner")
        if role not in NEW_USER_ROLES:
            raise invalid("role", "choice", options=", ".join(NEW_USER_ROLES))
        if target["role"] == "owner" and role != "owner":
            raise ApiError(403, "protected_account")
        if last_admin and role not in ("admin", "owner"):
            raise ApiError(400, "last_admin")
        changes["role"] = role
    if "suspended" in data:
        suspended = flag(data["suspended"], "suspended")
        if suspended and (last_admin or target["id"] == caller()["id"]):
            raise ApiError(400, "last_admin" if last_admin else "cannot_suspend_self")
        changes["suspended"] = 1 if suspended else 0
        if not suspended:
            changes["suspended_until"] = None
    if "api_access_enabled" in data:
        changes["api_access_enabled"] = 1 if flag(data["api_access_enabled"], "api_access_enabled") else 0
    new_password = None
    if "password" in data and not (isinstance(data["password"], str) and not data["password"].strip()):
        new_password = _password(data["password"])
        try:
            accounts.validate_password(new_password)
        except accounts.AccountError as error:
            raise from_service(error) from None
    payload: dict[str, Any] = {}
    with db.transaction():
        if changes:
            db.update("users", changes, "id = ?", (target["id"],))
        if changes.get("suspended"):
            auth.revoke_sessions(target["id"])
        if new_password is not None:
            payload["api_tokens_revoked"] = tokens.revoke_all_for_user(target["id"])
            accounts.set_password(target["id"], new_password)
    return ok(user=serialize.user(_target(user_id)), **payload)


@bp.delete("/users/<user_id>")
@requires("users", write=True)
def delete_user(user_id: str):
    require_admin()
    target = _target(user_id)
    if target.get("is_superuser") or target["role"] == "owner":
        raise ApiError(403, "protected_account")
    if target["id"] == caller()["id"]:
        raise ApiError(400, "cannot_delete_self")
    if target["role"] == "admin" and _admins() <= 1:
        raise ApiError(400, "last_admin")
    try:
        accounts.delete(target, deleted_by=caller()["id"])
    except accounts.AccountError as error:
        raise from_service(error) from None
    return ok(deleted=True, id=target["id"])
