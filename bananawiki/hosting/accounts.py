"""Hosting accounts: sign-up, credentials, email, approval, suspension, deletion.

Formats are those of 1.4: 12-character ids, Werkzeug password hashes,
SHA-256 digests of email-verification and password-reset tokens, and the
``session_version`` counter whose increment ends every login session.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import string
from typing import Any

from flask import current_app

from ..core import passwords
from ..core.crypto import sha256_hex
from ..core.timeutil import is_past, now_sql, parse, sql_in, utcnow
from . import api_tokens, attention, events, invites, settings
from .db import db
from .errors import ServiceError

USERNAME = re.compile(r"^[a-zA-Z0-9_-]{3,30}$")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PASSWORD_MAX_LENGTH = 1000
TERMS_VERSION = "2026-07-22"
REASON_MAX_LENGTH = 500
DECISION_MAX_LENGTH = 1000
MAX_SCHEDULE_SECONDS = 10 * 365 * 86400
_ID_ALPHABET = string.ascii_lowercase + string.digits


# ── Lookups ───────────────────────────────────────────────────────────────────


def get(account_id: str | None) -> dict[str, Any] | None:
    if not account_id:
        return None
    return db.one("SELECT * FROM accounts WHERE id = ?", (str(account_id),))


def by_username(username: str) -> dict[str, Any] | None:
    if not username:
        return None
    return db.one("SELECT * FROM accounts WHERE username = ? COLLATE NOCASE", (username.strip(),))


def active_by_username(username: str) -> dict[str, Any] | None:
    account = by_username(username)
    return account if account and not account["deleted_at"] else None


def by_email(address: str) -> dict[str, Any] | None:
    address = (address or "").strip().lower()
    if not address:
        return None
    return db.one("SELECT * FROM accounts WHERE email = ? COLLATE NOCASE AND deleted_at IS NULL "
                  "ORDER BY created_at LIMIT 1", (address,))


def all_accounts() -> list[dict[str, Any]]:
    return db.all("SELECT * FROM accounts ORDER BY created_at DESC")


def pending() -> list[dict[str, Any]]:
    return db.all("SELECT * FROM accounts WHERE approval_status = 'pending' AND deleted_at IS NULL "
                  "ORDER BY created_at DESC")


def count_active_admins(exclude: str | None = None) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM accounts WHERE is_admin = 1 AND suspended = 0 AND deleted_at IS NULL AND id != ?",
        (exclude or "",), default=0,
    ))


def set_attention_emails(account_id: str, enabled: bool) -> None:
    db.execute("UPDATE accounts SET attention_emails = ? WHERE id = ?", (1 if enabled else 0, account_id))


def set_language(account_id: str, language: str) -> None:
    """Remember the interface language, so emails sent later speak it too."""
    db.execute("UPDATE accounts SET language = ? WHERE id = ? AND language != ?", (language, account_id, language))


# ── Validation and passwords ──────────────────────────────────────────────────


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME.match(username):
        raise ServiceError("hosting.accounts.invalid_username")
    return username


def check_email(address: str, *, required: bool = False, exclude_id: str | None = None) -> str:
    address = (address or "").strip().lower()
    if not address:
        if required:
            raise ServiceError("hosting.accounts.email_required")
        return ""
    if len(address) > 254 or not EMAIL.match(address):
        raise ServiceError("hosting.accounts.invalid_email")
    existing = by_email(address)
    if existing and existing["id"] != exclude_id:
        raise ServiceError("hosting.accounts.email_taken")
    return address


def check_new_password(password: str, confirm: str | None = None) -> str:
    if len(password or "") < passwords.MIN_LENGTH:
        raise ServiceError("hosting.accounts.password_too_short")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ServiceError("hosting.accounts.password_too_long", max=PASSWORD_MAX_LENGTH)
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        raise ServiceError("hosting.accounts.password_weak")
    if confirm is not None and password != confirm:
        raise ServiceError("hosting.accounts.password_mismatch")
    return password


def hash_password(password: str) -> str:
    return passwords.hash_password(password, current_app.config["HOSTING"].password_hash_method)


def verify_password(account: dict[str, Any] | None, password: str) -> bool:
    """Constant-effort password check (burns time for unknown accounts)."""
    if not account or len(password or "") > PASSWORD_MAX_LENGTH or account.get("deleted_at"):
        passwords.burn_time(password or "")
        return False
    return passwords.verify_password(account["password"], password)


def _new_id() -> str:
    while True:
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))
        if not db.scalar("SELECT 1 FROM accounts WHERE id = ?", (candidate,)):
            return candidate


# ── Creation ──────────────────────────────────────────────────────────────────


def create(username: str, password: str, *, is_admin: bool = False, email: str = "") -> dict[str, Any]:
    """Create an approved account directly (administrators, operator tools)."""
    username = check_username(username)
    check_new_password(password)
    if by_username(username):
        raise ServiceError("hosting.accounts.username_taken")
    email = check_email(email)
    account_id = _new_id()
    try:
        db.insert("accounts", {"id": account_id, "username": username, "password": hash_password(password),
                               "is_admin": 1 if is_admin else 0, "created_at": now_sql(), "email": email})
    except sqlite3.IntegrityError as error:
        raise ServiceError("hosting.accounts.username_taken") from error
    return get(account_id)  # type: ignore[return-value]


def signup(username: str, password: str, *, email: str = "", invite_code: str = "", use_case: str = "",
           bootstrap_allowed: bool = False) -> dict[str, Any]:
    """Create an account through the public form under the current sign-up policy.

    The first account on an empty platform becomes the administrator and
    requires the installation token (checked by the caller). Afterwards
    ``closed`` refuses, ``invite`` redeems a code in the same transaction and
    ``approval`` (or ``hosting_activation_required``) leaves the account
    pending.
    """
    username = check_username(username)
    check_new_password(password)
    password_hash = hash_password(password)
    with db.transaction():
        first = not db.scalar("SELECT 1 FROM accounts LIMIT 1")
        mode = "open" if first else settings.signup_mode()
        if first and not bootstrap_allowed:
            raise ServiceError("hosting.signup.bootstrap_required")
        if mode == "closed":
            raise ServiceError("hosting.signup.closed")
        if by_username(username):
            raise ServiceError("hosting.accounts.username_taken")
        email = check_email(email)
        is_pending = not first and settings.approval_required()
        account_id = _new_id()
        try:
            db.insert("accounts", {
                "id": account_id, "username": username, "password": password_hash, "is_admin": 1 if first else 0,
                "created_at": now_sql(), "email": email, "terms_accepted_at": now_sql(),
                "terms_version": TERMS_VERSION, "signup_use_case": use_case[:2000],
                "approval_status": "pending" if is_pending else "approved",
            })
        except sqlite3.IntegrityError as error:
            raise ServiceError("hosting.accounts.username_taken") from error
        if mode == "invite":
            invites.redeem(invite_code, account_id)
        if is_pending:
            attention.created("accounts.pending", account_id)
    return get(account_id)  # type: ignore[return-value]


# ── Identity and credentials ──────────────────────────────────────────────────


# A reset link sent to the old address must not start working again once a new one is verified.
_RESET_CLEARED = {"password_reset_token_hash": "", "password_reset_sent_at": None, "password_reset_expires_at": None}


def update_identity(account: dict[str, Any], *, username: str, email: str, email_required: bool) -> bool:
    """Change username/email; returns True when the email changed (verification restarts)."""
    username = check_username(username)
    other = by_username(username)
    if other and other["id"] != account["id"]:
        raise ServiceError("hosting.accounts.username_taken")
    email = check_email(email, required=email_required, exclude_id=account["id"])
    changed_email = email != (account.get("email") or "")
    values: dict[str, Any] = {"username": username, "email": email, "email_prompt_dismissed": 1}
    if changed_email:
        values.update(email_verified_at=None, email_verification_token_hash="",
                      email_verification_sent_at=None, email_verification_expires_at=None, **_RESET_CLEARED)
    try:
        db.update("accounts", values, "id = ?", (account["id"],))
    except sqlite3.IntegrityError as error:
        raise ServiceError("hosting.accounts.username_taken") from error
    return changed_email


def set_contact_email(account: dict[str, Any], address: str) -> None:
    address = check_email(address, required=True, exclude_id=account["id"])
    db.update("accounts", {"email": address, "email_prompt_dismissed": 1, "email_verified_at": None,
                           "email_verification_token_hash": "", **_RESET_CLEARED}, "id = ?", (account["id"],))


def set_password(account_id: str, password: str, *, actor_id: str | None, reason: str) -> int:
    """Replace the password and end every session and API token. Returns the new session version."""
    password_hash = hash_password(password)
    with db.transaction():
        db.execute("UPDATE accounts SET password = ?, session_version = session_version + 1 WHERE id = ?",
                   (password_hash, account_id))
        db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                   (now_sql(), account_id))
        api_tokens.revoke_all(account_id, actor_id, reason)
        return int(db.scalar("SELECT session_version FROM accounts WHERE id = ?", (account_id,), default=0))


def bump_session_version(account_id: str) -> int:
    db.execute("UPDATE accounts SET session_version = session_version + 1 WHERE id = ?", (account_id,))
    return int(db.scalar("SELECT session_version FROM accounts WHERE id = ?", (account_id,), default=0))


def set_theme(account_id: str, mode: str) -> None:
    db.execute("UPDATE accounts SET theme_mode = ? WHERE id = ?",
               (mode if mode in ("dark", "light") else "default", account_id))


def set_admin(account_id: str, is_admin: bool) -> None:
    db.execute("UPDATE accounts SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, account_id))


# ── Email tokens ──────────────────────────────────────────────────────────────


def cooldown_remaining(account: dict[str, Any]) -> int:
    sent = parse(account.get("email_verification_sent_at"))
    if sent is None:
        return 0
    return max(0, int(settings.verification_cooldown() - (utcnow() - sent).total_seconds()))


def issue_verification(account: dict[str, Any]) -> str:
    """Store a new verification token for the account's email and return it."""
    raw = secrets.token_urlsafe(32)
    ttl = current_app.config["HOSTING"].email.token_ttl_seconds
    db.update("accounts", {"email_verification_token_hash": sha256_hex(raw), "email_verification_sent_at": now_sql(),
                           "email_verification_expires_at": sql_in(seconds=ttl), "email_verified_at": None},
              "id = ?", (account["id"],))
    return raw


