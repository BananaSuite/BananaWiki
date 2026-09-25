"""Page reservation management."""

import sqlite3
from datetime import datetime, timedelta, timezone

import config
from ._connection import get_db_context, retry_on_busy
from ._settings import get_site_settings

MAX_QUOTA_REQUEST_REASON_LENGTH = 2000
MAX_QUOTA_REVIEW_REASON_LENGTH = 1000


def _normalize_quota(value):
    """Normalise a reservation quota: -1 for unlimited, otherwise at least 1."""
    value = int(value)
    if value == -1:
        return -1
    return max(1, value)


def reservations_enabled():
    """Return True when the page reservation system is enabled site-wide."""
    settings = get_site_settings()
    return bool(settings and settings["page_reservations_enabled"])


def _get_reservation_hours(settings, key, default, minimum):
    """Return a validated reservation hour setting with config fallback.

    Args:
        settings: Site settings row or mapping to read from.
        key: Setting name containing the reservation hour value.
        default: Fallback hour value from config.
        minimum: Minimum allowed hour value.

    Returns:
        int: A validated hour value suitable for timedelta(hours=...).
    """
    if not settings:
        return default
    try:
        return max(minimum, int(settings[key]))
    except (KeyError, TypeError, ValueError):
        return default


@retry_on_busy
def get_default_reserved_pages_quota(settings=None):
    """Return the default concurrent reservation quota from site settings."""
    settings = settings or get_site_settings()
    try:
        return max(1, int(settings["default_reserved_pages_quota"]))
    except (KeyError, TypeError, ValueError):
        return 5


@retry_on_busy
def get_effective_reserved_pages_quota(user_id, settings=None, user_quota=None):
    """Return the active reservation quota for the specified user.

    Returns -1 when the user has unlimited quota, otherwise a positive int.
    """
    if user_quota is None:
        with get_db_context() as conn:
            user = conn.execute(
                "SELECT reserved_pages_quota FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            user_quota = user["reserved_pages_quota"] if user else None
    if user_quota is not None:
        try:
            return _normalize_quota(user_quota)
        except (TypeError, ValueError):
            pass
    return get_default_reserved_pages_quota(settings=settings)


@retry_on_busy
def get_user_active_reservation_count(user_id):
    """Return the number of active reservations currently held by *user_id*."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        count = conn.execute(
            "SELECT COUNT(*) FROM page_reservations "
            "WHERE user_id=? AND datetime(expires_at) > datetime(?) AND released_at IS NULL",
            (user_id, now),
        ).fetchone()[0]
        return count


@retry_on_busy
def get_pending_reservation_quota_request(user_id):
    """Return the user's pending reservation quota request, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT rqr.*, reviewer.username AS reviewed_by_username "
            "FROM reservation_quota_requests rqr "
            "LEFT JOIN users reviewer ON reviewer.id = rqr.reviewed_by "
            "WHERE rqr.user_id=? AND rqr.status='pending' "
            "ORDER BY rqr.created_at DESC, rqr.id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row


@retry_on_busy
def list_reservation_quota_requests(user_id):
    """Return every reservation quota request the user has made, newest first."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT rqr.*, reviewer.username AS reviewed_by_username "
            "FROM reservation_quota_requests rqr "
            "LEFT JOIN users reviewer ON reviewer.id = rqr.reviewed_by "
            "WHERE rqr.user_id=? "
            "ORDER BY rqr.created_at DESC, rqr.id DESC",
            (user_id,),
        ).fetchall()
        return rows


@retry_on_busy
def get_reservation_quota_request(request_id):
    """Return a single reservation quota request with reviewer details."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT rqr.*, reviewer.username AS reviewed_by_username "
            "FROM reservation_quota_requests rqr "
            "LEFT JOIN users reviewer ON reviewer.id = rqr.reviewed_by "
            "WHERE rqr.id=?",
            (request_id,),
        ).fetchone()
        return row


