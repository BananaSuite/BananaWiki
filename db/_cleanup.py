"""Database cleanup and optimization routines.

Provides auto-removal of stale data that accumulates over time:
- Expired / used / soft-deleted invite codes
- Stale drafts (untouched beyond a configurable retention period)
- Notified badge notifications
- Released page reservations and expired cooldowns
- Old login-attempt records
- Soft-deleted chat and group messages
- Expired announcements
- Old resolved reservation quota requests
- Revoked badges past retention
- Old role and username history entries
- Expired group member timeouts
- SQLite maintenance (ANALYZE + VACUUM)

The :func:`run_full_cleanup` helper orchestrates every individual cleanup
function and returns a summary dict.

It also exposes :func:`try_acquire_cleanup_lease` /
:func:`release_cleanup_lease` for leader-election among Gunicorn workers
so that periodic cleanup runs at most once per interval across the whole
process group, rather than once per worker.
"""

from datetime import datetime, timedelta, timezone

from ._connection import get_db_context


def try_acquire_cleanup_lease(holder_id, ttl_seconds=600):
    """Attempt to acquire the singleton cleanup lease.

    Returns ``True`` if *holder_id* now owns the lease (and should run
    cleanup), ``False`` otherwise.  The lease is released either by the
    explicit call to :func:`release_cleanup_lease` or by expiry after
    *ttl_seconds*; the TTL guards against a worker crashing while
    holding the lease.

    The implementation uses a single ``UPDATE`` whose ``WHERE`` clause
    makes the take-over atomic on SQLite (which serializes writes via
    ``BEGIN IMMEDIATE``):

    * If no one holds the lease (``held_by IS NULL``) → take it.
    * If the previous holder's TTL has passed (``expires_at < now``) →
      take it from them.
    * If the same holder is re-acquiring → refresh.
    """
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
    now_iso = now.isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE cleanup_lease "
            "SET held_by=?, acquired_at=?, expires_at=? "
            "WHERE id=1 AND ("
            "  held_by IS NULL "
            "  OR held_by=? "
            "  OR expires_at IS NULL "
            "  OR expires_at < ?"
            ")",
            (holder_id, now_iso, expires, holder_id, now_iso),
        )
        conn.commit()
        return cur.rowcount > 0


def release_cleanup_lease(holder_id):
    """Release the cleanup lease if (and only if) *holder_id* owns it.

    A non-owning caller is a no-op.  After release the next call to
    :func:`try_acquire_cleanup_lease` from any worker will succeed.
    """
    with get_db_context() as conn:
        conn.execute(
            "UPDATE cleanup_lease "
            "SET held_by=NULL, acquired_at=NULL, expires_at=NULL "
            "WHERE id=1 AND held_by=?",
            (holder_id,),
        )
        conn.commit()


def _validate_retention_days(retention_days):
    """Raise ``ValueError`` if *retention_days* is less than 1.

    A negative value would flip the sign in the SQLite modifier
    ``'-' || retention_days || ' days'``, turning it into a future date
    and causing every existing row to match the ``DELETE`` condition.
    """
    if retention_days < 1:
        raise ValueError(
            f"retention_days must be >= 1, got {retention_days}"
        )


# Each helper below is independently callable, validates its own retention
# window and returns the number of rows it removed; run_full_cleanup at the
# end of the module just sequences them.

