"""Authentication, login sessions and access control.

Sessions
--------
Every sign-in creates a ``user_sessions`` row holding the SHA-256 of a random
bearer token; the signed Flask cookie carries the raw token (``auth_session_token``)
and the user id. A request is authenticated only when both agree with an
active, unexpired, unrevoked row, so revoking a row (logout elsewhere, password
change, suspension, mass logout) ends that session immediately. Cookies from
1.4 that carry this token keep working after the upgrade; older cookies
without one are discarded.

Access
------
Views are private by default. Mark exceptions with decorators:

* :func:`public` - reachable by anyone (sign-in page, health checks, custom pages).
* :func:`public_read` - reachable anonymously while public mode is on.
* :func:`exempt` - skip one of the global gates (setup, maintenance,
  forced account steps), for the pages that resolve those states.

and require more with :func:`role_required` / :func:`permission_required`.
"""

from __future__ import annotations

import functools
import secrets
from collections.abc import Callable
from typing import Any

from flask import abort, current_app, flash, g, jsonify, redirect, request, session, url_for

from ..core import crypto
from ..core.timeutil import is_past, now_sql, parse, sql_in, utcnow
from ..core.web import client_ip
from . import permissions as perms
from . import settings
from .db import db
from .i18n import t

SESSION_TOKEN_KEY = "auth_session_token"
SESSION_CONFLICT_KEY = "_session_conflict"
TOUCH_INTERVAL_SECONDS = 300

# View attributes set by the decorators below.
_ACCESS_ATTR = "_bw_access"
_EXEMPT_ATTR = "_bw_exempt"

GATES = frozenset({"setup", "maintenance", "account_steps", "approval"})


def public(view: Callable) -> Callable:
    setattr(view, _ACCESS_ATTR, "public")
    return view


def stateless(view: Callable) -> Callable:
    """Public view that needs neither the database nor the session (health checks, robots.txt)."""
    setattr(view, _ACCESS_ATTR, "public")
    view._bw_stateless = True
    return view


def view_stateless(view: Callable | None) -> bool:
    return bool(getattr(view, "_bw_stateless", False))


def public_read(view: Callable) -> Callable:
    setattr(view, _ACCESS_ATTR, "public_read")
    return view


def exempt(*gates: str) -> Callable[[Callable], Callable]:
    unknown = set(gates) - GATES
    if unknown:
        raise ValueError(f"Unknown gates: {unknown}")

    def decorate(view: Callable) -> Callable:
        setattr(view, _EXEMPT_ATTR, frozenset(getattr(view, _EXEMPT_ATTR, frozenset())) | set(gates))
        return view

    return decorate


def view_access(view: Callable | None) -> str:
    return getattr(view, _ACCESS_ATTR, "login") if view else "login"


def view_exempt(view: Callable | None, gate: str) -> bool:
    return bool(view) and gate in getattr(view, _EXEMPT_ATTR, frozenset())


# ── Current user ──────────────────────────────────────────────────────────────