def clear_verification(account_id: str) -> None:
    db.update("accounts", {"email_verification_token_hash": "", "email_verification_sent_at": None,
                           "email_verification_expires_at": None}, "id = ?", (account_id,))


def mark_verified(account_id: str) -> None:
    db.update("accounts", {"email_verified_at": now_sql(), "email_verification_token_hash": "",
                           "email_verification_expires_at": None}, "id = ?", (account_id,))


def verify_email_token(raw: str) -> dict[str, Any] | None:
    """Consume a verification token; returns the verified account or None."""
    if not raw or len(raw) > 256:
        return None
    with db.transaction():
        row = db.one(
            "SELECT id FROM accounts WHERE email_verification_token_hash = ? AND email != '' "
            "AND email_verification_expires_at >= ? AND deleted_at IS NULL", (sha256_hex(raw), now_sql()),
        )
        if row is None:
            return None
        mark_verified(row["id"])
    return get(row["id"])


def issue_password_reset(account: dict[str, Any]) -> str:
    raw = secrets.token_urlsafe(32)
    db.update("accounts", {"password_reset_token_hash": sha256_hex(raw), "password_reset_sent_at": now_sql(),
                           "password_reset_expires_at": sql_in(hours=1)}, "id = ?", (account["id"],))
    return raw


