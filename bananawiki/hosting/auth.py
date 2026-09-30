"""Login sessions, the current account and access decorators.

Sessions
--------
A sign-in creates a ``hosting_account_sessions`` row holding the SHA-256 of a
random token; the signed ``bwh_session`` cookie carries the raw token
(``hosting_auth_session_token``) and the account id, under the same keys as
1.4, so portal sessions survive the upgrade. A request is authenticated only
when the row is active, unexpired and unrevoked, belongs to the (real)
account and carries the account's current ``session_version``: bumping that
counter (password change, two-step change, "sign out everywhere", merges)
ends every session at once.

Access
------
Views need a signed-in account unless marked :func:`public`. Administrator
views use :func:`admin_required`. Account states (scheduled deletion, flagged
email, missing contact email, unverified email, pending or denied approval)
redirect to the page that resolves them; views that must stay reachable in a
state are marked with :func:`exempt`.
"""

from __future__ import annotations

import functools
import secrets
from collections.abc import Callable
from typing import Any

from flask import abort, flash, g, jsonify, redirect, request, session, url_for

from ..core.crypto import sha256_hex
from ..core.timeutil import now_sql, parse, sql_in, utcnow
from ..core.web import client_ip
from . import accounts, settings
from .db import db
from .i18n import current_language, t

TOKEN_KEY = "hosting_auth_session_token"
ACCOUNT_KEY = "hosting_account_id"
VERSION_KEY = "hosting_session_version"
IMPERSONATOR_KEY = "hosting_impersonator_account_id"
IMPERSONATION_LOG_KEY = "hosting_impersonation_log_id"
SUSPENDED_KEY = "hosting_suspended_account_id"
LOGIN_BANNER_KEY = "hosting_attention_after_login"
SESSION_DAYS = 7
TOUCH_INTERVAL = 300
_KEEP = ("interface_language", "_csrf")

GATES = ("deletion", "email_flag", "contact_email", "verification", "approval", "admin_mfa")


def public(view: Callable) -> Callable:
    view._hosting_public = True
    return view


def is_public(view: Callable | None) -> bool:
    return bool(getattr(view, "_hosting_public", False))


def exempt(*gates: str) -> Callable[[Callable], Callable]:
    unknown = set(gates) - set(GATES)
    if unknown:
        raise ValueError(f"Unknown gates: {unknown}")

    def decorate(view: Callable) -> Callable:
        view._hosting_exempt = frozenset(getattr(view, "_hosting_exempt", frozenset())) | set(gates)
        return view

    return decorate


def is_exempt(view: Callable | None, gate: str) -> bool:
    return gate in getattr(view, "_hosting_exempt", frozenset())


# ── Current account ───────────────────────────────────────────────────────────


def current_account() -> dict[str, Any] | None:
    return g.get("account")


def real_account() -> dict[str, Any] | None:
    """The signed-in account, even while it impersonates someone."""
    return g.get("real_account")


def impersonating() -> bool:
    return bool(session.get(IMPERSONATOR_KEY)) and g.get("real_account") is not None


def _clear(keep_language: bool = True) -> None:
    kept = {key: session[key] for key in _KEEP if keep_language and key in session}
    session.clear()
    session.update(kept)


def load() -> None:
    """Resolve the cookie into ``g.account`` / ``g.real_account`` (once per request)."""
    g.account = g.real_account = g.auth_session = None
    raw = session.get(TOKEN_KEY)
    if not raw:
        if session.get(ACCOUNT_KEY):
            _clear()
        return
    row = db.one(
        "SELECT * FROM hosting_account_sessions WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?",
        (sha256_hex(str(raw)[:256]), now_sql()),
    )
    real_id = session.get(IMPERSONATOR_KEY) or session.get(ACCOUNT_KEY)
    real = accounts.get(row["account_id"]) if row else None
    if (row is None or real is None or row["account_id"] != real_id or real["deleted_at"]
            or int(row["session_version"]) != int(real["session_version"] or 0)):
        _clear()
        g.session_ended = True
        return
    if accounts.is_suspended(real):
        end_session()
        session[SUSPENDED_KEY] = real["id"]
        g.suspended_redirect = True
        return
    g.auth_session = row
    g.real_account = real
    if session.get(IMPERSONATOR_KEY):
        target = accounts.get(session.get(ACCOUNT_KEY))
        if target is None or not real["is_admin"] or target["deleted_at"]:
            session.pop(IMPERSONATOR_KEY, None)
            session[ACCOUNT_KEY] = real["id"]
            target = real
        g.account = target
    else:
        g.account = real
    seen = parse(row["last_seen_at"])
    if seen is None or (utcnow() - seen).total_seconds() >= TOUCH_INTERVAL:
        db.execute("UPDATE hosting_account_sessions SET last_seen_at = ?, last_ip = ? WHERE id = ?",
                   (now_sql(), client_ip()[:45], row["id"]))


def refresh() -> None:
    account = g.get("account")
    if account:
        g.account = accounts.get(account["id"])
    real = g.get("real_account")
    if real:
        g.real_account = accounts.get(real["id"])


# ── Sessions ──────────────────────────────────────────────────────────────────


