"""Pending contribution management for the draft approval system.

Read-only users (role='user') can propose edits to wiki pages when the
contribution_approval_enabled site setting is active.  Each proposed edit
is stored as a "pending contribution" that an admin must approve or deny
before the changes are applied.

Key constraints enforced at the DB level:
- One pending contribution per (page_id, user_id): UNIQUE constraint.
- Status must be one of: pending, approved, denied, expired, withdrawn.
- Per-user quota limits how many pending contributions a user may have.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from ._connection import get_db_context, retry_on_busy
from ._settings import get_site_settings


MAX_QUOTA_REQUEST_REASON_LENGTH = 2000
MAX_QUOTA_REVIEW_REASON_LENGTH = 1000
MAX_CONTRIBUTION_REASON_LENGTH = 2000
MAX_REVIEW_REASON_LENGTH = 500


def _normalize_quota(value):
    """Normalise a contribution quota: -1 for unlimited, otherwise at least 1."""
    value = int(value)
    if value == -1:
        return -1
    return max(1, value)


def create_contribution(page_id, user_id, title, content, reason):
    """Submit a new pending contribution for a page.

    Returns the new contribution id.  Raises ``IntegrityError`` if the
    user already has a pending contribution for this page (the UNIQUE
    constraint on page_id + user_id fires).
    """
    title = (title or "").strip()
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required for the proposed edit.")
    if len(reason) > MAX_CONTRIBUTION_REASON_LENGTH:
        raise ValueError(f"Reason cannot exceed {MAX_CONTRIBUTION_REASON_LENGTH} characters.")
    if len(title) > 200:
        raise ValueError("Title cannot exceed 200 characters.")
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO pending_contributions "
            "(page_id, user_id, title, content, reason, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (page_id, user_id, title, content, reason, now, now),
        )
        conn.commit()
        return cur.lastrowid


def update_contribution(contribution_id, title, content, reason):
    """Update the content of a still-pending contribution."""
    title = (title or "").strip()
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required for the proposed edit.")
    if len(reason) > MAX_CONTRIBUTION_REASON_LENGTH:
        raise ValueError(f"Reason cannot exceed {MAX_CONTRIBUTION_REASON_LENGTH} characters.")
    if len(title) > 200:
        raise ValueError("Title cannot exceed 200 characters.")
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE pending_contributions "
            "SET title=?, content=?, reason=?, updated_at=? "
            "WHERE id=? AND status='pending'",
            (title, content, reason, now, contribution_id),
        )
        conn.commit()


def withdraw_contribution(contribution_id, user_id):
    """Withdraw a pending contribution (by the user who created it)."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE pending_contributions "
            "SET status='withdrawn', updated_at=? "
            "WHERE id=? AND user_id=? AND status='pending'",
            (now, contribution_id, user_id),
        )
        conn.commit()