def _get_quota_request_cooldown_hours():
    """Return the site-wide cooldown, in hours, between reservation quota
    requests (0 disables the cooldown)."""
    from ._settings import get_site_settings
    settings = get_site_settings()
    try:
        return max(0, int(settings["quota_request_cooldown_hours"]))
    except (KeyError, TypeError, ValueError):
        return 0


def create_reservation_quota_request(user_id, requested_quota, reason):
    """Ask an admin to raise the user's concurrent reservation quota.

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
            "SELECT quota_request_cooldown_hours, reservation_quota_auto_approve_max, "
            "default_reserved_pages_quota "
            "FROM site_settings WHERE id=1"
        ).fetchone()
        try:
            cooldown_hours = max(0, int(settings["quota_request_cooldown_hours"]))
        except (KeyError, TypeError, ValueError):
            cooldown_hours = 0
        try:
            auto_approve_max = max(0, int(settings["reservation_quota_auto_approve_max"]))
        except (KeyError, TypeError, ValueError):
            auto_approve_max = 0
        user = conn.execute(
            "SELECT reserved_pages_quota, quota_request_cooldown_until "
            "FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if conn.execute(
            "SELECT 1 FROM reservation_quota_requests "
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
        stored_quota = user["reserved_pages_quota"] if user else None
        current_quota = (
            stored_quota
            if stored_quota is not None
            else get_default_reserved_pages_quota(settings=settings)
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
                "INSERT INTO reservation_quota_requests "
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
                    "UPDATE users SET reserved_pages_quota=? WHERE id=?",
                    (requested_quota, user_id),
                )
                _set_quota_cooldown(conn, user_id, cooldown_hours=cooldown_hours)
            result = conn.execute(
                "SELECT rqr.*, reviewer.username AS reviewed_by_username "
                "FROM reservation_quota_requests rqr "
                "LEFT JOIN users reviewer ON reviewer.id = rqr.reviewed_by "
                "WHERE rqr.id=?",
                (cur.lastrowid,),
            ).fetchone()
            conn.commit()
            return result
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("You already have a pending quota request.") from exc


def review_reservation_quota_request(request_id, reviewed_by, approved, review_reason=""):
    """Approve or deny a quota request and return the updated row."""
    review_reason = (review_reason or "").strip()
    if len(review_reason) > MAX_QUOTA_REVIEW_REASON_LENGTH:
        raise ValueError(
            f"Review reason cannot exceed {MAX_QUOTA_REVIEW_REASON_LENGTH} characters."
        )
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        request_row = conn.execute(
            "SELECT * FROM reservation_quota_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if not request_row:
            raise ValueError("Quota request not found")
        if request_row["status"] != "pending":
            raise ValueError("Quota request has already been reviewed")

        status = "approved" if approved else "denied"
        conn.execute(
            "UPDATE reservation_quota_requests "
            "SET status=?, reviewed_by=?, review_reason=?, review_source='manual', reviewed_at=? "
            "WHERE id=?",
            (status, reviewed_by, review_reason, now, request_id),
        )
        if approved:
            conn.execute(
                "UPDATE users SET reserved_pages_quota=? WHERE id=?",
                (request_row["requested_quota"], request_row["user_id"]),
            )
        _set_quota_cooldown(conn, request_row["user_id"])
        conn.commit()
    return get_reservation_quota_request(request_id)


def _set_quota_cooldown(conn, user_id, cooldown_hours=None):
    """Start the user's reservation quota request cooldown from the site setting."""
    if cooldown_hours is None:
        cooldown_hours = _get_quota_request_cooldown_hours()
    if cooldown_hours > 0:
        cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)).isoformat()
        conn.execute(
            "UPDATE users SET quota_request_cooldown_until=? WHERE id=?",
            (cooldown_until, user_id),
        )


def cancel_reservation_quota_request(request_id, user_id):
    """Withdraw a pending reservation quota request, freeing the user to file another.

    Only the request owner may cancel, and only while the request is pending.
    """
    with get_db_context() as conn:
        request_row = conn.execute(
            "SELECT * FROM reservation_quota_requests WHERE id=?",
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
            "UPDATE reservation_quota_requests SET status='cancelled', reviewed_at=? WHERE id=?",
            (now, request_id),
        )
        _set_quota_cooldown(conn, user_id)
        conn.commit()