def start_session(account: dict[str, Any], method: str = "password") -> str:
    """Create a login session, bind it to the cookie and return its row id."""
    raw = secrets.token_urlsafe(32)
    row_id = secrets.token_urlsafe(32)
    now = now_sql()
    address = client_ip()[:45]
    with db.transaction():
        db.execute("DELETE FROM hosting_account_sessions WHERE expires_at < ? OR revoked_at < ?",
                   (sql_in(days=-30), sql_in(days=-30)))
        db.insert("hosting_account_sessions", {
            "id": row_id, "token_hash": sha256_hex(raw), "account_id": account["id"],
            "session_version": int(account.get("session_version") or 0), "created_at": now, "last_seen_at": now,
            "expires_at": sql_in(days=SESSION_DAYS), "auth_method": method[:64], "ip_address": address,
            "last_ip": address, "user_agent": (request.headers.get("User-Agent") or "")[:512],
        })
    _clear()
    session.permanent = True
    session[TOKEN_KEY] = raw
    session[ACCOUNT_KEY] = account["id"]
    session[VERSION_KEY] = int(account.get("session_version") or 0)
    # The first page after signing in summarises what waits for an administrator.
    session[LOGIN_BANNER_KEY] = True
    accounts.set_language(account["id"], current_language())
    g.account = g.real_account = accounts.get(account["id"])
    g.auth_session = db.one("SELECT * FROM hosting_account_sessions WHERE id = ?", (row_id,))
    return row_id


def end_session() -> None:
    raw = session.get(TOKEN_KEY)
    if raw:
        db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                   (now_sql(), sha256_hex(str(raw)[:256])))
    accounts.log_impersonation_stop(session.get(IMPERSONATION_LOG_KEY))
    _clear()
    g.account = g.real_account = g.auth_session = None


def current_session_id() -> str | None:
    row = g.get("auth_session")
    return row["id"] if row else None


def keep_current_session(account_id: str, version: int) -> None:
    """After a password change: move this session to the new version, revoke the rest."""
    current = current_session_id()
    db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL "
               "AND id != ?", (now_sql(), account_id, current or ""))
    if current:
        db.execute("UPDATE hosting_account_sessions SET session_version = ?, revoked_at = NULL WHERE id = ?",
                   (version, current))
    session[VERSION_KEY] = version


def active_sessions(account_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT * FROM hosting_account_sessions WHERE account_id = ? AND revoked_at IS NULL AND expires_at > ? "
        "ORDER BY last_seen_at DESC", (account_id, now_sql()),
    )


def session_history(account_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT * FROM hosting_account_sessions WHERE account_id = ? AND (revoked_at IS NOT NULL OR expires_at <= ?) "
        "ORDER BY COALESCE(revoked_at, expires_at) DESC LIMIT 50", (account_id, now_sql()),
    )


def revoke_session(account_id: str, row_id: str) -> bool:
    return db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE account_id = ? AND id = ? "
                      "AND revoked_at IS NULL", (now_sql(), account_id, row_id)).rowcount == 1


def revoke_all(account_id: str) -> None:
    db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
               (now_sql(), account_id))


def clear_history(account_id: str) -> int:
    return db.execute("DELETE FROM hosting_account_sessions WHERE account_id = ? AND (revoked_at IS NOT NULL "
                      "OR expires_at <= ?)", (account_id, now_sql())).rowcount


# ── Impersonation ─────────────────────────────────────────────────────────────


def start_impersonation(target: dict[str, Any]) -> None:
    admin = real_account()
    if admin is None:
        abort(403)
    session[IMPERSONATOR_KEY] = admin["id"]
    session[IMPERSONATION_LOG_KEY] = accounts.log_impersonation_start(admin["id"], target["id"])
    session[ACCOUNT_KEY] = target["id"]
    g.account = target


def stop_impersonation() -> None:
    admin_id = session.pop(IMPERSONATOR_KEY, None)
    accounts.log_impersonation_stop(session.pop(IMPERSONATION_LOG_KEY, None))
    if admin_id:
        session[ACCOUNT_KEY] = admin_id
        g.account = g.real_account


# ── Gates and decorators ──────────────────────────────────────────────────────


def wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def contact_email_required(account: dict[str, Any]) -> bool:
    return bool(not account.get("email") and not account.get("email_flagged_invalid")
                and settings.flag("email_required") and settings.flag("ask_email_existing_users"))


def pending_gate(account: dict[str, Any], view: Callable | None) -> str | None:
    """The endpoint an account in a blocking state must go to, or None."""
    from . import notifications

    checks = (
        ("deletion", bool(account.get("pending_deletion")), "auth.pending_deletion"),
        ("email_flag", bool(account.get("email_flagged_invalid")), "account.email_flagged"),
        ("contact_email", contact_email_required(account), "account.contact_email"),
        ("verification", bool(account.get("email") and not account.get("email_verified_at")
                              and notifications.verification_required()), "account.verify_email_pending"),
        ("approval", account.get("approval_status") in ("pending", "denied"),
         "auth.activation_denied" if account.get("approval_status") == "denied" else "auth.activation_pending"),
    )
    if impersonating():
        return None
    for gate, blocked, endpoint in checks:
        if blocked and not is_exempt(view, gate):
            return endpoint
    return None


def redirect_to_login():
    if wants_json():
        return jsonify({"error": t("hosting.auth.login_required")}), 401
    if g.get("session_ended"):
        flash(t("hosting.auth.session_ended"), "info")
    target = request.full_path.rstrip("?") if request.method == "GET" else None
    if target in (None, "/"):
        return redirect(url_for("auth.login"))
    return redirect(url_for("auth.login", next=target))


def admin_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        account = current_account()
        if account is None:
            return redirect_to_login()
        if not account["is_admin"]:
            abort(403)
        if (settings.flag("admin_mfa_required") and not account.get("totp_enabled")
                and not is_exempt(wrapper, "admin_mfa")):
            flash(t("hosting.mfa.required_for_admins"), "warning")
            return redirect(url_for("account.mfa"))
        return view(*args, **kwargs)

    return wrapper


def flash_t(key: str, category: str = "info", **values: Any) -> None:
    flash(t(key, **values), category)
