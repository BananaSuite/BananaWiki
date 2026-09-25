"""Account management for the hosting platform."""

import logging
import secrets
import string
import hashlib
from datetime import datetime, timezone

from ._api_tokens import delete_account_api_tokens, revoke_account_api_tokens
from ._connection import get_hosting_db_context

logger = logging.getLogger("hosting.db.accounts")

_MAX_SUSPEND_REASON_LENGTH = 500
_MAX_DECISION_REASON_LENGTH = 1000


def _normalize_decision_reason(reason):
    reason = (reason or "").strip()
    if len(reason) > _MAX_DECISION_REASON_LENGTH:
        raise ValueError("decision_reason_too_long")
    return reason


def _gen_account_id():
    """Generate a random 12-character alphanumeric account ID."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def create_account(username, hashed_password, is_admin=False):
    """Create a new hosting portal account and return its ID."""
    with get_hosting_db_context() as conn:
        aid = _gen_account_id()
        while conn.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
            aid = _gen_account_id()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO accounts (id, username, password, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (aid, username, hashed_password, 1 if is_admin else 0, now),
        )
        conn.commit()
    return aid


def get_account_by_id(account_id):
    """Return an account row by ID, or ``None``."""
    with get_hosting_db_context() as conn:
        return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def get_account_by_username(username):
    """Return an account row by username (case-insensitive), or ``None``."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()


def get_account_by_email(email):
    """Return the first active account for an email, case-insensitively."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE email=? COLLATE NOCASE "
            "AND deleted_at IS NULL ORDER BY created_at LIMIT 1",
            ((email or "").strip().lower(),),
        ).fetchone()


def issue_email_verification(account_id, email, raw_token, expires_at):
    """Store a one-way verification token for the account's current email."""
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE accounts SET email=?, email_verified_at=NULL, "
            "email_verification_token_hash=?, email_verification_sent_at=?, "
            "email_verification_expires_at=? WHERE id=?",
            ((email or "").strip().lower(), digest, now, expires_at, account_id),
        )
        conn.commit()


def clear_email_verification(account_id):
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE accounts SET email_verification_token_hash='', "
            "email_verification_sent_at=NULL, email_verification_expires_at=NULL "
            "WHERE id=?",
            (account_id,),
        )
        conn.commit()


def verify_email_token(raw_token):
    """Consume a valid token and return the verified account, or ``None``."""
    digest = hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM accounts WHERE email_verification_token_hash=? "
            "AND email_verification_expires_at IS NOT NULL "
            "AND email_verification_expires_at >= ? AND deleted_at IS NULL",
            (digest, now),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE accounts SET email_verified_at=?, "
            "email_verification_token_hash='', email_verification_sent_at=NULL, "
            "email_verification_expires_at=NULL WHERE id=?",
            (now, row["id"]),
        )
        conn.commit()
    return get_account_by_id(row["id"])


def issue_password_reset(account_id, raw_token, expires_at):
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE accounts SET password_reset_token_hash=?, "
            "password_reset_sent_at=?, password_reset_expires_at=? WHERE id=?",
            (digest, datetime.now(timezone.utc).isoformat(), expires_at, account_id),
        )
        conn.commit()


def consume_password_reset(raw_token, new_password_hash):
    digest = hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM accounts WHERE password_reset_token_hash=? "
            "AND password_reset_expires_at >= ? AND email_verified_at IS NOT NULL "
            "AND deleted_at IS NULL",
            (digest, now),
        ).fetchone()
        if row is None:
            return False
        result = conn.execute(
            "UPDATE accounts SET password=?, session_version=session_version+1, password_reset_token_hash='', "
            "password_reset_sent_at=NULL, password_reset_expires_at=NULL "
            "WHERE id=? AND password_reset_token_hash=?",
            (new_password_hash, row["id"], digest),
        )
        if result.rowcount == 1:
            conn.execute(
                "UPDATE hosting_account_sessions SET revoked_at=? "
                "WHERE account_id=? AND revoked_at IS NULL",
                (now, row["id"]),
            )
            revoke_account_api_tokens(conn, row["id"], row["id"], "Password reset by email")
        conn.commit()
        return result.rowcount == 1


def get_all_accounts():
    """Return all accounts ordered by creation date (newest first)."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM accounts ORDER BY created_at DESC"
        ).fetchall()


def set_account_admin(account_id, is_admin):
    """Set or unset the admin flag on an account."""
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE accounts SET is_admin=? WHERE id=?",
            (1 if is_admin else 0, account_id),
        )
        conn.commit()


def count_admin_accounts():
    """Return the number of admin accounts on the platform."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE is_admin = 1"
        ).fetchone()
    return row[0] if row else 0


