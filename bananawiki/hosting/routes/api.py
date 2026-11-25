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
from ..auth import admin_mfa_missing
from ..db import db
from ..errors import ServiceError
from ..i18n import t_lang
from ..limits import hit

bp = Blueprint("api", __name__, url_prefix="/api/v1")

REQUEST_LIMIT = 60
ACTION_LIMIT = 10
WINDOW = 60
LOOKUP_MAX = 500  # names and addresses in one lookup
_LOOKUP_CHUNK = 200


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
    if accounts.terms_outdated(account):
        return "Accept the current terms of service in the portal first."
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
            if scope in api_tokens.ADMIN_SCOPES and admin_mfa_missing(account):
                return _error(403, "Platform administrators must turn on two-step verification in the portal first.")
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
            instances.stop(inst, actor_id=account["id"], charge=account)
        else:
            instances.start(inst, actor_id=account["id"], charge=account)
    except ServiceError as error:
        if error.key == "hosting.instances.too_many_changes":
            return _error(429, "This wiki was paused and resumed too often. Wait a minute before trying again.",
                          retry_after=WINDOW)
        if error.key == "hosting.instances.too_many_changes_today":
            return _error(429, t_lang("en", error.key, **error.values), retry_after=error.values["retry_after"])
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


# Account lookup for the linking tools of apps (BananaChat, BananaGit): administrators' tokens with accounts:lookup.


def _lookup_status(row: dict[str, Any]) -> str:
    if row["approval_status"] != "approved":
        return "pending"
    if accounts.is_suspended(row) or row.get("pending_deletion"):
        return "suspended"
    return "active"


def _verified_email(row: dict[str, Any]) -> str:
    return row["email"] if row.get("email") and row.get("email_verified_at") and not row.get("email_flagged_invalid") \
        else ""


def _lookup_rows(column: str, values: list[str]) -> list[dict[str, Any]]:
    """Accounts whose *column* (username, or a verified email) matches one of *values*, ignoring case.

    Deleted accounts and denied ones (about to be deleted) are never found.
    """
    found: list[dict[str, Any]] = []
    for start in range(0, len(values), _LOOKUP_CHUNK):
        chunk = values[start:start + _LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        extra = (" AND email_verified_at IS NOT NULL AND COALESCE(email_flagged_invalid, 0) = 0 AND email <> ''"
                 if column == "email" else "")
        found += db.all(f"SELECT * FROM accounts WHERE {column} COLLATE NOCASE IN ({marks}) "  # noqa: S608
                        f"AND deleted_at IS NULL AND approval_status != 'denied'{extra}", chunk)
    return found


def _lookup_list(data: dict[str, Any], key: str, limit: int) -> list[str] | None:
    values = data.get(key, [])
    if not isinstance(values, list) or not all(isinstance(value, str) and 0 < len(value.strip()) <= limit
                                               for value in values):
        return None
    return list(dict.fromkeys(value.strip() for value in values))


@bp.route("/admin/accounts/lookup", methods=["GET", "POST"])
@token_required("accounts:lookup")
def lookup_accounts(account: dict[str, Any], token: dict[str, Any]):
    """Find hosting accounts by user name (any case) or verified address: ``sub``, ``username``, ``email`` (only
    when verified), ``email_verified`` and ``status``; what matched nothing is listed in ``not_found``."""
    if request.method == "GET":
        username = (request.args.get("username") or "").strip()
        address = (request.args.get("email") or "").strip()
        if bool(username) == bool(address) or len(username) > 60 or len(address) > 254:
            return _error(400, "Give either username or email.")
        names, emails = ([username], []) if username else ([], [address])
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error(400, "Send a JSON object with usernames and/or emails.")
        names, emails = _lookup_list(data, "usernames", 60), _lookup_list(data, "emails", 254)
        if names is None or emails is None:
            return _error(400, "usernames and emails must be lists of non-empty strings.")
        if not names and not emails:
            return _error(400, "Give at least one username or email.")
        if len(names) + len(emails) > LOOKUP_MAX:
            return _error(400, f"At most {LOOKUP_MAX} usernames and emails together.", max=LOOKUP_MAX)
    by_name = _lookup_rows("username", names)
    by_email = _lookup_rows("email", emails)
    rows = {row["id"]: row for row in by_name + by_email}
    found_names = {row["username"].lower() for row in by_name}
    found_emails = {row["email"].lower() for row in by_email}
    not_found = [name for name in names if name.lower() not in found_names] + \
        [address for address in emails if address.lower() not in found_emails]
    events.record("account", account["id"], "api.accounts.lookup", account["id"],
                  f"Through Token {token['id']} ({token['prefix']}): {len(names)} names, {len(emails)} addresses, "
                  f"{len(rows)} found")
    answer = []
    for row in sorted(rows.values(), key=lambda row: row["username"].lower()):
        item = {"sub": row["id"], "username": row["username"], "email_verified": bool(_verified_email(row)),
                "status": _lookup_status(row)}
        if _verified_email(row):
            item["email"] = _verified_email(row)  # an address that is not verified is never given out
        answer.append(item)
    return jsonify(accounts=answer, not_found=not_found)
