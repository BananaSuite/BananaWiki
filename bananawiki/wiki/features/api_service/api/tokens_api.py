"""Tokens (``tokens`` scope, own tokens) and administration (``admin`` scope)."""

from __future__ import annotations

from .... import accounts
from ....db import db
from ...admin import service as admin_service
from .. import audit, serialize, tokens
from ..errors import ApiError, flag, json_body, query_int, text, window_fields
from . import bp, caller, caller_token, ok, require_admin, requires, secret_response


@bp.get("/tokens")
@requires("tokens")
def list_own_tokens():
    return ok(tokens=[tokens.public_view(row) for row in tokens.list_for_user(caller()["id"])])


@bp.post("/tokens")
@requires("tokens", write=True)
@secret_response
def create_token():
    """Issue a token that can never exceed the calling token (scopes, flags, expiry)."""
    data = json_body()
    user = caller()
    name = text(data.get("name"), "name", maximum=tokens.MAX_NAME)
    permissions = data.get("permissions", {"read": True, "write": False, "scopes": ["pages"]})
    grant, expires_at = tokens.child_grant(caller_token(), user, permissions, data.get("expires_at"))
    tokens.check_quota(user["id"])
    raw, token_id = tokens.create(user["id"], name=name, grant=grant, expires_at=expires_at)
    return ok(201, token=raw, id=token_id, permissions=grant.as_dict(), expires_at=serialize.iso(expires_at))


@bp.delete("/tokens/<int:token_id>")
@requires("tokens", write=True)
def revoke_own_token(token_id: int):
    token = tokens.get(token_id)
    if token is None or token["user_id"] != caller()["id"] or not token["active"]:
        raise ApiError(404, "token_not_found")
    tokens.revoke(token_id)
    return ok(revoked=True, id=token_id)


@bp.get("/admin/tokens")
@requires("admin")
def admin_list_tokens():
    require_admin()
    return ok(tokens=[tokens.public_view(row, with_owner=True) for row in tokens.list_all()])


@bp.post("/admin/tokens/<int:token_id>/revoke")
@requires("admin", write=True)
def admin_revoke_token(token_id: int):
    require_admin()
    token = tokens.get(token_id)
    if token is None:
        raise ApiError(404, "token_not_found")
    owner = accounts.by_id(token["user_id"])
    if owner and admin_service.protection_error(caller(), owner):
        raise ApiError(403, "protected_account")
    tokens.revoke(token_id)
    return ok(revoked=True, id=token_id)


@bp.put("/admin/users/<user_id>/api-access")
@requires("admin", write=True)
def admin_api_access(user_id: str):
    require_admin()
    target = accounts.by_id(user_id)
    if target is None:
        raise ApiError(404, "user_not_found")
    if target.get("is_superuser") or (target["role"] == "owner" and target["id"] != caller()["id"]):
        raise ApiError(403, "protected_account")
    enabled = flag(json_body().get("enabled", True), "enabled")
    db.execute("UPDATE users SET api_access_enabled = ? WHERE id = ?", (1 if enabled else 0, user_id))
    return ok(api_access_enabled=enabled, id=user_id)


@bp.get("/admin/audit-log")
@requires("admin")
def admin_audit_log():
    require_admin()
    limit = query_int("limit", 100, 1, 500)
    offset = query_int("offset", 0, 0, 2**31 - 1)
    rows = audit.entries(limit=limit + 1, offset=offset)
    return ok(entries=[{**row, "created_at": serialize.iso(row["created_at"])} for row in rows[:limit]],
              total=audit.count(), **window_fields(limit, offset, len(rows)))


@bp.delete("/admin/audit-log")
@requires("admin", write=True)
def admin_clear_audit_log():
    require_admin()
    if not caller().get("is_superuser"):
        raise ApiError(403, "superuser_required")
    days = query_int("before_days", 90, 1, 3650)
    return ok(cleared=audit.clear(days), before_days=days)