def count_active_admin_accounts():
    """Return the number of non-suspended admin accounts."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE is_admin = 1 AND suspended = 0"
        ).fetchone()
    return row[0] if row else 0


def create_account_atomic_first_admin(username, hashed_password):
    """Create a new account, atomically promoting to admin if it's the first.

    Uses ``BEGIN IMMEDIATE`` so that checking the account count and inserting
    happen atomically: no TOCTOU race with concurrent signups.
    Returns ``(account_id, is_admin)`` tuple.
    """
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        aid = _gen_account_id()
        while conn.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
            aid = _gen_account_id()
        now = datetime.now(timezone.utc).isoformat()
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        is_first = count == 0
        conn.execute(
            "INSERT INTO accounts (id, username, password, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (aid, username, hashed_password, 1 if is_first else 0, now),
        )
        conn.commit()
    return aid, is_first


def create_signup_account(username, hashed_password, *, invite_code="", email="",
                          terms_version="", signup_use_case="", bootstrap_allowed=False):
    """Commit a signup only after checking current policy and redeeming its invite."""
    from ._invite_codes import _redeem_invite_in_transaction

    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        is_first = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        settings = conn.execute("SELECT * FROM hosting_settings WHERE id=1").fetchone()
        if is_first:
            if not bootstrap_allowed:
                raise ValueError("Initial administrator signup requires the installation token.")
            mode = "open"
        else:
            mode = settings["signup_mode"] if settings else "closed"
            if mode not in {"open", "invite", "approval"}:
                raise ValueError("Public signups are disabled on this platform.")
        pending = not is_first and bool(mode == "approval" or settings["hosting_activation_required"])
        aid = _gen_account_id()
        while conn.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
            aid = _gen_account_id()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO accounts (id, username, password, is_admin, created_at, email, "
            "terms_accepted_at, terms_version, signup_use_case, approval_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, username, hashed_password, int(is_first), now, email, now,
             terms_version, signup_use_case, "pending" if pending else "approved"),
        )
        if mode == "invite":
            accepted, reason = _redeem_invite_in_transaction(conn, invite_code, aid)
            if not accepted:
                raise ValueError(reason)
        conn.commit()
        return aid, is_first


def change_account_password(account_id, new_hashed_password):
    """Update the password hash for an account.

    Used when an administrator or the operator CLI sets a new password, so
    every session and API token of the account stops working at once.
    """
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE accounts SET password=?, session_version=session_version+1 WHERE id=?",
            (new_hashed_password, account_id),
        )
        conn.execute(
            "UPDATE hosting_account_sessions SET revoked_at=? "
            "WHERE account_id=? AND revoked_at IS NULL",
            (now, account_id),
        )
        revoke_account_api_tokens(conn, account_id, None, "Password reset")
        conn.commit()


def delete_account(account_id):
    """Soft-delete an account, preserving a tombstone row for admin visibility.

    Personal fields and credentials are erased immediately.  The row is
    kept with ``deleted_at`` set so platform administrators can still see
    the account in the admin dashboard.
    """
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE hosting_account_sessions SET revoked_at=? "
            "WHERE account_id=? AND revoked_at IS NULL",
            (now, account_id),
        )
        delete_account_api_tokens(conn, account_id)
        conn.execute(
            "UPDATE accounts SET username=?, email='', password='!', suspended=1, "
            "suspend_reason='', approval_status='denied', decision_reason='', pending_deletion=0, "
            "pending_deletion_at=NULL, deleted_at=? WHERE id=?",
            (f"deleted-{account_id}", now, account_id),
        )
        conn.commit()


def set_pending_deletion(account_id, seconds=86400, reason=""):
    """Mark an account for scheduled deletion after *seconds*.

    The user can still log in and will see a countdown page.  When the
    timer expires, ``cleanup_pending_deletion_accounts`` hard-deletes the
    account (after terminating any active instances).
    """
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE accounts SET pending_deletion=1, pending_deletion_at=?, "
            "pending_deletion_seconds=?, pending_deletion_reason=? WHERE id=?",
            (now, max(0, int(seconds)), str(reason)[:500], account_id),
        )
        conn.commit()


def cancel_pending_deletion(account_id):
    """Remove the pending-deletion flag, restoring normal access."""
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE accounts SET pending_deletion=0, pending_deletion_at=NULL, "
            "pending_deletion_seconds=86400, pending_deletion_reason='' WHERE id=?",
            (account_id,),
        )
        conn.commit()


def get_pending_deletion_accounts():
    """Return account IDs whose pending-deletion countdown has expired.

    The countdown is computed in Python rather than in SQL: the ISO-8601
    values stored by Python (``YYYY-MM-DDTHH:MM:SS.mmmmmm+00:00``) are not
    directly comparable as strings with SQLite's ``datetime()`` output
    (``YYYY-MM-DD HH:MM:SS``), which made the old query match every pending
    account immediately regardless of the remaining time.
    """
    now = datetime.now(timezone.utc)
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            "SELECT id, pending_deletion_at, pending_deletion_seconds "
            "FROM accounts WHERE pending_deletion=1 AND pending_deletion_at IS NOT NULL"
        ).fetchall()
    expired = []
    for row in rows:
        try:
            at = datetime.fromisoformat(row["pending_deletion_at"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            seconds = int(row["pending_deletion_seconds"] or 0)
            if at.astimezone(timezone.utc).timestamp() + seconds <= now.timestamp():
                expired.append(row["id"])
        except (ValueError, TypeError):
            logger.warning(
                "Skipping malformed pending_deletion_at for account %s",
                row["id"],
            )
    return expired


def purge_deleted_account_tombstones():
    """Remove erased account rows once no retained instance data remains."""
    with get_hosting_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM accounts WHERE deleted_at IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM instances WHERE instances.account_id=accounts.id "
            "AND instances.data_retained_until IS NOT NULL)"
        )
        conn.commit()
        return cur.rowcount


def log_hosting_impersonation_start(admin_account_id, target_account_id):
    """Record the start of a hosting account impersonation session."""
    with get_hosting_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO hosting_impersonation_logs "
            "(admin_account_id, target_account_id) VALUES (?, ?)",
            (admin_account_id, target_account_id),
        )
        conn.commit()
        return cur.lastrowid


def log_hosting_impersonation_stop(log_id):
    """Record the end of a hosting account impersonation session."""
    if not log_id:
        return
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE hosting_impersonation_logs SET ended_at=? WHERE id=?",
            (now, log_id),
        )
        conn.commit()


def suspend_account(account_id, reason="",
                    suspended_until=None,
                    reason_visible=False,
                    time_visible=False,
                    performed_by=None):
    """Suspend an account with optional timed suspension and visibility flags.

    ``suspended_until`` is an ISO datetime string for timed suspensions,
    or ``None`` for permanent suspensions.
    Returns True on success.
    """
    reason = (reason or "").strip()[:_MAX_SUSPEND_REASON_LENGTH]
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE accounts SET suspended=1, suspended_at=?, suspend_reason=?, "
            "suspended_until=?, suspend_reason_visible=?, suspend_time_visible=? "
            "WHERE id=?",
            (
                now,
                reason,
                suspended_until,
                1 if reason_visible and reason else 0,
                1 if time_visible and suspended_until else 0,
                account_id,
            ),
        )
        conn.commit()
    return True


def unsuspend_account(account_id):
    """Remove suspension from an account.  Returns True on success."""
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE accounts SET suspended=0, suspended_at=NULL, suspend_reason='', "
            "suspended_until=NULL, suspend_reason_visible=0, suspend_time_visible=0 "
            "WHERE id=?",
            (account_id,),
        )
        conn.commit()
    return True


def is_account_suspended(account_id):
    """Return True if the account is currently suspended.

    Handles both permanent and timed suspensions: a timed suspension
    that has passed its ``suspended_until`` is treated as expired.
    """
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT suspended, suspended_until FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    if not row or not row[0]:
        return False
    suspended_until = row[1]
    if suspended_until:
        try:
            exp = datetime.fromisoformat(suspended_until)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= exp:
                return False
        except (ValueError, TypeError):
            pass
    return True


def check_account_suspension_expired(account_id):
    """Auto-unsuspend an account whose timed suspension has elapsed.

    Returns True if the account was unsuspended, False otherwise.
    """
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT suspended, suspended_until FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    if not row or not row[0]:
        return False
    suspended_until = row[1]
    if not suspended_until:
        return False
    try:
        exp = datetime.fromisoformat(suspended_until)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= exp:
            unsuspend_account(account_id)
            return True
    except (ValueError, TypeError):
        pass
    return False


def cleanup_expired_account_suspensions():
    """Batch auto-unsuspend all accounts whose timed suspension has elapsed.

    Returns the number of accounts that were unsuspended.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_hosting_db_context() as conn:
        cur = conn.execute(
            "UPDATE accounts SET suspended=0, suspended_at=NULL, suspend_reason='', "
            "suspended_until=NULL, suspend_reason_visible=0, suspend_time_visible=0 "
            "WHERE suspended=1 AND suspended_until IS NOT NULL "
            "AND suspended_until <= ?",
            (now_iso,),
        )
        conn.commit()
    return cur.rowcount