def set_user_reserved_pages_quota(user_id, quota):
    """Directly set a user's reservation quota (admin action).

    *quota* may be ``-1`` for unlimited, ``None`` to reset to default,
    or any positive integer.
    """
    if quota is not None:
        quota = _normalize_quota(quota)
    with get_db_context() as conn:
        conn.execute(
            "UPDATE users SET reserved_pages_quota=? WHERE id=?",
            (quota, user_id),
        )
        conn.commit()


def reserve_page(page_id, user_id):
    """
    Reserve a page for the given user.

    Returns:
        dict: Reservation data with keys: id, page_id, user_id, reserved_at, expires_at, released_at
        None: If the page is already reserved or user is in cooldown

    Raises:
        ValueError: If page is already reserved or user is in cooldown
    """
    if not reservations_enabled():
        raise ValueError("Page reservations are currently disabled")

    settings = get_site_settings()
    with get_db_context() as conn:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=_get_reservation_hours(
            settings, "page_reservation_duration_hours", config.PAGE_RESERVATION_DURATION_HOURS, 1
        ))

        try:
            # Use BEGIN IMMEDIATE to serialise concurrent reservation attempts
            conn.execute("BEGIN IMMEDIATE")

            page = conn.execute(
                "SELECT is_home FROM pages WHERE id=?", (page_id,)
            ).fetchone()
            if not page:
                conn.execute("ROLLBACK")
                raise ValueError("Page not found")
            if page["is_home"]:
                conn.execute("ROLLBACK")
                raise ValueError("The home page cannot be reserved")

            # Check if page is already reserved (and not expired or released)
            existing = conn.execute(
                "SELECT pr.*, u.role FROM page_reservations pr "
                "LEFT JOIN users u ON pr.user_id = u.id "
                "WHERE pr.page_id=? AND datetime(pr.expires_at) > datetime(?) AND pr.released_at IS NULL",
                (page_id, now.isoformat()),
            ).fetchone()

            if existing:
                # Auto-release if the reserving user was deleted or lost edit permissions
                ex_role = existing["role"] if "role" in existing.keys() else None
                if ex_role is None or ex_role not in ("editor", "admin", "owner"):
                    conn.execute(
                        "UPDATE page_reservations SET released_at=? WHERE id=?",
                        (now.isoformat(), existing["id"]),
                    )
                else:
                    conn.execute("ROLLBACK")
                    raise ValueError("Page is already reserved by another user")

            # Check if user is in cooldown for this page
            cooldown = conn.execute(
                "SELECT * FROM user_page_cooldowns WHERE page_id=? AND user_id=? AND datetime(cooldown_until) > datetime(?)",
                (page_id, user_id, now.isoformat()),
            ).fetchone()

            if cooldown:
                conn.execute("ROLLBACK")
                raise ValueError("User is in cooldown period for this page")

            active_count = conn.execute(
                "SELECT COUNT(*) FROM page_reservations "
                "WHERE user_id=? AND datetime(expires_at) > datetime(?) AND released_at IS NULL",
                (user_id, now.isoformat()),
            ).fetchone()[0]
            user_row = conn.execute(
                "SELECT reserved_pages_quota FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            quota = get_effective_reserved_pages_quota(
                user_id,
                settings=settings,
                user_quota=user_row["reserved_pages_quota"] if user_row else None,
            )
            if quota != -1 and active_count >= quota:
                conn.execute("ROLLBACK")
                raise ValueError("User has reached the reservation quota limit")

            # Delete any old released or expired reservations for this page
            conn.execute(
                "DELETE FROM page_reservations WHERE page_id=? AND (released_at IS NOT NULL OR datetime(expires_at) <= datetime(?))",
                (page_id, now.isoformat())
            )

            # Create reservation
            conn.execute(
                "INSERT INTO page_reservations (page_id, user_id, reserved_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (page_id, user_id, now.isoformat(), expires.isoformat()),
            )
            conn.commit()

            # Fetch the created reservation
            reservation = conn.execute(
                "SELECT * FROM page_reservations WHERE page_id=? AND user_id=?",
                (page_id, user_id),
            ).fetchone()
            return dict(reservation) if reservation else None
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


