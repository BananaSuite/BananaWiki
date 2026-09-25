"""Badge system management."""

from ._connection import get_db_context, retry_on_busy


VALID_TRIGGER_TYPES = {
    '',  # Manual only
    'category_count',  # Written in X categories
    'contribution_count',  # Made X contributions
    'first_edit',  # Made first edit
    'member_days',  # Member for X days
    # Note: reading_time and article_count removed - no tracking system exists for these
}


def create_badge_type(name, description='', icon='🏆', color='#ffd700',
                     enabled=True, auto_trigger=False, trigger_type='',
                     trigger_threshold=0, allow_multiple=False, created_by=None):
    """Create a new badge type. Returns the new badge type id."""
    if trigger_type not in VALID_TRIGGER_TYPES:
        raise ValueError(f"Invalid trigger_type: {trigger_type}")

    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO badge_types (name, description, icon, color, enabled, "
            "auto_trigger, trigger_type, trigger_threshold, allow_multiple, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, description, icon, color, int(enabled), int(auto_trigger),
             trigger_type, trigger_threshold, int(allow_multiple), created_by),
        )
        badge_type_id = cur.lastrowid
        conn.commit()
    return badge_type_id


@retry_on_busy
def get_badge_type(badge_type_id):
    """Return a single badge type by id, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM badge_types WHERE id=?", (badge_type_id,)
        ).fetchone()
    return row


@retry_on_busy
def get_badge_type_by_name(name):
    """Return a badge type by name, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM badge_types WHERE name=?", (name,)
        ).fetchone()
    return row


@retry_on_busy
def list_badge_types(enabled_only=False):
    """List all badge types. If enabled_only=True, only return enabled badges."""
    with get_db_context() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM badge_types WHERE enabled=1 ORDER BY name ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM badge_types ORDER BY name ASC"
            ).fetchall()
    return rows


def update_badge_type(badge_type_id, **kwargs):
    """Update badge type fields. Accepts: name, description, icon, color, enabled,
    auto_trigger, trigger_type, trigger_threshold, allow_multiple."""
    allowed = {
        'name', 'description', 'icon', 'color', 'enabled',
        'auto_trigger', 'trigger_type', 'trigger_threshold', 'allow_multiple'
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}

    if 'trigger_type' in fields and fields['trigger_type'] not in VALID_TRIGGER_TYPES:
        raise ValueError(f"Invalid trigger_type: {fields['trigger_type']}")

    # Convert boolean fields to int
    for bool_field in ['enabled', 'auto_trigger', 'allow_multiple']:
        if bool_field in fields:
            fields[bool_field] = int(fields[bool_field])

    if not fields:
        return

    with get_db_context() as conn:
        # Build SET clause using only validated column names from the whitelist.
        set_parts = []
        vals = []
        for col in allowed:
            if col in fields:
                set_parts.append(f"{col}=?")
                vals.append(fields[col])
        vals.append(badge_type_id)
        conn.execute(
            f"UPDATE badge_types SET {', '.join(set_parts)} WHERE id=?", vals  # noqa: S608
        )
        conn.commit()


def delete_badge_type(badge_type_id, remove_user_badges=False):
    """Delete a badge type. If remove_user_badges=True, also delete all user badges
    of this type. Otherwise, user badges are deleted automatically via CASCADE."""
    with get_db_context() as conn:
        if remove_user_badges:
            # Explicitly delete user badges first
            conn.execute("DELETE FROM user_badges WHERE badge_type_id=?", (badge_type_id,))
        # Delete badge type (CASCADE will handle user_badges and notifications)
        conn.execute("DELETE FROM badge_types WHERE id=?", (badge_type_id,))
        conn.commit()


# From here down the rows are awards in ``user_badges``, not the badge
# definitions above; a badge type with allow_multiple=0 may appear at most
# once per user.