def auto_withdraw_contributions_for_promoted_user(user_id, reason="User promoted: direct edit access granted"):
    """Withdraw all pending contributions for a user who gained direct edit access.

    Called when a user's role is changed from user to editor/admin, or when
    their custom role is updated to include page.edit_all.  Pending contributions
    are no longer needed since the user can now edit directly.

    Returns the number of contributions withdrawn.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE pending_contributions "
            "SET status='withdrawn', review_reason=?, updated_at=? "
            "WHERE user_id=? AND status='pending'",
            (reason, now, user_id),
        )
        conn.commit()
        return cur.rowcount


def approve_contribution(contribution_id, reviewer_id, review_reason=""):
    """Approve a pending contribution and apply the edit to the page.

    Returns the approved row dict (with page_id, user_id, title, content)
    so the caller can apply the edit.  The caller is responsible for
    actually updating the page and cleaning up any related drafts.
    """
    review_reason = (review_reason or "").strip()
    if len(review_reason) > MAX_REVIEW_REASON_LENGTH:
        raise ValueError(f"Review reason cannot exceed {MAX_REVIEW_REASON_LENGTH} characters.")
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        row = conn.execute(
            "SELECT * FROM pending_contributions WHERE id=? AND status='pending'",
            (contribution_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE pending_contributions "
            "SET status='approved', reviewed_by=?, review_reason=?, reviewed_at=?, updated_at=? "
            "WHERE id=? AND status='pending'",
            (reviewer_id, review_reason, now, now, contribution_id),
        )
        conn.commit()
        return dict(row)


def deny_contribution(contribution_id, reviewer_id, review_reason=""):
    """Deny a pending contribution."""
    review_reason = (review_reason or "").strip()
    if len(review_reason) > MAX_REVIEW_REASON_LENGTH:
        raise ValueError(f"Review reason cannot exceed {MAX_REVIEW_REASON_LENGTH} characters.")
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE pending_contributions "
            "SET status='denied', reviewed_by=?, review_reason=?, reviewed_at=?, updated_at=? "
            "WHERE id=? AND status='pending'",
            (reviewer_id, review_reason, now, now, contribution_id),
        )
        conn.commit()


@retry_on_busy
def get_contribution(contribution_id):
    """Return a single contribution row dict (with username/page info), or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT pc.*, u.username, p.title AS page_title, p.slug AS page_slug "
            "FROM pending_contributions pc "
            "JOIN users u ON u.id = pc.user_id "
            "JOIN pages p ON p.id = pc.page_id "
            "WHERE pc.id=?",
            (contribution_id,),
        ).fetchone()
        return dict(row) if row else None


@retry_on_busy
def get_pending_contribution_for_page(page_id, user_id):
    """Return the user's pending (status='pending') contribution for a page, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM pending_contributions "
            "WHERE page_id=? AND user_id=? AND status='pending'",
            (page_id, user_id),
        ).fetchone()
        return dict(row) if row else None


@retry_on_busy
def get_user_contribution_for_page(page_id, user_id):
    """Return the user's latest non-expired/withdrawn contribution for a page, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM pending_contributions "
            "WHERE page_id=? AND user_id=? AND status NOT IN ('expired','withdrawn') "
            "ORDER BY updated_at DESC LIMIT 1",
            (page_id, user_id),
        ).fetchone()
        return dict(row) if row else None


@retry_on_busy
def list_pending_contributions():
    """Return all pending contributions (for admin review page)."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT pc.*, u.username, p.title AS page_title, p.slug AS page_slug "
            "FROM pending_contributions pc "
            "JOIN users u ON u.id = pc.user_id "
            "JOIN pages p ON p.id = pc.page_id "
            "WHERE pc.status='pending' "
            "ORDER BY pc.created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


@retry_on_busy
def list_user_contributions(user_id):
    """Return all contributions for a user (all statuses)."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT pc.*, p.title AS page_title, p.slug AS page_slug "
            "FROM pending_contributions pc "
            "JOIN pages p ON p.id = pc.page_id "
            "WHERE pc.user_id=? "
            "ORDER BY pc.updated_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@retry_on_busy
def list_contributions_for_page(page_id):
    """Return all non-expired contributions for a page (for conflict detection)."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT pc.*, u.username "
            "FROM pending_contributions pc "
            "JOIN users u ON u.id = pc.user_id "
            "WHERE pc.page_id=? AND pc.status NOT IN ('expired','withdrawn') "
            "ORDER BY pc.updated_at DESC",
            (page_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@retry_on_busy
def count_pending_contributions_for_page(page_id):
    """Count pending contributions for a page."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_contributions "
            "WHERE page_id=? AND status='pending'",
            (page_id,),
        ).fetchone()
        return row[0] if row else 0