def consume_password_reset(raw: str, password: str) -> bool:
    """Set a new password from a reset link (single use, verified email only)."""
    check_new_password(password)
    if not raw or len(raw) > 256:
        return False
    digest = sha256_hex(raw)
    row = db.one(
        "SELECT id FROM accounts WHERE password_reset_token_hash = ? AND password_reset_expires_at >= ? "
        "AND email_verified_at IS NOT NULL AND deleted_at IS NULL", (digest, now_sql()),
    )
    if row is None:
        return False
    with db.transaction():
        cleared = db.execute(
            "UPDATE accounts SET password_reset_token_hash = '', password_reset_sent_at = NULL, "
            "password_reset_expires_at = NULL WHERE id = ? AND password_reset_token_hash = ?", (row["id"], digest),
        ).rowcount
        if cleared != 1:
            return False
        set_password(row["id"], password, actor_id=row["id"], reason="Password reset by email")
    return True


# ── Email flag ────────────────────────────────────────────────────────────────


def flag_email(account: dict[str, Any], *, reason: str, reason_visible: bool, replacement: str = "",
               actor_id: str) -> None:
    """Ask the user for a new address, or (with *replacement*) set one for them."""
    if replacement:
        replacement = check_email(replacement, required=True, exclude_id=account["id"])
        db.update("accounts", {"email": replacement, "email_verified_at": None, "email_verification_token_hash": "",
                               "email_flagged_invalid": 0, "email_flag_reason": "", "email_flagged_previous": "",
                               "email_flag_reason_visible": 0, **_RESET_CLEARED}, "id = ?", (account["id"],))
        events.record("account", account["id"], "email.replaced", actor_id)
        return
    db.update("accounts", {"email_flagged_invalid": 1, "email_flag_reason": reason[:REASON_MAX_LENGTH],
                           "email_flag_reason_visible": 1 if reason_visible and reason else 0,
                           "email_flagged_previous": (account.get("email") or "").lower(), "email": "",
                           "email_verified_at": None, "email_verification_token_hash": "", **_RESET_CLEARED},
              "id = ?", (account["id"],))
    events.record("account", account["id"], "email.flagged", actor_id, reason)