def award_badge(user_id, badge_type_id, awarded_by=None):
    """Award a badge to a user.

    For badges with allow_multiple=False (the default): returns the new
    user_badge id, or None if the badge is already held and not revoked.
    If the badge was previously revoked it is re-activated instead.

    For badges with allow_multiple=True: always inserts a new award row and
    returns its id, regardless of how many times the badge has been awarded
    before.
    """
    with get_db_context() as conn:
        # Look up the badge type to check allow_multiple
        badge_type = conn.execute(
            "SELECT allow_multiple FROM badge_types WHERE id=?", (badge_type_id,)
        ).fetchone()
        allow_multiple = badge_type['allow_multiple'] if badge_type else 0

        if not allow_multiple:
            # Check if user already has this badge
            existing = conn.execute(
                "SELECT id, revoked FROM user_badges WHERE user_id=? AND badge_type_id=?",
                (user_id, badge_type_id),
            ).fetchone()

            if existing:
                if existing['revoked'] == 0:
                    # Badge already awarded and not revoked
                    return None
                else:
                    # Badge was revoked, re-award it
                    conn.execute(
                        "UPDATE user_badges SET revoked=0, revoked_at=NULL, revoked_by=NULL, "
                        "earned_at=datetime('now'), awarded_by=? WHERE id=?",
                        (awarded_by, existing['id']),
                    )
                    conn.commit()
                    return existing['id']

        # Award new badge (either allow_multiple=True or no existing record)
        cur = conn.execute(
            "INSERT INTO user_badges (user_id, badge_type_id, awarded_by) "
            "VALUES (?, ?, ?)",
            (user_id, badge_type_id, awarded_by),
        )
        user_badge_id = cur.lastrowid

        # Create notification: skip if an unread one already exists (avoids
        # unbounded notification rows for allow_multiple badges).
        conn.execute(
            "INSERT INTO badge_notifications (user_id, badge_type_id) "
            "SELECT ?, ? WHERE NOT EXISTS ("
            "SELECT 1 FROM badge_notifications "
            "WHERE user_id=? AND badge_type_id=? AND notified=0)",
            (user_id, badge_type_id, user_id, badge_type_id),
        )
        conn.commit()

        return user_badge_id


def revoke_badge(user_id, badge_type_id, revoked_by=None, permanent=False):
    """Revoke a badge from a user. If permanent=False, user can re-earn it.
    If permanent=True, the badge record is deleted entirely."""
    with get_db_context() as conn:
        if permanent:
            # Permanently delete the badge
            conn.execute(
                "DELETE FROM user_badges WHERE user_id=? AND badge_type_id=?",
                (user_id, badge_type_id),
            )
        else:
            # Mark as revoked but keep record
            conn.execute(
                "UPDATE user_badges SET revoked=1, revoked_at=datetime('now'), "
                "revoked_by=? WHERE user_id=? AND badge_type_id=?",
                (revoked_by, user_id, badge_type_id),
            )
        conn.commit()


@retry_on_busy
def get_user_badges(user_id, include_revoked=False):
    """Return all badges for a user, joined with badge type info."""
    with get_db_context() as conn:
        if include_revoked:
            rows = conn.execute(
                "SELECT ub.*, bt.name, bt.description, bt.icon, bt.color "
                "FROM user_badges ub "
                "JOIN badge_types bt ON ub.badge_type_id = bt.id "
                "WHERE ub.user_id=? "
                "ORDER BY ub.earned_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ub.*, bt.name, bt.description, bt.icon, bt.color "
                "FROM user_badges ub "
                "JOIN badge_types bt ON ub.badge_type_id = bt.id "
                "WHERE ub.user_id=? AND ub.revoked=0 "
                "ORDER BY ub.earned_at DESC",
                (user_id,),
            ).fetchall()
    return rows