@retry_on_busy
def count_pending_contributions():
    """Count all pending contributions (for admin badge)."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_contributions WHERE status='pending'"
        ).fetchone()
        return row[0] if row else 0


def cleanup_expired_contributions(expiration_hours):
    """Mark pending contributions as expired if older than expiration_hours.

    Returns the number of contributions expired.
    """
    if not expiration_hours or expiration_hours <= 0:
        return 0
    with get_db_context() as conn:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=expiration_hours)
        cutoff_iso = cutoff.isoformat()
        cur = conn.execute(
            "UPDATE pending_contributions "
            "SET status='expired', updated_at=? "
            "WHERE status='pending' AND created_at < ?",
            (datetime.now(timezone.utc).isoformat(), cutoff_iso),
        )
        conn.commit()
        return cur.rowcount


def cleanup_reviewed_contributions(retention_days=90):
    """Delete old approved/denied/expired/withdrawn contributions beyond retention.

    Returns the number of rows deleted.
    """
    if not retention_days or retention_days <= 0:
        return 0
    with get_db_context() as conn:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        cutoff_iso = cutoff.isoformat()
        cur = conn.execute(
            "DELETE FROM pending_contributions "
            "WHERE status IN ('approved','denied','expired','withdrawn') "
            "AND updated_at < ?",
            (cutoff_iso,),
        )
        conn.commit()
        return cur.rowcount


# Contribution quotas follow the same per-user override plus site default
# layering as the reservation quotas in db/_reservations.py; keep the two in step.

@retry_on_busy
def get_default_contribution_quota(settings=None):
    """Return the default concurrent contribution quota from site settings."""
    settings = settings or get_site_settings()
    try:
        return max(1, int(settings["default_contribution_quota"]))
    except (KeyError, TypeError, ValueError):
        return 5


@retry_on_busy
def get_effective_contribution_quota(user_id, settings=None, user_quota=None):
    """Return the active contribution quota for the specified user.

    Returns -1 when the user has unlimited quota, otherwise a positive int.
    """
    if user_quota is None:
        with get_db_context() as conn:
            user = conn.execute(
                "SELECT contribution_quota FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            user_quota = user["contribution_quota"] if user else None
    if user_quota is not None:
        try:
            return _normalize_quota(user_quota)
        except (TypeError, ValueError):
            pass
    return get_default_contribution_quota(settings=settings)


@retry_on_busy
def get_user_pending_contribution_count(user_id):
    """Return the number of pending contributions currently held by *user_id*."""
    with get_db_context() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM pending_contributions "
            "WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()[0]
        return count


@retry_on_busy
def can_user_submit_contribution(user_id, settings=None):
    """Return True when the user has not reached their contribution quota."""
    quota = get_effective_contribution_quota(user_id, settings=settings)
    if quota == -1:
        return True
    return get_user_pending_contribution_count(user_id) < quota


@retry_on_busy
def get_pending_contribution_quota_request(user_id):
    """Return the user's pending contribution quota request, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT cqr.*, reviewer.username AS reviewed_by_username "
            "FROM contribution_quota_requests cqr "
            "LEFT JOIN users reviewer ON reviewer.id = cqr.reviewed_by "
            "WHERE cqr.user_id=? AND cqr.status='pending' "
            "ORDER BY cqr.created_at DESC, cqr.id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row


@retry_on_busy
def list_contribution_quota_requests(user_id):
    """Return every contribution quota request the user has made, newest first."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT cqr.*, reviewer.username AS reviewed_by_username "
            "FROM contribution_quota_requests cqr "
            "LEFT JOIN users reviewer ON reviewer.id = cqr.reviewed_by "
            "WHERE cqr.user_id=? "
            "ORDER BY cqr.created_at DESC, cqr.id DESC",
            (user_id,),
        ).fetchall()
        return rows


@retry_on_busy
def get_contribution_quota_request(request_id):
    """Return a single contribution quota request with reviewer details."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT cqr.*, reviewer.username AS reviewed_by_username "
            "FROM contribution_quota_requests cqr "
            "LEFT JOIN users reviewer ON reviewer.id = cqr.reviewed_by "
            "WHERE cqr.id=?",
            (request_id,),
        ).fetchone()
        return row


