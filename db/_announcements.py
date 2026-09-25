"""Announcements and contributions."""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


def create_announcement(content, color, text_size, visibility, expires_at, user_id,
                        not_removable=1, show_countdown=1, audience_mode="all",
                        audience_user_ids=None, custom_background=None,
                        custom_text_color=None):
    """Create and persist a new announcement.  Returns the new announcement ID."""
    with get_db_context() as conn:
        cur = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO announcements (content, color, text_size, visibility, expires_at, is_active, not_removable, show_countdown, audience_mode, custom_background, custom_text_color, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (content, color, text_size, visibility, expires_at or None,
             int(not_removable), int(show_countdown), audience_mode,
             custom_background, custom_text_color, user_id, now),
        )
        ann_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO announcement_audience_users (announcement_id, user_id) VALUES (?, ?)",
            [(ann_id, target_id) for target_id in dict.fromkeys(audience_user_ids or [])],
        )
        conn.commit()
    return ann_id


@retry_on_busy
def get_announcement(ann_id):
    """Return the announcement row for the given *ann_id*, or None if not found."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    return row


@retry_on_busy
def list_announcements():
    """Return all announcements ordered by creation date (newest first), with creator names joined."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT a.*, COALESCE(u.username, 'deleted user') AS creator_name, "
            "(SELECT GROUP_CONCAT(tu.username, ', ') "
            " FROM announcement_audience_users aau "
            " JOIN users tu ON tu.id=aau.user_id "
            " WHERE aau.announcement_id=a.id) AS audience_usernames "
            "FROM announcements a LEFT JOIN users u ON a.created_by=u.id "
            "ORDER BY a.created_at DESC"
        ).fetchall()
    return rows


_ALLOWED_ANN_COLUMNS = {
    "content", "color", "text_size", "visibility", "expires_at", "is_active",
    "not_removable", "show_countdown", "audience_mode", "custom_background",
    "custom_text_color",
}


def update_announcement(ann_id, audience_user_ids=None, **kwargs):
    """Update one or more announcement columns.  Only columns in ``_ALLOWED_ANN_COLUMNS`` are permitted."""
    for k in kwargs:
        if k not in _ALLOWED_ANN_COLUMNS:
            raise ValueError(f"Invalid column: {k}")
    with get_db_context() as conn:
        if kwargs:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            vals = list(kwargs.values()) + [ann_id]
            conn.execute(
                f"UPDATE announcements SET {sets}, revision=revision+1 WHERE id=?",
                vals,
            )
        if audience_user_ids is not None:
            conn.execute(
                "DELETE FROM announcement_audience_users WHERE announcement_id=?",
                (ann_id,),
            )
            conn.executemany(
                "INSERT INTO announcement_audience_users (announcement_id, user_id) VALUES (?, ?)",
                [(ann_id, target_id) for target_id in dict.fromkeys(audience_user_ids)],
            )
        conn.commit()


def delete_announcement(ann_id):
    """Permanently delete the announcement identified by *ann_id*."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
        conn.commit()


@retry_on_busy
def get_user_contributions(user_id, year=None):
    """Return all page history entries edited by a user, with current page metadata.

    Returned rows include ``page_title``, ``page_slug``, ``category_id``, and
    ``is_deindexed`` from the live page record when the page still exists.
    Deleted pages keep their history entry but return an empty slug and null
    category metadata.

    When ``year`` is provided, only entries from that calendar year are returned.
    """
    with get_db_context() as conn:
        base_query = (
            "SELECT ph.id, ph.page_id, COALESCE(p.title, '[deleted page]') AS page_title, "
            "COALESCE(p.slug, '') AS page_slug, "
            "p.category_id, COALESCE(p.is_deindexed, 0) AS is_deindexed, "
            "ph.title AS edit_title, ph.content, ph.edit_message, ph.created_at "
            "FROM page_history ph "
            "LEFT JOIN pages p ON ph.page_id = p.id "
            "WHERE ph.edited_by=?"
        )
        params = [user_id]
        if year is not None:
            start = f"{year}-01-01"
            end_exclusive = f"{year + 1}-01-01"
            base_query += " AND ph.created_at >= ? AND ph.created_at < ?"
            params.extend([start, end_exclusive])
        rows = conn.execute(
            base_query + " ORDER BY ph.created_at DESC",
            tuple(params),
        ).fetchall()
    return rows


@retry_on_busy
def get_active_announcements(is_logged_in, user_id=None):
    """Return active, non-expired announcements matching the user's login state.

    Wrapped in :func:`retry_on_busy` because :func:`inject_globals` calls
    this for every page render whenever the ``announcements`` plugin is
    enabled.  Without the retry, a transient ``database is locked`` here
    would surface as a 500 on a normal page load.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        logged_in_int = 1 if is_logged_in else 0
        rows = conn.execute(
            "SELECT * FROM announcements "
            "WHERE is_active=1 "
            "  AND (expires_at IS NULL OR julianday(expires_at) > julianday(?)) "
            "  AND (visibility='both' "
            "       OR (visibility='logged_in' AND ?=1) "
            "       OR (visibility='logged_out' AND ?=0)) "
            "  AND (audience_mode='all' "
            "       OR (audience_mode='allowlist' AND ? IS NOT NULL AND EXISTS ("
            "           SELECT 1 FROM announcement_audience_users aau "
            "           WHERE aau.announcement_id=announcements.id AND aau.user_id=?)) "
            "       OR (audience_mode='denylist' AND (? IS NULL OR NOT EXISTS ("
            "           SELECT 1 FROM announcement_audience_users aau "
            "           WHERE aau.announcement_id=announcements.id AND aau.user_id=?)))) "
            "ORDER BY created_at DESC",
            (now, logged_in_int, logged_in_int, user_id, user_id, user_id, user_id),
        ).fetchall()
    return rows


@retry_on_busy
def get_visible_announcement(ann_id, is_logged_in, user_id=None):
    """Return one announcement only when it is currently visible to the user."""
    return next(
        (row for row in get_active_announcements(is_logged_in, user_id) if row["id"] == ann_id),
        None,
    )


@retry_on_busy
def get_announcement_audience_user_ids(ann_id):
    """Return stable user IDs selected for an announcement audience."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT user_id FROM announcement_audience_users WHERE announcement_id=?",
            (ann_id,),
        ).fetchall()
    return [row["user_id"] for row in rows]