def _load_user(user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    return db.one("SELECT * FROM users WHERE id = ?", (str(user_id),))


def load_request_user() -> None:
    """Resolve the session cookie into ``g.user`` (called once per request)."""
    g.user = None
    g.real_user = None
    g.auth_session = None
    raw = session.get(SESSION_TOKEN_KEY)
    if not raw:
        if session.get("user_id"):
            session.clear()  # 1.4 cookie without a server-side session
        return
    row = db.one(
        "SELECT * FROM user_sessions WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?",
        (crypto.sha256_hex(raw), now_sql()),
    )
    authenticated_id = session.get("impersonator_id") or session.get("user_id")
    if row is None or row["user_id"] != authenticated_id:
        conflict = row is None and _ended_by_session_limit(raw, authenticated_id)
        session.clear()
        if conflict:
            # Another sign-in ended this session: explain that instead of "expired".
            session[SESSION_CONFLICT_KEY] = authenticated_id
            g.session_conflict = True
        else:
            g.session_ended = True
        return
    real = _load_user(row["user_id"])
    if real is None:
        session.clear()
        g.session_ended = True
        return
    g.auth_session = row
    g.real_user = real
    if session.get("impersonator_id"):
        target = _load_user(session.get("user_id"))
        if not can_impersonate(real, target):
            db.execute(
                "UPDATE impersonation_logs SET ended_at = ? "
                "WHERE admin_id = ? AND target_user_id = ? AND ended_at IS NULL",
                (now_sql(), real["id"], session.get("user_id")),
            )
            session.pop("impersonator_id", None)
            session["user_id"] = real["id"]
            target = real
        g.user = target
    else:
        g.user = real
    last_seen = parse(row["last_seen_at"])
    if last_seen is None or (utcnow() - last_seen).total_seconds() >= TOUCH_INTERVAL_SECONDS:
        db.execute(
            "UPDATE user_sessions SET last_seen_at = ?, last_ip = ? WHERE id = ?",
            (now_sql(), client_ip(), row["id"]),
        )


def _ended_by_session_limit(raw: str, user_id: str | None) -> bool:
    return bool(user_id) and bool(db.scalar(
        "SELECT 1 FROM user_sessions WHERE token_hash = ? AND user_id = ? AND revoked_reason = 'session_limit'",
        (crypto.sha256_hex(raw), user_id),
    ))


def current_user() -> dict[str, Any] | None:
    return g.get("user")


def real_user() -> dict[str, Any] | None:
    """The signed-in account even while it impersonates someone else."""
    return g.get("real_user")


def is_impersonating() -> bool:
    return bool(session.get("impersonator_id")) and g.get("real_user") is not None


def refresh_current_user() -> None:
    user = g.get("user")
    if user:
        g.user = _load_user(user["id"])
        g.pop("_grants", None)


# ── Sign-in and sign-out ──────────────────────────────────────────────────────


def start_session(user: dict[str, Any], *, remember: bool = False, method: str = "password") -> str:
    """Create a login session for *user* and bind it to the cookie."""
    cfg = current_app.config["BW"]
    raw = secrets.token_urlsafe(32)
    session_id = secrets.token_urlsafe(32)
    now = now_sql()
    days = cfg.remember_me_days if remember else cfg.session_days
    user_agent = (request.headers.get("User-Agent") or "")[:500]
    with db.transaction():
        current = _load_user(user["id"])
        # Hashing happens outside this write lock. A password reset may have
        # revoked every session meanwhile; never recreate one from the old
        # verified credentials after that revocation has committed.
        if current is None or current["password"] != user["password"]:
            abort(401, description=t("auth.error.invalid_credentials"))
        user = current
        if settings.get("session_limit_enabled"):
            # One active session per account: end the others.
            db.execute(
                "UPDATE user_sessions SET revoked_at = ?, revoked_reason = 'session_limit' "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user["id"]),
            )
        db.execute(
            "INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at, remember_me, "
            "auth_method, ip_address, last_ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, crypto.sha256_hex(raw), user["id"], now, now, sql_in(days=days), 1 if remember else 0,
             method[:40], client_ip(), client_ip(), user_agent),
        )
        db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
    language = session.get("interface_language")
    session.clear()
    if language:
        session["interface_language"] = language
    session[SESSION_TOKEN_KEY] = raw
    session["user_id"] = user["id"]
    # Without "remember me" the cookie ends with the browser session; the
    # server-side row still expires after session_days either way.
    session.permanent = bool(remember)
    session["_remember_me"] = bool(remember)
    g.user = g.real_user = _load_user(user["id"])
    return session_id


def end_session() -> None:
    raw = session.get(SESSION_TOKEN_KEY)
    if raw:
        db.execute(
            "UPDATE user_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (now_sql(), crypto.sha256_hex(raw)),
        )
    language = session.get("interface_language")
    session.clear()
    if language:
        session["interface_language"] = language
    g.user = g.real_user = None


def revoke_sessions(user_id: str, *, except_session_id: str | None = None) -> int:
    """End every session of *user_id* (password change, suspension, admin action)."""
    params: list[Any] = [now_sql(), user_id]
    extra = ""
    if except_session_id:
        extra = " AND id != ?"
        params.append(except_session_id)
    cursor = db.execute(
        f"UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL{extra}", params
    )
    return cursor.rowcount


def revoke_all_sessions(*, except_user_id: str | None = None) -> int:
    params: list[Any] = [now_sql()]
    extra = ""
    if except_user_id:
        extra = " AND user_id != ?"
        params.append(except_user_id)
    return db.execute(f"UPDATE user_sessions SET revoked_at = ? WHERE revoked_at IS NULL{extra}", params).rowcount


def current_session_id() -> str | None:
    row = g.get("auth_session")
    return row["id"] if row else None


def start_impersonation(target: dict[str, Any]) -> None:
    admin = real_user()
    if admin is None:
        abort(403)
    session["impersonator_id"] = admin["id"]
    session["user_id"] = target["id"]
    g.user = target
    g.pop("_grants", None)