def _get_quota_request_cooldown_hours():
    """Return the site-wide cooldown, in hours, between contribution quota
    requests (0 disables the cooldown)."""
    from ._settings import get_site_settings
    settings = get_site_settings()
    try:
        return max(0, int(settings["quota_request_cooldown_hours"]))
    except (KeyError, TypeError, ValueError):
        return 0


def create_contribution_quota_request(user_id, requested_quota, reason):
    """Ask an admin to raise the user's concurrent contribution quota.

    *requested_quota* may be ``-1`` to request unlimited quota, or any
    positive integer.

    Returns the resulting request row. Raises ``ValueError`` if a cooldown
    period is active for the user.
    """
    requested_quota = _normalize_quota(requested_quota)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to continue")
    if len(reason) > MAX_QUOTA_REQUEST_REASON_LENGTH:
        raise ValueError(f"Reason cannot exceed {MAX_QUOTA_REQUEST_REASON_LENGTH} characters.")

    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat()
        settings = conn.execute(
            "SELECT quota_request_cooldown_hours, contribution_quota_auto_approve_max, "
            "default_contribution_quota "
            "FROM site_settings WHERE id=1"
        ).fetchone()
        try:
            cooldown_hours = max(0, int(settings["quota_request_cooldown_hours"]))
        except (KeyError, TypeError, ValueError):
            cooldown_hours = 0
        try:
            auto_approve_max = max(0, int(settings["contribution_quota_auto_approve_max"]))
        except (KeyError, TypeError, ValueError):
            auto_approve_max = 0
        user = conn.execute(
            "SELECT contribution_quota, quota_request_cooldown_until "
            "FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if conn.execute(
            "SELECT 1 FROM contribution_quota_requests "
            "WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone():
            conn.rollback()
            raise ValueError("You already have a pending quota request.")
        if cooldown_hours > 0:
            if user and user["quota_request_cooldown_until"]:
                cooldown_dt = None
                try:
                    cooldown_dt = datetime.fromisoformat(
                        user["quota_request_cooldown_until"].replace("Z", "+00:00")
                    )
                    if cooldown_dt.tzinfo is None:
                        cooldown_dt = cooldown_dt.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    cooldown_dt = None
                if cooldown_dt and cooldown_dt > datetime.now(timezone.utc):
                    remaining = (cooldown_dt - datetime.now(timezone.utc)).total_seconds()
                    mins = int(remaining // 60)
                    raise ValueError(
                        f"You must wait {mins} minute(s) before submitting a new quota request."
                    )
        stored_quota = user["contribution_quota"] if user else None
        current_quota = (
            stored_quota
            if stored_quota is not None
            else get_default_contribution_quota(settings=settings)
        )
        auto_approved = (
            requested_quota != -1
            and current_quota != -1
            and current_quota < requested_quota <= auto_approve_max
        )
        status = "approved" if auto_approved else "pending"
        review_reason = (
            f"Automatically approved because the requested quota of {requested_quota} "
            f"is at or below the configured threshold of {auto_approve_max}."
            if auto_approved else ""
        )
        try:
            cur = conn.execute(
                "INSERT INTO contribution_quota_requests "
                "(user_id, requested_quota, reason, status, review_reason, review_source, "
                "reviewed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    requested_quota,
                    reason,
                    status,
                    review_reason,
                    "automatic" if auto_approved else "manual",
                    now if auto_approved else None,
                    now,
                ),
            )
            if auto_approved:
                conn.execute(
                    "UPDATE users SET contribution_quota=? WHERE id=?",
                    (requested_quota, user_id),
                )
                _set_quota_cooldown(conn, user_id, cooldown_hours=cooldown_hours)
            result = conn.execute(
                "SELECT cqr.*, reviewer.username AS reviewed_by_username "
                "FROM contribution_quota_requests cqr "
                "LEFT JOIN users reviewer ON reviewer.id = cqr.reviewed_by "
                "WHERE cqr.id=?",
                (cur.lastrowid,),
            ).fetchone()
            conn.commit()
            return result
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("You already have a pending quota request.") from exc


def _set_quota_cooldown(conn, user_id, cooldown_hours=None):
    """Start the user's contribution quota request cooldown from the site setting."""
    if cooldown_hours is None:
        cooldown_hours = _get_quota_request_cooldown_hours()
    if cooldown_hours > 0:
        from datetime import timedelta
        cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)).isoformat()
        conn.execute(
            "UPDATE users SET quota_request_cooldown_until=? WHERE id=?",
            (cooldown_until, user_id),
        )