@retry_on_busy
def has_badge(user_id, badge_type_id):
    """Check if user has an active (not revoked) badge."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM user_badges WHERE user_id=? AND badge_type_id=? AND revoked=0",
            (user_id, badge_type_id),
        ).fetchone()
    return row is not None


@retry_on_busy
def get_badge_holders(badge_type_id, include_revoked=False):
    """Return all users who have a specific badge."""
    with get_db_context() as conn:
        if include_revoked:
            rows = conn.execute(
                "SELECT ub.*, u.username "
                "FROM user_badges ub "
                "JOIN users u ON ub.user_id = u.id "
                "WHERE ub.badge_type_id=? "
                "ORDER BY ub.earned_at DESC",
                (badge_type_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ub.*, u.username "
                "FROM user_badges ub "
                "JOIN users u ON ub.user_id = u.id "
                "WHERE ub.badge_type_id=? AND ub.revoked=0 "
                "ORDER BY ub.earned_at DESC",
                (badge_type_id,),
            ).fetchall()
    return rows


@retry_on_busy
def get_all_badge_holder_counts():
    """Return a dict mapping badge_type_id to active (non-revoked) holder count.

    A single GROUP BY query replaces the N+1 pattern of calling
    :func:`get_badge_holders` for every badge type on the admin badges page.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT badge_type_id, COUNT(*) as cnt "
            "FROM user_badges "
            "WHERE revoked=0 "
            "GROUP BY badge_type_id"
        ).fetchall()
    return {row["badge_type_id"]: row["cnt"] for row in rows}


@retry_on_busy
def count_user_badges(user_id, include_revoked=False):
    """Count how many badges a user has."""
    with get_db_context() as conn:
        if include_revoked:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM user_badges WHERE user_id=?",
                (user_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM user_badges WHERE user_id=? AND revoked=0",
                (user_id,),
            ).fetchone()
    return row['count'] if row else 0


@retry_on_busy
def get_unnotified_badges(user_id):
    """Get all badges that user has earned but not been notified about."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT bn.*, bt.name, bt.description, bt.icon, bt.color "
            "FROM badge_notifications bn "
            "JOIN badge_types bt ON bn.badge_type_id = bt.id "
            "WHERE bn.user_id=? AND bn.notified=0 "
            "ORDER BY bn.created_at DESC",
            (user_id,),
        ).fetchall()
    return rows


def mark_badges_notified(user_id):
    """Mark all pending badge notifications as notified for a user."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE badge_notifications SET notified=1 WHERE user_id=? AND notified=0",
            (user_id,),
        )
        conn.commit()