def release_page_reservation(page_id, user_id=None):
    """
    Release a page reservation and start cooldown for the user.

    Args:
        page_id: ID of the page to release
        user_id: If provided, only release if this user holds the reservation.
                If None, release regardless of who holds it.

    Returns:
        bool: True if reservation was released, False if no active reservation found
    """
    if not reservations_enabled():
        return False

    settings = get_site_settings()
    with get_db_context() as conn:
        now = datetime.now(timezone.utc)

        # Find active reservation
        query = "SELECT * FROM page_reservations WHERE page_id=? AND datetime(expires_at) > datetime(?) AND released_at IS NULL"
        params = [page_id, now.isoformat()]

        if user_id:
            query += " AND user_id=?"
            params.append(user_id)

        reservation = conn.execute(query, params).fetchone()

        if not reservation:
            return False

        # Mark as released
        conn.execute(
            "UPDATE page_reservations SET released_at=? WHERE id=?",
            (now.isoformat(), reservation["id"]),
        )

        # Create cooldown entry
        cooldown_until = now + timedelta(hours=_get_reservation_hours(
            settings, "page_reservation_cooldown_hours", config.PAGE_RESERVATION_COOLDOWN_HOURS, 0
        ))
        conn.execute(
            "INSERT OR REPLACE INTO user_page_cooldowns (page_id, user_id, cooldown_until) "
            "VALUES (?, ?, ?)",
            (page_id, reservation["user_id"], cooldown_until.isoformat()),
        )

        conn.commit()
        return True