def review_contribution_quota_request(request_id, reviewed_by, approved, review_reason=""):
    """Approve or deny a contribution quota request and return the updated row."""
    review_reason = (review_reason or "").strip()
    if len(review_reason) > MAX_QUOTA_REVIEW_REASON_LENGTH:
        raise ValueError(
            f"Review reason cannot exceed {MAX_QUOTA_REVIEW_REASON_LENGTH} characters."
        )
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        request_row = conn.execute(
            "SELECT * FROM contribution_quota_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if not request_row:
            raise ValueError("Quota request not found")
        if request_row["status"] != "pending":
            raise ValueError("Quota request has already been reviewed")

        status = "approved" if approved else "denied"
        conn.execute(
            "UPDATE contribution_quota_requests "
            "SET status=?, reviewed_by=?, review_reason=?, review_source='manual', reviewed_at=? "
            "WHERE id=?",
            (status, reviewed_by, review_reason, now, request_id),
        )
        if approved:
            conn.execute(
                "UPDATE users SET contribution_quota=? WHERE id=?",
                (request_row["requested_quota"], request_row["user_id"]),
            )
        _set_quota_cooldown(conn, request_row["user_id"])
        conn.commit()
    return get_contribution_quota_request(request_id)


def cancel_contribution_quota_request(request_id, user_id):
    """Withdraw a pending contribution quota request, freeing the user to file another.

    Only the request owner may cancel, and only while the request is pending.
    """
    with get_db_context() as conn:
        request_row = conn.execute(
            "SELECT * FROM contribution_quota_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if not request_row:
            raise ValueError("Quota request not found")
        if request_row["user_id"] != user_id:
            raise ValueError("You do not have permission to cancel this request")
        if request_row["status"] != "pending":
            raise ValueError("Only pending quota requests can be cancelled")

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE contribution_quota_requests SET status='cancelled', reviewed_at=? WHERE id=?",
            (now, request_id),
        )
        _set_quota_cooldown(conn, user_id)
        conn.commit()


def set_user_contribution_quota(user_id, quota):
    """Directly set a user's contribution quota (admin action).

    *quota* may be ``-1`` for unlimited, ``None`` to reset to default,
    or any positive integer.
    """
    if quota is None:
        with get_db_context() as conn:
            conn.execute("UPDATE users SET contribution_quota=NULL WHERE id=?", (user_id,))
            conn.commit()
        return
    quota = _normalize_quota(quota)
    with get_db_context() as conn:
        conn.execute("UPDATE users SET contribution_quota=? WHERE id=?", (quota, user_id))
        conn.commit()


def count_pending_contribution_quota_requests():
    """Return the total number of pending contribution quota requests across all users."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM contribution_quota_requests WHERE status='pending'"
        ).fetchone()
        return row[0] if row else 0


def cleanup_resolved_contribution_quota_requests(retention_days=90):
    """Delete old approved/denied/cancelled quota requests beyond retention.

    Returns the number of rows deleted.
    """
    if not retention_days or retention_days <= 0:
        return 0
    with get_db_context() as conn:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        cutoff_iso = cutoff.isoformat()
        cur = conn.execute(
            "DELETE FROM contribution_quota_requests "
            "WHERE status IN ('approved','denied','cancelled') "
            "AND created_at < ?",
            (cutoff_iso,),
        )
        conn.commit()
        return cur.rowcount