def get_pending_accounts():
    """Return all accounts with approval_status='pending', newest first."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE approval_status='pending' ORDER BY created_at DESC"
        ).fetchall()


def count_pending_hosting_accounts():
    """Return the number of hosting accounts awaiting approval."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE approval_status='pending'"
        ).fetchone()
    return row[0] if row else 0


def approve_hosting_account(account_id, admin_id, reason=""):
    """Approve a pending (or previously denied) hosting account.

    Also clears any scheduled deletion so an approved account is never
    auto-deleted.  Returns True if the account was pending/denied and is
    now approved.
    """
    reason = _normalize_decision_reason(reason)
    from ._events import record_event
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE accounts SET approval_status='approved', "
            "approved_by=?, denied_by=NULL, denied_at=NULL, denied_notified=0, "
            "approved_denied_at=?, decision_reason=? "
            "WHERE id=? AND approval_status IN ('pending','denied')",
            (admin_id, now, reason, account_id),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE accounts SET pending_deletion=0, pending_deletion_at=NULL, "
                "pending_deletion_seconds=86400, pending_deletion_reason='' WHERE id=?",
                (account_id,),
            )
            record_event(conn, "account", account_id, "account.approved", admin_id, reason)
        conn.commit()
        return cur.rowcount > 0


def deny_hosting_account(account_id, admin_id, reason=""):
    """Deny a pending hosting account.

    Sets approval_status to 'denied' and records the timestamp
    for the auto-deletion countdown.
    Returns True if the account was pending and is now denied.
    """
    reason = _normalize_decision_reason(reason)
    from ._events import record_event
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE accounts SET approval_status='denied', "
            "denied_by=?, approved_by=NULL, denied_at=?, approved_denied_at=?, "
            "denied_notified=0, decision_reason=? "
            "WHERE id=? AND approval_status='pending'",
            (admin_id, now, now, reason, account_id),
        )
        if cur.rowcount:
            record_event(conn, "account", account_id, "account.denied", admin_id, reason)
        conn.commit()
        return cur.rowcount > 0