def clear_email_flag(account_id: str) -> None:
    db.update("accounts", {"email_flagged_invalid": 0, "email_flag_reason": "", "email_flagged_previous": "",
                           "email_flag_reason_visible": 0}, "id = ?", (account_id,))


def replace_flagged_email(account: dict[str, Any], address: str) -> None:
    address = check_email(address, required=True, exclude_id=account["id"])
    if settings.flag("email_flag_block_reentry") and address == (account.get("email_flagged_previous") or ""):
        raise ServiceError("hosting.email_flagged.same_address")
    with db.transaction():
        db.update("accounts", {"email": address, "email_verified_at": None, **_RESET_CLEARED}, "id = ?",
                  (account["id"],))
        clear_email_flag(account["id"])


# ── Approval ──────────────────────────────────────────────────────────────────


def _decision(reason: str) -> str:
    reason = (reason or "").strip()
    if len(reason) > DECISION_MAX_LENGTH:
        raise ServiceError("hosting.accounts.decision_too_long", max=DECISION_MAX_LENGTH)
    return reason


def approve(account_id: str, admin_id: str, reason: str = "") -> bool:
    reason = _decision(reason)
    with db.transaction():
        changed = db.execute(
            "UPDATE accounts SET approval_status = 'approved', approved_by = ?, denied_by = NULL, denied_at = NULL, "
            "denied_notified = 0, approved_denied_at = ?, decision_reason = ?, pending_deletion = 0, "
            "pending_deletion_at = NULL, pending_deletion_reason = '' "
            "WHERE id = ? AND approval_status IN ('pending', 'denied') AND deleted_at IS NULL",
            (admin_id, now_sql(), reason, account_id),
        ).rowcount
        if changed:
            events.record("account", account_id, "account.approved", admin_id, reason)
            attention.decided(account_id, "accounts.pending", "approved", email=False)
    return bool(changed)


def deny(account_id: str, admin_id: str, reason: str = "") -> bool:
    reason = _decision(reason)
    with db.transaction():
        changed = db.execute(
            "UPDATE accounts SET approval_status = 'denied', denied_by = ?, approved_by = NULL, denied_at = ?, "
            "approved_denied_at = ?, denied_notified = 0, decision_reason = ? "
            "WHERE id = ? AND approval_status = 'pending' AND deleted_at IS NULL",
            (admin_id, now_sql(), now_sql(), reason, account_id),
        ).rowcount
        if changed:
            events.record("account", account_id, "account.denied", admin_id, reason)
            attention.decided(account_id, "accounts.pending", "denied", email=False)
    return bool(changed)


