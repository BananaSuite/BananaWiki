"""REST API for the hosting portal, served under ``/api/v1``.

Personal access tokens created on the Account page are the only way in. The
API reads ``Authorization: Bearer`` and never the browser session, so a
signed-in tab cannot be made to call it from another site; that is also why
these views, and only these, are exempt from CSRF protection.

Version 1 is deliberately small. It reads the account and its wikis and can
pause or resume a wiki. Nothing here terminates, deletes, resets or downloads
anything, and platform administrators get read-only counts and the approval
queue, not the admin tools.
"""

import hmac
import logging
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    request,
    session,
    url_for,
)

import sqlite_runtime
from helpers._passwords import check_password_hash
from helpers._translations import t as translate
from ops_observability import record_http_error

from ..db import (
    ADMIN_API_SCOPES,
    API_TOKEN_NAME_MAX_LENGTH,
    MAX_ACTIVE_API_TOKENS,
    api_scopes_for_account,
    check_account_suspension_expired,
    check_and_record_hosting_rate_limit,
    create_api_token,
    get_account_by_id,
    get_active_api_token,
    get_all_instances,
    get_hosting_api_enabled,
    get_hosting_rate_limit_retry_after,
    get_hosting_settings,
    get_instance,
    get_instances_for_account,
    get_pending_accounts,
    get_shared_instances_for_account,
    is_account_suspended,
    is_collaborator,
    parse_api_token_scopes,
    record_api_instance_action,
    revoke_api_token,
    touch_api_token,
)
from ..instance_manager import (
    _instance_dir_for,
    _instance_storage_limit_mb,
    _instance_url,
    get_storage_bytes,
    restart_instance,
    stop_instance,
)
from .auth import (
    API_TOKEN_FORM_NONCE_KEY,
    _email_policy_active,
    _email_verification_required,
    _redirect_to_login_with_next,
    hosting_login_required,
    hosting_rate_limit,
    render_account_page,
)
from .dashboard_common import (
    _SUSPENDED_LOCKOUT_MESSAGE,
    _can_access_instance,
    _owner_is_locked_out,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# Limits are per token. Every authenticated request counts towards the first;
# pause and resume count towards the second as well.
_REQUEST_LIMIT = 60
_INSTANCE_ACTION_LIMIT = 10
_RATE_WINDOW = 60
_REQUEST_BUCKET = "hosting_api"
_INSTANCE_ACTION_BUCKET = "hosting_api_instance_action"

# Choices offered on the Account page, in days. ``never`` means no expiry.
_EXPIRY_CHOICES = {"30": 30, "90": 90, "365": 365, "never": None}

_PASSWORD_MAX_LENGTH = 1000
_INSTANCE_ID_MAX_LENGTH = 64

_NOT_FOUND = "Not found."
_MISSING_TOKEN = "Send a personal access token in the header Authorization: Bearer <token>."
_BAD_TOKEN = "The token is invalid, revoked or expired."
_INSTANCE_STATUSES = ("running", "stopped", "suspended", "terminated")


def is_api_path(path):
    """Return ``True`` for any path under the API prefix."""
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def _error(code, message, **extra):
    response = jsonify(error=message, **extra)
    response.status_code = code
    response.headers["Cache-Control"] = "no-store"
    return response


def api_disabled_error():
    """Return the JSON 404 that every path under the prefix gets while the API is off."""
    return _error(404, _NOT_FOUND)


def api_routing_error(error):
    """Answer a routing 404 or 405 under the API prefix in JSON.

    The portal's own error handlers call this. While the API is switched off
    every path under the prefix is a 404, so trying another method does not
    reveal which routes exist.
    """
    if getattr(error, "code", 404) == 405 and get_hosting_api_enabled():
        response = _error(405, "Method not allowed.")
        valid_methods = getattr(error, "valid_methods", None)
        if valid_methods:
            response.headers["Allow"] = ", ".join(sorted(valid_methods))
        return response
    return _error(404, _NOT_FOUND)


def _unauthorized(message):
    response = _error(401, message)
    response.headers["WWW-Authenticate"] = 'Bearer realm="hosting-api"'
    return response


def _bearer_token():
    """Return the credential from ``Authorization: Bearer``, or ``None``."""
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def _rate_limited(token_id, bucket, limit):
    """Return a 429 response once *token_id* has used up *bucket*, else ``None``."""
    key = f"api-token:{token_id}"
    if check_and_record_hosting_rate_limit(key, bucket, limit, _RATE_WINDOW):
        return None
    retry_after = get_hosting_rate_limit_retry_after(key, bucket, _RATE_WINDOW)
    response = _error(429, "Too many requests. Wait before trying again.", retry_after=retry_after)
    response.headers["Retry-After"] = str(retry_after)
    return response


def _account_refusal(account):
    """Return a 403 response if *account* may not use the API now, else ``None``.

    These are the gates ``hosting_login_required`` applies to the dashboard.
    Where the portal would redirect to a page explaining the problem, the API
    says what it is and leaves fixing it to the portal.
    """
    if is_account_suspended(account["id"]):
        return _error(403, "This account is suspended.")
    if account.get("pending_deletion"):
        return _error(403, "This account is scheduled for deletion.")
    if account.get("email_flagged_invalid"):
        return _error(403, "An administrator has asked for a new contact email. Sign in to the portal to provide it.")
    if (_email_policy_active() and _email_verification_required() and account.get("email")
            and not account.get("email_verified_at")):
        return _error(403, "Verify the contact email of this account in the portal first.")
    if account.get("approval_status", "approved") != "approved":
        return _error(403, "This account has not been approved.")
    return None


def _token_required(scope):
    """Authenticate the request with a bearer token that carries *scope*.

    The view receives the account and token rows before its URL arguments.
    Every check runs again on every request, so revoking a token, suspending
    the account or demoting an administrator takes effect on the next call.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            raw_token = _bearer_token()
            if raw_token is None:
                return _unauthorized(_MISSING_TOKEN)
            token = get_active_api_token(raw_token)
            if token is None:
                return _unauthorized(_BAD_TOKEN)
            limited = _rate_limited(token["id"], _REQUEST_BUCKET, _REQUEST_LIMIT)
            if limited is not None:
                return limited
            account = get_account_by_id(token["account_id"])
            if account is None or account.get("deleted_at"):
                return _unauthorized(_BAD_TOKEN)
            # A timed suspension that has run out is lifted here, as the
            # dashboard would, rather than refusing the call.
            if account["suspended"] and check_account_suspension_expired(account["id"]):
                account = get_account_by_id(account["id"])
            refusal = _account_refusal(account)
            if refusal is not None:
                return refusal
            if scope not in parse_api_token_scopes(token["scopes"]):
                return _error(403, f"This token does not have the {scope} scope.")
            if scope in ADMIN_API_SCOPES and not account["is_admin"]:
                return _error(403, "This account is not a platform administrator.")
            touch_api_token(token["id"])
            return view(account, token, *args, **kwargs)
        return wrapper
    return decorator


def _owner_is_admin(inst, account, owners):
    """Return whether the owner of *inst* is a platform administrator.

    Administrators' wikis have no storage cap, so the answer changes the
    reported limit. *owners* caches lookups across one response.
    """
    owner_id = inst["account_id"]
    if owner_id == account["id"]:
        return bool(account["is_admin"])
    if owner_id not in owners:
        owner = get_account_by_id(owner_id)
        owners[owner_id] = bool(owner and owner["is_admin"])
    return owners[owner_id]


def _instance_json(inst, role, owner_is_admin):
    """Serialize an instance for the API from an explicit list of fields.

    Credentials, ports, container details, data paths and other people's
    details never leave through here, whatever the row happens to contain.
    """
    try:
        used_bytes = get_storage_bytes(_instance_dir_for(inst))
    except Exception:
        logger.warning("Could not measure storage for instance %s", inst["id"], exc_info=True)
        used_bytes = None
    limit_mb = _instance_storage_limit_mb({**dict(inst), "account_is_admin": owner_is_admin})
    return {
        "id": inst["id"],
        "subdomain": inst["subdomain"],
        "url": _instance_url(inst) or None,
        "status": inst["status"],
        "role": role,
        "created_at": inst["created_at"],
        "expires_at": inst["expires_at"],
        "storage_used_bytes": used_bytes,
        "storage_limit_bytes": int(limit_mb * 1024 * 1024) if limit_mb is not None else None,
    }


def _find_instance(account, instance_id):
    """Return ``(instance, role)`` for a wiki the account owns or collaborates on.

    Anything else, including terminated wikis and ids that do not exist,
    returns ``(None, None)`` so the caller answers 404 either way. Tokens
    act for the account as a customer: an administrator's token does not
    reach other people's wikis through this API.
    """
    if not instance_id or len(instance_id) > _INSTANCE_ID_MAX_LENGTH:
        return None, None
    inst = get_instance(instance_id)
    if inst is None or inst["status"] == "terminated":
        return None, None
    if inst["account_id"] == account["id"]:
        return inst, "owner"
    if is_collaborator(inst["id"], account["id"]):
        return inst, "collaborator"
    return None, None


def _token_json(token):
    return {
        "id": token["id"],
        "name": token["name"],
        "prefix": token["prefix"],
        "scopes": parse_api_token_scopes(token["scopes"]),
        "created_at": token["created_at"],
        "expires_at": token["expires_at"],
    }


def register_hosting_api_routes(app):
    """Register the ``/api/v1`` blueprint and the Account page token forms."""
    api = Blueprint("hosting_api", __name__, url_prefix=API_PREFIX)

    @api.before_request
    def require_enabled_api():
        """Answer 404 for every API route until an administrator switches the API on."""
        if not get_hosting_api_enabled():
            return api_disabled_error()
        return None

    @api.after_request
    def forbid_caching(response):
        """Keep account data out of browser and proxy caches."""
        response.headers["Cache-Control"] = "no-store"
        return response

    @api.errorhandler(500)
    def api_internal_error(error):
        """Log the failure like the portal's own handler, but answer in JSON."""
        request_id = getattr(g, "_request_id", None)
        logger.exception(
            "Hosting API 500 [request_id=%s, path=%s, method=%s]",
            request_id, request.path, request.method,
        )
        try:
            record_http_error("hosting_portal", 500, route=request.path)
        except Exception:
            pass
        return _error(500, "Internal error.", request_id=request_id)

    @api.errorhandler(sqlite3.DatabaseError)
    def api_database_error(error):
        """Answer database failures in JSON instead of the portal's text or HTML."""
        if not sqlite_runtime.is_unavailable(error):
            return api_internal_error(error)
        logger.error("Platform database unavailable during an API request", exc_info=error)
        response = _error(503, "The service is temporarily unavailable. Try again later.", retry_after=30)
        response.headers["Retry-After"] = "30"
        return response

    @api.get("/status")
    def api_status():
        """Tell a client the API is available. Needs no token."""
        return jsonify(api="v1")

    @api.get("/me")
    @_token_required("account:read")
    def api_me(account, token):
        return jsonify(
            account={
                "id": account["id"],
                "username": account["username"],
                "email": account.get("email") or "",
                "is_admin": bool(account["is_admin"]),
                "created_at": account["created_at"],
            },
            token=_token_json(token),
        )

    @api.get("/instances")
    @_token_required("instances:read")
    def api_list_instances(account, token):
        owners = {}
        instances = [
            _instance_json(inst, "owner", bool(account["is_admin"]))
            for inst in get_instances_for_account(account["id"])
        ]
        # Collaborators see a wiki here only with the "view" permission, the
        # same permission the dashboard needs to open its detail page.
        for inst in get_shared_instances_for_account(account["id"]):
            if _can_access_instance(inst, account["id"], False, "view"):
                instances.append(
                    _instance_json(inst, "collaborator", _owner_is_admin(inst, account, owners))
                )
        return jsonify(instances=instances)

    @api.get("/instances/<instance_id>")
    @_token_required("instances:read")
    def api_get_instance(account, token, instance_id):
        inst, role = _find_instance(account, instance_id)
        if inst is None or not _can_access_instance(inst, account["id"], False, "view"):
            return _error(404, _NOT_FOUND)
        return jsonify(instance=_instance_json(inst, role, _owner_is_admin(inst, account, {})))

    def _change_instance_state(account, token, instance_id, action):
        """Pause or resume a wiki with the dashboard's own checks and functions."""
        limited = _rate_limited(token["id"], _INSTANCE_ACTION_BUCKET, _INSTANCE_ACTION_LIMIT)
        if limited is not None:
            return limited
        inst, role = _find_instance(account, instance_id)
        if inst is None:
            return _error(404, _NOT_FOUND)
        # is_admin is False on purpose: see _find_instance.
        if not _can_access_instance(inst, account["id"], False, "start_stop"):
            return _error(403, "Your collaborator permissions on this wiki do not include pausing and resuming it.")
        if _owner_is_locked_out(inst, account["id"], False):
            return _error(403, _SUSPENDED_LOCKOUT_MESSAGE)
        if action == "resume" and inst["suspended_at"]:
            return _error(403, _SUSPENDED_LOCKOUT_MESSAGE)
        if action == "pause":
            required, change, done = "running", stop_instance, "paused"
            wrong_state = "Only a running wiki can be paused."
        else:
            required, change, done = "stopped", restart_instance, "resumed"
            wrong_state = "Only a paused wiki can be resumed."
        if inst["status"] != required:
            return _error(409, wrong_state, status=inst["status"])
        ok, reason = change(inst["id"])
        current = get_instance(inst["id"])
        if not ok:
            if current is None or current["status"] != required:
                # Something else changed the wiki between the check and the call.
                return _error(409, wrong_state, status=current["status"] if current else None)
            logger.warning("API %s of instance %s failed: %s", action, inst["id"], reason)
            return _error(500, reason or f"The wiki could not be {done}.")
        record_api_instance_action(inst["id"], done, account["id"], token["id"], token["prefix"])
        return jsonify(instance=_instance_json(current or inst, role, _owner_is_admin(inst, account, {})))

    @api.post("/instances/<instance_id>/pause")
    @_token_required("instances:manage")
    def api_pause_instance(account, token, instance_id):
        return _change_instance_state(account, token, instance_id, "pause")

    @api.post("/instances/<instance_id>/resume")
    @_token_required("instances:manage")
    def api_resume_instance(account, token, instance_id):
        return _change_instance_state(account, token, instance_id, "resume")

    @api.get("/admin/pending-accounts")
    @_token_required("admin:read")
    def api_admin_pending_accounts(account, token):
        return jsonify(accounts=[
            {
                "id": row["id"],
                "username": row["username"],
                "email": row["email"] or "",
                "email_verified": bool(row["email_verified_at"]),
                "created_at": row["created_at"],
                "use_case": row["signup_use_case"] or "",
            }
            for row in get_pending_accounts()
        ])

    @api.get("/admin/instances")
    @_token_required("admin:read")
    def api_admin_instances(account, token):
        counts = Counter(row["status"] for row in get_all_instances())
        return jsonify(
            total=sum(counts.values()),
            by_status={status: counts.get(status, 0) for status in _INSTANCE_STATUSES},
        )

    app.register_blueprint(api)
    # Bearer tokens are not sent automatically by a browser, so a forged
    # cross-site request carries no credential for these views to accept.
    csrf = app.extensions.get("csrf")
    if csrf is not None:
        csrf.exempt(api)

    def _back_to_tokens():
        return redirect(url_for("hosting_account") + "#api-tokens")

    @app.route("/account/api-tokens", methods=["GET"])
    @hosting_login_required
    def hosting_api_tokens_page():
        """Send a reload or a Referer-based redirect of the form's URL to the Account page."""
        return _back_to_tokens()

    @app.route("/account/api-tokens", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_create_api_token():
        """Create a token and show it on the Account page, once."""
        if not get_hosting_api_enabled():
            abort(404)
        # An administrator looking at the portal as a customer must not be
        # able to leave behind a credential that outlives the impersonation.
        if session.get("hosting_impersonator_account_id"):
            flash(translate("hosting.account.api_tokens.impersonation"), "error")
            return _back_to_tokens()
        # Each rendering of the form carries a nonce that is used up by the
        # first token it creates, so reloading the page that shows the token
        # sends the form again without creating a second one.
        expected_nonce = session.get(API_TOKEN_FORM_NONCE_KEY) or ""
        sent_nonce = request.form.get("form_nonce") or ""
        if not expected_nonce or not hmac.compare_digest(expected_nonce, sent_nonce):
            flash(translate("hosting.account.api_tokens.form_used"), "info")
            return _back_to_tokens()
        account = get_account_by_id(session["hosting_account_id"])
        password = request.form.get("current_password") or ""
        if len(password) > _PASSWORD_MAX_LENGTH or not check_password_hash(account["password"], password):
            flash(translate("hosting.account.api_tokens.wrong_password"), "error")
            return _back_to_tokens()
        name = (request.form.get("name") or "").strip()
        if not 1 <= len(name) <= API_TOKEN_NAME_MAX_LENGTH or not name.isprintable():
            flash(translate("hosting.account.api_tokens.invalid_name", max=API_TOKEN_NAME_MAX_LENGTH), "error")
            return _back_to_tokens()
        scopes = request.form.getlist("scopes")
        offered = api_scopes_for_account(account)
        if not scopes or any(scope not in offered for scope in scopes):
            flash(translate("hosting.account.api_tokens.invalid_scopes"), "error")
            return _back_to_tokens()
        expires_in = request.form.get("expires_in") or ""
        if expires_in not in _EXPIRY_CHOICES:
            flash(translate("hosting.account.api_tokens.invalid_expiry"), "error")
            return _back_to_tokens()
        days = _EXPIRY_CHOICES[expires_in]
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None
        session.pop(API_TOKEN_FORM_NONCE_KEY, None)
        try:
            _token_id, raw_token = create_api_token(
                account["id"], name, scopes, expires_at,
                expected_session_version=int(account.get("session_version") or 0),
            )
        except ValueError as error:
            if str(error) == "api_token_stale":
                # The password changed, or every session was ended, after the
                # check above. The token would have outlived that, so none is
                # made and this session ends as well.
                session.clear()
                flash(translate("hosting.account.api_tokens.stale"), "info")
                return _redirect_to_login_with_next()
            if str(error) == "api_token_limit":
                flash(translate("hosting.account.api_tokens.limit_reached", max=MAX_ACTIVE_API_TOKENS), "error")
            elif str(error) == "api_token_scopes":
                flash(translate("hosting.account.api_tokens.invalid_scopes"), "error")
            else:
                flash(translate("hosting.account.api_tokens.invalid_name", max=API_TOKEN_NAME_MAX_LENGTH), "error")
            return _back_to_tokens()
        # Rendered from this response rather than after a redirect, so the
        # token is never written to the session or any other storage.
        response = make_response(
            render_account_page(account, get_hosting_settings(), new_api_token=raw_token)
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/account/api-tokens/<int:token_id>/revoke", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_revoke_api_token(token_id):
        """Revoke one of the account's tokens.

        Revoking only takes access away, so it stays possible while the API
        is switched off and for an administrator impersonating the account,
        who is then recorded as the one who revoked it.
        """
        account_id = session["hosting_account_id"]
        actor_id = session.get("hosting_impersonator_account_id") or account_id
        if not revoke_api_token(account_id, token_id, actor_id):
            abort(404)
        flash(translate("hosting.account.api_tokens.revoked"), "success")
        return _back_to_tokens()
