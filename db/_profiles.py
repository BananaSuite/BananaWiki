"""User profiles and contribution heatmap."""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


@retry_on_busy
def get_user_profile(user_id):
    """Return the profile row for a user, or None if not set up."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
    return row


def upsert_user_profile(user_id, real_name=None, bio=None,
                        avatar_filename=None, page_published=None,
                        page_disabled_by_admin=None, birth_date=None):
    """Create or update a user profile, updating only the supplied fields."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        existing = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO user_profiles "
                "(user_id, real_name, bio, birth_date, avatar_filename, page_published, "
                " page_disabled_by_admin, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    real_name or "",
                    bio or "",
                    birth_date or "",
                    avatar_filename or "",
                    1 if page_published else 0,
                    1 if page_disabled_by_admin else 0,
                    now,
                ),
            )
        else:
            fields = {"updated_at": now}
            if real_name is not None:
                fields["real_name"] = real_name
            if bio is not None:
                fields["bio"] = bio
            if birth_date is not None:
                fields["birth_date"] = birth_date
            if avatar_filename is not None:
                fields["avatar_filename"] = avatar_filename
            if page_published is not None:
                fields["page_published"] = 1 if page_published else 0
            if page_disabled_by_admin is not None:
                fields["page_disabled_by_admin"] = 1 if page_disabled_by_admin else 0
            set_clause = ", ".join(f"{k}=?" for k in fields)
            vals = list(fields.values()) + [user_id]
            conn.execute(
                f"UPDATE user_profiles SET {set_clause} WHERE user_id=?", vals  # noqa: S608
            )
        conn.commit()


def delete_user_profile(user_id):
    """Delete a user profile (retains contribution history)."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM user_profiles WHERE user_id=?", (user_id,))
        conn.commit()


@retry_on_busy
def list_published_profiles(*, limit=None, include_bio=True):
    """Return published profiles; compact widgets can bound rows and omit bios."""
    bio_column = "up.bio" if include_bio else "'' AS bio"
    limit_clause, parameters = ("", ()) if limit is None else (" LIMIT ?", (max(0, int(limit)),))
    with get_db_context() as conn:
        rows = conn.execute(
            f"SELECT u.id, u.username, u.userbot_enabled, up.real_name, {bio_column}, up.avatar_filename "
            "FROM users u "
            "JOIN user_profiles up ON u.id = up.user_id "
            "WHERE up.page_published=1 AND up.page_disabled_by_admin=0 "
            "AND u.suspended=0 "
            "ORDER BY u.username COLLATE NOCASE, u.id" + limit_clause,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


@retry_on_busy
def list_all_users_with_profiles():
    """Return all users with their profile data (for admin / People page)."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.role, u.suspended, u.userbot_enabled, "
            "COALESCE(up.real_name, '') AS real_name, "
            "COALESCE(up.bio, '') AS bio, "
            "COALESCE(up.avatar_filename, '') AS avatar_filename, "
            "COALESCE(up.page_published, 0) AS page_published, "
            "COALESCE(up.page_disabled_by_admin, 0) AS page_disabled_by_admin "
            "FROM users u "
            "LEFT JOIN user_profiles up ON u.id = up.user_id "
            "ORDER BY u.username COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


@retry_on_busy
def get_contribution_years(user_id):
    """Return contribution years (descending) for a user's page history."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT DISTINCT CAST(substr(created_at, 1, 4) AS INTEGER) AS year "
            "FROM page_history "
            "WHERE edited_by=? AND length(created_at) >= 10 "
            "ORDER BY year DESC",
            (user_id,),
        ).fetchall()
    return [int(r["year"]) for r in rows if r["year"]]


@retry_on_busy
def get_contributions_by_day(user_id, year=None):
    """Return ``(year, {date_str: count})`` of daily wiki edits for a user."""
    with get_db_context() as conn:
        if year is None:
            year = datetime.now(timezone.utc).year
        start = f"{year}-01-01"
        end_exclusive = f"{year + 1}-01-01"
        rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS cnt "
            "FROM page_history "
            "WHERE edited_by=? AND created_at >= ? AND created_at < ? "
            "GROUP BY day",
            (user_id, start, end_exclusive),
        ).fetchall()
    return year, {r["day"]: r["cnt"] for r in rows}


@retry_on_busy
def get_profile_group_badges(user_id):
    """Return visible group badge rows for a user, joined with group info.

    Only returns groups the user is still a member of and where
    ``visible = 1``.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT pgb.group_id, pgb.visible, gc.name AS group_name "
            "FROM profile_group_badges pgb "
            "JOIN group_members gm ON pgb.user_id = gm.user_id AND pgb.group_id = gm.group_id "
            "JOIN group_chats gc ON pgb.group_id = gc.id "
            "WHERE pgb.user_id = ? AND pgb.visible = 1 AND gm.banned = 0",
            (user_id,),
        ).fetchall()
        return rows


@retry_on_busy
def get_user_profile_group_settings(user_id):
    """Return all groups a user belongs to with their badge visibility setting.

    Each row includes the group name and current visibility toggle state.
    Groups the user belongs to but has no ``profile_group_badges`` row for
    are returned with ``visible = 0`` (private by default).
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT gc.id AS group_id, gc.name AS group_name, "
            "COALESCE(pgb.visible, 0) AS visible "
            "FROM group_members gm "
            "JOIN group_chats gc ON gm.group_id = gc.id "
            "LEFT JOIN profile_group_badges pgb "
            "  ON pgb.user_id = gm.user_id AND pgb.group_id = gm.group_id "
            "WHERE gm.user_id = ? AND gm.banned = 0 "
            "ORDER BY gc.name COLLATE NOCASE",
            (user_id,),
        ).fetchall()
        return rows


def set_profile_group_badge_visible(user_id, group_id, visible):
    """Set the visibility of a group badge on the user's profile.

    Creates or updates the ``profile_group_badges`` row.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO profile_group_badges (user_id, group_id, visible, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET visible=?, updated_at=?",
            (user_id, group_id, 1 if visible else 0, now, 1 if visible else 0, now),
        )
        conn.commit()


def clear_profile_group_badge(user_id, group_id):
    """Remove the profile group badge entry for a user/group pair.

    Called when a user leaves a group so that rejoining defaults to private.
    """
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM profile_group_badges WHERE user_id=? AND group_id=?",
            (user_id, group_id),
        )
        conn.commit()