def approve_all_pending(admin_id: str) -> int:
    with db.transaction():
        ids = db.column("SELECT id FROM accounts WHERE approval_status = 'pending' AND deleted_at IS NULL")
        for account_id in ids:
            db.execute(
                "UPDATE accounts SET approval_status = 'approved', approved_by = ?, approved_denied_at = ?, "
                "decision_reason = '' WHERE id = ?", (admin_id, now_sql(), account_id),
            )
            events.record("account", account_id, "account.approved", admin_id, "Approval requirement removed.")
    return len(ids)


# ── Suspension ────────────────────────────────────────────────────────────────


def is_suspended(account: dict[str, Any]) -> bool:
    """True while suspended; a timed suspension that has run out is lifted here."""
    if not account.get("suspended"):
        return False
    until = account.get("suspended_until")
    if until and is_past(until) and not account.get("deleted_at"):
        unsuspend(account["id"], actor_id=None, automatic=True)
        account["suspended"] = 0
        return False
    return True


def suspend(account_id: str, *, until: str | None, reason: str, reason_visible: bool, time_visible: bool,
            actor_id: str, duration_label: str) -> None:
    reason = (reason or "").strip()[:REASON_MAX_LENGTH]
    with db.transaction():
        db.update("accounts", {
            "suspended": 1, "suspended_at": now_sql(), "suspend_reason": reason, "suspended_until": until,
            "suspend_reason_visible": 1 if reason_visible and reason else 0,
            "suspend_time_visible": 1 if time_visible and until else 0,
        }, "id = ?", (account_id,))
        db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                   (now_sql(), account_id))
        _audit(account_id, "suspend", actor_id, reason or None, reason_visible and bool(reason),
               time_visible and bool(until), duration_label, until)


def unsuspend(account_id: str, *, actor_id: str | None, automatic: bool = False) -> None:
    with db.transaction():
        db.update("accounts", {"suspended": 0, "suspended_at": None, "suspend_reason": "", "suspended_until": None,
                               "suspend_reason_visible": 0, "suspend_time_visible": 0},
                  "id = ? AND deleted_at IS NULL", (account_id,))
        _audit(account_id, "unsuspend", actor_id, "Timed suspension ended" if automatic else None,
               False, False, None, None)


def _audit(account_id: str, action: str, actor_id: str | None, reason: str | None, reason_visible: bool,
           time_visible: bool, duration: str | None, until: str | None) -> None:
    db.insert("account_suspension_audit", {
        "account_id": account_id, "action": action, "reason": reason, "reason_visible": int(reason_visible),
        "time_visible": int(time_visible), "duration": duration, "suspended_until": until,
        "performed_by": actor_id, "created_at": now_sql(),
    })
    events.record("account", account_id, f"account.{action}", actor_id, reason or "")


def suspension_history(account_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT s.*, a.username AS performed_by_name FROM account_suspension_audit s "
        "LEFT JOIN accounts a ON a.id = s.performed_by WHERE s.account_id = ? ORDER BY s.created_at DESC, s.id DESC",
        (account_id,),
    )


def expired_suspensions() -> list[str]:
    return db.column("SELECT id FROM accounts WHERE suspended = 1 AND suspended_until IS NOT NULL "
                     "AND suspended_until <= ? AND deleted_at IS NULL", (now_sql(),))


# ── Deletion ──────────────────────────────────────────────────────────────────


def delete(account_id: str) -> None:
    """Erase an account's personal data and credentials, keeping a tombstone row.

    The tombstone keeps retained (terminated) wikis attributable until their
    grace period ends; :func:`purge_tombstones` removes it afterwards. The
    caller terminates or transfers the account's wikis first.
    """
    with db.transaction():
        db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                   (now_sql(), account_id))
        db.execute("DELETE FROM hosting_api_tokens WHERE account_id = ?", (account_id,))
        db.execute("DELETE FROM hosting_oauth_access_tokens WHERE account_id = ?", (account_id,))
        db.execute("DELETE FROM instance_collaborators WHERE account_id = ?", (account_id,))
        db.update("accounts", {
            "username": f"deleted-{account_id}", "email": "", "password": "!", "suspended": 1, "suspend_reason": "",
            "approval_status": "denied", "decision_reason": "", "pending_deletion": 0, "pending_deletion_at": None,
            "totp_enabled": 0, "totp_secret_encrypted": "", "totp_recovery_hashes": "[]", "signup_use_case": "",
            "email_verification_token_hash": "", "password_reset_token_hash": "", "email_flagged_previous": "",
            "deleted_at": now_sql(),
        }, "id = ?", (account_id,))
        db.execute("UPDATE accounts SET session_version = session_version + 1 WHERE id = ?", (account_id,))


