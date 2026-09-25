"""Temporary accounts & pages: auto-deletion for pages, users, and role grants."""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


def set_page_expiry(page_id, expires_at, *, show_countdown=True, set_by=None):
    """Set or update the auto-deletion schedule for a page.

    *expires_at* should be an ISO datetime string or ``None`` to remove the
    schedule (making the page permanent / indefinite).
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        existing = conn.execute(
            "SELECT id FROM temp_pages WHERE page_id = ?", (page_id,)
        ).fetchone()
        if expires_at is None:
            # Remove the schedule entirely
            if existing:
                conn.execute("DELETE FROM temp_pages WHERE page_id = ?", (page_id,))
        elif existing:
            conn.execute(
                "UPDATE temp_pages SET expires_at = ?, show_countdown = ?, "
                "set_by = ?, updated_at = ? WHERE page_id = ?",
                (expires_at, 1 if show_countdown else 0, set_by, now, page_id),
            )
        else:
            conn.execute(
                "INSERT INTO temp_pages "
                "(page_id, expires_at, show_countdown, set_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (page_id, expires_at, 1 if show_countdown else 0, set_by, now, now),
            )
        conn.commit()


@retry_on_busy
def get_page_expiry(page_id):
    """Return the temp_pages row for *page_id*, or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM temp_pages WHERE page_id = ?", (page_id,)
        ).fetchone()


@retry_on_busy
def list_temp_pages():
    """Return all temporary pages joined with page info."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT tp.*, p.title, p.slug "
            "FROM temp_pages tp "
            "JOIN pages p ON tp.page_id = p.id "
            "ORDER BY tp.expires_at ASC"
        ).fetchall()


@retry_on_busy
def get_expired_pages():
    """Return temp_pages rows whose expiry has passed."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        return conn.execute(
            "SELECT tp.*, p.title, p.slug "
            "FROM temp_pages tp "
            "JOIN pages p ON tp.page_id = p.id "
            "WHERE tp.expires_at IS NOT NULL AND julianday(tp.expires_at) <= julianday(?) "
            "ORDER BY tp.expires_at ASC",
            (now,),
        ).fetchall()


def cleanup_expired_temp_pages():
    """Delete pages whose expiry has passed.

    Returns the number of pages deleted.
    """
    from ._pages import delete_page
    expired = get_expired_pages()
    count = 0
    for row in expired:
        try:
            delete_page(row["page_id"])
            count += 1
        except Exception:
            # Page may have already been deleted or have integrity issues;
            # skip and continue so one failure does not block the batch.
            pass
    # Clean up any orphaned temp_pages rows
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM temp_pages WHERE page_id NOT IN (SELECT id FROM pages)"
        )
        conn.commit()
    return count


def set_user_expiry(user_id, expires_at, *, show_countdown=True, set_by=None):
    """Set or update the auto-deletion schedule for a user account.

    *expires_at* should be an ISO datetime string or ``None`` to make the
    account permanent (indefinite).
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        existing = conn.execute(
            "SELECT id FROM temp_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if expires_at is None:
            if existing:
                conn.execute("DELETE FROM temp_users WHERE user_id = ?", (user_id,))
        elif existing:
            conn.execute(
                "UPDATE temp_users SET expires_at = ?, show_countdown = ?, "
                "set_by = ?, updated_at = ? WHERE user_id = ?",
                (expires_at, 1 if show_countdown else 0, set_by, now, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO temp_users "
                "(user_id, expires_at, show_countdown, set_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, expires_at, 1 if show_countdown else 0, set_by, now, now),
            )
        conn.commit()


@retry_on_busy
def get_user_expiry(user_id):
    """Return the temp_users row for *user_id*, or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM temp_users WHERE user_id = ?", (user_id,)
        ).fetchone()


@retry_on_busy
def list_temp_users():
    """Return all temporary users joined with user info."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT tu.*, u.username, u.role "
            "FROM temp_users tu "
            "JOIN users u ON tu.user_id = u.id "
            "ORDER BY tu.expires_at ASC"
        ).fetchall()


@retry_on_busy
def get_expired_users():
    """Return temp_users rows whose expiry has passed."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        return conn.execute(
            "SELECT tu.*, u.username, u.role "
            "FROM temp_users tu "
            "JOIN users u ON tu.user_id = u.id "
            "WHERE tu.expires_at IS NOT NULL AND julianday(tu.expires_at) <= julianday(?) "
            "ORDER BY tu.expires_at ASC",
            (now,),
        ).fetchall()