def stop_impersonation() -> None:
    impersonator = session.pop("impersonator_id", None)
    if impersonator:
        session["user_id"] = impersonator
        g.user = g.real_user
        g.pop("_grants", None)


# ── Account state ─────────────────────────────────────────────────────────────


def account_block(user: dict[str, Any]) -> str | None:
    """Why *user* may not use the wiki right now: 'suspended', 'pending', 'denied' or None."""
    if user.get("suspended"):
        until = user.get("suspended_until")
        if until and is_past(until):
            db.execute(
                "UPDATE users SET suspended = 0, suspended_until = NULL WHERE id = ? AND suspended = 1",
                (user["id"],),
            )
            user["suspended"] = 0
        else:
            return "suspended"
    status = user.get("approval_status") or "approved"
    if status in ("pending", "denied"):
        return status
    return None


# ── Permission checks ────────────────────────────────────────────────────────


def grants(user: dict[str, Any] | None = None) -> perms.Grants | None:
    user = current_user() if user is None else user
    if not user:
        return None
    if user is g.get("user"):
        cached = g.get("_grants")
        if cached is not None:
            return cached
        result = perms.load_grants(db, user)
        g._grants = result
        return result
    return perms.load_grants(db, user)


def has_permission(key: str, user: dict[str, Any] | None = None) -> bool:
    from .registry import is_enabled

    resolved = grants(user)
    if resolved is None:
        return False
    return perms.grants_permission(resolved, key, is_enabled)


def has_role(minimum: str, user: dict[str, Any] | None = None) -> bool:
    user = current_user() if user is None else user
    return bool(user) and perms.role_at_least(user.get("role"), minimum)


def is_admin(user: dict[str, Any] | None = None) -> bool:
    return has_role("admin", user)


def can_impersonate(actor: dict[str, Any], target: dict[str, Any] | None) -> bool:
    """Whether impersonation remains allowed with the accounts' current state.

    Rechecked on every request: promotion of the target or removal of the
    actor's superuser status must not leave an existing privileged session.
    """
    if target is None or actor["id"] == target["id"] or not is_admin(actor) or account_block(actor):
        return False
    if (is_admin(target) or target.get("is_superuser")) and not actor.get("is_superuser"):
        return False
    return account_block(target) is None


def can_read_category(category_id: int | None, user: dict[str, Any] | None = None) -> bool:
    resolved = grants(user)
    if resolved is None:
        return settings.public_mode_active()
    return perms.can_read_category(resolved, category_id)


def can_write_category(category_id: int | None, user: dict[str, Any] | None = None) -> bool:
    resolved = grants(user)
    return resolved is not None and perms.can_write_category(resolved, category_id)


def minimum_role_allows(setting_value: str | None, user: dict[str, Any] | None = None) -> bool:
    """For ``*_access`` settings holding a minimum role name ('user'/'editor'/'admin')."""
    minimum = setting_value if setting_value in perms.ROLE_RANK else "admin"
    return has_role(minimum, user)


# ── Decorators ────────────────────────────────────────────────────────────────


def wants_json() -> bool:
    return request.path.startswith("/api/") or (
        request.accept_mimetypes.best == "application/json"
    ) or request.is_json


def deny(status: int = 403, message_key: str = "error.forbidden"):
    """Refuse the request in the right format."""
    if wants_json():
        return jsonify({"error": t(message_key)}), status
    abort(status)


def role_required(minimum: str) -> Callable[[Callable], Callable]:
    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            if current_user() is None:
                return redirect_to_login()
            if not has_role(minimum):
                return deny()
            return view(*args, **kwargs)

        return wrapper

    return decorate


admin_required = role_required("admin")
editor_required = role_required("editor")


def permission_required(*keys: str) -> Callable[[Callable], Callable]:
    """Require every permission in *keys*."""

    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            if current_user() is None:
                return redirect_to_login()
            if not all(has_permission(key) for key in keys):
                return deny()
            return view(*args, **kwargs)

        return wrapper

    return decorate


def redirect_to_login():
    if wants_json():
        return jsonify({"error": t("auth.error.login_required")}), 401
    target = request.full_path.rstrip("?") if request.method == "GET" else None
    endpoint = "auth.session_conflict" if g.get("session_conflict") else "auth.login"
    if target in ("/", None):
        return redirect(url_for(endpoint))
    return redirect(url_for(endpoint, next=target))


def flash_t(key: str, category: str = "info", **values: Any) -> None:
    flash(t(key, **values), category)