def schedule_deletion(account_id: str, seconds: int, reason: str, actor_id: str) -> None:
    seconds = max(1, min(int(seconds), MAX_SCHEDULE_SECONDS))
    db.update("accounts", {"pending_deletion": 1, "pending_deletion_at": now_sql(), "pending_deletion_seconds": seconds,
                           "pending_deletion_reason": (reason or "")[:REASON_MAX_LENGTH]}, "id = ?", (account_id,))
    events.record("account", account_id, "account.deletion_scheduled", actor_id, reason)


def cancel_deletion(account_id: str, actor_id: str) -> None:
    db.update("accounts", {"pending_deletion": 0, "pending_deletion_at": None, "pending_deletion_seconds": 86400,
                           "pending_deletion_reason": ""}, "id = ?", (account_id,))
    events.record("account", account_id, "account.deletion_cancelled", actor_id)


def deletion_remaining(account: dict[str, Any]) -> int | None:
    started = parse(account.get("pending_deletion_at"))
    if started is None:
        return None
    seconds = int(account.get("pending_deletion_seconds") or 0)
    return max(0, int(seconds - (utcnow() - started).total_seconds()))


def due_deletions() -> list[str]:
    rows = db.all("SELECT * FROM accounts WHERE pending_deletion = 1 AND deleted_at IS NULL")
    return [row["id"] for row in rows if deletion_remaining(row) == 0]


def denied_remaining(account: dict[str, Any]) -> int | None:
    timeout = settings.denied_timeout_seconds()
    denied = parse(account.get("denied_at"))
    if timeout < 0 or denied is None:
        return None
    return max(0, int(timeout - (utcnow() - denied).total_seconds()))


def expired_denials() -> list[str]:
    """Denied accounts past the configured timeout (never tombstones, never admins)."""
    timeout = settings.denied_timeout_seconds()
    if timeout < 0:
        return []
    return db.column(
        "SELECT id FROM accounts WHERE approval_status = 'denied' AND deleted_at IS NULL AND is_admin = 0 "
        "AND denied_at IS NOT NULL AND denied_at <= ?", (sql_in(seconds=-timeout),),
    )


def purge_tombstones() -> int:
    return db.execute(
        "DELETE FROM accounts WHERE deleted_at IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM instances i WHERE i.account_id = accounts.id "
        "AND (i.status != 'terminated' OR i.data_retained_until IS NOT NULL))"
    ).rowcount


# ── Impersonation log and data export ─────────────────────────────────────────


def log_impersonation_start(admin_id: str, target_id: str) -> int:
    return db.insert("hosting_impersonation_logs", {"admin_account_id": admin_id, "target_account_id": target_id,
                                                    "started_at": now_sql()})


def log_impersonation_stop(log_id: int | None) -> None:
    if log_id:
        db.execute("UPDATE hosting_impersonation_logs SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                   (now_sql(), log_id))


_PRIVATE = ("password", "email_verification_token_hash", "password_reset_token_hash", "totp_secret_encrypted",
            "totp_recovery_hashes", "totp_last_counter")


def export_data(account: dict[str, Any]) -> dict[str, Any]:
    """What the account may download about itself (no credentials or secrets)."""
    instances = db.all(
        "SELECT id, subdomain, domain_mode, status, created_at, expires_at, declared_use_case, easy_wiki "
        "FROM instances WHERE account_id = ? AND status != 'terminated'", (account["id"],),
    )
    return {"exported_at": now_sql(), "account": {k: v for k, v in account.items() if k not in _PRIVATE},
            "instances": instances}