def cleanup_denied_hosting_accounts(timeout_seconds=86400):
    """Delete denied accounts whose denial is older than *timeout_seconds*.

    Special values:
        -1  = never delete (disabled)
         0  = delete immediately
        >0  = delete after *timeout_seconds* seconds

    Returns the number of accounts deleted.
    """
    if timeout_seconds == -1:
        return 0
    if timeout_seconds == 0:
        with get_hosting_db_context() as conn:
            cur = conn.execute(
                "DELETE FROM accounts WHERE approval_status='denied'"
            )
            conn.commit()
            return cur.rowcount
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    with get_hosting_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM accounts WHERE approval_status='denied' "
            "AND denied_at IS NOT NULL AND denied_at < ?",
            (cutoff,),
        )
        conn.commit()
    return cur.rowcount


def count_denied_hosting_accounts():
    """Return the number of hosting accounts with approval_status='denied'."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE approval_status='denied'"
        ).fetchone()
    return row[0] if row else 0


def approve_all_pending_hosting_accounts(admin_id=None):
    """Approve every pending hosting account in bulk.

    Used when the admin switches away from approval mode so existing
    pending accounts are not left stuck.
    Returns the number of accounts approved.
    """
    with get_hosting_db_context() as conn:
        from ._events import record_event
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute("SELECT id FROM accounts WHERE approval_status='pending'").fetchall()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE accounts SET approval_status='approved', "
            "approved_by=?, denied_by=NULL, denied_at=NULL, denied_notified=0, "
            "approved_denied_at=?, decision_reason='' "
            "WHERE approval_status='pending'",
            (admin_id or "", now),
        )
        for account in pending:
            record_event(conn, "account", account["id"], "account.approved", admin_id, "Approval requirement removed by administrator.")
        conn.commit()
        return cur.rowcount


def record_account_suspension_action(account_id, action, performed_by=None,
                                     reason=None, reason_visible=False,
                                     time_visible=False, duration=None,
                                     suspended_until=None):
    """Insert a row into account_suspension_audit."""
    with get_hosting_db_context() as conn:
        conn.execute(
            "INSERT INTO account_suspension_audit "
            "(account_id, action, reason, reason_visible, time_visible, "
            "duration, suspended_until, performed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                action,
                reason,
                1 if reason_visible else 0,
                1 if time_visible else 0,
                duration,
                suspended_until,
                performed_by,
            ),
        )
        from ._events import record_event
        record_event(conn, "account", account_id, "account." + action, performed_by, reason)
        conn.commit()


def set_account_theme_mode(account_id, theme_mode):
    """Set the theme mode preference for an account.

    *theme_mode* must be ``'dark'``, ``'light'``, or ``'default'``.
    ``'default'`` means the platform's default mode applies.
    """
    if theme_mode not in ("dark", "light", "default"):
        theme_mode = "default"
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE accounts SET theme_mode=? WHERE id=?",
            (theme_mode, account_id),
        )
        conn.commit()


def get_account_theme_mode(account_id):
    """Return the theme mode for *account_id*, or ``'default'``."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT theme_mode FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    if row and row[0] in ("dark", "light"):
        return row[0]
    return "default"