def cleanup_expired_temp_users():
    """Delete users whose expiry has passed.

    Returns the number of users deleted.  Protected admins and the last
    remaining admin are never auto-deleted.
    """
    from ._users import delete_user, list_users
    expired = get_expired_users()
    count = 0
    for row in expired:
        try:
            # Never auto-delete an owner
            if row["role"] == "owner":
                continue
            # Never delete the last admin
            if row["role"] in ("admin", "owner"):
                admins = [u for u in list_users() if u["role"] in ("admin", "owner")]
                if len(admins) <= 1:
                    continue
            delete_user(row["user_id"])
            count += 1
        except Exception:
            # User may have already been deleted or have integrity issues;
            # skip and continue so one failure does not block the batch.
            pass
    # Clean up orphaned temp_users rows
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM temp_users WHERE user_id NOT IN (SELECT id FROM users)"
        )
        conn.commit()
    return count


def set_role_expiry(user_id, original_role, *, expires_at, show_countdown=True,
                    set_by=None):
    """Schedule a role revocation: when *expires_at* is reached the user's
    role will be reverted to *original_role*.

    *expires_at* should be an ISO datetime string or ``None`` to cancel the
    revocation schedule.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        existing = conn.execute(
            "SELECT id FROM temp_roles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if expires_at is None:
            if existing:
                conn.execute("DELETE FROM temp_roles WHERE user_id = ?", (user_id,))
        elif existing:
            conn.execute(
                "UPDATE temp_roles SET original_role = ?, expires_at = ?, "
                "show_countdown = ?, set_by = ?, updated_at = ? WHERE user_id = ?",
                (original_role, expires_at, 1 if show_countdown else 0,
                 set_by, now, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO temp_roles "
                "(user_id, original_role, expires_at, show_countdown, set_by, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, original_role, expires_at, 1 if show_countdown else 0,
                 set_by, now, now),
            )
        conn.commit()


@retry_on_busy
def get_role_expiry(user_id):
    """Return the temp_roles row for *user_id*, or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM temp_roles WHERE user_id = ?", (user_id,)
        ).fetchone()


@retry_on_busy
def list_temp_roles():
    """Return all temporary role grants joined with user info."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT tr.*, u.username, u.role AS current_role "
            "FROM temp_roles tr "
            "JOIN users u ON tr.user_id = u.id "
            "ORDER BY tr.expires_at ASC"
        ).fetchall()


@retry_on_busy
def get_expired_roles():
    """Return temp_roles rows whose expiry has passed."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        return conn.execute(
            "SELECT tr.*, u.username, u.role AS current_role "
            "FROM temp_roles tr "
            "JOIN users u ON tr.user_id = u.id "
            "WHERE tr.expires_at IS NOT NULL AND julianday(tr.expires_at) <= julianday(?) "
            "ORDER BY tr.expires_at ASC",
            (now,),
        ).fetchall()


def cleanup_expired_temp_roles():
    """Revoke roles whose expiry has passed.

    Returns the number of users whose role was reverted.
    """
    from ._users import update_user, list_users
    from ._audit import record_role_change
    expired = get_expired_roles()
    count = 0
    # Pre-fetch admin list to avoid repeated queries inside the loop
    all_users = list_users()
    admin_ids = {u["id"] for u in all_users if u["role"] in ("admin", "owner")}
    with get_db_context() as conn:
        for row in expired:
            try:
                original = row["original_role"]
                current = row["current_role"]
                user_id = row["user_id"]
                # If the current role is already the original (or lower), just remove schedule
                if current == original:
                    conn.execute("DELETE FROM temp_roles WHERE user_id = ?", (user_id,))
                    conn.commit()
                    continue
                # Owners are reverted like everyone else on purpose.  Skipping
                # them would let a temporarily granted admin keep the grant
                # by turning on owner status; the routes instead refuse to
                # turn owner on while a schedule exists.
                # Don't demote the last admin
                if current in ("admin", "owner") and original not in ("admin", "owner"):
                    if len(admin_ids) <= 1:
                        continue
                update_user(user_id, role=original)
                # changed_by=None indicates an automatic system action (expiry)
                record_role_change(user_id, current, original, changed_by=None)
                conn.execute("DELETE FROM temp_roles WHERE user_id = ?", (user_id,))
                conn.commit()
                # Update the in-memory admin set after demotion
                admin_ids.discard(user_id)
                count += 1
            except Exception:
                # User may have been deleted or have integrity issues;
                # skip and continue so one failure does not block the batch.
                pass
        # Clean up orphaned rows
        conn.execute(
            "DELETE FROM temp_roles WHERE user_id NOT IN (SELECT id FROM users)"
        )
        conn.commit()
    return count


