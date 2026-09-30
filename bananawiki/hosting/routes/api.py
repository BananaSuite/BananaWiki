"""REST API under ``/api/v1`` (same routes, scopes and answers as 1.4).

Only personal access tokens (``Authorization: Bearer bwh_…``) are accepted;
the browser session is never read, so CSRF does not apply. The whole prefix
answers a JSON 404 while an administrator has not switched the API on.
"""

from __future__ import annotations

import functools
from collections import Counter
from collections.abc import Callable
from typing import Any

from flask import Blueprint, jsonify, request

from .. import accounts, api_tokens, collaborators, events, instances, notifications
from ..errors import ServiceError
from ..i18n import t_lang
from ..limits import hit

bp = Blueprint("api", __name__, url_prefix="/api/v1")

REQUEST_LIMIT = 60
ACTION_LIMIT = 10
WINDOW = 60


def _error(code: int, message: str, **extra: Any):
    response = jsonify(error=message, **extra)
    response.status_code = code
    response.headers["Cache-Control"] = "no-store"
    return response


def _unauthorized(message: str):
    response = _error(401, message)
    response.headers["WWW-Authenticate"] = 'Bearer realm="hosting-api"'
    return response


def _refusal(account: dict[str, Any]) -> str | None:
    if account["deleted_at"]:
        return "The token is invalid, revoked or expired."
    if accounts.is_suspended(account):
        return "This account is suspended."
    if account.get("pending_deletion"):
        return "This account is scheduled for deletion."
    if account.get("email_flagged_invalid"):
        return "An administrator has asked for a new contact email. Sign in to the portal to provide it."
    if account.get("email") and not account.get("email_verified_at") and notifications.verification_required():
        return "Verify the contact email of this account in the portal first."
    if account["approval_status"] != "approved":
        return "This account has not been approved."
    return None