def get_account_suspension_history(account_id):
    """Return the full suspension audit log for an account, newest first."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT sa.*, a.username AS performed_by_name "
            "FROM account_suspension_audit sa "
            "LEFT JOIN accounts a ON sa.performed_by = a.id "
            "WHERE sa.account_id = ? "
            "ORDER BY sa.created_at DESC",
            (account_id,),
        ).fetchall()


def update_hosting_account(account_id, **kwargs):
    """Update arbitrary columns on a hosting account.

    Accepts keyword arguments matching column names in the
    ``accounts`` table.  Only the provided columns are updated.
    """
    _ALLOWED_COLUMNS = {
        "username", "password", "is_admin", "suspended",
        "suspended_at", "suspend_reason", "suspended_until",
        "suspend_reason_visible", "suspend_time_visible",
        "approval_status", "denied_at", "denied_notified",
        "approved_by", "denied_by", "approved_denied_at", "decision_reason",
        "theme_mode",
        "email", "terms_accepted_at", "terms_version",
        "email_prompt_dismissed",
        "email_verified_at", "email_verification_token_hash",
        "email_verification_sent_at", "email_verification_expires_at",
        "password_reset_token_hash", "password_reset_sent_at",
        "password_reset_expires_at",
        "session_version",
        "deleted_at",
        "pending_merge_source_id", "pending_merge_target_id",
        "totp_secret_encrypted", "totp_enabled", "totp_recovery_hashes",
        "totp_enabled_at",
        "email_flagged_invalid", "email_flag_reason", "email_flagged_previous",
        "email_flag_reason_visible",
        "signup_use_case",
    }
    allowed = {k: v for k, v in kwargs.items() if k in _ALLOWED_COLUMNS}
    if not allowed:
        return

    sets = ", ".join(f"{k} = ?" for k in allowed)
    values = list(allowed.values()) + [account_id]
    with get_hosting_db_context() as conn:
        conn.execute(
            f"UPDATE accounts SET {sets} WHERE id = ?",
            values,
        )
        conn.commit()


def flag_email_invalid(account_id, reason="", reason_visible=False,
                       replacement_email=None):
    """Mark a user's email as invalid, forcing them to re-enter it.

    *reason_visible* controls whether the reason text is displayed to
    the user on the re-entry page.

    When *replacement_email* is provided the email is changed directly
    on behalf of the user and no flag is set. The user is not
    prompted and can continue using the platform immediately.

    Otherwise the rejected address is saved in ``email_flagged_previous``
    so the platform can optionally block re-entry of the same address
    (controlled by the ``email_flag_block_reentry`` hosting setting).
    """
    from ._connection import get_hosting_db_context

    if replacement_email:
        # Admin is setting a new email directly. No flag needed.
        update_hosting_account(
            account_id,
            email=replacement_email.strip().lower(),
            email_verified_at=None,
            email_verification_token_hash="",
            email_verification_sent_at=None,
            email_verification_expires_at=None,
            # Clear any existing flag state
            email_flagged_invalid=0,
            email_flag_reason="",
            email_flagged_previous="",
            email_flag_reason_visible=0,
        )
        return

    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT email FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    old_email = (row["email"] if row else "") or ""
    update_hosting_account(
        account_id,
        email_flagged_invalid=1,
        email_flag_reason=reason or "",
        email_flag_reason_visible=1 if reason_visible else 0,
        email_flagged_previous=old_email.lower(),
        email="",
        email_verified_at=None,
        email_verification_token_hash="",
        email_verification_sent_at=None,
        email_verification_expires_at=None,
    )


def clear_email_flag(account_id):
    """Remove the invalid-email flag after the user provides a new address."""
    update_hosting_account(
        account_id,
        email_flagged_invalid=0,
        email_flag_reason="",
        email_flagged_previous="",
        email_flag_reason_visible=0,
    )