def get_page_reservation_status(page_id, user_id=None):
    """
    Get the reservation status for a page.

    Returns:
        dict: {
            'is_reserved': bool,
            'reserved_by': user_id or None,
            'reserved_by_username': username or None,
            'reserved_at': ISO datetime string or None,
            'expires_at': ISO datetime string or None,
            'time_remaining': timedelta or None,
            'user_in_cooldown': bool (only if user_id provided),
            'cooldown_until': ISO datetime string or None (only if user_id provided),
            'cooldown_remaining': timedelta or None (only if user_id provided),
        }
    """
    if not reservations_enabled():
        result = {
            'is_reserved': False,
            'reserved_by': None,
            'reserved_by_username': None,
            'reserved_at': None,
            'expires_at': None,
            'time_remaining': None,
        }
        if user_id:
            result.update({
                'user_in_cooldown': False,
                'cooldown_until': None,
                'cooldown_remaining': None,
            })
        return result

    with get_db_context() as conn:
        now = datetime.now(timezone.utc)

        # Check active reservation
        reservation = conn.execute(
            "SELECT pr.*, u.username, u.role FROM page_reservations pr "
            "LEFT JOIN users u ON pr.user_id = u.id "
            "WHERE pr.page_id=? AND datetime(pr.expires_at) > datetime(?) AND pr.released_at IS NULL",
            (page_id, now.isoformat()),
        ).fetchone()

        result = {
            'is_reserved': False,
            'reserved_by': None,
            'reserved_by_username': None,
            'reserved_at': None,
            'expires_at': None,
            'time_remaining': None,
        }

        if reservation:
            # Auto-release if the reserving user was deleted or lost edit permissions
            user_role = reservation["role"] if "role" in reservation.keys() else None
            if user_role is None or user_role not in ("editor", "admin", "owner"):
                conn.execute(
                    "UPDATE page_reservations SET released_at=? WHERE id=?",
                    (now.isoformat(), reservation["id"]),
                )
                conn.commit()
                reservation = None

        if reservation:
            try:
                expires = datetime.fromisoformat(reservation["expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                expires = now  # Fallback: expired now
            result.update({
                'is_reserved': True,
                'reserved_by': reservation["user_id"],
                'reserved_by_username': reservation["username"] if "username" in reservation.keys() else None,
                'reserved_at': reservation["reserved_at"],
                'expires_at': reservation["expires_at"],
                'time_remaining': expires - now,
            })

        # Check user cooldown if user_id provided
        if user_id:
            cooldown = conn.execute(
                "SELECT * FROM user_page_cooldowns WHERE page_id=? AND user_id=? AND datetime(cooldown_until) > datetime(?)",
                (page_id, user_id, now.isoformat()),
            ).fetchone()

            result['user_in_cooldown'] = bool(cooldown)
            result['cooldown_until'] = cooldown["cooldown_until"] if cooldown else None
            result['cooldown_remaining'] = None

            if cooldown:
                try:
                    cooldown_dt = datetime.fromisoformat(cooldown["cooldown_until"].replace("Z", "+00:00"))
                    if cooldown_dt.tzinfo is None:
                        cooldown_dt = cooldown_dt.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    cooldown_dt = now
                result['cooldown_remaining'] = cooldown_dt - now

        return result


def cleanup_expired_reservations():
    """
    Clean up expired reservations (past expires_at) and old cooldown entries.
    This is useful for keeping the database clean, but not strictly necessary
    since all queries check expiry times.

    Returns:
        dict: {'reservations_cleaned': int, 'cooldowns_cleaned': int}
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()

        # Mark expired reservations as released (if not already marked)
        result = conn.execute(
            "UPDATE page_reservations SET released_at=? WHERE datetime(expires_at) <= datetime(?) AND released_at IS NULL",
            (now, now),
        )
        reservations_cleaned = result.rowcount

        # Delete expired cooldowns
        result = conn.execute(
            "DELETE FROM user_page_cooldowns WHERE datetime(cooldown_until) <= datetime(?)",
            (now,),
        )
        cooldowns_cleaned = result.rowcount

        conn.commit()

        return {
            'reservations_cleaned': reservations_cleaned,
            'cooldowns_cleaned': cooldowns_cleaned,
        }


def can_user_reserve_page(page_id, user_id):
    """
    Check if a user can reserve a page.

    Returns:
        tuple: (can_reserve: bool, reason: str)
        - (True, "") if user can reserve
        - (False, reason) if user cannot reserve
    """
    if not reservations_enabled():
        return (False, "Page reservations are currently disabled")

    with get_db_context() as conn:
        page = conn.execute(
            "SELECT is_home FROM pages WHERE id=?", (page_id,)
        ).fetchone()
    if not page:
        return (False, "Page not found")
    if page["is_home"]:
        return (False, "The home page cannot be reserved")

    status = get_page_reservation_status(page_id, user_id)

    if status['is_reserved']:
        if status['reserved_by'] == user_id:
            return (False, "You have already reserved this page")
        else:
            return (False, f"Page is reserved by {status['reserved_by_username']}")

    if status.get('user_in_cooldown'):
        return (False, "You are in cooldown period for this page")

    return (True, "")


def can_user_edit_page(page_id, user_id):
    """
    Check if a user can edit a page based on reservation status.

    Admins and protected admins can always edit, even if the page is
    reserved by someone else (consistent with the template-level bypass).

    Returns:
        tuple: (can_edit: bool, reason: str)
        - (True, "") if user can edit
        - (False, reason) if user cannot edit
    """
    if not reservations_enabled():
        return (True, "")

    status = get_page_reservation_status(page_id, user_id)

    if not status['is_reserved']:
        # No reservation, anyone can edit
        return (True, "")

    if status['reserved_by'] == user_id:
        # User holds the reservation
        return (True, "")

    # Someone else holds the reservation: check if user is admin
    with get_db_context() as conn:
        user = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if user and user["role"] in ("admin", "owner"):
        return (True, "")

    return (False, f"Page is reserved by {status['reserved_by_username']}")


@retry_on_busy
def get_user_reservations(user_id):
    """
    Get all active reservations for a user.

    Returns:
        list: List of reservation dicts with page info
    """
    if not reservations_enabled():
        return []

    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()

        rows = conn.execute(
            "SELECT pr.*, p.title, p.slug FROM page_reservations pr "
            "JOIN pages p ON pr.page_id = p.id "
            "WHERE pr.user_id=? AND datetime(pr.expires_at) > datetime(?) AND pr.released_at IS NULL "
            "ORDER BY pr.reserved_at DESC",
            (user_id, now),
        ).fetchall()

        return rows


def get_all_active_reservations():
    """
    Get all active (non-expired, non-released) reservations across all pages.
    Also cleans up expired reservations and cooldowns before returning.

    Returns:
        list: List of reservation dicts including page title/slug and username.
    """
    if not reservations_enabled():
        return []

    cleanup_expired_reservations()

    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()

        rows = conn.execute(
            "SELECT pr.*, p.title, p.slug, u.username "
            "FROM page_reservations pr "
            "JOIN pages p ON pr.page_id = p.id "
            "JOIN users u ON pr.user_id = u.id "
            "WHERE datetime(pr.expires_at) > datetime(?) AND pr.released_at IS NULL "
            "ORDER BY pr.reserved_at DESC",
            (now,),
        ).fetchall()

        return rows


@retry_on_busy
def get_all_active_cooldowns():
    """
    Get all active (non-expired) cooldown entries across all pages.

    Returns:
        list: List of dicts with page_id, page title, page slug, username, user_id, cooldown_until.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(
            "SELECT upc.page_id, upc.user_id, upc.cooldown_until, "
            "p.title, p.slug, u.username "
            "FROM user_page_cooldowns upc "
            "JOIN pages p ON upc.page_id = p.id "
            "JOIN users u ON upc.user_id = u.id "
            "WHERE datetime(upc.cooldown_until) > datetime(?) "
            "ORDER BY upc.cooldown_until ASC",
            (now,),
        ).fetchall()
        return rows


def _build_sidebar_page_status(
    is_reserved=False,
    reserved_by_current_user=False,
    reservation_label=None,
    user_in_cooldown=False,
    cooldown_label=None,
):
    """Build a consistent sidebar reservation/cooldown payload."""
    return {
        "is_reserved": is_reserved,
        "reserved_by_current_user": reserved_by_current_user,
        "reservation_label": reservation_label,
        "user_in_cooldown": user_in_cooldown,
        "cooldown_label": cooldown_label,
    }


@retry_on_busy
def get_active_page_reservations_map(user_id=None, page_ids=None):
    """
    Return active reservation/cooldown metadata keyed by page ID for sidebar/search UI.

    Args:
        user_id: Current viewer ID, used to mark reservations owned by them.
        page_ids: Optional iterable of page IDs to limit the query scope.

    Returns:
        dict: ``{page_id: {"is_reserved": bool, "reserved_by_current_user": bool,
               "reservation_label": str | None, "user_in_cooldown": bool,
               "cooldown_label": str | None}}`` for viewer-visible sidebar status.
    """
    if not reservations_enabled():
        return {}

    has_page_filter = page_ids is not None
    if has_page_filter:
        normalized_page_ids = []
        for page_id in page_ids:
            try:
                normalized_page_ids.append(int(page_id))
            except (TypeError, ValueError):
                continue
        page_ids = normalized_page_ids
    else:
        page_ids = []
    if has_page_filter and not page_ids:
        return {}

    cleanup_expired_reservations()

    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        query = (
            "SELECT pr.page_id, pr.user_id "
            "FROM page_reservations pr "
            "JOIN users u ON pr.user_id = u.id "
            "WHERE datetime(pr.expires_at) > datetime(?) AND pr.released_at IS NULL "
            "AND u.role IN ('editor', 'admin', 'owner')"
        )
        params = [now]
        if has_page_filter:
            placeholders = ",".join("?" for _ in page_ids)
            query += f" AND pr.page_id IN ({placeholders})"
            params.extend(page_ids)
        rows = conn.execute(query, params).fetchall()

        reservations = {}
        for row in rows:
            reserved_by_current_user = bool(user_id and row["user_id"] == user_id)
            reservations[row["page_id"]] = _build_sidebar_page_status(
                is_reserved=True,
                reserved_by_current_user=reserved_by_current_user,
                reservation_label=(
                    "Reserved by you" if reserved_by_current_user else "Reserved by another user"
                ),
            )

        if user_id:
            cooldown_query = (
                "SELECT page_id FROM user_page_cooldowns "
                "WHERE user_id=? AND datetime(cooldown_until) > datetime(?)"
            )
            cooldown_params = [user_id, now]
            if has_page_filter:
                placeholders = ",".join("?" for _ in page_ids)
                cooldown_query += f" AND page_id IN ({placeholders})"
                cooldown_params.extend(page_ids)
            cooldown_rows = conn.execute(cooldown_query, cooldown_params).fetchall()

            for row in cooldown_rows:
                reservations.setdefault(row["page_id"], _build_sidebar_page_status())
                reservations[row["page_id"]]["user_in_cooldown"] = True
                reservations[row["page_id"]]["cooldown_label"] = "Cooldown active for you"

        return reservations


def get_directory_reservation_statuses(user_id, page_ids):
    """Return reservation/cooldown status for multiple pages in two bulk queries.

    This is the bulk variant of :func:`get_page_reservation_status` used by the
    reservation directory listing to avoid an N+1 query per page.

    Args:
        user_id: Current viewer's user ID (used for cooldown look-up).
        page_ids: Iterable of integer page IDs to include.

    Returns:
        dict: ``{page_id: {"is_reserved": bool, "reserved_by": user_id or None,
               "reserved_by_username": str or None, "expires_at": str or None,
               "user_in_cooldown": bool, "cooldown_until": str or None}}``
        Missing page IDs in the result mean "no reservation and no cooldown".
    """
    # Two bulk queries rather than one get_page_reservation_status() call per
    # page, so the reservation directory stays flat as the page count grows.
    # Normalise page_ids to int, silently dropping any non-numeric values.
    normalized = []
    for pid in page_ids:
        try:
            normalized.append(int(pid))
        except (TypeError, ValueError):
            continue
    if not normalized:
        return {}

    # Build the default "nothing active" entry for every requested page.
    statuses = {
        pid: {
            "is_reserved": False,
            "reserved_by": None,
            "reserved_by_username": None,
            "expires_at": None,
            "user_in_cooldown": False,
            "cooldown_until": None,
        }
        for pid in normalized
    }

    if not reservations_enabled():
        return statuses

    cleanup_expired_reservations()

    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        # `placeholders` is a string of "?,?,…" built from the *count* of page IDs,
        # not from user-supplied strings, so this f-string cannot cause SQL injection.
        placeholders = ",".join("?" for _ in normalized)

        # Single query for all active reservations across the requested pages.
        reservation_rows = conn.execute(  # noqa: S608
            f"SELECT pr.page_id, pr.user_id, u.username, pr.expires_at "
            f"FROM page_reservations pr "
            f"JOIN users u ON pr.user_id = u.id "
            f"WHERE pr.page_id IN ({placeholders}) "
            f"AND datetime(pr.expires_at) > datetime(?) AND pr.released_at IS NULL "
            f"AND u.role IN ('editor', 'admin', 'owner')",
            (*normalized, now),
        ).fetchall()

        for row in reservation_rows:
            statuses[row["page_id"]].update({
                "is_reserved": True,
                "reserved_by": row["user_id"],
                "reserved_by_username": row["username"],
                "expires_at": row["expires_at"],
            })

        # Single query for all cooldown entries for the current user.
        # Reuses the same `placeholders` string; values are passed as parameters.
        if user_id:
            cooldown_rows = conn.execute(  # noqa: S608
                f"SELECT page_id, cooldown_until FROM user_page_cooldowns "
                f"WHERE user_id=? AND page_id IN ({placeholders}) AND datetime(cooldown_until) > datetime(?)",
                (user_id, *normalized, now),
            ).fetchall()

            for row in cooldown_rows:
                statuses[row["page_id"]].update({
                    "user_in_cooldown": True,
                    "cooldown_until": row["cooldown_until"],
                })

        return statuses


def force_release_reservation(page_id):
    """
    Force release a reservation (admin action).
    Does NOT create a cooldown entry.

    Returns:
        bool: True if reservation was released, False if no active reservation found
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc)

        # Find active reservation
        reservation = conn.execute(
            "SELECT * FROM page_reservations WHERE page_id=? AND datetime(expires_at) > datetime(?) AND released_at IS NULL",
            (page_id, now.isoformat()),
        ).fetchone()

        if not reservation:
            return False

        # Mark as released without creating cooldown
        conn.execute(
            "UPDATE page_reservations SET released_at=? WHERE id=?",
            (now.isoformat(), reservation["id"]),
        )

        conn.commit()
        return True


def admin_assign_reservation(page_id, user_id):
    """
    Admin action: assign a page reservation to a specific user.

    Bypasses all normal checks (existing reservations, cooldowns, quotas).
    Any active reservation on the page is force-released first (without
    creating a cooldown for the previous holder). Any cooldown the target
    user has on this page is cleared.

    Returns:
        dict: The new reservation row.

    Raises:
        ValueError: If reservations are disabled or the page/user is invalid.
    """
    if not reservations_enabled():
        raise ValueError("Page reservations are currently disabled")

    settings = get_site_settings()
    with get_db_context() as conn:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=_get_reservation_hours(
            settings, "page_reservation_duration_hours", config.PAGE_RESERVATION_DURATION_HOURS, 1
        ))

        try:
            conn.execute("BEGIN IMMEDIATE")

            # Force-release any existing reservation (no cooldown)
            existing = conn.execute(
                "SELECT * FROM page_reservations WHERE page_id=? AND datetime(expires_at) > datetime(?) AND released_at IS NULL",
                (page_id, now.isoformat()),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE page_reservations SET released_at=? WHERE id=?",
                    (now.isoformat(), existing["id"]),
                )

            # Clear any cooldown the target user has on this page
            conn.execute(
                "DELETE FROM user_page_cooldowns WHERE page_id=? AND user_id=?",
                (page_id, user_id),
            )

            # Clean up old released/expired reservations
            conn.execute(
                "DELETE FROM page_reservations WHERE page_id=? AND (released_at IS NOT NULL OR datetime(expires_at) <= datetime(?))",
                (page_id, now.isoformat()),
            )

            # Create the new reservation
            conn.execute(
                "INSERT INTO page_reservations (page_id, user_id, reserved_at, expires_at) VALUES (?, ?, ?, ?)",
                (page_id, user_id, now.isoformat(), expires.isoformat()),
            )

            conn.commit()
        except (sqlite3.Error, ValueError):
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        # Retrieve and return the new reservation
        row = conn.execute(
            "SELECT * FROM page_reservations WHERE page_id=? AND user_id=? AND released_at IS NULL ORDER BY id DESC LIMIT 1",
            (page_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def count_pending_reservation_quota_requests():
    """Return the total number of pending reservation quota requests across all users."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM reservation_quota_requests WHERE status='pending'"
        ).fetchone()
        return row[0] if row else 0


def admin_clear_cooldown(page_id, user_id=None):
    """
    Admin action: clear cooldown entries for a page.

    Args:
        page_id: ID of the page.
        user_id: If provided, only clear cooldown for this user.
                 If None, clear all cooldowns for the page.

    Returns:
        int: Number of cooldown entries removed.
    """
    with get_db_context() as conn:
        if user_id:
            result = conn.execute(
                "DELETE FROM user_page_cooldowns WHERE page_id=? AND user_id=?",
                (page_id, user_id),
            )
        else:
            result = conn.execute(
                "DELETE FROM user_page_cooldowns WHERE page_id=?",
                (page_id,),
            )
        count = result.rowcount
        conn.commit()
        return count