def set_page_temp_index_state(page_id, target_state, restore_state, expires_at,
                               *, show_countdown=True, set_by=None):
    """Schedule a temporary deindex or reindex for *page_id*.

    *target_state* is the ``is_deindexed`` value to apply immediately
    (0 = indexed/visible, 1 = deindexed/hidden).  *restore_state* is the
    value to restore when *expires_at* is reached.  Pass ``expires_at=None``
    to remove any existing schedule without changing the page's current state.
    """
    from ._pages import set_page_deindexed
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        existing = conn.execute(
            "SELECT id FROM temp_page_index_state WHERE page_id = ?", (page_id,)
        ).fetchone()
        if expires_at is None:
            if existing:
                conn.execute(
                    "DELETE FROM temp_page_index_state WHERE page_id = ?", (page_id,)
                )
            conn.commit()
            return
        if existing:
            conn.execute(
                "UPDATE temp_page_index_state SET target_state = ?, restore_state = ?, "
                "expires_at = ?, show_countdown = ?, set_by = ?, updated_at = ? "
                "WHERE page_id = ?",
                (target_state, restore_state,
                 expires_at, 1 if show_countdown else 0, set_by, now, page_id),
            )
        else:
            conn.execute(
                "INSERT INTO temp_page_index_state "
                "(page_id, target_state, restore_state, expires_at, show_countdown, "
                " set_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (page_id, target_state, restore_state,
                 expires_at, 1 if show_countdown else 0, set_by, now, now),
            )
        conn.commit()
    # Apply the target state to the page immediately
    set_page_deindexed(page_id, bool(target_state))


@retry_on_busy
def get_page_temp_index_state(page_id):
    """Return the temp_page_index_state row for *page_id*, or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM temp_page_index_state WHERE page_id = ?", (page_id,)
        ).fetchone()


@retry_on_busy
def list_temp_page_index_states():
    """Return all temporary page index states joined with page info."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT tpi.*, p.title, p.slug, p.is_deindexed "
            "FROM temp_page_index_state tpi "
            "JOIN pages p ON tpi.page_id = p.id "
            "ORDER BY tpi.expires_at ASC"
        ).fetchall()


@retry_on_busy
def get_expired_page_index_states():
    """Return temp_page_index_state rows whose expiry has passed."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        return conn.execute(
            "SELECT tpi.*, p.title, p.slug "
            "FROM temp_page_index_state tpi "
            "JOIN pages p ON tpi.page_id = p.id "
            "WHERE tpi.expires_at IS NOT NULL AND julianday(tpi.expires_at) <= julianday(?) "
            "ORDER BY tpi.expires_at ASC",
            (now,),
        ).fetchall()


def cleanup_expired_temp_page_index_states():
    """Restore pages to their original index state when the schedule has expired.

    Returns the number of pages whose index state was restored.
    """
    from ._pages import set_page_deindexed
    expired = get_expired_page_index_states()
    count = 0
    for row in expired:
        try:
            set_page_deindexed(row["page_id"], bool(row["restore_state"]))
            with get_db_context() as conn:
                conn.execute(
                    "DELETE FROM temp_page_index_state WHERE page_id = ?",
                    (row["page_id"],),
                )
                conn.commit()
            count += 1
        except Exception:
            pass
    # Clean up any orphaned rows
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM temp_page_index_state "
            "WHERE page_id NOT IN (SELECT id FROM pages)"
        )
        conn.commit()
    return count


def cleanup_all_expired_temporary():
    """Run all four temporary cleanup routines.

    Returns a dict with counts of deleted pages, deleted users,
    reverted roles, and restored page index states.
    """
    return {
        "expired_temp_pages": cleanup_expired_temp_pages(),
        "expired_temp_users": cleanup_expired_temp_users(),
        "expired_temp_roles": cleanup_expired_temp_roles(),
        "expired_temp_page_index_states": cleanup_expired_temp_page_index_states(),
    }
