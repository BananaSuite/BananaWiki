"""Global banner persistence for the hosting portal."""

from datetime import datetime, timezone

from ._connection import get_hosting_db_context


def create_hosting_banner(content, color, visibility, audience_mode, expires_at,
                          created_by, not_removable=1, show_countdown=0,
                          audience_account_ids=None, custom_background=None,
                          custom_text_color=None):
    """Create a banner and its optional allowlist or denylist atomically."""
    now = datetime.now(timezone.utc).isoformat()
    with get_hosting_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO hosting_banners (content, color, visibility, audience_mode, "
            "expires_at, not_removable, show_countdown, custom_background, "
            "custom_text_color, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content, color, visibility, audience_mode, expires_at,
             int(not_removable), int(show_countdown), custom_background,
             custom_text_color, created_by, now, now),
        )
        banner_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO hosting_banner_audience (banner_id, account_id) VALUES (?, ?)",
            [(banner_id, account_id)
             for account_id in dict.fromkeys(audience_account_ids or [])],
        )
        conn.commit()
    return banner_id


def get_hosting_banner(banner_id):
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM hosting_banners WHERE id=?", (banner_id,)
        ).fetchone()


def list_hosting_banners():
    """Return all banners with creator and audience names for administration."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT b.*, COALESCE(a.username, 'deleted account') AS creator_name, "
            "(SELECT GROUP_CONCAT(ta.username, ', ') "
            " FROM hosting_banner_audience hba "
            " JOIN accounts ta ON ta.id=hba.account_id "
            " WHERE hba.banner_id=b.id) AS audience_usernames "
            "FROM hosting_banners b LEFT JOIN accounts a ON a.id=b.created_by "
            "ORDER BY b.created_at DESC"
        ).fetchall()


def get_hosting_banner_audience_ids(banner_id):
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            "SELECT account_id FROM hosting_banner_audience WHERE banner_id=?",
            (banner_id,),
        ).fetchall()
    return [row["account_id"] for row in rows]


_ALLOWED_COLUMNS = {
    "content", "color", "visibility", "audience_mode", "expires_at",
    "is_active", "not_removable", "show_countdown", "custom_background",
    "custom_text_color",
}


def update_hosting_banner(banner_id, audience_account_ids=None, **values):
    for key in values:
        if key not in _ALLOWED_COLUMNS:
            raise ValueError(f"Invalid hosting banner column: {key}")
    with get_hosting_db_context() as conn:
        if values:
            assignments = ", ".join(f"{key}=?" for key in values)
            params = list(values.values())
            params.extend([datetime.now(timezone.utc).isoformat(), banner_id])
            conn.execute(
                f"UPDATE hosting_banners SET {assignments}, "
                "revision=revision+1, updated_at=? WHERE id=?",
                params,
            )
        if audience_account_ids is not None:
            conn.execute(
                "DELETE FROM hosting_banner_audience WHERE banner_id=?",
                (banner_id,),
            )
            conn.executemany(
                "INSERT INTO hosting_banner_audience (banner_id, account_id) VALUES (?, ?)",
                [(banner_id, account_id)
                 for account_id in dict.fromkeys(audience_account_ids)],
            )
        conn.commit()


def delete_hosting_banner(banner_id):
    with get_hosting_db_context() as conn:
        conn.execute("DELETE FROM hosting_banners WHERE id=?", (banner_id,))
        conn.commit()


def get_active_hosting_banners(account_id=None):
    """Return active, unexpired banners visible to the current portal account."""
    logged_in = 1 if account_id else 0
    now = datetime.now(timezone.utc).isoformat()
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM hosting_banners b "
            "WHERE is_active=1 "
            "AND (expires_at IS NULL OR julianday(expires_at) > julianday(?)) "
            "AND (visibility='both' "
            " OR (visibility='logged_in' AND ?=1) "
            " OR (visibility='logged_out' AND ?=0)) "
            "AND (audience_mode='all' "
            " OR (audience_mode='allowlist' AND ? IS NOT NULL AND EXISTS ("
            "     SELECT 1 FROM hosting_banner_audience hba "
            "     WHERE hba.banner_id=b.id AND hba.account_id=?)) "
            " OR (audience_mode='denylist' AND (? IS NULL OR NOT EXISTS ("
            "     SELECT 1 FROM hosting_banner_audience hba "
            "     WHERE hba.banner_id=b.id AND hba.account_id=?)))) "
            "ORDER BY created_at DESC",
            (now, logged_in, logged_in, account_id, account_id,
             account_id, account_id),
        ).fetchall()
