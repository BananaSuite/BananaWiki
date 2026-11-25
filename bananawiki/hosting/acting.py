"""Administrators acting as an account (impersonation): its safeguards, and signing in to wikis and apps.

Starting needs the administrator's password (or, with two-step sign-in on,
a current code of it) and a reason, both checked by the caller
(:func:`confirm`); administrators are never acted as. Acting ends by itself
after ``admin_act_minutes`` (30): :func:`current` ends it on the first
request after that. Every start and end is in the moderation log with the
reason; ``admin_act_notify`` also emails the account when it starts.

With ``admin_app_signin`` on, ``/oauth/authorize`` signs the acting
administrator in to wikis and apps as the account. The sign-in code and its
access token carry the acting (``act_account_id``, ``act_expires_at``,
``act_log_id``): wikis and apps receive ``act`` and ``act_expires_at`` and
end their session then. A token stops working when the acting ends (early or
by itself), when the setting is switched off, and when the administrator
loses the role or the account. Such sign-ins never record consent (the
administrator is not the one consenting) and each one is in the moderation
log.
"""

from __future__ import annotations

import calendar
from typing import Any

from ..core.timeutil import is_past, now_sql, parse, sql_in
from . import accounts, events, settings
from .db import db
from .limits import confirm_code, confirm_password

DEFAULT_MINUTES = 30
MINUTES_RANGE = (5, 480)
REASON_MAX = 500


def minutes() -> int:
    low, high = MINUTES_RANGE
    try:
        return min(high, max(low, int(settings.get("admin_act_minutes", DEFAULT_MINUTES))))
    except (TypeError, ValueError):
        return DEFAULT_MINUTES


def app_signin_enabled() -> bool:
    """Whether acting administrators may sign in to wikis and apps as the account (``admin_app_signin``)."""
    return settings.flag("admin_app_signin")


def notify_enabled() -> bool:
    return settings.flag("admin_act_notify")


def may_act_as(admin: dict[str, Any], target: dict[str, Any] | None) -> bool:
    """Only active, approved accounts that are not administrators, never oneself."""
    return bool(target is not None and target["id"] != admin["id"] and not target["is_admin"]
                and not target["deleted_at"] and not accounts.is_suspended(target)
                and target["approval_status"] == "approved")


def confirm(admin: dict[str, Any], password: str, code: str) -> bool:
    """The administrator's own password or, with two-step sign-in on, a current code of it (on the budget of
    the account pages' confirmations, which answer 429 once spent)."""
    if password:
        return confirm_password(admin, password)
    code = (code or "").strip()
    return bool(code) and confirm_code(admin, code)


def start(admin: dict[str, Any], target: dict[str, Any], reason: str) -> int:
    """Record the start of acting (log entry, moderation log, optional email); returns the log id."""
    reason = " ".join((reason or "").split())[:REASON_MAX]
    log_id = db.insert("hosting_impersonation_logs", {
        "admin_account_id": admin["id"], "target_account_id": target["id"], "started_at": now_sql(),
        "reason": reason, "expires_at": sql_in(minutes=minutes()),
    })
    events.record("account", target["id"], "account.impersonated", admin["id"], reason)
    if notify_enabled():
        from . import notifications

        notifications.notify_account(target, "admin_acting", admin=admin["username"], minutes=minutes())
    return log_id


def log_row(log_id: Any) -> dict[str, Any] | None:
    try:
        return db.one("SELECT * FROM hosting_impersonation_logs WHERE id = ?", (int(log_id),)) if log_id else None
    except (TypeError, ValueError):
        return None


def live(row: dict[str, Any] | None) -> bool:
    """Whether the acting of *row* is still going on (not ended, not past its end)."""
    # Acting started before the time limit existed (no end recorded) is over: it starts again with a reason.
    return bool(row) and not row["ended_at"] and bool(row.get("expires_at")) and not is_past(row["expires_at"])


def end(log_id: Any, why: str) -> None:
    """End the acting of *log_id* (``stopped``, ``expired`` or ``signed_out``) once: moderation log, and the
    wikis' and apps' access tokens and unused codes of sign-ins made while acting go."""
    row = log_row(log_id)
    if row is None or row["ended_at"]:
        return
    with db.transaction():
        if db.execute("UPDATE hosting_impersonation_logs SET ended_at = ?, end_reason = ? WHERE id = ? "
                      "AND ended_at IS NULL", (now_sql(), why, row["id"])).rowcount != 1:
            return
        db.execute("DELETE FROM hosting_oauth_access_tokens WHERE act_log_id = ?", (row["id"],))
        db.execute("DELETE FROM hosting_oauth_authorization_codes WHERE act_log_id = ?", (row["id"],))
        events.record("account", row["target_account_id"], "account.impersonation_ended", row["admin_account_id"],
                      why)


def unix(sql_time: str | None) -> int | None:
    """``YYYY-MM-DD HH:MM:SS`` (UTC) as Unix seconds."""
    moment = parse(sql_time)
    return calendar.timegm(moment.utctimetuple()) if moment is not None else None


def sign_in_valid(row: dict[str, Any]) -> bool:
    """Whether a code or token issued while acting (``act_account_id`` set) may still be used.

    Codes and tokens without acting always may (as far as acting goes).
    """
    if not row.get("act_account_id"):
        return True
    if not app_signin_enabled() or not row.get("act_expires_at") or is_past(row["act_expires_at"]):
        return False
    admin = accounts.get(row["act_account_id"])
    if admin is None or admin["deleted_at"] or not admin["is_admin"] or accounts.is_suspended(admin):
        return False
    return live(log_row(row.get("act_log_id")))


def claims(row: dict[str, Any]) -> dict[str, Any]:
    """``act`` and ``act_expires_at`` for a token response and userinfo ({} without acting)."""
    if not row.get("act_account_id"):
        return {}
    admin = accounts.get(row["act_account_id"]) or {}
    return {"act": {"sub": row["act_account_id"], "preferred_username": admin.get("username", "")},
            "act_expires_at": unix(row["act_expires_at"])}