def clear_badge_notifications(user_id):
    """Delete all badge notifications for a user."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM badge_notifications WHERE user_id=?", (user_id,))
        conn.commit()


# Auto-trigger badges are awarded by evaluating a user's stats against every
# badge type that declares a trigger, rather than by an explicit admin action.

def check_and_award_auto_badges(user_id):
    """Check all auto-trigger badges and award any that the user qualifies for.

    All queries run on a single shared connection to avoid the N+1 pattern of
    opening a new connection per badge via ``has_badge`` / ``award_badge``.

    Returns a list of newly awarded badge names.
    """
    from datetime import datetime, timezone

    with get_db_context() as conn:
        # Get all enabled auto-trigger badges
        badge_types = conn.execute(
            "SELECT * FROM badge_types WHERE enabled=1 AND auto_trigger=1"
        ).fetchall()

        awarded_badges = []

        for badge in badge_types:
            # Skip if user already has this badge (inline has_badge check)
            if not badge['allow_multiple']:
                already = conn.execute(
                    "SELECT id FROM user_badges "
                    "WHERE user_id=? AND badge_type_id=? AND revoked=0",
                    (user_id, badge['id']),
                ).fetchone()
                if already:
                    continue

            qualifies = False
            trigger_type = badge['trigger_type']
            threshold = badge['trigger_threshold']

            if trigger_type == 'first_edit':
                # Check if user has made at least one edit
                count = conn.execute(
                    "SELECT COUNT(*) as count FROM page_history WHERE edited_by=?",
                    (user_id,),
                ).fetchone()['count']
                qualifies = count >= 1

            elif trigger_type == 'contribution_count':
                # Check if user has made X contributions
                count = conn.execute(
                    "SELECT COUNT(*) as count FROM page_history WHERE edited_by=?",
                    (user_id,),
                ).fetchone()['count']
                qualifies = count >= threshold

            elif trigger_type == 'category_count':
                # Check if user has contributed to X different categories
                count = conn.execute(
                    """SELECT COUNT(DISTINCT p.category_id) as count
                       FROM page_history ph
                       JOIN pages p ON ph.page_id = p.id
                       WHERE ph.edited_by=? AND p.category_id IS NOT NULL""",
                    (user_id,),
                ).fetchone()['count']
                qualifies = count >= threshold

            elif trigger_type == 'member_days':
                # Check if user has been a member for X days
                user = conn.execute(
                    "SELECT created_at FROM users WHERE id=?", (user_id,)
                ).fetchone()
                if user and user['created_at']:
                    created = datetime.fromisoformat(user['created_at'].replace('Z', '+00:00'))
                    days = (datetime.now(timezone.utc) - created).days
                    qualifies = days >= threshold

            # Note: reading_time and article_count triggers have been removed
            # as no tracking system exists for these features yet

            if qualifies:
                result = _award_badge_on_conn(
                    conn, user_id, badge['id'], badge['allow_multiple'],
                )
                if result is not None:
                    awarded_badges.append(badge['name'])


    return awarded_badges


def _award_badge_on_conn(conn, user_id, badge_type_id, allow_multiple):
    """Award a badge using an existing connection (no new connection opened).

    Mirrors the logic of :func:`award_badge` but operates on *conn* so that
    callers doing batch work (e.g. :func:`check_and_award_auto_badges`) avoid
    the overhead of opening / closing a connection per badge.

    Returns the ``user_badge`` row id on success, or ``None`` when the badge
    was already held (and ``allow_multiple`` is false).
    """
    if not allow_multiple:
        existing = conn.execute(
            "SELECT id, revoked FROM user_badges "
            "WHERE user_id=? AND badge_type_id=?",
            (user_id, badge_type_id),
        ).fetchone()

        if existing:
            if existing['revoked'] == 0:
                return None
            # Badge was revoked: re-award it
            conn.execute(
                "UPDATE user_badges SET revoked=0, revoked_at=NULL, "
                "revoked_by=NULL, earned_at=datetime('now'), awarded_by=NULL "
                "WHERE id=?",
                (existing['id'],),
            )
            conn.commit()
            return existing['id']

    cur = conn.execute(
        "INSERT INTO user_badges (user_id, badge_type_id, awarded_by) "
        "VALUES (?, ?, NULL)",
        (user_id, badge_type_id),
    )
    user_badge_id = cur.lastrowid

    # Skip if an unread notification already exists (avoids unbounded rows
    # for allow_multiple badges).
    conn.execute(
        "INSERT INTO badge_notifications (user_id, badge_type_id) "
        "SELECT ?, ? WHERE NOT EXISTS ("
        "SELECT 1 FROM badge_notifications "
        "WHERE user_id=? AND badge_type_id=? AND notified=0)",
        (user_id, badge_type_id, user_id, badge_type_id),
    )
    conn.commit()

    return user_badge_id


def revoke_all_badges_for_type(badge_type_id, revoked_by=None, permanent=False):
    """Revoke all user badges of a specific type. Used when admin disables/deletes a badge type."""
    with get_db_context() as conn:
        if permanent:
            conn.execute(
                "DELETE FROM user_badges WHERE badge_type_id=?",
                (badge_type_id,),
            )
        else:
            conn.execute(
                "UPDATE user_badges SET revoked=1, revoked_at=datetime('now'), "
                "revoked_by=? WHERE badge_type_id=? AND revoked=0",
                (revoked_by, badge_type_id),
            )
        conn.commit()
