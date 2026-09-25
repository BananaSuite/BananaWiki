"""Deletion Slowdown: pending-deletion queue for the deletion_slowdown plugin."""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


# Duration (in hours) before a pending-deletion page is permanently removed.
PENDING_DELETION_HOURS = 48


def mark_page_pending_deletion(page_id, user_id):
    """Mark a page as pending deletion.

    Sets *pending_deletion = 1*, records *user_id* and the current UTC
    timestamp.  The page will be permanently deleted by the cleanup routine
    after :data:`PENDING_DELETION_HOURS` hours.

    Returns ``True`` if the page was marked, ``False`` if it was not found,
    is the home page, or is already pending deletion.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id, is_home FROM pages WHERE id=?", (page_id,)
        ).fetchone()
        if not row or row["is_home"]:
            return False
        cur = conn.execute(
            "UPDATE pages SET pending_deletion=1, pending_deletion_by=?, "
            "pending_deletion_at=? WHERE id=? AND pending_deletion=0",
            (user_id, now, page_id),
        )
        conn.commit()
        return cur.rowcount > 0


def restore_page_from_pending_deletion(page_id):
    """Clear the pending-deletion flag on a page, restoring it to normal.

    Returns ``True`` if the page was restored, ``False`` if not found.
    """
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE pages SET pending_deletion=0, pending_deletion_by=NULL, "
            "pending_deletion_at=NULL WHERE id=? AND pending_deletion=1",
            (page_id,),
        )
        conn.commit()
        return cur.rowcount > 0


@retry_on_busy
def get_pending_deletion_info(page_id):
    """Return pending-deletion metadata for *page_id*, or ``None``.

    The returned dict contains ``page_id``, ``pending_deletion_by``,
    ``pending_deletion_at``, and ``expires_at`` (computed).
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id, title, slug, pending_deletion_by, pending_deletion_at "
            "FROM pages WHERE id=? AND pending_deletion=1",
            (page_id,),
        ).fetchone()
        if not row:
            return None
        pd_at = row["pending_deletion_at"]
        if pd_at:
            from datetime import timedelta
            try:
                dt = datetime.fromisoformat(pd_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                expires_at = (dt + timedelta(hours=PENDING_DELETION_HOURS)).isoformat()
            except (ValueError, TypeError):
                expires_at = None
        else:
            expires_at = None
        return {
            "page_id": row["id"],
            "title": row["title"],
            "slug": row["slug"],
            "pending_deletion_by": row["pending_deletion_by"],
            "pending_deletion_at": pd_at,
            "expires_at": expires_at,
        }


@retry_on_busy
def list_pending_deletions():
    """Return all pages currently in pending-deletion state, newest first.

    Each row includes page title, slug, category info, who triggered the
    deletion, when it happened, and the computed expiry timestamp.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT p.id, p.title, p.slug, p.category_id, p.pending_deletion_by, "
            "p.pending_deletion_at, "
            "COALESCE(u.username, '[deleted user]') AS deleted_by_username, "
            "c.name AS category_name "
            "FROM pages p "
            "LEFT JOIN users u ON p.pending_deletion_by = u.id "
            "LEFT JOIN categories c ON p.category_id = c.id "
            "WHERE p.pending_deletion=1 "
            "ORDER BY p.pending_deletion_at DESC",
        ).fetchall()

    from datetime import timedelta
    result = []
    for row in rows:
        pd_at = row["pending_deletion_at"]
        expires_at = None
        if pd_at:
            try:
                dt = datetime.fromisoformat(pd_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                expires_at = (dt + timedelta(hours=PENDING_DELETION_HOURS)).isoformat()
            except (ValueError, TypeError):
                pass
        result.append({
            "id": row["id"],
            "title": row["title"],
            "slug": row["slug"],
            "category_id": row["category_id"],
            "category_name": row["category_name"],
            "pending_deletion_by": row["pending_deletion_by"],
            "deleted_by_username": row["deleted_by_username"],
            "pending_deletion_at": pd_at,
            "expires_at": expires_at,
        })
    return result


@retry_on_busy
def get_expired_pending_deletions():
    """Return page IDs whose pending-deletion grace period has passed.

    Uses a pure-SQL datetime comparison to avoid pulling all pages into
    Python memory.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT id FROM pages "
            "WHERE pending_deletion=1 "
            "AND pending_deletion_at IS NOT NULL "
            "AND julianday(pending_deletion_at, '+' || ? || ' hours') <= julianday('now')",
            (str(PENDING_DELETION_HOURS),),
        ).fetchall()
        return [row["id"] for row in rows]


def cleanup_expired_pending_deletions():
    """Permanently delete pages whose pending-deletion grace period has expired.

    Returns the number of pages permanently deleted.
    """
    from ._pages import delete_page
    expired_ids = get_expired_pending_deletions()
    count = 0
    for page_id in expired_ids:
        try:
            delete_page(page_id)
            count += 1
        except Exception:
            # Page may already be gone or have an integrity issue; skip.
            pass
    return count