def cleanup_expired_invite_codes(retention_days=30):
    """Permanently delete invite codes that are expired, used, or soft-deleted
    and older than *retention_days*.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """DELETE FROM invite_codes
               WHERE (
                   use_count >= max_uses
                   OR deleted = 1
                   OR (expires_at IS NOT NULL AND julianday(expires_at) <= julianday(?))
               )
               AND datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (now, retention_days),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_stale_drafts(retention_days=30):
    """Delete drafts that have not been updated within *retention_days*.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM drafts
               WHERE datetime(updated_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_expired_drafts(expiration_hours):
    """Delete drafts older than *expiration_hours* (configurable draft expiration).

    This is separate from ``cleanup_stale_drafts`` which uses days.
    Returns 0 if expiration_hours is 0 or None (disabled).
    Returns the number of rows removed.
    """
    if not expiration_hours or expiration_hours <= 0:
        return 0
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM drafts
               WHERE datetime(updated_at) < datetime('now', '-' || ? || ' hours')""",
            (expiration_hours,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_notified_badge_notifications(retention_days=7):
    """Delete badge notifications that have already been shown to users
    (``notified = 1``) and are older than *retention_days*.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM badge_notifications
               WHERE notified = 1
               AND datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_released_reservations(retention_days=30):
    """Delete page reservations that have been released (``released_at IS NOT NULL``)
    and are older than *retention_days*.

    Also deletes any expired cooldowns (delegated to the existing helper in
    ``_reservations.py`` which is idempotent).

    Returns the number of reservation rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        # Delete old released reservations
        cur = conn.execute(
            """DELETE FROM page_reservations
               WHERE released_at IS NOT NULL
               AND datetime(released_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        reservations = cur.rowcount
        # Also sweep expired cooldowns
        conn.execute(
            "DELETE FROM user_page_cooldowns WHERE julianday(cooldown_until) <= julianday(?)",
            (now,),
        )
        conn.commit()
    return reservations


def cleanup_old_login_attempts(retention_days=7):
    """Delete login-attempt records older than *retention_days*.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM login_attempts
               WHERE datetime(attempted_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_old_rate_limit_hits(retention_days=1):
    """Delete rate-limit-hit records older than *retention_days*.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM rate_limit_hits
               WHERE datetime(hit_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_soft_deleted_messages(retention_days=7):
    """Permanently delete chat and group messages that were soft-deleted
    (``is_deleted = 1``) more than *retention_days* ago.

    Returns a dict with ``dm`` and ``group`` counts.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:

        # Direct messages
        dm_cur = conn.execute(
            """DELETE FROM chat_messages
               WHERE is_deleted = 1
               AND deleted_at IS NOT NULL
               AND datetime(deleted_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        dm_count = dm_cur.rowcount

        # Group messages
        grp_cur = conn.execute(
            """DELETE FROM group_messages
               WHERE is_deleted = 1
               AND deleted_at IS NOT NULL
               AND datetime(deleted_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        grp_count = grp_cur.rowcount

        conn.commit()
    return {"dm": dm_count, "group": grp_count}


def cleanup_expired_announcements(retention_days=30):
    """Delete inactive announcements whose expiry date has passed and that are
    older than *retention_days* beyond their expiry.

    Only removes announcements that are **not** currently active
    (``is_active = 0``) **or** whose ``expires_at`` is in the past.
    Active announcements without an expiry are never touched.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """DELETE FROM announcements
               WHERE (
                   (expires_at IS NOT NULL AND julianday(expires_at) <= julianday(?))
                   OR is_active = 0
               )
               AND datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (now, retention_days),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_resolved_quota_requests(retention_days=90):
    """Delete reservation quota requests that have been resolved (approved,
    denied, or cancelled) and are older than *retention_days*.

    Pending requests are never touched.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM reservation_quota_requests
               WHERE status IN ('approved', 'denied', 'cancelled')
               AND datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_revoked_badges(retention_days=90):
    """Delete badge records that were revoked more than *retention_days* ago.

    Only removes badges where ``revoked = 1`` and ``revoked_at`` is far
    enough in the past.  Active badges are never touched.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM user_badges
               WHERE revoked = 1
               AND revoked_at IS NOT NULL
               AND datetime(revoked_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_old_role_history(retention_days=365):
    """Delete role history entries older than *retention_days*.

    Role history is useful for auditing but does not need to be retained
    indefinitely.  One year is the default retention.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM role_history
               WHERE datetime(changed_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_old_username_history(retention_days=365):
    """Delete username history entries older than *retention_days*.

    Username changes are useful for auditing but do not need to be kept
    indefinitely.  One year is the default retention.

    Returns the number of rows removed.
    """
    _validate_retention_days(retention_days)
    with get_db_context() as conn:
        cur = conn.execute(
            """DELETE FROM username_history
               WHERE datetime(changed_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def cleanup_expired_group_timeouts():
    """Clear group member timeouts that have already elapsed.

    Sets ``timed_out_until`` to NULL for members whose timeout is in the
    past, freeing them to post again without waiting for a reactive check.

    Returns the number of members un-timed-out.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE group_members SET timed_out_until = NULL "
            "WHERE timed_out_until IS NOT NULL AND julianday(timed_out_until) <= julianday(?)",
            (now,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def optimize_database():
    """Run SQLite maintenance commands.

    - ``PRAGMA optimize``: runs ANALYZE on tables that would benefit.
    - ``VACUUM``: rebuilds the database file to reclaim free pages.

    VACUUM acquires an exclusive lock and can be slow on large databases.
    If the lock cannot be obtained within the connection timeout, the
    operation is silently skipped so that regular request traffic is not
    blocked.
    """
    with get_db_context() as conn:
        try:
            conn.execute("PRAGMA optimize")
            conn.execute("VACUUM")
        except Exception:
            # VACUUM may fail if another connection holds a lock; this is
            # acceptable during scheduled maintenance: it will succeed on
            # the next cycle.
            pass


def cleanup_expired_suspensions():
    """Auto-unsuspend users whose timed suspension has elapsed.

    Only affects users with ``suspended = 1`` **and** a non-NULL
    ``suspended_until`` that is in the past.  Permanent suspensions
    (``suspended_until IS NULL``) are never touched.

    Returns the number of users unsuspended.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE users SET suspended = 0, suspended_until = NULL, "
            "suspend_reason = NULL, suspend_reason_visible = 0, suspend_time_visible = 0 "
            "WHERE suspended = 1 AND suspended_until IS NOT NULL "
            "AND julianday(suspended_until) <= julianday(?)",
            (now,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def _cleanup_expired_temporary():
    """Process expired temporary pages, users, and roles.

    Delegates to :func:`db.cleanup_all_expired_temporary`; returns a summary
    dict or ``0`` if the tables don't exist yet.
    """
    try:
        from ._temporary import cleanup_all_expired_temporary
        return cleanup_all_expired_temporary()
    except Exception:
        return 0


def run_full_cleanup(
    invite_retention_days=30,
    draft_retention_days=30,
    draft_expiration_hours=0,
    badge_notification_retention_days=7,
    reservation_retention_days=30,
    login_attempt_retention_days=7,
    rate_limit_hit_retention_days=1,
    soft_delete_retention_days=7,
    announcement_retention_days=30,
    quota_request_retention_days=90,
    revoked_badge_retention_days=90,
    role_history_retention_days=365,
    username_history_retention_days=365,
    contribution_retention_days=90,
    vacuum=True,
):
    """Run every cleanup routine and return a summary dict.

    Each key maps to the number of rows removed (or a sub-dict for messages).
    The ``optimized`` key is ``True`` when VACUUM was executed.
    """
    from ._contributions import (
        cleanup_expired_contributions,
        cleanup_reviewed_contributions,
        cleanup_resolved_contribution_quota_requests,
    )

    summary = {
        "expired_invite_codes": cleanup_expired_invite_codes(invite_retention_days),
        "stale_drafts": cleanup_stale_drafts(draft_retention_days),
        "expired_drafts": cleanup_expired_drafts(draft_expiration_hours),
        "notified_badge_notifications": cleanup_notified_badge_notifications(
            badge_notification_retention_days
        ),
        "released_reservations": cleanup_released_reservations(reservation_retention_days),
        "old_login_attempts": cleanup_old_login_attempts(login_attempt_retention_days),
        "old_rate_limit_hits": cleanup_old_rate_limit_hits(rate_limit_hit_retention_days),
        "soft_deleted_messages": cleanup_soft_deleted_messages(soft_delete_retention_days),
        "expired_announcements": cleanup_expired_announcements(announcement_retention_days),
        "resolved_quota_requests": cleanup_resolved_quota_requests(
            quota_request_retention_days
        ),
        "revoked_badges": cleanup_revoked_badges(revoked_badge_retention_days),
        "old_role_history": cleanup_old_role_history(role_history_retention_days),
        "old_username_history": cleanup_old_username_history(
            username_history_retention_days
        ),
        "expired_group_timeouts": cleanup_expired_group_timeouts(),
        "expired_suspensions": cleanup_expired_suspensions(),
        "expired_temporary": _cleanup_expired_temporary(),
        "expired_contributions": cleanup_expired_contributions(draft_expiration_hours or 0),
        "reviewed_contributions": cleanup_reviewed_contributions(contribution_retention_days),
        "resolved_contribution_quota_requests": cleanup_resolved_contribution_quota_requests(
            contribution_retention_days
        ),
        "optimized": False,
    }
    if vacuum:
        optimize_database()
        summary["optimized"] = True
    return summary
