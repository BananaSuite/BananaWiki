"""The REST API under ``/api/v1``.

Every call except ``/status`` and ``/openapi.json`` carries
``Authorization: Bearer <token>``. The request pipeline skips cookie sessions
and CSRF for such calls; :func:`authenticate` below resolves the token, makes
its owner the current user (``g.user``) for the request, and applies the
same account gates the web interface applies (suspension, approval,
forced password change or onboarding, maintenance). Each view then checks
the token's scope and read/write flag and the owner's permissions through
the shared services, so the API can never do more than the owner could do
in the browser.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from flask import current_app, g, jsonify, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from .....core.ratelimit import SqlLimiter
from .....core.timeutil import parse, sql_in, utcnow
from .....core.web import client_ip
from .... import auth, registry, settings
from ....db import db
from .. import audit, idempotency, tokens
from ..errors import MAX_JSON_BODY, ApiError

bp = registry.feature_blueprint("api_service", "api_v1", __name__, url_prefix="/api/v1")

RATE_WINDOW = 60
RATE_BUCKET = "api_service"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def requires(scope: str, *, write: bool = False, feature: str | None = None) -> Callable[[Callable], Callable]:
    """Mark a view as needing *scope* (and write access) on the calling token.

    With *feature*, the endpoint answers 404 while that feature is switched off.
    """

    def decorate(view: Callable) -> Callable:
        view._api_scope = (scope, write)  # type: ignore[attr-defined]
        view._api_feature = feature  # type: ignore[attr-defined]
        return auth.public(view)  # the token, not the session, authorises the call

    return decorate


def open_endpoint(view: Callable) -> Callable:
    """An endpoint that needs no token (status, OpenAPI description)."""
    view._api_open = True  # type: ignore[attr-defined]
    return auth.exempt("setup", "maintenance", "account_steps", "approval")(auth.public(view))


def caller() -> dict[str, Any]:
    return g.user


def caller_token() -> dict[str, Any]:
    return g.api_token


def require_admin() -> None:
    if not tokens.is_admin_role(caller()):
        raise ApiError(403, "admin_required")


def require_editor() -> None:
    if not auth.has_role("editor", caller()):
        raise ApiError(403, "editor_required")


def _origin_rejected() -> bool:
    """Browsers send Origin; refuse other sites so a page cannot script the API with a stolen token."""
    origin = request.headers.get("Origin")
    if origin is None:
        return False
    try:
        actual, expected = urlsplit(origin), urlsplit(request.host_url)
        if actual.scheme not in ("http", "https") or not actual.hostname or actual.path not in ("", "/"):
            return True
        default = {"https": 443, "http": 80}
        return ((actual.scheme, actual.hostname, actual.port or default[actual.scheme])
                != (expected.scheme, expected.hostname, expected.port or default.get(expected.scheme, 80)))
    except ValueError:
        return True


def _account_gate(user: dict[str, Any]) -> ApiError | None:
    block = auth.account_block(user)
    if block:
        return ApiError(403, "account_blocked", state=block)
    if user.get("force_password_change"):
        return ApiError(403, "password_change_required")
    if user.get("onboarding_required"):
        return ApiError(403, "onboarding_required")
    if settings.maintenance_active() and not tokens.is_admin_role(user):
        return ApiError(503, "maintenance")
    return None


def _rate_limit(user: dict[str, Any]) -> bool:
    """Count the call against the account's per-minute budget; return whether it may proceed.

    The limit, what is left and when the oldest counted call leaves the
    window are kept for the ``X-RateLimit-*`` headers of the answer.
    """
    limits = tokens.service_settings()
    limit = limits["admin_rate_limit"] if tokens.is_admin_role(user) else limits["rate_limit"]
    key = f"user:{user['id']}"
    allowed = SqlLimiter(db.session).hit(key, RATE_BUCKET, limit, RATE_WINDOW)
    window = db.one("SELECT COUNT(*) AS used, MIN(hit_at) AS oldest FROM rate_limit_hits "
                    "WHERE ip = ? AND bucket = ? AND hit_at > ?", (key, RATE_BUCKET, sql_in(seconds=-RATE_WINDOW)))
    oldest = parse(window["oldest"]) if window and window["oldest"] else None
    reset = RATE_WINDOW
    if oldest is not None:
        reset = max(1, min(RATE_WINDOW, int(RATE_WINDOW - (utcnow() - oldest).total_seconds()) + 1))
    g.api_rate = (limit, max(0, limit - int(window["used"] if window else 0)), reset)
    return allowed


def _rate_headers(response) -> None:
    state = g.get("api_rate")
    if state is not None:
        response.headers["X-RateLimit-Limit"] = str(state[0])
        response.headers["X-RateLimit-Remaining"] = str(state[1])
        response.headers["X-RateLimit-Reset"] = str(state[2])


def _idempotency(user: dict[str, Any]):
    """Reserve or replay an ``Idempotency-Key`` sent with a POST; None lets the view run."""
    key = request.headers.get(idempotency.HEADER)
    if key is None or request.method != "POST":
        return None
    if not idempotency.valid_key(key):
        return ApiError(400, "idempotency_key_invalid").response()
    if request.mimetype == "multipart/form-data":
        return ApiError(400, "idempotency_unsupported").response()
    if request.content_length is not None and request.content_length > MAX_JSON_BODY:
        return ApiError(413, "body_too_large").response()
    request.max_content_length = MAX_JSON_BODY
    try:
        body = request.get_data(cache=True)
    except RequestEntityTooLarge:
        return ApiError(413, "body_too_large").response()
    fingerprint = idempotency.request_hash(request.method, request.path, request.query_string, body)
    try:
        reservation, stored = idempotency.begin(user["id"], key, fingerprint)
    except ApiError as error:
        return error.response()
    if stored is not None:
        replay = current_app.response_class(stored["response_body"] or "", status=stored["status_code"],
                                            mimetype="application/json")
        replay.headers[idempotency.REPLAY_HEADER] = "true"
        return replay
    g.api_idempotency = reservation
    return None


def request_etag() -> list[str] | None:
    """The entity tags of an ``If-Match`` header (``["*"]`` for any), or None without one."""
    header = request.headers.get("If-Match")
    if header is None:
        return None
    tags = [part.strip().removeprefix("W/").strip('"') for part in header.split(",") if part.strip()]
    return tags or [""]


def etag_value(prefix: str, number: int) -> str:
    return f'"{prefix}{int(number)}"'


@bp.before_request
def authenticate():
    g.api_started = time.perf_counter()
    view = current_app.view_functions.get(request.endpoint or "")
    if getattr(view, "_api_open", False):
        return None
    if _origin_rejected():
        return ApiError(403, "cross_origin").response()
    if not tokens.service_settings()["enabled"]:
        return ApiError(503, "api_disabled").response()
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer ") or not header[7:].strip():
        return ApiError(401, "missing_token").response()
    found = tokens.authenticate(header[7:].strip())
    if found is None:
        return ApiError(401, "invalid_token").response()
    token, user = found
    g.user = g.real_user = user
    g.pop("_grants", None)
    g.api_token = token
    if not tokens.has_api_access(user):
        return ApiError(403, "api_access_disabled").response()
    blocked = _account_gate(user)
    if blocked is not None:
        return blocked.response()
    scope = getattr(view, "_api_scope", None)
    if scope is None:
        return ApiError(404, "not_found").response()
    if not tokens.parse_grant(token["permissions"]).allows(scope[0], write=scope[1]):
        return ApiError(403, "scope_missing", extra={"scope": scope[0], "write": scope[1]},
                        scope=scope[0]).response()
    feature = getattr(view, "_api_feature", None)
    if feature and not registry.is_enabled(feature):
        return ApiError(404, "not_found").response()
    tokens.touch(token)
    if not _rate_limit(user):
        response = ApiError(429, "rate_limited").response()
        response[0].headers["Retry-After"] = str(g.api_rate[2])
        return response
    return _idempotency(user)


@bp.after_request
def record_call(response):
    token = g.get("api_token")
    if token is not None and g.get("user") is not None:
        audit.record(
            token_id=token["id"], user=g.user, endpoint=request.path, method=request.method,
            status=response.status_code, ip=client_ip(),
            body=g.get("api_body") if request.method in WRITE_METHODS else None,
            duration_ms=int((time.perf_counter() - g.get("api_started", time.perf_counter())) * 1000),
        )
    reservation = g.pop("api_idempotency", None)
    if reservation is not None:
        idempotency.finish(reservation, response.status_code,
                           b"" if response.direct_passthrough else response.get_data())
    _rate_headers(response)
    # Interceptors written for the browser may flash messages; a bearer call has no session to keep them.
    if token is not None and session.modified:
        session.clear()
        session.modified = False
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.errorhandler(ApiError)
def api_error(error: ApiError):
    return error.response()


def ok(status: int = 200, **payload: Any):
    return jsonify({"ok": True, **payload}), status


from . import (  # noqa: E402,F401
    attachments_api,
    canvas_api,
    categories,
    kanban_api,
    meta,
    pages,
    settings_api,
    tokens_api,
    userbot_api,
    users,
    webhooks_api,
)