def token_required(scope: str) -> Callable[[Callable], Callable]:
    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            scheme, _, raw = request.headers.get("Authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not raw.strip():
                return _unauthorized("Send a personal access token in the header Authorization: Bearer <token>.")
            token = api_tokens.lookup(raw.strip())
            if token is None:
                return _unauthorized("The token is invalid, revoked or expired.")
            if not hit("hosting_api", REQUEST_LIMIT, WINDOW, key=f"api-token:{token['id']}"):
                return _error(429, "Too many requests. Wait before trying again.", retry_after=WINDOW)
            account = accounts.get(token["account_id"])
            if account is None:
                return _unauthorized("The token is invalid, revoked or expired.")
            refusal = _refusal(account)
            if refusal:
                return _error(403, refusal)
            if scope not in api_tokens.parse_scopes(token["scopes"]):
                return _error(403, f"This token does not have the {scope} scope.")
            if scope in api_tokens.ADMIN_SCOPES and not account["is_admin"]:
                return _error(403, "This account is not a platform administrator.")
            api_tokens.touch(token["id"])
            return view(account, token, *args, **kwargs)

        return wrapper

    return decorate


def _instance_json(inst: dict[str, Any], role: str) -> dict[str, Any]:
    limit = instances.storage_limit_mb(inst)
    return {
        "id": inst["id"], "subdomain": inst["subdomain"], "url": instances.urls.instance_url(inst) or None,
        "status": inst["status"], "role": role, "created_at": inst["created_at"], "expires_at": inst["expires_at"],
        "storage_used_bytes": instances.usage_bytes(inst),
        "storage_limit_bytes": limit * 1024 * 1024 if limit else None,
    }


def _find(account: dict[str, Any], instance_id: str) -> tuple[dict[str, Any] | None, str]:
    """A live wiki the account owns or collaborates on (never other people's, even for admins)."""
    inst = instances.get(instance_id)
    if inst is None or inst["status"] == "terminated":
        return None, ""
    if inst["account_id"] == account["id"]:
        return inst, "owner"
    member = collaborators.membership(inst["id"], account["id"])
    if member:
        return inst, "collaborator"
    return None, ""


def _customer_can(inst: dict[str, Any], account: dict[str, Any], permission: str) -> bool:
    customer = {**account, "is_admin": 0}
    return collaborators.can(inst, customer, permission)


@bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/status")
def status():
    return jsonify(api="v1")


@bp.get("/me")
@token_required("account:read")
def me(account: dict[str, Any], token: dict[str, Any]):
    return jsonify(
        account={"id": account["id"], "username": account["username"], "email": account["email"] or "",
                 "is_admin": bool(account["is_admin"]), "created_at": account["created_at"]},
        token={"id": token["id"], "name": token["name"], "prefix": token["prefix"],
               "scopes": api_tokens.parse_scopes(token["scopes"]), "created_at": token["created_at"],
               "expires_at": token["expires_at"]},
    )


@bp.get("/instances")
@token_required("instances:read")
def list_instances(account: dict[str, Any], token: dict[str, Any]):
    items = [_instance_json(inst, "owner") for inst in instances.owned_by(account["id"])]
    shared = instances.db.all(
        "SELECT i.* FROM instance_collaborators c JOIN instances i ON i.id = c.instance_id "
        "WHERE c.account_id = ? AND i.status != 'terminated' ORDER BY i.created_at DESC", (account["id"],),
    )
    items += [_instance_json(inst, "collaborator") for inst in shared if _customer_can(inst, account, "view")]
    return jsonify(instances=items)


@bp.get("/instances/<instance_id>")
@token_required("instances:read")
def get_instance(account: dict[str, Any], token: dict[str, Any], instance_id: str):
    inst, role = _find(account, instance_id)
    if inst is None or not _customer_can(inst, account, "view"):
        return _error(404, "Not found.")
    return jsonify(instance=_instance_json(inst, role))


def _change_state(account: dict[str, Any], token: dict[str, Any], instance_id: str, action: str):
    if not hit("hosting_api_instance_action", ACTION_LIMIT, WINDOW, key=f"api-token:{token['id']}"):
        return _error(429, "Too many requests. Wait before trying again.", retry_after=WINDOW)
    inst, role = _find(account, instance_id)
    if inst is None:
        return _error(404, "Not found.")
    if not _customer_can(inst, account, "start_stop"):
        return _error(403, "Your collaborator permissions on this wiki do not include pausing and resuming it.")
    if inst["status"] == "suspended" or inst.get("suspended_at"):
        return _error(403, "This wiki has been suspended by an administrator.")
    required = "running" if action == "pause" else "stopped"
    if inst["status"] != required:
        message = "Only a running wiki can be paused." if action == "pause" else "Only a paused wiki can be resumed."
        return _error(409, message, status=inst["status"])
    try:
        if action == "pause":
            instances.stop(inst, actor_id=account["id"])
        else:
            instances.start(inst, actor_id=account["id"])
    except ServiceError as error:
        current = instances.get(inst["id"])
        if current is None or current["status"] != required:
            return _error(409, "The wiki changed state.", status=current["status"] if current else None)
        return _error(500, t_lang("en", error.key, **error.values))
    done = "paused" if action == "pause" else "resumed"
    events.record("instance", inst["id"], f"api.instance.{done}", account["id"],
                  f"Through Token {token['id']} ({token['prefix']})")
    return jsonify(instance=_instance_json(instances.get(inst["id"]), role))  # type: ignore[arg-type]


@bp.post("/instances/<instance_id>/pause")
@token_required("instances:manage")
def pause(account: dict[str, Any], token: dict[str, Any], instance_id: str):
    return _change_state(account, token, instance_id, "pause")


@bp.post("/instances/<instance_id>/resume")
@token_required("instances:manage")
def resume(account: dict[str, Any], token: dict[str, Any], instance_id: str):
    return _change_state(account, token, instance_id, "resume")


@bp.get("/admin/pending-accounts")
@token_required("admin:read")
def pending_accounts(account: dict[str, Any], token: dict[str, Any]):
    return jsonify(accounts=[
        {"id": row["id"], "username": row["username"], "email": row["email"] or "",
         "email_verified": bool(row["email_verified_at"]), "created_at": row["created_at"],
         "use_case": row["signup_use_case"] or ""}
        for row in accounts.pending()
    ])


@bp.get("/admin/instances")
@token_required("admin:read")
def admin_instances(account: dict[str, Any], token: dict[str, Any]):
    counts = Counter(row["status"] for row in instances.db.all("SELECT status FROM instances"))
    return jsonify(total=sum(counts.values()), by_status={s: counts.get(s, 0) for s in instances.STATUSES})
