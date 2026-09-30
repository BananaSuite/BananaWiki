"""Accounts (administrators, ``users`` scope).

Usernames and passwords follow the account forms (``accounts.py``). Changes
go through the administration service (``features/admin/service.py``), so
the web interface's hierarchy applies: superusers and owners are changed only
by themselves, other administrators only by an owner or a superuser. Role
changes and suspensions are recorded (``role_history``, ``suspension_audit``)
and announced (``user.role_changed``, ``user.suspended``) as in the web
interface. The last administrator cannot be demoted, suspended or deleted,
and a password change signs the account out everywhere and revokes its API
tokens.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .... import accounts, settings
from ....db import db
from ...admin import service as admin_service
from .. import serialize, tokens
from ..errors import ApiError, flag, from_service, invalid, items, json_body, page_window, window_fields
from . import bp, caller, ok, require_admin, requires

MAX_BULK_USERS = 20
NEW_USER_ROLES = ("user", "editor", "admin")
PROTECTION_ERRORS = frozenset({"admin.users.error.protected", "admin.users.error.owner_only_self",
                               "admin.users.error.admin_needs_owner"})


def _target(user_id: str) -> dict[str, Any]:
    target = accounts.by_id(user_id)
    if target is None:
        raise ApiError(404, "user_not_found")
    return target


def _admins() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'owner')", default=0))


def _check_modifiable(target: dict[str, Any]) -> None:
    """The administrators' hierarchy of the web interface (``admin.service.protection_error``)."""
    if admin_service.protection_error(caller(), target):
        raise ApiError(403, "protected_account")


def _service(action: Callable[..., Any], target: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
    """Run an administration service call as the caller; a refusal becomes an API error."""
    try:
        return action(caller(), target, *args, **kwargs)
    except accounts.AccountError as error:
        if error.key in PROTECTION_ERRORS:
            raise ApiError(403, "protected_account") from None
        raise from_service(error) from None


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
    """Everything is checked before anything changes; each change then runs as in the web interface."""
    require_admin()
    target = _target(user_id)
    _check_modifiable(target)
    data = json_body()
    own = target["id"] == caller()["id"]
    last_admin = target["role"] in ("admin", "owner") and _admins() <= 1
    role = None
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
        if role == target["role"]:
            role = None
        elif own:
            raise ApiError(400, "cannot_change_own_role")
    suspended = None
    if "suspended" in data:
        suspended = flag(data["suspended"], "suspended")
        if suspended and (last_admin or own):
            raise ApiError(400, "last_admin" if last_admin else "cannot_suspend_self")
        if suspended == bool(target["suspended"]):
            suspended = None
    api_access = None
    if "api_access_enabled" in data:
        api_access = flag(data["api_access_enabled"], "api_access_enabled")
    new_password = None
    if "password" in data and not (isinstance(data["password"], str) and not data["password"].strip()):
        new_password = _password(data["password"])
        try:
            accounts.validate_password(new_password)
        except accounts.AccountError as error:
            raise from_service(error) from None
    payload: dict[str, Any] = {}
    if role is not None:
        target = _service(admin_service.change_role, target, role)
    if suspended:
        _service(admin_service.suspend, target, until=None, label="permanent", reason="", reason_visible=False,
                 time_visible=False)
    elif suspended is False:
        _service(admin_service.unsuspend, target)
    if api_access is not None:
        db.execute("UPDATE users SET api_access_enabled = ? WHERE id = ?", (1 if api_access else 0, target["id"]))
    if new_password is not None:
        payload["api_tokens_revoked"] = tokens.revoke_all_for_user(target["id"])
        _service(admin_service.reset_password, _target(user_id), new_password, require_change=False,
                 keep_original=False)
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
    _check_modifiable(target)
    if target["role"] == "admin" and _admins() <= 1:
        raise ApiError(400, "last_admin")
    _service(admin_service.delete_user, target)
    return ok(deleted=True, id=target["id"])
